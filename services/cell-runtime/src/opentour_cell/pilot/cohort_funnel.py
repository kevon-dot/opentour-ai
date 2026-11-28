"""Publish the private first-ten cohort funnel and reconcile acceptance and payment (OPE-455).

Inputs

* A read-only snapshot of the authoritative qualification and lease register
  (``cohort_lease.read_register_snapshot``). It says which organizations were admitted,
  queued, leased, closed, withdrawn or lost.
* Private stage receipts (``opentour.pilot.cohort-stage-receipt.v1``), one immutable receipt
  per stage event. Each column is credited only by its own receipt kind. A receipt credits an
  organization only when the register admitted that organization's engagement.
* Optionally, and only when the owner permits outcome disclosure, the OPE-522 readiness cohort
  receipt and the OPE-517 tuning-benefit and label-efficiency records.

Outputs

* ``build_report``: the private reconciliation report. Every column lists the distinct
  organizations and the evidence references behind its count, so a total can be compared with
  the underlying evidence IDs.
* ``weekly_view``: the evidence-free aggregate for team-wide use: counts, closed reason codes
  and durations only.

What it never does: write the register or a receipt; infer one stage from another (payment from
approval, acceptance from a starter, a fit from a comparison, delivery from a decision); count
one organization twice in a column; or report a missing value as zero.

Evidence tier: the report is ``digest_self_consistent_only``. It re-checks no key. Register rows
were keyed when admitted (``locally_keyed``) and each stage receipt states its own tier, which
this module caps at ``locally_keyed`` because no signing or custody contract exists yet
(OPE-566). Internal acceptance counts are split by that capped tier. Only ``customer_witnessed``
acceptances are externally claimable, so the external count stays 0 until a verifier exists.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from fractions import Fraction
from pathlib import Path
from typing import Any, Final

from opentour_cell.pilot.cohort_lease import SNAPSHOT_VERSION, _hex, read_register_snapshot
from opentour_cell.pilot.operating_point import canonical, digest
from opentour_cell.pilot.pilot_metrics import amount, rendered
from opentour_cell.pilot.qualification import (
    Json,
    QualificationError,
    _json_object,
    _private_parent,
    _private_record,
    _ref,
)
from opentour_cell.pilot.simple_repair import RepairError

REPORT_VERSION: Final = "opentour.pilot.cohort-funnel-report.v1"
WEEKLY_VERSION: Final = "opentour.pilot.cohort-funnel-weekly-view.v1"
FIELD_MAP_VERSION: Final = "opentour.pilot.cohort-funnel-field-map.v1"
RECEIPT_VERSION: Final = "opentour.pilot.cohort-stage-receipt.v1"
BATCH_VERSION: Final = "opentour.pilot.cohort-stage-receipt-batch.v1"
READINESS_RECEIPT_VERSION: Final = "opentour.pilot.readiness-cohort-receipt.v1"
# Consumed from producers this package cannot import. OPE-522's readout is unmerged
# (codex/ope-522-readiness-cohort-core at 2eed18b); OPE-517 lives in the separate training
# project, and the tests check these values against its source file.
READINESS_SCHEMA: Final = "opentour.pilot.readiness-cohort.v1"
TUNING_RESULT_SCHEMA: Final = "opentour.tuning-benefit-result.v1"
LABEL_EFFICIENCY_SCHEMA: Final = "opentour.label-efficiency.v1"
TUNING_VERDICTS: Final = (
    "measured_benefit",
    "no_material_benefit",
    "regression",
    "inconclusive",
    "unrun",
)
AUGMENTATION_UNPROVEN: Final = "Augmentation benefit unproven"
LABEL_SAVINGS_NOT_MEASURED: Final = "Label savings not measured"
TUNING_RESULT_FIELDS: Final = frozenset(
    {
        "schema",
        "protocol_digest",
        "arm_seal_digest",
        "evidence_tier",
        "claim_scope",
        "verdict",
        "status",
        "reasons",
        "claim",
        "improvement_claim_blocked",
        "significance_claim",
        "preferred_arm",
        "promotion_allowed",
        "denominator",
        "paired",
        "family_count_reconciliation",
        "unsatisfied_external_gates",
    }
)
TUNING_RESULT_EXTRAS: Final = frozenset({"retained_route", "cost"})
# OPE-517 builds a base record and adds the claim and its details on every return path.
LABEL_EFFICIENCY_FIELDS: Final = frozenset(
    {"schema", "tested_budgets", "ope_522_handoff", "evidence_tier", "launches_job"}
)
LABEL_EFFICIENCY_EXTRAS: Final = frozenset(
    {
        "claim",
        "reason",
        "tested_bound",
        "reduction_bps",
        "real_only_budget",
        "augmented_budget",
        "labor_delta_minutes",
        "labor_note",
    }
)

PROGRAM_CAPACITY: Final = 10
OPERATOR_GUIDE: Final = "services/cell-runtime/docs/pilot-cohort-funnel.md"
EVIDENCE_TIERS: Final = (
    "digest_self_consistent_only",
    "locally_keyed",
    "signed_separated_custody",
    "provider_witnessed",
    "customer_witnessed",
)
TIER_CEILING: Final = "locally_keyed"
EXTERNAL_CLAIM_TIER: Final = "customer_witnessed"
SETUP_KINDS: Final = ("generated", "approved_test_installation", "real_customer")
_CLAIM_SCOPE: Final = {
    "generated": "implementation_only",
    "approved_test_installation": "installed_test_only",
    "real_customer": "private_customer_register",
}
# OPE-517 labels its own evidence origin; a report only consumes results of its own kind.
_TUNING_ORIGINS: Final = {
    "generated": frozenset({"generated"}),
    "approved_test_installation": frozenset({"generated"}),
    "real_customer": frozenset({"owner-reviewed", "observed"}),
}
DECISION_OUTCOMES: Final = ("continue", "revise", "gather_evidence", "stop", "defer")
ACCEPTED_OUTCOMES: Final = ("continue", "revise", "gather_evidence", "stop")
MAX_RECEIPTS: Final = 10_000
MAX_INPUT_BYTES: Final = 8 * 1024 * 1024
_MAX_SECONDS: Final = 253_402_300_800  # 10000-01-01T00:00:00Z: a range check, not a policy.
UNSATISFIED_EXTERNAL_GATES: Final = (
    "crm_board_stage_mapping_and_disclosure_grant_unavailable",
    "scheduled_reporting_zap_and_task_history_unavailable",
    "real_customer_stage_receipts_absent",
    "ope_522_receipt_adapter_unmerged",
    "ope_517_real_heldout_results_unavailable",
    "ope_566_signing_and_custody_verifier_absent",
    "independent_exact_commit_review_pending",
)

# Stage receipt facts. "x?" admits null for unknown. A frozenset is a closed vocabulary.
_FACTS: Final[dict[str, dict[str, str | frozenset[str]]]] = {
    "ready_to_result": {"customer_wait_seconds": "seconds?", "operator_setup_seconds": "seconds?"},
    "starter_readout": {
        "readout_revision_ref": "ref",
        "benchmark_state": frozenset({"frozen_unscored", "scored"}),
        "compute_seconds": "seconds?",
        "limit_exceeded": "flag?",
        "cost": "cost?",
    },
    "real_case_evaluation": {
        "readout_revision_ref": "ref",
        "population": frozenset({"real_permitted"}),
        "comparison": frozenset({"rule_only", "incumbent_vs_repair", "exact_model"}),
        "witness_ref": "ref",
        "compute_seconds": "seconds?",
        "fit_count": "count?",
        "limit_exceeded": "flag?",
        "cost": "cost?",
    },
    "exact_fit": {"fit_job_ref": "ref", "artifact_digest": "hex"},
    "owner_decision": {
        "readout_revision_ref": "ref",
        "decided_on": "date",
        "owner_ref": "ref",
        "outcome": frozenset(DECISION_OUTCOMES),
        "stop_reason_ref": "ref?",
    },
    "delivery": {"artifact_digest": "hex", "destination_receipt_ref": "ref"},
    "payment": {"continuation_ref": "ref", "payment_evidence_ref": "ref", "recurring": "true"},
    "fee_waiver": {"status": frozenset({"proposed", "agreed"}), "terms_ref": "ref"},
    "correction": {
        "target_receipt_id": "ref",
        "target_revision": "revision",
        "kind": frozenset({"disputed", "reversed"}),
        "reason_ref": "ref",
    },
}
_RECEIPT_FIELDS: Final = frozenset(
    {
        "schema_version",
        "receipt_id",
        "revision",
        "supersedes_revision",
        "stage",
        "organization_ref",
        "engagement_revision",
        "recorded_at",
        "effective_at",
        "evidence_digest",
        "evidence_tier",
        "setup_kind",
        "source_record_ref",
        "facts",
    }
)
_RUN_STAGES: Final = ("starter_readout", "real_case_evaluation")
_STAGE_COLUMN: Final = {
    "ready_to_result": "ready_to_result",
    "starter_readout": "starter_readout",
    "real_case_evaluation": "real_case_evaluation",
    "exact_fit": "exact_fit",
    "owner_decision": "owner_decision",
    "delivery": "delivered",
    "payment": "paid_recurring",
}


def _link(anchor: str) -> str:
    return f"{OPERATOR_GUIDE}#{anchor}"


def _column(name: str, source: str, credit: str, owner: str, never: Sequence[str] = ()) -> Json:
    return {
        "column": name,
        "source": source,
        "credited_only_by": credit,
        "never_from": sorted(never),
        "owner_role": owner,
        "operator_link": _link(name),
    }


_NOT_EVIDENCE: Final = ("crm_stage", "slack_action", "generated_fixture", "meeting_or_proposal")
FIELD_MAP: Final[Json] = {
    "schema_version": FIELD_MAP_VERSION,
    "unit": "distinct canonical organization_ref",
    "register_snapshot": SNAPSHOT_VERSION,
    "stage_receipt": RECEIPT_VERSION,
    "columns": [
        _column(
            "qualified",
            "register.qualification_event",
            "a stored qualification receipt with disposition qualified",
            "intake_owner",
            _NOT_EVIDENCE,
        ),
        _column(
            "enrolled",
            "register.cohort_lease",
            "a live lease row for an admitted engagement",
            "cohort_operator",
            _NOT_EVIDENCE,
        ),
        _column(
            "ready_to_result",
            "stage_receipt.ready_to_result",
            "the instant permissions, inputs, worker and policy were all ready",
            "intake_owner",
            _NOT_EVIDENCE,
        ),
        _column(
            "starter_readout",
            "stage_receipt.starter_readout",
            "a frozen starter readout; it never credits a real-case evaluation",
            "cohort_operator",
            _NOT_EVIDENCE,
        ),
        _column(
            "real_case_evaluation",
            "stage_receipt.real_case_evaluation",
            "the authorized recipient's recorded rerun or witness entry for a comparison on real "
            "permitted cases (locally_keyed at most)",
            "authorized_recipient",
            (*_NOT_EVIDENCE, "starter_readout"),
        ),
        _column(
            "exact_fit",
            "stage_receipt.exact_fit",
            "a completed fit job and its artifact digest",
            "ml_owner",
            (*_NOT_EVIDENCE, "real_case_evaluation"),
        ),
        _column(
            "owner_decision",
            "stage_receipt.owner_decision",
            "a dated owner decision bound to a credited readout of the same engagement",
            "workflow_owner",
            _NOT_EVIDENCE,
        ),
        _column(
            "accepted",
            "stage_receipt.real_case_evaluation+owner_decision",
            "a latest owner decision of continue, revise, gather_evidence or stop on the same "
            "readout revision, made at or after a credited real-case evaluation",
            "cohort_operator",
            (*_NOT_EVIDENCE, "starter_readout", "exact_fit", "enrolled"),
        ),
        _column(
            "delivered",
            "stage_receipt.delivery",
            "a destination receipt for the exact artifact",
            "authorized_recipient",
            (*_NOT_EVIDENCE, "owner_decision", "approval"),
        ),
        _column(
            "paid_recurring",
            "stage_receipt.payment",
            "a continuation agreement reference and actual recurring payment evidence",
            "account_owner",
            (*_NOT_EVIDENCE, "approval", "owner_decision", "delivery", "fee_waiver", "proposal"),
        ),
        _column(
            "fee_waiver_proposed",
            "stage_receipt.fee_waiver",
            "a fee waiver receipt with status proposed",
            "account_owner",
            _NOT_EVIDENCE,
        ),
        _column(
            "fee_waiver_agreed",
            "stage_receipt.fee_waiver",
            "a fee waiver receipt with status agreed",
            "account_owner",
            _NOT_EVIDENCE,
        ),
    ],
    "current": [
        {"state": "queued", "source": "register.cohort_waiter", "owner_role": "cohort_operator"},
        {
            "state": "queue_stale",
            "source": "register.cohort_waiter",
            "owner_role": "cohort_operator",
        },
        {"state": "active", "source": "register.cohort_lease", "owner_role": "cohort_operator"},
        {
            "state": "active_requires_reconciliation",
            "source": "register.cohort_lease",
            "owner_role": "cohort_operator",
        },
        {"state": "closed", "source": "register.cohort_lease", "owner_role": "cohort_operator"},
        {"state": "withdrawn", "source": "register.cohort_lease", "owner_role": "custodian"},
        {"state": "lost", "source": "register.qualification_event", "owner_role": "intake_owner"},
        {
            "state": "deferred",
            "source": "stage_receipt.owner_decision",
            "owner_role": "workflow_owner",
        },
    ],
    "stage_facts": {
        stage: {
            name: sorted(kind) if isinstance(kind, frozenset) else kind
            for name, kind in facts.items()
        }
        for stage, facts in _FACTS.items()
    },
}
COLUMNS: Final = tuple(item["column"] for item in FIELD_MAP["columns"])
CURRENT_STATES: Final = tuple(item["state"] for item in FIELD_MAP["current"])
_COLUMN_OWNER: Final = {item["column"]: item["owner_role"] for item in FIELD_MAP["columns"]}
# A changed map needs a new version. The registry pins each published version's digest, so an
# edit that keeps the old version name refuses every report until the version is bumped.
_PUBLISHED_FIELD_MAPS: Final = {
    FIELD_MAP_VERSION: "a87f89f33d6f28ca69f0d3039b3a260a5c84d56d70f269fdc9aa8a25bcc79e11",
}
FIELD_MAP_DIGEST: Final = digest(FIELD_MAP)

MISMATCH_OWNERS: Final = {
    "receipt_not_canonical_json": "cohort_operator",
    "receipt_schema_unknown": "cohort_operator",
    "receipt_fields_invalid": "cohort_operator",
    "receipt_value_invalid": "cohort_operator",
    "receipt_time_invalid": "cohort_operator",
    "receipt_tier_unknown": "cohort_operator",
    "receipt_outside_setup_scope": "cohort_operator",
    "receipt_revision_conflict": "cohort_operator",
    "receipt_identity_changed": "cohort_operator",
    "receipt_predecessor_missing": "cohort_operator",
    "receipt_not_admitted": "intake_owner",
    "correction_target_unknown": "cohort_operator",
    "correction_target_foreign": "cohort_operator",
    "correction_target_not_creditable": "cohort_operator",
    "decision_readout_unknown": "workflow_owner",
    "decision_precedes_readout": "workflow_owner",
    "timing_inconsistent": "cohort_operator",
    "active_lease_needs_reconciliation": "cohort_operator",
    "register_multiple_open_leases": "custodian",
}
_PATH: Final = (
    "ready_to_result",
    "real_case_evaluation",
    "owner_decision",
    "accepted",
    "delivered",
    "paid_recurring",
)


class FunnelError(ValueError):
    """The report cannot be built from these inputs. Nothing was published."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _Refused(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _check(condition: object, code: str) -> None:
    if not condition:
        raise FunnelError(code)


def _refuse(condition: object, code: str) -> None:
    if not condition:
        raise _Refused(code)


def _instant(value: object) -> bool:
    return type(value) is int and 0 <= value < _MAX_SECONDS


def _revision(value: object) -> bool:
    return type(value) is int and 1 <= value < 2**63


def _iso_date(value: object) -> date | None:
    if not isinstance(value, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _decimal(value: object) -> Fraction | None:
    if not isinstance(value, str):
        return None
    try:
        return amount(value)
    except RepairError:
        return None


def _cost(value: object) -> Json | None:
    if (
        not isinstance(value, dict)
        or set(value) != {"amount", "currency", "basis"}
        or _decimal(value["amount"]) is None
        or not isinstance(value["currency"], str)
        or re.fullmatch("[A-Z]{3}", value["currency"]) is None
        or value["basis"] not in ("observed", "modeled")
    ):
        return None
    return value


def _fact(kind: str | frozenset[str], value: object) -> bool:
    if isinstance(kind, frozenset):
        return isinstance(value, str) and value in kind
    if kind.endswith("?") and value is None:
        return True
    base = kind.removesuffix("?")
    if base in ("seconds", "count"):
        return _instant(value)
    if base == "flag":
        return type(value) is bool
    if base == "ref":
        return _ref(value)
    if base == "hex":
        return _hex(value)
    if base == "date":
        return _iso_date(value) is not None
    if base == "true":
        return value is True
    if base == "revision":
        return _revision(value)
    if base == "cost":
        return _cost(value) is not None
    return False


def _tier_rank(tier: str) -> int:
    return EVIDENCE_TIERS.index(tier)


def _capped(tier: str) -> str:
    return tier if _tier_rank(tier) <= _tier_rank(TIER_CEILING) else TIER_CEILING


@dataclass(frozen=True)
class _Receipt:
    receipt_id: str
    revision: int
    stage: str
    org: str
    engagement: int
    recorded_at: int
    effective_at: int
    tier: str
    tier_claimed: str
    source_record: str | None
    facts: Json
    content: str

    @property
    def ref(self) -> str:
        return f"{self.receipt_id}@{self.revision}"

    @property
    def order(self) -> tuple[int, int, str]:
        return (self.effective_at, self.recorded_at, self.receipt_id)


def _materialize(raw: object) -> Json:
    """Consume a receipt once into plain JSON values; validate and keep only that copy."""
    try:
        value = json.loads(canonical(raw), object_pairs_hook=_json_object)
    except (TypeError, ValueError, RecursionError):
        raise _Refused("receipt_not_canonical_json") from None
    _refuse(isinstance(value, dict), "receipt_fields_invalid")
    assert isinstance(value, dict)
    return value


def _stage_receipt(raw: object, scope: str) -> _Receipt:
    value = _materialize(raw)
    _refuse(value.get("schema_version") == RECEIPT_VERSION, "receipt_schema_unknown")
    _refuse(set(value) == _RECEIPT_FIELDS, "receipt_fields_invalid")
    stage = value["stage"]
    _refuse(isinstance(stage, str) and stage in _FACTS, "receipt_value_invalid")
    facts = value["facts"]
    spec = _FACTS[stage]
    _refuse(isinstance(facts, dict) and set(facts) == set(spec), "receipt_fields_invalid")
    revision, supersedes = value["revision"], value["supersedes_revision"]
    _refuse(
        _ref(value["receipt_id"])
        and _ref(value["organization_ref"])
        and _revision(revision)
        and _revision(value["engagement_revision"])
        and _hex(value["evidence_digest"])
        and (value["source_record_ref"] is None or _ref(value["source_record_ref"]))
        and (
            supersedes is None
            if revision == 1
            else _revision(supersedes) and supersedes == revision - 1
        )
        and isinstance(value["setup_kind"], str)
        and value["setup_kind"] in SETUP_KINDS
        and all(_fact(kind, facts[name]) for name, kind in spec.items()),
        "receipt_value_invalid",
    )
    recorded, effective = value["recorded_at"], value["effective_at"]
    _refuse(
        _instant(recorded) and _instant(effective) and effective <= recorded, "receipt_time_invalid"
    )
    tier = value["evidence_tier"]
    _refuse(isinstance(tier, str) and tier in EVIDENCE_TIERS, "receipt_tier_unknown")
    _refuse(value["setup_kind"] == scope, "receipt_outside_setup_scope")
    if stage == "owner_decision":
        _refuse(
            (facts["stop_reason_ref"] is not None) == (facts["outcome"] == "stop"),
            "receipt_value_invalid",
        )
        decided = _iso_date(facts["decided_on"])
        # One day of slack for the owner's time zone; a later date was not yet decided.
        recorded_day = datetime.fromtimestamp(recorded, UTC).date().toordinal()
        _refuse(
            decided is not None and decided.toordinal() <= recorded_day + 1,
            "receipt_time_invalid",
        )
    return _Receipt(
        receipt_id=value["receipt_id"],
        revision=revision,
        stage=stage,
        org=value["organization_ref"],
        engagement=value["engagement_revision"],
        recorded_at=recorded,
        effective_at=effective,
        tier=_capped(tier),
        tier_claimed=tier,
        source_record=value["source_record_ref"],
        facts=facts,
        content=digest(value),
    )


def _peek(raw: object) -> str | None:
    """Name a refused receipt only by an identity that is itself well-formed."""
    if isinstance(raw, dict):
        rid, revision = raw.get("receipt_id"), raw.get("revision")
        if _ref(rid) and _revision(revision):
            return f"{rid}@{revision}"
    return None


class _Mismatches:
    def __init__(self) -> None:
        self.items: list[Json] = []

    def add(self, code: str, *, receipt: str | None = None, org: str | None = None) -> None:
        self.items.append(
            {
                "code": code,
                "owner_role": MISMATCH_OWNERS[code],
                "operator_link": _link("mismatch-codes"),
                "receipt_ref": receipt,
                "organization_ref": org,
            }
        )

    def sorted(self) -> list[Json]:
        unique = {canonical(item): item for item in self.items}
        return [unique[key] for key in sorted(unique)]


def _snapshot(register: Path) -> Json:
    try:
        snapshot = read_register_snapshot(register)
    except FileNotFoundError:
        raise FunnelError("register_unavailable") from None
    except (OSError, QualificationError):
        raise FunnelError("register_unreadable") from None
    return snapshot


def _median(values: Sequence[int]) -> str | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    value = (
        Fraction(ordered[middle])
        if len(ordered) % 2
        else Fraction(ordered[middle - 1] + ordered[middle], 2)
    )
    return rendered(value)


def _durations(population: int, values: Sequence[int]) -> Json:
    return {
        "population": population,
        "known": len(values),
        "unknown": population - len(values),
        "median_seconds": _median(values),
        "max_seconds": max(values) if values else None,
    }


def _known(values: Iterable[object]) -> list[int]:
    return [value for value in values if type(value) is int]


class _Credits:
    """Distinct organizations per column, with every evidence reference behind each count."""

    def __init__(self) -> None:
        self.orgs: dict[str, set[str]] = defaultdict(set)
        self.evidence: dict[str, set[str]] = defaultdict(set)

    def add(self, column: str, org: str, evidence: str) -> None:
        self.orgs[column].add(org)
        self.evidence[column].add(evidence)

    def block(self, column: str) -> Json:
        orgs = sorted(self.orgs.get(column, set()))
        return {
            "count": len(orgs),
            "organizations": orgs,
            "evidence": sorted(self.evidence.get(column, set())),
        }


def build_report(
    *,
    register: Path,
    receipts: Sequence[object],
    as_of: int,
    setup_kind: str,
    readiness: object = None,
    tuning_benefit: object = None,
    label_efficiency: object = None,
    outcome_disclosure_permitted: bool = False,
    supersedes_report_revision: str | None = None,
) -> Json:
    """Reconcile the register and stage receipts into one private funnel report."""
    _check(_instant(as_of), "as_of_invalid")
    _check(isinstance(setup_kind, str) and setup_kind in SETUP_KINDS, "setup_kind_invalid")
    _check(type(outcome_disclosure_permitted) is bool, "outcome_disclosure_invalid")
    _check(
        supersedes_report_revision is None or _hex(supersedes_report_revision),
        "previous_report_invalid",
    )
    _check(
        isinstance(receipts, (list, tuple)) and len(receipts) <= MAX_RECEIPTS,
        "receipt_batch_invalid",
    )
    _check(
        _PUBLISHED_FIELD_MAPS.get(FIELD_MAP_VERSION) == FIELD_MAP_DIGEST, "field_map_unpublished"
    )
    snapshot = _snapshot(register)
    mismatches = _Mismatches()

    quarantined: list[Json] = []
    considered: list[_Receipt] = []
    after_cutoff = 0
    for index, raw in enumerate(receipts):
        try:
            receipt = _stage_receipt(raw, setup_kind)
        except _Refused as refused:
            quarantined.append({"index": index, "code": refused.code, "receipt_ref": _peek(raw)})
            continue
        if receipt.recorded_at > as_of:
            after_cutoff += 1
            continue
        considered.append(receipt)

    current, duplicates = _current_revisions(considered, mismatches)
    corrected = _corrections(current, mismatches)
    admitted = _admitted(snapshot)
    bindable: list[_Receipt] = []
    for rid in sorted(current):
        receipt = current[rid]
        if receipt.stage == "correction":
            continue
        if (receipt.org, receipt.engagement) not in admitted:
            mismatches.add("receipt_not_admitted", receipt=receipt.ref, org=receipt.org)
            continue
        bindable.append(receipt)
    # A corrected receipt credits nothing, but a decision on it is still a dated decision.
    credited = [receipt for receipt in bindable if receipt.receipt_id not in corrected]

    credits = _Credits()
    state = _register_state(snapshot, as_of, credits, mismatches)
    decisions = _stage_credits(credited, bindable, credits, mismatches)
    accepted = _acceptances(decisions, corrected, credits, mismatches)
    latest = _latest_decisions(decisions)
    outcome_counts = {outcome: 0 for outcome in DECISION_OUTCOMES}
    for decision in latest.values():
        outcome_counts[decision.facts["outcome"]] += 1
    deferred = sorted(
        org for org, decision in latest.items() if decision.facts["outcome"] == "defer"
    )

    columns = {column: credits.block(column) for column in COLUMNS}
    accepted_orgs = columns["accepted"]["organizations"]
    tiers = {tier: 0 for tier in EVIDENCE_TIERS}
    by_outcome = {outcome: 0 for outcome in ACCEPTED_OUTCOMES}
    for org in accepted_orgs:
        tiers[accepted[org]["tier"]] += 1
        by_outcome[accepted[org]["outcome"]] += 1
    stops = [org for org in accepted_orgs if accepted[org]["outcome"] == "stop"]
    stopped = sorted(org for org, item in latest.items() if item.facts["outcome"] == "stop")
    other_stops = [org for org in stopped if org not in stops]
    current_states = {**state, "deferred": deferred}
    reached = {column: set(block["organizations"]) for column, block in columns.items()}
    # A lost organization or a stop has no next stage; a withdrawal is followed by lost.
    terminal = set(current_states["lost"]) | set(stopped) | set(stops)
    next_stage = []
    for org in sorted(reached["qualified"]):
        stage = None if org in terminal else next((s for s in _PATH if org not in reached[s]), None)
        next_stage.append(
            {
                "organization_ref": org,
                "next_stage": stage,
                "owner_role": _COLUMN_OWNER[stage] if stage else None,
                "operator_link": _link(stage) if stage else None,
            }
        )

    capped = sum(1 for receipt in credited if receipt.tier != receipt.tier_claimed)
    sources: dict[str, set[str]] = defaultdict(set)
    for receipt in credited:
        if receipt.source_record is not None:
            sources[receipt.org].add(receipt.source_record)
    duplicated_records = sorted(org for org, refs in sources.items() if len(refs) > 1)
    body: Json = {
        "schema_version": REPORT_VERSION,
        "field_map_version": FIELD_MAP_VERSION,
        "field_map_digest": FIELD_MAP_DIGEST,
        "as_of": as_of,
        "setup_kind": setup_kind,
        "claim_scope": _CLAIM_SCOPE[setup_kind],
        "customer_credit": setup_kind == "real_customer",
        "headline": {
            "accepted": len(accepted_orgs),
            "capacity": PROGRAM_CAPACITY,
            "display": f"{len(accepted_orgs)}/{PROGRAM_CAPACITY}",
            "active": len(current_states["active"]),
            "paid_recurring": columns["paid_recurring"]["count"],
            "accepted_by_evidence_tier": tiers,
            "externally_claimable_accepted": tiers[EXTERNAL_CLAIM_TIER],
            "external_claim_rule": "only customer_witnessed acceptances may be claimed externally",
        },
        "columns": columns,
        "current": {
            name: {"count": len(orgs), "organizations": orgs}
            for name, orgs in current_states.items()
        },
        "active_pending_effects": _pending_effects(snapshot),
        "accepted_by_outcome": by_outcome,
        "qualifying_stops": {
            "rule": "OPE-357: a stop decided on a credited real-case evaluation counts once",
            "organizations": stops,
        },
        "non_qualifying_stops": {
            "reason": "no credited real-case evaluation behind the latest stop decision",
            "organizations": other_stops,
        },
        "owner_decision_latest_outcome": outcome_counts,
        "drop_off": _drop_off(current_states, latest, stopped),
        "timings": _timings(credited, admitted, mismatches),
        "runs": _runs(credited),
        "costs": _costs(credited, accepted, accepted_orgs),
        "corrections": [
            {
                "receipt_ref": current[rid].ref,
                "column": _correction_column(current[rid]),
                "kind": kind,
            }
            for rid, kind in sorted(corrected.items())
        ],
        "duplicate_source_records": {
            "organizations": duplicated_records,
            "count": len(duplicated_records),
        },
        "next_missing_stage": next_stage,
        "mismatches": mismatches.sorted(),
        "quarantined": quarantined,
        "inputs": {
            "register_snapshot_digest": snapshot["snapshot_digest"],
            "receipt_contents_digest": digest(sorted(r.content for r in considered)),
            "receipts_read": len(receipts),
            "receipts_considered": len(considered),
            "receipts_quarantined": len(quarantined),
            "duplicate_deliveries": duplicates,
            "recorded_after_cutoff": after_cutoff,
            "receipts_credited": len(credited),
            "tier_claims_capped": capped,
        },
        "outcome_rollup": _outcome_rollup(
            setup_kind,
            outcome_disclosure_permitted,
            readiness,
            tuning_benefit,
            label_efficiency,
        ),
        "refresh": {
            "route": "owner_run",
            "zap_tasks": 0,
            "register_reads": 1,
            "receipts_read": len(receipts),
        },
        "integrity": "digest_self_consistent_only",
        "issuer_authenticated": False,
        "unsatisfied_external_gates": list(UNSATISFIED_EXTERNAL_GATES),
        "supersedes_report_revision": supersedes_report_revision,
    }
    return {**body, "report_revision": digest(body)}


def _pending_effects(snapshot: Json) -> int:
    """Count unreconciled effects on the open lease generation."""
    open_generations = {
        lease["generation"] for lease in snapshot["leases"] if lease["terminal_kind"] is None
    }
    return sum(
        1
        for effect in snapshot["effects"]
        if effect["pending"] and effect["generation"] in open_generations
    )


def _current_revisions(
    considered: Sequence[_Receipt], mismatches: _Mismatches
) -> tuple[dict[str, _Receipt], int]:
    grouped: dict[tuple[str, int], list[_Receipt]] = defaultdict(list)
    for receipt in considered:
        grouped[(receipt.receipt_id, receipt.revision)].append(receipt)
    duplicates = 0
    conflicted: set[str] = set()
    revisions: dict[str, dict[int, _Receipt]] = defaultdict(dict)
    for (rid, revision), group in sorted(grouped.items()):
        if len({receipt.content for receipt in group}) > 1:
            orgs = {receipt.org for receipt in group}
            mismatches.add(
                "receipt_revision_conflict",
                receipt=f"{rid}@{revision}",
                org=orgs.pop() if len(orgs) == 1 else None,
            )
            conflicted.add(rid)
            continue
        duplicates += len(group) - 1
        revisions[rid][revision] = group[0]
    current: dict[str, _Receipt] = {}
    for rid, versions in sorted(revisions.items()):
        if rid in conflicted:
            continue
        identities = {(r.stage, r.org, r.engagement) for r in versions.values()}
        if len(identities) != 1:
            mismatches.add("receipt_identity_changed", receipt=rid)
            continue
        top = 0
        while top + 1 in versions:
            top += 1
        for held in sorted(revision for revision in versions if revision > top):
            mismatches.add(
                "receipt_predecessor_missing",
                receipt=f"{rid}@{held}",
                org=versions[held].org,
            )
        if top:
            current[rid] = versions[top]
    return current, duplicates


def _corrections(current: Mapping[str, _Receipt], mismatches: _Mismatches) -> dict[str, str]:
    corrected: dict[str, str] = {}
    for rid in sorted(current):
        correction = current[rid]
        if correction.stage != "correction":
            continue
        target_id = correction.facts["target_receipt_id"]
        target = current.get(target_id)
        if target is None or correction.facts["target_revision"] > target.revision:
            mismatches.add("correction_target_unknown", receipt=correction.ref, org=correction.org)
        elif target.org != correction.org:
            mismatches.add("correction_target_foreign", receipt=correction.ref, org=correction.org)
        elif target.stage == "correction":
            mismatches.add(
                "correction_target_not_creditable", receipt=correction.ref, org=correction.org
            )
        elif correction.facts["target_revision"] == target.revision:
            kind = correction.facts["kind"]
            corrected[target_id] = (
                "reversed" if "reversed" in (kind, corrected.get(target_id)) else kind
            )
        # A correction of an already superseded revision stays history and moves no credit.
    return corrected


def _admitted(snapshot: Json) -> dict[tuple[str, int], int]:
    """Admitted engagements and the first instant each was observed as qualified."""
    admitted: dict[tuple[str, int], int] = {}
    for event in snapshot["qualification_events"]:
        if event["disposition"] == "qualified":
            key = (event["organization_ref"], event["engagement_revision"])
            admitted[key] = min(admitted.get(key, event["observed_at"]), event["observed_at"])
    return admitted


def _register_state(
    snapshot: Json, as_of: int, credits: _Credits, mismatches: _Mismatches
) -> dict[str, list[str]]:
    latest: dict[str, Json] = {}
    for event in snapshot["qualification_events"]:
        if event["disposition"] == "qualified":
            credits.add(
                "qualified", event["organization_ref"], f"register:{event['operation_ref']}"
            )
        org = event["organization_ref"]
        rank = (event["engagement_revision"], event["source_revision"])
        prior = latest.get(org)
        if prior is None or rank > (prior["engagement_revision"], prior["source_revision"]):
            latest[org] = event
    leased = {
        (lease["organization_ref"], lease["engagement_revision"]) for lease in snapshot["leases"]
    }
    states: dict[str, set[str]] = {name: set() for name in CURRENT_STATES if name != "deferred"}

    def current_for(org: str, engagement: int, qualification: str) -> bool:
        event = latest.get(org)
        return (
            event is not None
            and event["disposition"] == "qualified"
            and event["engagement_revision"] == engagement
            and event["event_digest"] == qualification
        )

    for waiter in snapshot["waiters"]:
        org, engagement = waiter["organization_ref"], waiter["engagement_revision"]
        if (org, engagement) in leased:
            continue
        fresh = waiter["expires_at"] > as_of and current_for(
            org, engagement, waiter["qualification_digest"]
        )
        states["queued" if fresh else "queue_stale"].add(org)
    open_leases = 0
    for lease in snapshot["leases"]:
        org = lease["organization_ref"]
        credits.add("enrolled", org, f"register:lease:{lease['lease_ref']}")
        if lease["terminal_kind"] == "closeout":
            states["closed"].add(org)
        elif lease["terminal_kind"] == "withdrawal":
            states["withdrawn"].add(org)
        else:
            open_leases += 1
            states["active"].add(org)
            if lease["expires_at"] <= as_of or not current_for(
                org, lease["engagement_revision"], lease["qualification_digest"]
            ):
                states["active_requires_reconciliation"].add(org)
                mismatches.add("active_lease_needs_reconciliation", org=org)
    if open_leases > 1:
        mismatches.add("register_multiple_open_leases")
    for org, event in latest.items():
        if event["disposition"] == "lost":
            states["lost"].add(org)
    states["queued"] -= states["lost"]
    states["queue_stale"] -= states["lost"] | states["queued"]
    return {name: sorted(orgs) for name, orgs in states.items()}


def _stage_credits(
    credited: Sequence[_Receipt],
    bindable: Sequence[_Receipt],
    credits: _Credits,
    mismatches: _Mismatches,
) -> list[tuple[_Receipt, list[_Receipt]]]:
    """Credit single-receipt columns; bind each owner decision to its admitted readouts."""
    readouts: dict[tuple[str, int, str], list[_Receipt]] = defaultdict(list)
    for receipt in bindable:
        if receipt.stage in _RUN_STAGES:
            key = (receipt.org, receipt.engagement, receipt.facts["readout_revision_ref"])
            readouts[key].append(receipt)
    decisions: list[tuple[_Receipt, list[_Receipt]]] = []
    for receipt in credited:
        if receipt.stage == "fee_waiver":
            credits.add(
                f"fee_waiver_{receipt.facts['status']}", receipt.org, f"receipt:{receipt.ref}"
            )
        elif receipt.stage == "owner_decision":
            key = (receipt.org, receipt.engagement, receipt.facts["readout_revision_ref"])
            if key not in readouts:
                mismatches.add("decision_readout_unknown", receipt=receipt.ref, org=receipt.org)
                continue
            credits.add("owner_decision", receipt.org, f"receipt:{receipt.ref}")
            decisions.append((receipt, readouts[key]))
        elif receipt.stage in _STAGE_COLUMN:
            credits.add(_STAGE_COLUMN[receipt.stage], receipt.org, f"receipt:{receipt.ref}")
    return decisions


def _acceptances(
    decisions: Sequence[tuple[_Receipt, list[_Receipt]]],
    corrected: Mapping[str, str],
    credits: _Credits,
    mismatches: _Mismatches,
) -> dict[str, Json]:
    """Accept an organization once from a credited real-case evaluation and a later decision."""
    latest: dict[tuple[str, int, str], tuple[_Receipt, list[_Receipt]]] = {}
    for decision, readouts in decisions:
        key = (decision.org, decision.engagement, decision.facts["readout_revision_ref"])
        if key not in latest or decision.order > latest[key][0].order:
            latest[key] = (decision, readouts)
    accepted: dict[str, Json] = {}
    for key in sorted(latest):
        decision, readouts = latest[key]
        real = [
            r
            for r in readouts
            if r.stage == "real_case_evaluation" and r.receipt_id not in corrected
        ]
        witnessed = [r for r in real if r.effective_at <= decision.effective_at]
        if real and not witnessed:
            mismatches.add("decision_precedes_readout", receipt=decision.ref, org=decision.org)
        if not witnessed or decision.facts["outcome"] not in ACCEPTED_OUTCOMES:
            continue
        best = max(witnessed, key=lambda r: (_tier_rank(r.tier), r.order))
        # A pair is only as strong as its weaker receipt; the organization keeps its best pair.
        tier = min(_tier_rank(best.tier), _tier_rank(decision.tier))
        for readout in witnessed:
            credits.add("accepted", decision.org, f"receipt:{decision.ref}|{readout.ref}")
        prior = accepted.setdefault(
            decision.org, {"tier": tier, "outcome": None, "order": None, "engagements": set()}
        )
        prior["tier"] = max(prior["tier"], tier)
        prior["engagements"].add(decision.engagement)
        if prior["order"] is None or decision.order > prior["order"]:
            prior["outcome"], prior["order"] = decision.facts["outcome"], decision.order
    for item in accepted.values():
        item["tier"] = EVIDENCE_TIERS[item["tier"]]
    return accepted


def _latest_decisions(
    decisions: Sequence[tuple[_Receipt, list[_Receipt]]],
) -> dict[str, _Receipt]:
    latest: dict[str, _Receipt] = {}
    for decision, _ in decisions:
        if decision.org not in latest or decision.order > latest[decision.org].order:
            latest[decision.org] = decision
    return latest


def _correction_column(receipt: _Receipt) -> str:
    if receipt.stage == "fee_waiver":
        return f"fee_waiver_{receipt.facts['status']}"
    return _STAGE_COLUMN[receipt.stage]


def _drop_off(
    states: Mapping[str, list[str]], latest: Mapping[str, _Receipt], stopped: Sequence[str]
) -> Json:
    """Closed reason codes only. Owner stop reasons stay as opaque refs in this private report."""
    reasons = {
        "qualification_lost": list(states["lost"]),
        "engagement_withdrawn": list(states["withdrawn"]),
        "queue_stale": list(states["queue_stale"]),
        "owner_stop": list(stopped),
        "owner_deferred": list(states["deferred"]),
    }
    return {
        "counts": {reason: len(orgs) for reason, orgs in reasons.items()},
        "organizations": reasons,
        "stop_reason_refs": {
            org: latest[org].facts["stop_reason_ref"] for org in reasons["owner_stop"]
        },
    }


def _timings(
    credited: Sequence[_Receipt],
    admitted: Mapping[tuple[str, int], int],
    mismatches: _Mismatches,
) -> Json:
    by_engagement: dict[tuple[str, int], list[_Receipt]] = defaultdict(list)
    for receipt in credited:
        by_engagement[(receipt.org, receipt.engagement)].append(receipt)
    onboarding: list[int] = []
    customer_wait: list[object] = []
    operator_setup: list[object] = []
    first_readout: list[int] = []
    first_real: list[int] = []
    compute: list[object] = []
    population = 0
    for key in sorted(by_engagement):
        receipts = by_engagement[key]
        ready = sorted((r for r in receipts if r.stage == "ready_to_result"), key=lambda r: r.order)
        if not ready:
            continue
        population += 1
        start = ready[0]
        customer_wait.append(start.facts["customer_wait_seconds"])
        operator_setup.append(start.facts["operator_setup_seconds"])
        if start.effective_at >= admitted[key]:
            onboarding.append(start.effective_at - admitted[key])
        else:
            mismatches.add("timing_inconsistent", receipt=start.ref, org=start.org)
        runs = sorted((r for r in receipts if r.stage in _RUN_STAGES), key=lambda r: r.order)
        if any(r.effective_at < start.effective_at for r in runs):
            mismatches.add("timing_inconsistent", receipt=start.ref, org=start.org)
        after = [r for r in runs if r.effective_at >= start.effective_at]
        if after:
            first_readout.append(after[0].effective_at - start.effective_at)
            compute.append(after[0].facts["compute_seconds"])
        real = [r for r in after if r.stage == "real_case_evaluation"]
        if real:
            first_real.append(real[0].effective_at - start.effective_at)
    return {
        "basis": "engagements with a credited ready_to_result receipt",
        "onboarding_wait": _durations(population, onboarding),
        "customer_wait": _durations(population, _known(customer_wait)),
        "operator_setup": _durations(population, _known(operator_setup)),
        "ready_to_first_readout": _durations(population, first_readout),
        "ready_to_first_real_evaluation": _durations(population, first_real),
        "first_readout_compute": _durations(len(compute), _known(compute)),
    }


def _runs(credited: Sequence[_Receipt]) -> Json:
    runs = [r for r in credited if r.stage in _RUN_STAGES]
    evaluations = [r for r in runs if r.stage == "real_case_evaluation"]
    limits = [r.facts["limit_exceeded"] for r in runs if r.facts["limit_exceeded"] is not None]
    fits = _known(r.facts["fit_count"] for r in evaluations)
    return {
        "runs": len(runs),
        "limit_exceeded": {
            "known": len(limits),
            "unknown": len(runs) - len(limits),
            "exceeded": sum(1 for flag in limits if flag),
        },
        "zero_fit": {
            "evaluations": len(evaluations),
            "known": len(fits),
            "unknown": len(evaluations) - len(fits),
            "zero_fit": sum(1 for count in fits if count == 0),
        },
    }


def _costs(
    credited: Sequence[_Receipt], accepted: Mapping[str, Json], accepted_orgs: Sequence[str]
) -> Json:
    """Cost per accepted pilot from complete costs only; partial or mixed costs stay unknown."""
    result: Json = {}
    for basis in ("observed", "modeled"):
        known: dict[str, tuple[str, Fraction]] = {}
        for org in accepted_orgs:
            engagements = accepted[org]["engagements"]
            runs = [
                r
                for r in credited
                if r.org == org and r.stage in _RUN_STAGES and r.engagement in engagements
            ]
            costs = [r.facts["cost"] for r in runs]
            if (
                runs
                and all(c is not None and c["basis"] == basis for c in costs)
                and len({c["currency"] for c in costs}) == 1
            ):
                known[org] = (
                    costs[0]["currency"],
                    sum((amount(c["amount"]) for c in costs), Fraction(0)),
                )
        currencies = {currency for currency, _ in known.values()}
        base: Json = {
            "accepted_organizations": len(accepted_orgs),
            "organizations_with_known_cost": len(known),
            "unknown_cost_organizations": len(accepted_orgs) - len(known),
        }
        if not accepted_orgs:
            result[basis] = {**base, "state": "unavailable", "reason": "no_accepted_organizations"}
        elif not known:
            result[basis] = {**base, "state": "unavailable", "reason": "no_complete_cost"}
        elif len(currencies) > 1:
            result[basis] = {**base, "state": "unavailable", "reason": "multiple_currencies"}
        else:
            total = sum((value for _, value in known.values()), Fraction(0))
            result[basis] = {
                **base,
                "state": "available",
                "currency": currencies.pop(),
                "total": rendered(total),
                "per_accepted_where_known": rendered(total / len(known)),
            }
    return result


def _outcome_rollup(
    setup_kind: str,
    permitted: bool,
    readiness: object,
    tuning_benefit: object,
    label_efficiency: object,
) -> Json:
    if not permitted:
        withheld = {"state": "withheld", "reason": "owner_disclosure_not_permitted"}
        return {
            "disclosure": "withheld",
            "readiness": withheld,
            "tuning_benefit": {**withheld, "claim": AUGMENTATION_UNPROVEN},
            "label_efficiency": {**withheld, "claim": LABEL_SAVINGS_NOT_MEASURED},
        }
    return {
        "disclosure": "permitted",
        "readiness": _readiness(setup_kind, readiness),
        "tuning_benefit": _tuning(setup_kind, tuning_benefit),
        "label_efficiency": _label_efficiency(setup_kind, label_efficiency),
    }


_READINESS_FIELDS: Final = frozenset(
    {
        "schema_version",
        "window_start",
        "window_end",
        "cutoff",
        "source_revision",
        "workflows",
        "ever_fit_ready",
        "current",
        "policy_only",
        "unknown_seed",
        "median_seconds_to_ready",
        "pending_ages_seconds",
    }
)
_READINESS_DISPOSITIONS: Final = frozenset(
    {"held", "fit_ready", "simple_resolved", "withdrawn", "disputed", "pending"}
)
_WAITING: Final = ("held", "pending", "disputed")


def _readiness(setup_kind: str, receipt: object) -> Json:
    """Consume one OPE-522 readiness cohort receipt, or fail closed with the gate it waits on."""
    if receipt is None:
        return {
            "state": "unavailable",
            "reason": "ope522_readiness_receipt_absent",
            "gate": "OPE-522",
        }
    try:
        value = _materialize(receipt)
        readout = value.get("readout")
        _refuse(
            set(value) == {"schema_version", "setup_kind", "readout", "revision"}
            and value["schema_version"] == READINESS_RECEIPT_VERSION
            and isinstance(readout, dict)
            and set(readout) == _READINESS_FIELDS
            and readout["schema_version"] == READINESS_SCHEMA
            and value["revision"] == digest(readout),
            "shape",
        )
        assert isinstance(readout, dict)
        _refuse(value["setup_kind"] == setup_kind, "scope")
        window = (readout["window_start"], readout["window_end"], readout["cutoff"])
        counts = (
            readout["workflows"],
            readout["ever_fit_ready"],
            readout["policy_only"],
            readout["unknown_seed"],
        )
        current = readout["current"]
        ages = readout["pending_ages_seconds"]
        median = readout["median_seconds_to_ready"]
        _refuse(
            all(_instant(item) for item in (*window, *counts))
            and window[0] < window[1] <= window[2]
            and _hex(readout["source_revision"])
            and isinstance(current, list)
            and all(
                isinstance(pair, list)
                and len(pair) == 2
                and pair[0] in _READINESS_DISPOSITIONS
                and _revision(pair[1])
                for pair in current
            )
            and [pair[0] for pair in current] == sorted({pair[0] for pair in current})
            and isinstance(ages, list)
            and all(_instant(age) for age in ages)
            and ages == sorted(ages),
            "shape",
        )
        by_state = {pair[0]: pair[1] for pair in current}
        workflows, ready = counts[0], counts[1]
        _refuse(
            sum(by_state.values()) == workflows
            and ready <= workflows
            and by_state.get("fit_ready", 0) <= ready
            and len(ages) == sum(by_state.get(name, 0) for name in _WAITING)
            and (
                median is None
                if ready == 0
                else (
                    type(median) in (int, float) and median >= 0 and float(median * 2).is_integer()
                )
            ),
            "reconciliation",
        )
    except _Refused as refused:
        reason = {
            "scope": "ope522_readiness_receipt_outside_setup_scope",
            "reconciliation": "ope522_readiness_receipt_inconsistent",
        }.get(refused.code, "ope522_readiness_receipt_invalid")
        return {"state": "held", "reason": reason, "gate": "OPE-522"}
    waiting = sum(by_state.get(name, 0) for name in _WAITING)
    return {
        "state": "available",
        "source": "OPE-522",
        "revision": value["revision"],
        "integrity": "digest_self_consistent_only",
        "unit": "workflow (organizations are counted separately in the funnel)",
        "window": {"start": window[0], "end": window[1], "cutoff": window[2]},
        "workflows": workflows,
        "ever_fit_ready": ready,
        "fit_ready_display": f"{ready}/{workflows}",
        "current": {name: by_state.get(name, 0) for name in sorted(_READINESS_DISPOSITIONS)},
        "simple_resolved": by_state.get("simple_resolved", 0),
        "still_waiting": waiting,
        "zero_seed_starters": counts[2],
        "unknown_seed": counts[3],
        "median_seconds_to_ready": rendered(Fraction(median)) if median is not None else None,
        "pending_ages_seconds": ages,
    }


def _tuning(setup_kind: str, result: object) -> Json:
    """Consume one OPE-517 tuning-benefit result; absence keeps the benefit unproven."""
    if result is None:
        return {
            "state": "unavailable",
            "reason": "ope517_result_absent",
            "gate": "OPE-517",
            "claim": AUGMENTATION_UNPROVEN,
        }
    held = {"state": "held", "gate": "OPE-517", "claim": AUGMENTATION_UNPROVEN}
    try:
        value = _materialize(result)
        body = value.get("body")
        _refuse(
            set(value) == {"body", "result_digest"}
            and isinstance(body, dict)
            and TUNING_RESULT_FIELDS <= set(body) <= TUNING_RESULT_FIELDS | TUNING_RESULT_EXTRAS
            and body["schema"] == TUNING_RESULT_SCHEMA
            and value["result_digest"] == "sha256:" + digest(body)
            and body["verdict"] in TUNING_VERDICTS
            and isinstance(body["claim"], str)
            and type(body["improvement_claim_blocked"]) is bool
            and type(body["promotion_allowed"]) is bool
            and body["improvement_claim_blocked"] == (body["verdict"] != "measured_benefit")
            and isinstance(body["evidence_tier"], str)
            and isinstance(body["claim_scope"], str),
            "shape",
        )
        assert isinstance(body, dict)
        _refuse(body["evidence_tier"] in _TUNING_ORIGINS[setup_kind], "scope")
    except _Refused as refused:
        reason = (
            "ope517_result_outside_setup_scope"
            if refused.code == "scope"
            else "ope517_result_invalid"
        )
        return {**held, "reason": reason}
    verdict = body["verdict"]
    return {
        "state": "available",
        "source": "OPE-517",
        "result_digest": value["result_digest"],
        "integrity": "digest_self_consistent_only",
        "verdict": verdict,
        "claim": body["claim"]
        if verdict == "measured_benefit"
        else (AUGMENTATION_UNPROVEN if verdict in ("inconclusive", "unrun") else body["claim"]),
        "evidence_tier": body["evidence_tier"],
        "claim_scope": body["claim_scope"],
        "promotion_allowed": body["promotion_allowed"],
    }


def _label_efficiency(setup_kind: str, record: object) -> Json:
    """Consume one OPE-517 label-efficiency record; absence means label savings not measured.

    The producer owns the claim wording, so it is passed through rather than re-derived.
    Negative savings are kept: more expert minutes on the augmented route is extra burden.
    """
    missing = {"gate": "OPE-517", "claim": LABEL_SAVINGS_NOT_MEASURED}
    if record is None:
        return {**missing, "state": "unavailable", "reason": "ope517_label_efficiency_absent"}
    try:
        value = _materialize(record)
        fields = set(value)
        _refuse(
            LABEL_EFFICIENCY_FIELDS | {"claim"}
            <= fields
            <= LABEL_EFFICIENCY_FIELDS | LABEL_EFFICIENCY_EXTRAS
            and value["schema"] == LABEL_EFFICIENCY_SCHEMA
            and isinstance(value["claim"], str)
            and value["launches_job"] is False
            and isinstance(value["tested_budgets"], list)
            and all(_instant(budget) for budget in value["tested_budgets"])
            and isinstance(value["evidence_tier"], str),
            "shape",
        )
        delta = value.get("labor_delta_minutes")
        _refuse(delta is None or (type(delta) is int and abs(delta) < _MAX_SECONDS), "shape")
        _refuse(value["evidence_tier"] in _TUNING_ORIGINS[setup_kind], "scope")
    except _Refused as refused:
        reason = (
            "ope517_label_efficiency_outside_setup_scope"
            if refused.code == "scope"
            else "ope517_label_efficiency_invalid"
        )
        return {**missing, "state": "held", "reason": reason}
    direction = (
        "not_measured"
        if delta is None
        else ("additional_burden" if delta > 0 else "saved" if delta < 0 else "unchanged")
    )
    return {
        "state": "available",
        "source": "OPE-517",
        "integrity": "digest_self_consistent_only",
        "claim": value["claim"],
        "tested_budgets": len(value["tested_budgets"]),
        "evidence_tier": value["evidence_tier"],
        "expert_minutes_delta": delta,
        "expert_minutes_direction": direction,
    }


def verify_report(report: Mapping[str, Any]) -> list[str]:
    """Recompute every total from the report's own evidence lists; return the problems found."""
    problems: list[str] = []
    body = {key: value for key, value in report.items() if key != "report_revision"}
    if report.get("schema_version") != REPORT_VERSION:
        return ["report_schema_unknown"]
    if report.get("report_revision") != digest(body):
        problems.append("report_revision_mismatch")
    try:
        columns = report["columns"]
        for column in COLUMNS:
            block = columns[column]
            orgs = block["organizations"]
            if block["count"] != len(orgs) or len(set(orgs)) != len(orgs):
                problems.append(f"column_count_mismatch:{column}")
            if block["count"] > len(block["evidence"]):
                problems.append(f"column_evidence_missing:{column}")
        headline = report["headline"]
        accepted = columns["accepted"]["count"]
        if (
            headline["accepted"] != accepted
            or headline["display"] != f"{accepted}/{headline['capacity']}"
        ):
            problems.append("headline_mismatch")
        if sum(headline["accepted_by_evidence_tier"].values()) != accepted:
            problems.append("evidence_tier_split_mismatch")
        if sum(report["accepted_by_outcome"].values()) != accepted:
            problems.append("accepted_outcome_split_mismatch")
        if (
            headline["externally_claimable_accepted"]
            != headline["accepted_by_evidence_tier"][EXTERNAL_CLAIM_TIER]
        ):
            problems.append("external_claim_mismatch")
        if headline["paid_recurring"] != columns["paid_recurring"]["count"]:
            problems.append("paid_mismatch")
        if (
            sum(report["owner_decision_latest_outcome"].values())
            != columns["owner_decision"]["count"]
        ):
            problems.append("owner_decision_split_mismatch")
        if (
            report["current"]["deferred"]["count"]
            != report["owner_decision_latest_outcome"]["defer"]
        ):
            problems.append("deferred_mismatch")
        for state in report["current"].values():
            if state["count"] != len(state["organizations"]):
                problems.append("current_count_mismatch")
    except (KeyError, TypeError):
        problems.append("report_shape_invalid")
    return problems


def weekly_view(report: Mapping[str, Any]) -> Json:
    """Project the evidence-free weekly aggregate: counts, closed codes and durations only."""
    problems = verify_report(report)
    if problems:
        raise FunnelError("report_inconsistent")
    outcome = report["outcome_rollup"]
    readiness = {
        key: value for key, value in outcome["readiness"].items() if key != "pending_ages_seconds"
    }
    counts: dict[str, int] = defaultdict(int)
    for item in report["mismatches"]:
        counts[item["code"]] += 1
    for item in report["quarantined"]:
        counts[item["code"]] += 1
    corrections: dict[str, int] = defaultdict(int)
    for item in report["corrections"]:
        corrections[item["column"]] += 1
    stages: dict[str, int] = defaultdict(int)
    for item in report["next_missing_stage"]:
        stages[item["next_stage"] or "none"] += 1
    return {
        "schema_version": WEEKLY_VERSION,
        "report_revision": report["report_revision"],
        "field_map_version": report["field_map_version"],
        "as_of": report["as_of"],
        "setup_kind": report["setup_kind"],
        "claim_scope": report["claim_scope"],
        "customer_credit": report["customer_credit"],
        "headline": report["headline"],
        "columns": {name: block["count"] for name, block in report["columns"].items()},
        "collapsed_duplicate_evidence": {
            name: len(block["evidence"]) - block["count"]
            for name, block in report["columns"].items()
        },
        "current": {name: block["count"] for name, block in report["current"].items()},
        "active_pending_effects": report["active_pending_effects"],
        "accepted_by_outcome": report["accepted_by_outcome"],
        "qualifying_stops": len(report["qualifying_stops"]["organizations"]),
        "non_qualifying_stops": len(report["non_qualifying_stops"]["organizations"]),
        "owner_decision_latest_outcome": report["owner_decision_latest_outcome"],
        "drop_off": report["drop_off"]["counts"],
        "timings": report["timings"],
        "runs": report["runs"],
        "costs": report["costs"],
        "corrections_by_column": dict(sorted(corrections.items())),
        "duplicate_source_records": report["duplicate_source_records"]["count"],
        "next_missing_stage": dict(sorted(stages.items())),
        "mismatches_by_code": dict(sorted(counts.items())),
        "inputs": {
            key: report["inputs"][key]
            for key in (
                "receipts_read",
                "receipts_considered",
                "receipts_quarantined",
                "duplicate_deliveries",
                "recorded_after_cutoff",
                "receipts_credited",
                "tier_claims_capped",
            )
        },
        "outcome_rollup": {**outcome, "readiness": readiness},
        "refresh": report["refresh"],
        "integrity": report["integrity"],
        "issuer_authenticated": report["issuer_authenticated"],
        "unsatisfied_external_gates": report["unsatisfied_external_gates"],
    }


def _private_document(path: Path) -> Json:
    """Read one owner-private JSON object, refusing duplicate keys and oversized input."""
    _private_parent(path)
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    with os.fdopen(os.open(path, flags), "rb") as handle:
        _private_record(os.fstat(handle.fileno()))
        content = handle.read(MAX_INPUT_BYTES + 1)
    if len(content) > MAX_INPUT_BYTES:
        raise QualificationError("funnel_input_too_large")
    value = json.loads(content, object_pairs_hook=_json_object)
    if not isinstance(value, dict):
        raise QualificationError("funnel_input_invalid")
    return value


def _write_private(path: Path, value: Json) -> None:
    """Create the private report once (mode 0600); an earlier revision is never overwritten."""
    _private_parent(path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    with os.fdopen(os.open(path, flags, 0o600), "w", encoding="ascii") as handle:
        handle.write(canonical(value) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--register", required=True, type=Path)
    parser.add_argument("--receipts", required=True, type=Path)
    parser.add_argument("--as-of", required=True, type=int)
    parser.add_argument("--setup-kind", required=True, choices=SETUP_KINDS)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--previous-report", type=Path)
    parser.add_argument("--readiness", type=Path)
    parser.add_argument("--tuning-benefit", type=Path)
    parser.add_argument("--label-efficiency", type=Path)
    parser.add_argument("--outcome-disclosure-permitted", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.as_of > int(time.time()):
            raise FunnelError("as_of_in_future")
        batch = _private_document(args.receipts)
        if set(batch) != {"schema_version", "receipts"} or batch["schema_version"] != BATCH_VERSION:
            raise FunnelError("receipt_batch_invalid")
        previous = None
        if args.previous_report is not None:
            prior = _private_document(args.previous_report)
            if verify_report(prior):
                raise FunnelError("previous_report_invalid")
            previous = prior["report_revision"]
        optional = {
            name: _private_document(path) if path is not None else None
            for name, path in (
                ("readiness", args.readiness),
                ("tuning_benefit", args.tuning_benefit),
                ("label_efficiency", args.label_efficiency),
            )
        }
        report = build_report(
            register=args.register,
            receipts=batch["receipts"],
            as_of=args.as_of,
            setup_kind=args.setup_kind,
            outcome_disclosure_permitted=args.outcome_disclosure_permitted,
            supersedes_report_revision=previous,
            **optional,
        )
        view = weekly_view(report)
        _write_private(args.output, report)
    except FunnelError as error:
        print(f"Funnel held: {error.code}.", file=sys.stderr)
        return 1
    except (OSError, ValueError, RecursionError):
        print("Funnel held: private input or output unavailable.", file=sys.stderr)
        return 1
    print(canonical(view))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
