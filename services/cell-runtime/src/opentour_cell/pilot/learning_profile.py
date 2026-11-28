"""Versioned per-pilot learning settings and the source-pair field mapping (OPE-416).

A settings batch is the worker-held record that pins, for one tenant and workflow:

* the intake-created workflow benchmark and the policy and evaluation-policy revisions;
* the bound sources, each with a role (decision or correction) and an evidence origin;
* the source-pair mapping from source columns to the logical join fields;
* the permitted decision-time feature allowlist and who may adjudicate a correction;
* the source freshness and correction maturity windows;
* the development/protected split handed to the existing partitioner;
* the permitted uses and the counters the snapshot receipt may disclose.

Revisions are write-once and consecutive. Re-committing identical bytes is a no-op; different
bytes under a committed revision conflict. Only a principal bound as ``data_custodian`` may
commit. The principal is resolved by the worker's authenticated connection; this module does
not authenticate it (that is the connection's job) and never accepts an authority value from
the rows it maps.

Evidence tier: every record here is ``digest_self_consistent_only``. The pin beside each file
detects an edit of one file; anyone who can rewrite both files can forge a batch. No signing or
key-custody contract exists (OPE-566).

Simulated and NOT proven: generated sentinel fixtures in a local directory only. Nothing here
is provider, installed-witness or customer evidence.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from opentour_cell.pilot.operating_point import canonical, digest

SETTINGS_SCHEMA = "opentour.pilot.learning-profile-settings.v1"
EVIDENCE_TIER = "digest_self_consistent_only"

CUSTODIAN = "data_custodian"
PRINCIPAL_ROLES = frozenset({CUSTODIAN, "workflow_owner", "reviewer", "verifier"})
ADJUDICATOR_ROLES = frozenset({"verifier"})

DECISION, CORRECTION = "decision", "correction"
STARTER_GENERATED = "starter_generated"
STARTER_OWNER_TARGET = "starter_owner_target"
OBSERVED_DECISION = "observed_decision"
INDEPENDENT_OUTCOME = "independent_outcome"
ORIGINS = {
    DECISION: frozenset({STARTER_GENERATED, STARTER_OWNER_TARGET, OBSERVED_DECISION}),
    CORRECTION: frozenset({INDEPENDENT_OUTCOME}),
}
STARTER_ORIGINS = frozenset({STARTER_GENERATED, STARTER_OWNER_TARGET})

DECISION_FIELDS = frozenset(
    {
        "decision_id",
        "decision_revision",
        "case",
        "entity",
        "group",
        "decision_time",
        "prediction",
        "features_available_at",
    }
)
CORRECTION_FIELDS = frozenset(
    {
        "correction_id",
        "decision_id",
        "decision_revision",
        "case",
        "correction_version",
        "corrected_value",
        "status",
        "provenance",
        "verifier",
        "correction_time",
    }
)
OPTIONAL_FIELDS = frozenset({"row_state"})
ROW_STATES = frozenset({"admitted", "disputed"})
STATUSES = frozenset({"final", "reopened", "withdrawn"})
VERIFYING_PROVENANCE = frozenset({"independent_adjudication"})
# A model answer, ticket close, thumbs-up or repeated reply never verifies a correction.
NON_VERIFYING_PROVENANCE = frozenset(
    {"model_answer", "ticket_close", "thumbs_up", "repeated_reply"}
)
USES = frozenset({"inspect", "evaluate", "train"})
COUNTERS = ("eligible", "pending", "quarantined", "disputed")

_KEYS = frozenset(
    {
        "schema",
        "tenant",
        "workflow_ref",
        "settings_revision",
        "policy_revision",
        "evaluation_policy_revision",
        "benchmark_ref",
        "benchmark_version",
        "engagement_epoch",
        "grant_epoch",
        "sources",
        "mapping",
        "feature_allowlist",
        "adjudicators",
        "abstain_values",
        "freshness_seconds",
        "maturity_seconds",
        "split",
        "uses",
        "receipt_counters",
    }
)
_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:\-]{0,127}")
_COLUMN = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")
_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_MAX_WINDOW = 366 * 24 * 3600


class ProfileError(Exception):
    """Fail-closed refusal with a stable, value-free reason code."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def need(condition: object, reason: str) -> None:
    if not condition:
        raise ProfileError(reason)


def ref(value: object, reason: str) -> str:
    need(type(value) is str and _REF.fullmatch(value), reason)
    assert type(value) is str
    return value


def count(value: object, reason: str, low: int = 0, high: int = 2**53 - 1) -> int:
    need(type(value) is int and low <= value <= high, reason)
    assert type(value) is int
    return value


def parse_time(value: object, reason: str) -> datetime:
    """A whole-second UTC instant in one fixed form, so text order equals time order."""
    need(type(value) is str and _TIME.fullmatch(value), reason)
    try:
        return datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise ProfileError(reason) from exc


def format_time(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ref_list(value: object, reason: str, *, empty: bool = False, limit: int = 256) -> list[str]:
    need(type(value) is list and (empty or value) and len(value) <= limit, reason)
    assert type(value) is list
    items = [ref(item, reason) for item in value]
    need(len(set(items)) == len(items), reason)
    return sorted(items)


@dataclass(frozen=True)
class Source:
    source_ref: str
    role: str
    origin: str


@dataclass(frozen=True)
class Principal:
    """An actor already resolved by the worker's authenticated connection."""

    actor_id: str
    role: str


@dataclass(frozen=True)
class PilotSettings:
    tenant: str
    workflow_ref: str
    settings_revision: int
    policy_revision: str
    evaluation_policy_revision: str
    benchmark_ref: str
    benchmark_version: int
    engagement_epoch: int
    grant_epoch: int
    sources: tuple[Source, ...]
    decision_mapping: Mapping[str, str]
    correction_mapping: Mapping[str, str]
    feature_columns: Mapping[str, str]
    feature_allowlist: tuple[str, ...]
    adjudicators: frozenset[str]
    abstain_values: frozenset[str]
    freshness_seconds: int
    maturity_seconds: int
    split: Mapping[str, Any]
    uses: frozenset[str]
    receipt_counters: tuple[str, ...]
    body: Mapping[str, Any]

    @property
    def digest(self) -> str:
        return digest(self.body)

    def source(self, source_ref: str) -> Source | None:
        return next((s for s in self.sources if s.source_ref == source_ref), None)


def _mapping(raw: object, required: frozenset[str], reason: str) -> dict[str, str]:
    need(isinstance(raw, Mapping), reason)
    assert isinstance(raw, Mapping)
    keys = set(raw) - {"features"}
    # A mapping names logical join fields only. An unknown logical field (for example a
    # caller-shaped "verified" or "authority" column) is refused, never silently dropped.
    need(required <= keys <= required | OPTIONAL_FIELDS, reason)
    columns = {key: raw[key] for key in keys}
    need(all(type(c) is str and _COLUMN.fullmatch(c) for c in columns.values()), reason)
    need(len(set(columns.values())) == len(columns), reason)
    return dict(sorted(columns.items()))


def parse_settings(raw: Mapping[str, Any]) -> PilotSettings:
    """Validate a settings batch; every unknown key, role, origin or value fails closed."""
    need(isinstance(raw, Mapping) and raw.get("schema") == SETTINGS_SCHEMA, "settings_schema")
    need(set(raw) == _KEYS, "settings_unknown_or_missing_key")
    tenant = ref(raw["tenant"], "settings_tenant")
    workflow = ref(raw["workflow_ref"], "settings_workflow")
    revision = count(raw["settings_revision"], "settings_revision", 1)
    policy = ref(raw["policy_revision"], "settings_policy_revision")
    evaluation = ref(raw["evaluation_policy_revision"], "settings_evaluation_policy_revision")
    benchmark = ref(raw["benchmark_ref"], "settings_benchmark")
    version = count(raw["benchmark_version"], "settings_benchmark", 1)
    engagement = count(raw["engagement_epoch"], "settings_engagement_epoch")
    epoch = count(raw["grant_epoch"], "settings_grant_epoch")

    sources_raw = raw["sources"]
    need(type(sources_raw) is list and 2 <= len(sources_raw) <= 16, "settings_sources")
    sources: list[Source] = []
    for item in sources_raw:
        need(
            isinstance(item, Mapping) and set(item) == {"source_ref", "role", "origin"},
            "settings_sources",
        )
        role = item["role"]
        need(type(role) is str and role in ORIGINS, "settings_source_role")
        need(type(item["origin"]) is str and item["origin"] in ORIGINS[role], "settings_origin")
        sources.append(Source(ref(item["source_ref"], "settings_sources"), role, item["origin"]))
    names = [s.source_ref for s in sources]
    need(len(set(names)) == len(names), "settings_sources")
    # A correction must come from a source other than the one that made the decision.
    need(
        any(s.role == DECISION for s in sources) and any(s.role == CORRECTION for s in sources),
        "independent_correction_source_required",
    )

    mapping = raw["mapping"]
    need(isinstance(mapping, Mapping) and set(mapping) == {DECISION, CORRECTION}, "mapping")
    decision_map = _mapping(mapping[DECISION], DECISION_FIELDS, "mapping_decision")
    correction_map = _mapping(mapping[CORRECTION], CORRECTION_FIELDS, "mapping_correction")
    features = mapping[DECISION].get("features")
    need(isinstance(features, Mapping) and 0 < len(features) <= 64, "mapping_features")
    assert isinstance(features, Mapping)
    feature_columns = {ref(k, "mapping_features"): v for k, v in features.items()}
    need(
        all(type(c) is str and _COLUMN.fullmatch(c) for c in feature_columns.values())
        and not set(feature_columns.values()) & set(decision_map.values()),
        "mapping_features",
    )
    allowlist = _ref_list(raw["feature_allowlist"], "settings_feature_allowlist", limit=64)
    # Only permitted decision-time features can be mapped, so nothing else is ever consumed.
    need(set(feature_columns) <= set(allowlist), "feature_outside_allowlist")

    adjudicators_raw = raw["adjudicators"]
    need(type(adjudicators_raw) is list and adjudicators_raw, "settings_adjudicators")
    adjudicators: set[str] = set()
    for item in adjudicators_raw:
        need(isinstance(item, Mapping) and set(item) == {"actor_id", "role"}, "adjudicator")
        need(type(item["role"]) is str and item["role"] in ADJUDICATOR_ROLES, "adjudicator_role")
        actor = ref(item["actor_id"], "adjudicator")
        need(actor not in adjudicators, "adjudicator")
        adjudicators.add(actor)

    abstain = raw["abstain_values"]
    need(type(abstain) is list and len(abstain) <= 16, "settings_abstain_values")
    need(all(type(v) is str and 0 < len(v) <= 64 for v in abstain), "settings_abstain_values")
    freshness = count(raw["freshness_seconds"], "settings_freshness", 1, _MAX_WINDOW)
    maturity = count(raw["maturity_seconds"], "settings_maturity", 0, _MAX_WINDOW)

    split = raw["split"]
    need(isinstance(split, Mapping), "settings_split")
    if split.get("mode") == "chronological":
        need(set(split) == {"mode", "cutoff"}, "settings_split")
        parse_time(split["cutoff"], "settings_split")
        split_out: dict[str, Any] = {"mode": "chronological", "cutoff": split["cutoff"]}
    elif split.get("mode") == "heldout_customer":
        need(set(split) == {"mode", "customers"}, "settings_split")
        split_out = {
            "mode": "heldout_customer",
            "customers": _ref_list(split["customers"], "settings_split"),
        }
    else:
        raise ProfileError("settings_split")

    uses_raw = raw["uses"]
    need(type(uses_raw) is list and all(type(u) is str for u in uses_raw), "settings_uses")
    uses = set(uses_raw)
    need(len(uses) == len(uses_raw) and "inspect" in uses and uses <= USES, "settings_uses")
    counters_raw = raw["receipt_counters"]
    need(type(counters_raw) is list and all(type(c) is str for c in counters_raw), "counters")
    need(
        len(set(counters_raw)) == len(counters_raw) and set(counters_raw) <= set(COUNTERS),
        "settings_receipt_counters",
    )
    receipt_counters = tuple(c for c in COUNTERS if c in counters_raw)

    body: dict[str, Any] = {
        "schema": SETTINGS_SCHEMA,
        "tenant": tenant,
        "workflow_ref": workflow,
        "settings_revision": revision,
        "policy_revision": policy,
        "evaluation_policy_revision": evaluation,
        "benchmark_ref": benchmark,
        "benchmark_version": version,
        "engagement_epoch": engagement,
        "grant_epoch": epoch,
        "sources": sorted(
            ({"source_ref": s.source_ref, "role": s.role, "origin": s.origin} for s in sources),
            key=lambda s: s["source_ref"],
        ),
        "mapping": {
            DECISION: {**decision_map, "features": dict(sorted(feature_columns.items()))},
            CORRECTION: correction_map,
        },
        "feature_allowlist": allowlist,
        "adjudicators": [{"actor_id": a, "role": "verifier"} for a in sorted(adjudicators)],
        "abstain_values": sorted(set(abstain)),
        "freshness_seconds": freshness,
        "maturity_seconds": maturity,
        "split": split_out,
        "uses": sorted(uses),
        "receipt_counters": list(receipt_counters),
    }
    return PilotSettings(
        tenant=tenant,
        workflow_ref=workflow,
        settings_revision=revision,
        policy_revision=policy,
        evaluation_policy_revision=evaluation,
        benchmark_ref=benchmark,
        benchmark_version=version,
        engagement_epoch=engagement,
        grant_epoch=epoch,
        sources=tuple(sorted(sources, key=lambda s: s.source_ref)),
        decision_mapping=decision_map,
        correction_mapping=correction_map,
        feature_columns=dict(sorted(feature_columns.items())),
        feature_allowlist=tuple(allowlist),
        adjudicators=frozenset(adjudicators),
        abstain_values=frozenset(abstain),
        freshness_seconds=freshness,
        maturity_seconds=maturity,
        split=split_out,
        uses=frozenset(uses),
        receipt_counters=receipt_counters,
        body=body,
    )


# --------------------------------------------------------------------------- storage


def _pin(body: str) -> str:
    return digest(["opentour.pilot.file-pin.v1", body])


def read_text(path: Path, reason: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ProfileError(reason) from exc


def create_once(folder: Path, name: str, body: str, conflict: str) -> bool:
    """Atomically create ``name`` unless it exists; an existing file must be identical.

    Returns True when this call created the file. The bytes are fsynced before the link, so a
    crash leaves either no file or the whole file.
    """
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / name
    handle, temporary = tempfile.mkstemp(dir=folder, prefix=".tmp-")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, target)
            return True
        except FileExistsError:
            need(read_text(target, conflict) == body, conflict)
            return False
    finally:
        os.unlink(temporary)


def write_pinned(folder: Path, name: str, value: object, conflict: str) -> bool:
    """Write-once JSON with a sidecar pin written first (an orphan pin is harmless)."""
    body = canonical(value)
    create_once(folder, f"{name}.pin", _pin(body), conflict)
    return create_once(folder, f"{name}.json", body, conflict)


def read_pinned(folder: Path, name: str, reason: str) -> Any:
    body = read_text(folder / f"{name}.json", reason)
    need(read_text(folder / f"{name}.pin", reason) == _pin(body), reason)
    try:
        value = json.loads(body)
    except ValueError as exc:
        raise ProfileError(reason) from exc
    need(canonical(value) == body, reason)
    return value


class SettingsBatchStore:
    """Write-once, consecutive settings revisions per tenant and workflow."""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)

    def _folder(self, tenant: str, workflow_ref: str) -> Path:
        return (
            self.directory
            / ref(tenant, "settings_tenant")
            / ref(workflow_ref, "settings_workflow")
            / "settings"
        )

    def commit(self, raw: Mapping[str, Any], *, principal: Principal) -> PilotSettings:
        need(
            type(principal.role) is str and principal.role in PRINCIPAL_ROLES,
            "unknown_principal_role",
        )
        need(principal.role == CUSTODIAN, "custodian_required")
        ref(principal.actor_id, "unknown_principal_role")
        settings = parse_settings(raw)
        folder = self._folder(settings.tenant, settings.workflow_ref)
        if settings.settings_revision > 1:
            previous = self.revision(
                settings.tenant, settings.workflow_ref, settings.settings_revision - 1
            )
            need(previous is not None, "settings_revision_gap")
        write_pinned(
            folder, f"rev-{settings.settings_revision:08d}", settings.body, "settings_conflict"
        )
        return settings

    def revision(self, tenant: str, workflow_ref: str, revision: int) -> PilotSettings | None:
        folder = self._folder(tenant, workflow_ref)
        name = f"rev-{count(revision, 'settings_revision', 1):08d}"
        if not (folder / f"{name}.json").exists():
            return None
        settings = parse_settings(read_pinned(folder, name, "settings_tampered"))
        need(
            settings.tenant == tenant
            and settings.workflow_ref == workflow_ref
            and settings.settings_revision == revision,
            "settings_tampered",
        )
        return settings

    def current(self, tenant: str, workflow_ref: str) -> PilotSettings | None:
        folder = self._folder(tenant, workflow_ref)
        if not folder.is_dir():
            return None
        revisions = sorted(
            int(p.stem[4:]) for p in folder.glob("rev-*.json") if re.fullmatch(r"\d{8}", p.stem[4:])
        )
        if not revisions:
            return None
        need(revisions == list(range(1, len(revisions) + 1)), "settings_revision_gap")
        return self.revision(tenant, workflow_ref, revisions[-1])


# --------------------------------------------------------------------------- source pair


@dataclass(frozen=True)
class SourceCapture:
    """One bound source as read by the worker's own reader.

    ``tenant``, ``source_revision`` and ``captured_at`` come from the reader's binding, not from
    any row. Rows are plain mappings of source columns and are treated as data only.
    """

    tenant: str
    source_ref: str
    source_revision: int
    captured_at: str
    rows: tuple[Mapping[str, Any], ...]

    def fingerprint(self) -> str:
        return digest(
            [
                self.tenant,
                self.source_ref,
                self.source_revision,
                self.captured_at,
                [dict(row) for row in self.rows],
            ]
        )


@dataclass(frozen=True)
class SourcePair:
    captures: tuple[SourceCapture, ...]

    def by_ref(self) -> dict[str, SourceCapture]:
        return {c.source_ref: c for c in self.captures}


def map_row(row: Mapping[str, Any], columns: Mapping[str, str]) -> dict[str, Any]:
    """Project a source row onto logical fields. A missing mapped column is absent (None)."""
    need(isinstance(row, Mapping), "malformed_row")
    return {logical: row.get(column) for logical, column in columns.items()}
