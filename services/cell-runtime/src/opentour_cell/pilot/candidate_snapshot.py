"""Immutable as-of candidate snapshots, a separate current-eligibility reducer and the opaque
snapshot receipt (OPE-416, worker side).

All joins happen here, on the customer's worker, over a ``SourcePair`` read by the worker's own
reader and a committed ``PilotSettings`` batch. The flow is:

1. ``reduce_pair`` maps both sources through the settings, joins each independent correction to
   the exact decision revision it targets, and assigns every distinct decision one state:
   ``verified``, ``pending`` or ``disputed``. Malformed or unattributable rows are quarantined
   and count zero. Every exclusion has a reason code from the closed ``REASONS`` vocabulary.
2. Verified observed decisions go through the existing partitioner
   (``source_intake._partition``) so related cases are grouped before the development and
   protected populations exist.
3. ``SnapshotProducer.trigger`` commits the result write-once. A settings change while the
   sources were being read commits nothing and returns the previous committed snapshot. The same
   trigger, or the same canonical intent under another trigger, returns the same receipt.
4. ``SnapshotProducer.current`` re-reduces the current sources without touching the as-of
   snapshot. Reopening or withdrawing a correction retracts the example from future training and
   marks outstanding proposals that consumed it stale.

Decided rules applied: disputed rows never enter a training set, snapshot eligibility or a
verified count and are reported as disputed. Authority (verifier, origin, tenant, clock) comes
only from worker-held records. A model answer, ticket close, thumbs-up or repeated reply never
verifies a correction. The trigger reference is the only caller-supplied value; it is labelled
``caller_asserted`` and supports no claim.

Disclosure: the receipt carries only the counters the settings allow, and only the reason
codes that break those counters down (``REASON_COUNTER``). Any non-zero cell below the
settings' ``min_reportable_cell`` is reported as ``"<k"``, never as a number. Live totals count
distinct cases, not decisions.

Store binding: the producer pins the settings store's identity into its snapshots folder on
first use and into every snapshot and receipt; a snapshot made against, or read through, any
other store is refused with ``settings_store_mismatch``.

Evidence tier: ``digest_self_consistent_only``. The snapshot digest agrees with its own bytes; it
is not keyed or signed (OPE-566 owns custody). Label digests are HMACs under a key in the
producer's private folder (``locally_keyed``), so a short label vocabulary cannot be enumerated
from them; that is not a signature. Simulated and NOT proven: generated sentinel fixtures only;
no provider, installed Zap, Slack or customer evidence.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import re
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from opentour_cell.pilot import source_intake
from opentour_cell.pilot.learning_profile import (
    CORRECTION,
    COUNTERS,
    DECISION,
    EVIDENCE_TIER,
    LABEL_DIGEST_TIER,
    NON_VERIFYING_PROVENANCE,
    OBSERVED_DECISION,
    STARTER_ORIGINS,
    STATUSES,
    VERIFYING_PROVENANCE,
    PilotSettings,
    ProfileError,
    SettingsBatchStore,
    Source,
    SourceCapture,
    SourcePair,
    count,
    format_time,
    local_key,
    map_row,
    mapped_columns,
    need,
    parse_time,
    pin_identity,
    read_pinned,
    ref,
    write_pinned,
)
from opentour_cell.pilot.operating_point import canonical, digest

SNAPSHOT_SCHEMA = "opentour.pilot.candidate-snapshot.v1"
RECEIPT_SCHEMA = "opentour.pilot.candidate-snapshot-receipt.v1"
CURRENT_SCHEMA = "opentour.pilot.current-eligibility.v1"

VERIFIED, PENDING, DISPUTED = "verified", "pending", "disputed"
DEVELOPMENT, PROTECTED, STARTER = "development", "protected", "starter"

# Closed reason vocabulary. A code outside it is a defect and fails the build closed.
QUARANTINE_REASONS = frozenset(
    {
        "malformed_decision",
        "malformed_correction",
        "unknown_row_state",
        "unknown_correction_status",
        "unknown_provenance",
        "future_decision_features",
        "ambiguous_join",
    }
)
EXCLUSION_REASONS = frozenset(
    {"redundant_delivery", "duplicate_correction", "orphan_correction", "stale_revision"}
)
STATE_REASONS = frozenset(
    {
        "source_disputed",
        "contradictory_decision",
        "decision_case_changed",
        "correction_id_conflict",
        "contradictory_corrections",
        "missing_decision_predecessor",
        "awaiting_outcome",
        "superseded_decision_revision",
        "missing_correction_predecessor",
        "late_correction_revision",
        "outcome_before_decision",
        "reopened",
        "correction_withdrawn",
        "non_verifying_provenance",
        "unattested_label",
        "correction_immature",
        "starter_origin",
        "protected_partition",
        "training_not_permitted",
        "group_crosses_split",
        "development_label_after_split",
        "decision_absent",
    }
)
HOLD_REASONS = frozenset(
    {
        "settings_missing",
        "source_unbound",
        "foreign_tenant_capture",
        "capture_after_as_of",
        "source_stale",
        "policy_changed_during_read",
        "no_committed_snapshot",
        "trigger_conflict",
        "snapshot_commit_conflict",
        "settings_store_mismatch",
        "label_key_missing",
    }
)
REASONS = QUARANTINE_REASONS | EXCLUSION_REASONS | STATE_REASONS | HOLD_REASONS
# The receipt counter each data reason code breaks down. A reason code is disclosed only when
# its counter is one the settings allow. Row-level exclusions count zero, like quarantine.
_ELIGIBILITY_REASONS = frozenset(
    {
        "starter_origin",
        "protected_partition",
        "training_not_permitted",
        "group_crosses_split",
        "development_label_after_split",
        "decision_absent",
    }
)
_DISPUTED_REASONS = frozenset(
    {
        "source_disputed",
        "contradictory_decision",
        "decision_case_changed",
        "correction_id_conflict",
        "contradictory_corrections",
    }
)
REASON_COUNTER: dict[str, str] = {
    **{r: "quarantined" for r in QUARANTINE_REASONS | EXCLUSION_REASONS},
    **{r: "disputed" for r in _DISPUTED_REASONS},
    **{r: "eligible" for r in _ELIGIBILITY_REASONS},
    **{r: "pending" for r in STATE_REASONS - _DISPUTED_REASONS - _ELIGIBILITY_REASONS},
}
SUPPRESSED = "<k"
RECEIPT_FIELDS = (
    "schema",
    "state",
    "reason",
    "snapshot_digest",
    "digest_tier",
    "settings_store",
    "policy_revision",
    "counters",
    "reason_codes",
)

_TRIGGER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:\-]{0,127}")


class SnapshotHold(Exception):
    """The whole snapshot is held; nothing is committed."""

    def __init__(self, reason: str) -> None:
        need(reason in HOLD_REASONS, "reason_outside_vocabulary")
        super().__init__(reason)
        self.reason = reason


def example_ref(tenant: str, workflow_ref: str, source_ref: str, decision_id: str) -> str:
    """Tenant-scoped: identical decision IDs in two tenants never share an example."""
    return (
        "ex-"
        + digest(["opentour.pilot.example.v1", tenant, workflow_ref, source_ref, decision_id])[:32]
    )


def label_digest(key: bytes, tenant: str, example: str, value: str) -> str:
    """HMAC-SHA256 under the producer's private key (``locally_keyed``, not a signature)."""
    need(type(key) is bytes and len(key) == 32, "label_key_missing")
    message = canonical(["opentour.pilot.label.v2", tenant, example, value]).encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class _Decision:
    source: Source
    capture: SourceCapture
    decision_id: str
    revision: int
    case: str
    entity: str
    group: str
    decision_time: datetime
    prediction: str
    features: Mapping[str, str]
    available_at: datetime
    disputed: bool

    def payload(self) -> str:
        return digest(
            [
                self.case,
                self.entity,
                self.group,
                format_time(self.decision_time),
                self.prediction,
                dict(self.features),
                format_time(self.available_at),
            ]
        )


@dataclass(frozen=True)
class _Correction:
    source: Source
    capture: SourceCapture
    index: int
    correction_id: str
    decision_id: str
    decision_revision: int
    case: str
    version: int
    value: str | None
    status: str
    provenance: str
    verifier: str | None
    time: datetime
    disputed: bool

    def content(self) -> str:
        return digest(
            [
                self.decision_id,
                self.decision_revision,
                self.case,
                self.version,
                self.value,
                self.status,
                self.provenance,
                self.verifier,
                format_time(self.time),
            ]
        )

    def judgement(self) -> str:
        return digest([self.value, self.status, self.provenance, self.verifier])


def _row_state(settings: PilotSettings, mapped: Mapping[str, Any]) -> bool:
    """True when the row is disputed. ``row_state`` is always mapped; anything outside the
    settings' closed admitted and disputed sets is quarantined."""
    value = mapped["row_state"]
    # Exactly ``str``: a subclass could answer membership in one set through ``__eq__``.
    need(type(value) is str, "unknown_row_state")
    need(value in settings.admitted_states | settings.disputed_states, "unknown_row_state")
    return value in settings.disputed_states


def _text(value: object, reason: str, limit: int = 128) -> str:
    need(type(value) is str and 0 < len(value) <= limit and value.isprintable(), reason)
    assert type(value) is str
    return value


def _decision(settings: PilotSettings, source: Source, cap: SourceCapture, row: Any) -> _Decision:
    bad = "malformed_decision"
    need(isinstance(row, Mapping), bad)
    mapped = map_row(row, settings.decision_mapping)
    disputed = _row_state(settings, mapped)
    features: dict[str, str] = {}
    for name, column in settings.feature_columns.items():
        value = row.get(column)
        if value is not None:
            features[name] = _text(value, bad, 512)
    decided = parse_time(mapped["decision_time"], bad)
    available = parse_time(mapped["features_available_at"], bad)
    decision = _Decision(
        source=source,
        capture=cap,
        decision_id=ref(mapped["decision_id"], bad),
        revision=count(mapped["decision_revision"], bad, 1),
        case=ref(mapped["case"], bad),
        entity=ref(mapped["entity"], bad),
        group=ref(mapped["group"], bad),
        decision_time=decided,
        prediction=_text(mapped["prediction"], bad),
        features=features,
        available_at=available,
        disputed=disputed,
    )
    # Exposure comes from the source record: inputs not available at decision time are refused.
    need(available <= decided, "future_decision_features")
    return decision


def _correction(
    settings: PilotSettings, source: Source, cap: SourceCapture, index: int, row: Any
) -> _Correction:
    bad = "malformed_correction"
    need(isinstance(row, Mapping), bad)
    mapped = map_row(row, settings.correction_mapping)
    disputed = _row_state(settings, mapped)
    status = mapped["status"]
    need(type(status) is str and status in STATUSES, "unknown_correction_status")
    provenance = mapped["provenance"]
    need(
        type(provenance) is str and provenance in VERIFYING_PROVENANCE | NON_VERIFYING_PROVENANCE,
        "unknown_provenance",
    )
    verifier = mapped["verifier"]
    need(verifier is None or type(verifier) is str, bad)
    value = mapped["corrected_value"]
    need(value is None or type(value) is str, bad)
    need(status != "final" or value is not None, bad)
    return _Correction(
        source=source,
        capture=cap,
        index=index,
        correction_id=ref(mapped["correction_id"], bad),
        decision_id=ref(mapped["decision_id"], bad),
        decision_revision=count(mapped["decision_revision"], bad, 1),
        case=ref(mapped["case"], bad),
        version=count(mapped["correction_version"], bad, 1),
        value=None if value is None else _text(value, bad),
        status=status,
        provenance=provenance,
        verifier=ref(verifier, bad) if verifier else None,
        time=parse_time(mapped["correction_time"], bad),
        disputed=disputed,
    )


def capture_digest(settings: PilotSettings, capture: SourceCapture) -> str:
    """Fingerprint of one capture over the columns its source role maps, and nothing else."""
    source = settings.source(capture.source_ref)
    return capture.fingerprint(mapped_columns(settings, source.role) if source else ())


def _check_captures(settings: PilotSettings, pair: SourcePair, as_of: datetime) -> None:
    captures = pair.by_ref()
    if len(captures) != len(pair.captures) or set(captures) != {
        s.source_ref for s in settings.sources
    }:
        raise SnapshotHold("source_unbound")
    for cap in pair.captures:
        if cap.tenant != settings.tenant:
            raise SnapshotHold("foreign_tenant_capture")
        try:
            count(cap.source_revision, "source_unbound")
            captured = parse_time(cap.captured_at, "source_unbound")
        except ProfileError as exc:
            raise SnapshotHold("source_unbound") from exc
        if captured > as_of:
            raise SnapshotHold("capture_after_as_of")
        if as_of - captured > timedelta(seconds=settings.freshness_seconds):
            raise SnapshotHold("source_stale")


def reduce_pair(
    settings: PilotSettings, pair: SourcePair, as_of: datetime, *, label_key: bytes
) -> dict[str, Any]:
    """Pure reducer: the as-of snapshot body for one tenant and workflow (no I/O).

    ``label_key`` is the producer's private HMAC key for label digests.
    """
    need(type(label_key) is bytes and len(label_key) == 32, "label_key_missing")
    _check_captures(settings, pair, as_of)
    captures = pair.by_ref()
    quarantined: list[dict[str, Any]] = []
    exclusions: list[dict[str, Any]] = []

    def quarantine(cap: SourceCapture, index: int, reason: str) -> None:
        quarantined.append({"source_ref": cap.source_ref, "row": index, "reason": reason})

    def exclude(cap: SourceCapture, index: int, reason: str) -> None:
        exclusions.append({"source_ref": cap.source_ref, "row": index, "reason": reason})

    # ---- decisions: one per deciding-source namespace and stable decision ID
    deliveries: dict[tuple[str, str, int], list[tuple[int, _Decision]]] = defaultdict(list)
    for source in (s for s in settings.sources if s.role == DECISION):
        cap = captures[source.source_ref]
        for index, row in enumerate(cap.rows):
            try:
                decision = _decision(settings, source, cap, row)
            except ProfileError as exc:
                quarantine(cap, index, exc.reason)
                continue
            if decision.decision_time > as_of:
                continue  # not yet decided as of this snapshot
            deliveries[(source.source_ref, decision.decision_id, decision.revision)].append(
                (index, decision)
            )
    disputes: dict[tuple[str, str], str] = {}
    revisions: dict[tuple[str, str], dict[int, _Decision]] = defaultdict(dict)
    for (src, did, rev), items in sorted(deliveries.items()):
        first = items[0][1]
        if len({d.payload() for _, d in items}) > 1:
            disputes.setdefault((src, did), "contradictory_decision")
        for index, extra in items[1:]:
            if extra.payload() == first.payload():
                exclude(extra.capture, index, "redundant_delivery")
        if any(d.disputed for _, d in items):
            disputes.setdefault((src, did), "source_disputed")
        revisions[(src, did)][rev] = first
    for key, revs in revisions.items():
        if len({d.case for d in revs.values()}) > 1:
            disputes.setdefault(key, "decision_case_changed")

    # ---- corrections: resolve the exact decision revision, in the deciding namespace
    by_id: dict[tuple[str, str], list[_Correction]] = defaultdict(list)
    for source in (s for s in settings.sources if s.role == CORRECTION):
        cap = captures[source.source_ref]
        for index, row in enumerate(cap.rows):
            try:
                correction = _correction(settings, source, cap, index, row)
            except ProfileError as exc:
                quarantine(cap, index, exc.reason)
                continue
            if correction.time > as_of:
                continue  # not yet known as of this snapshot
            by_id[(source.source_ref, correction.correction_id)].append(correction)
    targets: dict[tuple[str, str], list[_Correction]] = defaultdict(list)
    for _, sent in sorted(by_id.items()):
        lead = sent[0]
        matches = [k for k in revisions if k[1] == lead.decision_id]
        if not matches:
            for c in sent:
                exclude(c.capture, c.index, "orphan_correction")
            continue
        if len(matches) > 1 or any(
            {d.case for d in revisions[matches[0]].values()} != {c.case} for c in sent
        ):
            # Deny an ambiguous join: two namespaces, or a case that does not match.
            for c in sent:
                quarantine(c.capture, c.index, "ambiguous_join")
            continue
        key = matches[0]
        if len({c.content() for c in sent}) > 1:
            disputes.setdefault(key, "correction_id_conflict")
        for again in sent[1:]:
            if again.content() == lead.content():
                exclude(again.capture, again.index, "redundant_delivery")
        if any(c.disputed for c in sent):
            disputes.setdefault(key, "source_disputed")
        targets[key].append(lead)

    # ---- one state per distinct decision
    examples: list[dict[str, Any]] = []
    verified_rows: list[dict[str, Any]] = []
    for key in sorted(revisions):
        revs = revisions[key]
        head_rev = max(revs)
        decision = revs[head_rev]
        ex = example_ref(settings.tenant, settings.workflow_ref, key[0], key[1])
        state, reason, chosen = _judge(
            settings,
            decision,
            head_rev,
            revs,
            targets.get(key, []),
            disputes.get(key),
            as_of,
            exclude,
        )
        inputs = [
            {
                "path": f"features/{name}",
                "source_ref": decision.source.source_ref,
                "source_revision": decision.capture.source_revision,
                "available_at": format_time(decision.available_at),
            }
            for name in sorted(decision.features)
        ]
        record: dict[str, Any] = {
            "example_ref": ex,
            "source_ref": decision.source.source_ref,
            "origin": decision.source.origin,
            "decision_id": decision.decision_id,
            "decision_revision": head_rev,
            "case": decision.case,
            "decision_time": format_time(decision.decision_time),
            "automatic": decision.prediction not in settings.abstain_values,
            "inputs": inputs,
            "inputs_digest": digest(["opentour.pilot.inputs.v1", ex, dict(decision.features)]),
            "state": state,
            "reason": reason,
            "partition": None,
            "group": None,
            "eligible": False,
            "correction": None,
            "wrong": None,
        }
        if chosen is not None and state == VERIFIED:
            assert chosen.value is not None
            record["correction"] = {
                "correction_ref": "cor-"
                + digest([settings.tenant, chosen.source.source_ref, chosen.correction_id])[:32],
                "source_ref": chosen.source.source_ref,
                "source_revision": chosen.capture.source_revision,
                "version": chosen.version,
                "verifier": chosen.verifier,
                "settings_revision": settings.settings_revision,
                "outcome_time": format_time(chosen.time),
                "label_digest": label_digest(label_key, settings.tenant, ex, chosen.value),
            }
            record["wrong"] = record["automatic"] and decision.prediction != chosen.value
            if decision.source.origin == OBSERVED_DECISION:
                verified_rows.append(
                    {
                        "case_id": ex,
                        "group_id": decision.group,
                        "customer_id": decision.entity,
                        "features": dict(decision.features),
                        "decision_at": format_time(decision.decision_time),
                        "verified_at": format_time(chosen.time),
                    }
                )
            else:
                record["partition"], record["reason"] = STARTER, "starter_origin"
        examples.append(record)

    # ---- the existing partitioner groups related cases before populations exist
    partition_excluded: dict[str, str] = {}
    development, protected = source_intake._partition(
        copy.deepcopy(verified_rows), dict(settings.split), partition_excluded
    )
    placement = {r["case_id"]: (DEVELOPMENT, r["group_id"]) for r in development}
    placement.update({r["case_id"]: (PROTECTED, r["group_id"]) for r in protected})
    for record in examples:
        ex = record["example_ref"]
        if ex in partition_excluded:
            record["reason"] = partition_excluded[ex]
        elif ex in placement:
            record["partition"], record["group"] = placement[ex]
            if record["partition"] == PROTECTED:
                record["reason"] = "protected_partition"
            elif "train" not in settings.uses:
                record["reason"] = "training_not_permitted"
            else:
                record["eligible"], record["reason"] = True, None

    return _assemble(settings, pair, as_of, examples, quarantined, exclusions)


def _judge(
    settings: PilotSettings,
    decision: _Decision,
    head_rev: int,
    revs: Mapping[int, _Decision],
    corrections: list[_Correction],
    dispute: str | None,
    as_of: datetime,
    exclude: Callable[[SourceCapture, int, str], None],
) -> tuple[str, str | None, _Correction | None]:
    if dispute is not None:
        return DISPUTED, dispute, None
    if sorted(revs) != list(range(1, head_rev + 1)):
        return PENDING, "missing_decision_predecessor", None
    current = [c for c in corrections if c.decision_revision == head_rev]
    for older in (c for c in corrections if c.decision_revision < head_rev):
        exclude(older.capture, older.index, "stale_revision")
    if not current:
        reason = "superseded_decision_revision" if corrections else "awaiting_outcome"
        return PENDING, reason, None
    versions: dict[int, list[_Correction]] = defaultdict(list)
    for c in current:
        versions[c.version].append(c)
    for items in versions.values():
        if len({c.judgement() for c in items}) > 1:
            return DISPUTED, "contradictory_corrections", None
    chain: dict[int, _Correction] = {}
    for version, items in versions.items():
        ordered = sorted(items, key=lambda c: c.correction_id)
        chain[version] = ordered[0]
        for extra in ordered[1:]:
            exclude(extra.capture, extra.index, "duplicate_correction")
    head = max(chain)
    if sorted(chain) != list(range(1, head + 1)):
        return PENDING, "missing_correction_predecessor", None
    chosen = chain[head]
    maturity = timedelta(seconds=settings.maturity_seconds)
    if head > 1:
        prior = chain[head - 1]
        # Changing a matured final needs an explicit reopen; a direct late revision is held.
        late = chosen.time > prior.time + maturity
        if prior.status == "final" and chosen.status == "final" and late:
            return PENDING, "late_correction_revision", chosen
    if chosen.time < decision.decision_time:
        # An outcome recorded before the decision existed cannot be that decision's truth.
        return PENDING, "outcome_before_decision", chosen
    if chosen.status == "reopened":
        return PENDING, "reopened", chosen
    if chosen.status == "withdrawn":
        return PENDING, "correction_withdrawn", chosen
    if chosen.provenance not in VERIFYING_PROVENANCE:
        return PENDING, "non_verifying_provenance", chosen
    if chosen.verifier is None or chosen.verifier not in settings.adjudicators:
        return PENDING, "unattested_label", chosen
    if as_of < chosen.time + maturity:
        return PENDING, "correction_immature", chosen
    return VERIFIED, None, chosen


def _ratio(numerator: int, denominator: int) -> dict[str, Any]:
    if denominator == 0:
        return {"state": "unavailable", "reason": "zero_denominator"}
    return {"state": "available", "numerator": numerator, "denominator": denominator}


def _assemble(
    settings: PilotSettings,
    pair: SourcePair,
    as_of: datetime,
    examples: list[dict[str, Any]],
    quarantined: list[dict[str, Any]],
    exclusions: list[dict[str, Any]],
) -> dict[str, Any]:
    def case_states(chosen: list[dict[str, Any]]) -> dict[str, str]:
        # One state per distinct case: any disputed decision disputes it, else any pending one
        # keeps it pending; only a case whose every decision is verified counts as verified.
        by_case: dict[str, set[str]] = defaultdict(set)
        for e in chosen:
            by_case[e["case"]].add(e["state"])
        return {
            case: next(s for s in (DISPUTED, PENDING, VERIFIED) if s in states)
            for case, states in by_case.items()
        }

    def tally(origins: Iterable[str]) -> dict[str, int]:
        wanted = set(origins)
        chosen = [e for e in examples if e["origin"] in wanted]
        states = Counter(case_states(chosen).values())
        return {
            "cases": sum(states.values()),
            "decisions": len(chosen),
            "verified": states[VERIFIED],
            "pending": states[PENDING],
            "disputed": states[DISPUTED],
        }

    live = [e for e in examples if e["origin"] == OBSERVED_DECISION]
    # Recurrence over distinct cases: a case is wrong when any of its verified automatic
    # decisions was wrong.
    automatic = {e["case"] for e in live if e["state"] == VERIFIED and e["automatic"]}
    wrong_cases = {
        e["case"] for e in live if e["state"] == VERIFIED and e["automatic"] and e["wrong"]
    }
    wrong = [e for e in live if e["state"] == VERIFIED and e["automatic"] and e["wrong"]]
    reasons: Counter[str] = Counter(e["reason"] for e in examples if e["reason"])
    reasons.update(q["reason"] for q in quarantined)
    reasons.update(x["reason"] for x in exclusions)
    need(set(reasons) <= REASONS, "reason_outside_vocabulary")
    captures = pair.by_ref()
    live_tally = tally([OBSERVED_DECISION])
    return {
        "schema": SNAPSHOT_SCHEMA,
        "tenant": settings.tenant,
        "workflow_ref": settings.workflow_ref,
        "settings_revision": settings.settings_revision,
        "settings_digest": settings.digest,
        "policy_revision": settings.policy_revision,
        "evaluation_policy_revision": settings.evaluation_policy_revision,
        "benchmark": {"ref": settings.benchmark_ref, "version": settings.benchmark_version},
        "engagement_epoch": settings.engagement_epoch,
        "grant_epoch": settings.grant_epoch,
        "split_revision": digest(["opentour.pilot.split.v1", dict(settings.split)]),
        "as_of": format_time(as_of),
        "sources": [
            {
                "source_ref": s.source_ref,
                "role": s.role,
                "origin": s.origin,
                "source_revision": captures[s.source_ref].source_revision,
                "capture_digest": capture_digest(settings, captures[s.source_ref]),
            }
            for s in settings.sources
        ],
        "examples": sorted(examples, key=lambda e: e["example_ref"]),
        "quarantined": sorted(quarantined, key=lambda q: (q["source_ref"], q["row"])),
        "exclusions": sorted(exclusions, key=lambda x: (x["source_ref"], x["row"], x["reason"])),
        "counters": {
            "live": live_tally,
            "starter": tally(STARTER_ORIGINS),
            "eligible": len({e["case"] for e in examples if e["eligible"]}),
            "pending": live_tally["pending"],
            "disputed": live_tally["disputed"],
            "quarantined": len(quarantined),
            "awaiting_decision": sum(1 for x in exclusions if x["reason"] == "orphan_correction"),
        },
        "metrics": {
            # Live observed evidence only: starter cases count zero toward recurrence.
            "wrong_automatic_decisions": _ratio(len(wrong_cases), len(automatic)),
            "independent_incumbent_error_groups": len(
                {e["group"] for e in wrong if e["partition"] == DEVELOPMENT}
            ),
            "post_simple_repair_residual_groups": {
                "state": "unavailable",
                "reason": "simple_repair_not_bound",
            },
        },
        "reason_codes": dict(sorted(reasons.items())),
        "receipt_counters": list(settings.receipt_counters),
        "min_reportable_cell": settings.min_reportable_cell,
        "evidence": {
            "tier": EVIDENCE_TIER,
            "label_digest_tier": LABEL_DIGEST_TIER,
            "origins": sorted({e["origin"] for e in examples}),
        },
    }


def _cell(value: object, k: int) -> int | str:
    """A disclosed count: zero and counts of at least ``k`` as integers, others as ``"<k"``."""
    need(type(value) is int and value >= 0, "receipt_unsafe")
    assert type(value) is int
    return SUPPRESSED if 0 < value < k else value


def receipt(
    snapshot: Mapping[str, Any], state: str = "committed", reason: str | None = None
) -> dict[str, Any]:
    """The opaque receipt: digest, policy revision, disclosed counters and reason codes only.

    No tenant, case, decision, group, feature, prediction or label value can appear. Only the
    counters the settings allow are present, and only the reason codes that break one of them
    down. Every non-zero cell below ``min_reportable_cell`` reads ``"<k"``.
    """
    need(state in {"committed", "rolled_back"}, "receipt_state")
    counters = snapshot["counters"]
    allowed = list(snapshot["receipt_counters"])
    k = snapshot["min_reportable_cell"]
    need(type(k) is int and k >= 2, "receipt_unsafe")
    body: dict[str, Any] = {
        "schema": RECEIPT_SCHEMA,
        "state": state,
        "reason": reason,
        "snapshot_digest": digest(snapshot),
        "digest_tier": EVIDENCE_TIER,
        "settings_store": snapshot["settings_store"],
        "policy_revision": snapshot["policy_revision"],
        "counters": {name: _cell(counters[name], k) for name in allowed},
        "reason_codes": {
            code: _cell(n, k)
            for code, n in snapshot["reason_codes"].items()
            if REASON_COUNTER.get(code) in allowed
        },
    }
    need(
        set(body["counters"]) <= set(COUNTERS)
        and set(body["reason_codes"]) <= REASONS
        and all(
            type(v) is int or v == SUPPRESSED
            for v in [*body["counters"].values(), *body["reason_codes"].values()]
        ),
        "receipt_unsafe",
    )
    return body


def held(reason: str) -> dict[str, Any]:
    need(reason in HOLD_REASONS, "reason_outside_vocabulary")
    return {
        "schema": RECEIPT_SCHEMA,
        "state": "held",
        "reason": reason,
        "snapshot_digest": None,
        "digest_tier": EVIDENCE_TIER,
        "settings_store": None,
        "policy_revision": None,
        "counters": {},
        # The hold state itself, not a data cell: it is never suppressed.
        "reason_codes": {reason: 1},
    }


# --------------------------------------------------------------------------- producer

PairReader = Callable[[PilotSettings], SourcePair]


class SnapshotProducer:
    """Commits as-of snapshots write-once; the clock is the worker's, never the caller's.

    On first use the producer pins the settings store's identity into its snapshots folder.
    Every later trigger, snapshot read and current-eligibility read checks it: a snapshot made
    against, or read through, another store (a copy, a restore, a second store sharing this
    snapshots folder) is refused with ``settings_store_mismatch``.
    """

    def __init__(
        self,
        directory: Path,
        settings: SettingsBatchStore,
        *,
        clock: Callable[[], datetime],
    ) -> None:
        self.directory = Path(directory)
        self.settings = settings
        self.clock = clock

    def _base(self, tenant: str, workflow_ref: str) -> Path:
        return (
            self.directory / ref(tenant, "snapshot_tenant") / ref(workflow_ref, "snapshot_workflow")
        )

    def _folder(self, tenant: str, workflow_ref: str, kind: str) -> Path:
        return self._base(tenant, workflow_ref) / kind

    def _now(self) -> datetime:
        return self.clock().astimezone(UTC).replace(microsecond=0)

    def _names(self, tenant: str, workflow_ref: str) -> list[str]:
        folder = self._folder(tenant, workflow_ref, "snapshots")
        return sorted(p.stem for p in folder.glob("snap-*.json")) if folder.is_dir() else []

    def store_identity(self, tenant: str, workflow_ref: str, *, create: bool = False) -> str:
        """The pinned settings-store identity; refuses ``settings_store_mismatch`` otherwise.

        The pin is created only on first use, before any snapshot exists.
        """
        identity = self.settings.identity(tenant, workflow_ref)
        need(identity is not None, "settings_store_mismatch")
        assert identity is not None
        pin_identity(
            self._base(tenant, workflow_ref),
            identity,
            create=create and not self._names(tenant, workflow_ref),
        )
        return identity

    def label_key(self, tenant: str, workflow_ref: str, *, create: bool = False) -> bytes:
        """The private HMAC key for label digests. Created only before any snapshot exists;
        a key missing after that is refused (``label_key_missing``), never re-keyed."""
        return local_key(
            self._folder(tenant, workflow_ref, "private"),
            create=create and not self._names(tenant, workflow_ref),
            missing="label_key_missing",
        )

    def snapshots(self, tenant: str, workflow_ref: str) -> list[dict[str, Any]]:
        """Every committed snapshot in sequence order, each verified against its pin, the
        pinned settings store and the exact settings revision that store holds."""
        folder = self._folder(tenant, workflow_ref, "snapshots")
        names = self._names(tenant, workflow_ref)
        if not names:
            return []
        identity = self.store_identity(tenant, workflow_ref)
        found = []
        for position, name in enumerate(names, start=1):
            need(name == f"snap-{position:08d}", "snapshot_tampered")
            body = read_pinned(folder, name, "snapshot_tampered")
            need(
                isinstance(body, dict)
                and body.get("schema") == SNAPSHOT_SCHEMA
                and body.get("tenant") == tenant
                and body.get("workflow_ref") == workflow_ref
                and body.get("sequence") == position,
                "snapshot_tampered",
            )
            need(body.get("settings_store") == identity, "settings_store_mismatch")
            revision = body.get("settings_revision")
            held_by_store = (
                self.settings.revision(tenant, workflow_ref, revision)
                if type(revision) is int and revision >= 1
                else None
            )
            need(
                held_by_store is not None and held_by_store.digest == body.get("settings_digest"),
                "settings_store_mismatch",
            )
            found.append(body)
        return found

    def snapshot(self, tenant: str, workflow_ref: str, snapshot_digest: str) -> dict[str, Any]:
        for body in self.snapshots(tenant, workflow_ref):
            if digest(body) == snapshot_digest:
                return body
        raise ProfileError("snapshot_unknown")

    def trigger(
        self, tenant: str, workflow_ref: str, trigger_ref: str, read_pair: PairReader
    ) -> dict[str, Any]:
        """Read, reduce and commit one snapshot; return its opaque receipt.

        A hold is not memoized: a held trigger commits nothing, and retrying the same trigger
        later re-evaluates it (for example once a stale source is fresh again).
        """
        need(type(trigger_ref) is str and _TRIGGER.fullmatch(trigger_ref), "trigger_ref")
        before = self.settings.current(tenant, workflow_ref)
        if before is None:
            return held("settings_missing")
        try:
            identity = self.store_identity(tenant, workflow_ref, create=True)
        except ProfileError:
            return held("settings_store_mismatch")
        try:
            key = self.label_key(tenant, workflow_ref, create=True)
        except ProfileError as exc:
            if exc.reason == "label_key_missing":
                return held("label_key_missing")
            raise
        pair = read_pair(before)
        after = self.settings.current(tenant, workflow_ref)
        previous = self.snapshots(tenant, workflow_ref)
        if after is None or after.digest != before.digest:
            # The policy moved under the read: keep the previous committed snapshot.
            return self._rolled_back(previous)
        intent = digest(
            [
                "opentour.pilot.snapshot-intent.v1",
                tenant,
                workflow_ref,
                identity,
                before.digest,
                sorted(capture_digest(before, c) for c in pair.captures),
            ]
        )
        triggers = self._folder(tenant, workflow_ref, "triggers")
        name = "trigger-" + digest(["opentour.pilot.trigger.v1", trigger_ref])[:40]
        try:
            write_pinned(triggers, name, {"intent": intent}, "trigger_conflict")
        except ProfileError:
            return held("trigger_conflict")
        for body in previous:
            if body["intent"] == intent:
                return receipt(body)
        try:
            body = reduce_pair(before, pair, self._now(), label_key=key)
        except SnapshotHold as hold:
            return held(hold.reason)
        sequence = len(previous) + 1
        body["sequence"] = sequence
        body["intent"] = intent
        body["settings_store"] = identity
        body["caller_asserted"] = {"first_trigger_ref": trigger_ref}
        # The final check and the write happen under the settings store's commit lock, so no
        # settings commit can land between them.
        with self.settings.lock(tenant, workflow_ref):
            final = self.settings.current(tenant, workflow_ref)
            if final is None or final.digest != before.digest:
                return self._rolled_back(previous)
            try:
                write_pinned(
                    self._folder(tenant, workflow_ref, "snapshots"),
                    f"snap-{sequence:08d}",
                    body,
                    "snapshot_commit_conflict",
                )
            except ProfileError:
                return held("snapshot_commit_conflict")
        return receipt(body)

    @staticmethod
    def _rolled_back(previous: list[dict[str, Any]]) -> dict[str, Any]:
        if not previous:
            return held("no_committed_snapshot")
        return receipt(previous[-1], "rolled_back", "policy_changed_during_read")

    def current(
        self,
        tenant: str,
        workflow_ref: str,
        snapshot_digest: str,
        read_pair: PairReader,
        proposals: Mapping[str, Iterable[str]] | None = None,
    ) -> dict[str, Any]:
        """Current eligibility over a committed as-of snapshot; the snapshot is never rewritten.

        ``proposals`` maps an outstanding proposal reference to the example references it
        actually consumed (from the consumed-dependency manifest). The proposal references are
        consumer-declared, so every stale proposal is returned labelled ``caller_asserted``.
        """
        as_of = self.snapshot(tenant, workflow_ref, snapshot_digest)
        settings = self.settings.current(tenant, workflow_ref)
        need(settings is not None, "settings_missing")
        assert settings is not None
        need(
            self.store_identity(tenant, workflow_ref) == as_of["settings_store"],
            "settings_store_mismatch",
        )
        key = self.label_key(tenant, workflow_ref)
        try:
            now = reduce_pair(settings, read_pair(settings), self._now(), label_key=key)
        except SnapshotHold as hold:
            return {"schema": CURRENT_SCHEMA, "state": "held", "reason": hold.reason}
        current = {e["example_ref"]: e for e in now["examples"]}
        retracted = []
        for example in as_of["examples"]:
            if not example["eligible"]:
                continue
            later = current.get(example["example_ref"])
            if later is None or not later["eligible"]:
                retracted.append(
                    {
                        "example_ref": example["example_ref"],
                        "reason": "decision_absent" if later is None else later["reason"],
                    }
                )
        gone = {r["example_ref"] for r in retracted}
        stale = sorted(p for p, used in (proposals or {}).items() if set(used) & gone)
        return {
            "schema": CURRENT_SCHEMA,
            "state": "current",
            "as_of_snapshot": snapshot_digest,
            "settings_store": as_of["settings_store"],
            "settings_revision": settings.settings_revision,
            "evaluated_at": now["as_of"],
            "eligible": sorted(r for r, e in current.items() if e["eligible"]),
            "retracted": retracted,
            "stale_proposals": [{"proposal_ref": p, "basis": "caller_asserted"} for p in stale],
            "counters": now["counters"],
            "metrics": now["metrics"],
            "reason_codes": now["reason_codes"],
        }
