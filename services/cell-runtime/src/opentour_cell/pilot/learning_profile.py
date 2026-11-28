"""Versioned per-pilot learning settings and the source-pair field mapping (OPE-416).

A settings batch is the worker-held record that pins, for one tenant and workflow:

* the intake-created workflow benchmark and the policy and evaluation-policy revisions;
* the bound sources, each with a role (decision or correction) and an evidence origin;
* the source-pair mapping from source columns to the logical join fields;
* the permitted decision-time feature allowlist and who may adjudicate a correction;
* the source freshness and correction maturity windows;
* the development/protected split handed to the existing partitioner;
* the permitted uses and the counters the snapshot receipt may disclose.

Revisions are write-once and consecutive. Re-committing identical settings is a no-op;
different settings under a committed revision conflict.

Who may commit. The principal's ``data_custodian`` role is asserted by the resolved connection,
bound on first use; it is not authenticated until OPE-566. Concretely: the first commit for a
tenant and workflow records that actor as the only bound custodian (trust on first use). After
that, only a bound custodian actor ID may commit, and adding or removing a custodian binding
takes a commit by a currently bound custodian; the last custodian cannot be removed. Every
revision records the committing actor ID. A connection that asserts the role under a bound
actor ID is still believed: nothing here authenticates it. This module never accepts an
authority value from the rows it maps.

Store identity. On first use the store writes a random store ID inside itself. ``identity``
digests that ID with the tenant, workflow, real paths and device/inode numbers of the store
folder and its identity files, so a copied or recreated store has a different identity. The
snapshot producer and the dependency recorder pin that identity on first use.

Evidence tier: every record here is ``digest_self_consistent_only``. The pin beside each file
detects an edit of one file; anyone who can rewrite both files can forge a batch. No signing or
key-custody contract exists (OPE-566).

Simulated and NOT proven: generated sentinel fixtures in a local directory only. Nothing here
is provider, installed-witness or customer evidence.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import secrets
import stat
import tempfile
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from opentour_cell.pilot.operating_point import canonical, digest

SETTINGS_SCHEMA = "opentour.pilot.learning-profile-settings.v1"
REVISION_SCHEMA = "opentour.pilot.learning-profile-revision.v1"
BINDING_SCHEMA = "opentour.pilot.custodian-binding.v1"
IDENTITY_SCHEMA = "opentour.pilot.settings-store-identity.v1"
EVIDENCE_TIER = "digest_self_consistent_only"
# Label digests are HMACs under a key held in the worker's private directory: they resist
# enumeration of a small label vocabulary, but nobody else can verify them. Not a signature.
LABEL_DIGEST_TIER = "locally_keyed"
CUSTODIAN_BASIS = "role_asserted_by_resolved_connection_bound_on_first_use"

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
# ``row_state`` is required on both sides: without it a disputed source row would be admitted.
ROW_STATE = "row_state"
DEFAULT_ROW_STATE_VALUES = {"admitted": ["admitted"], "disputed": ["disputed"]}
STATUSES = frozenset({"final", "reopened", "withdrawn"})
VERIFYING_PROVENANCE = frozenset({"independent_adjudication"})
# A model answer, ticket close, thumbs-up or repeated reply never verifies a correction.
NON_VERIFYING_PROVENANCE = frozenset(
    {"model_answer", "ticket_close", "thumbs_up", "repeated_reply"}
)
USES = frozenset({"inspect", "evaluate", "train"})
COUNTERS = ("eligible", "pending", "quarantined", "disputed")
# Small-cell threshold for anything that leaves the worker. No repository-wide constant exists,
# so the settings carry ``min_reportable_cell``: default 5, never below 2.
DEFAULT_MIN_REPORTABLE_CELL = 5
MIN_REPORTABLE_CELL_FLOOR = 2

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
_OPTIONAL_KEYS = frozenset({"min_reportable_cell", "row_state_values"})
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
    """An actor resolved by the worker's connection.

    The role is asserted by the resolved connection, bound on first use; it is not
    authenticated until OPE-566.
    """

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
    min_reportable_cell: int
    admitted_states: frozenset[str]
    disputed_states: frozenset[str]
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
    need(keys == required | {ROW_STATE}, reason)
    columns = {key: raw[key] for key in keys}
    need(all(type(c) is str and _COLUMN.fullmatch(c) for c in columns.values()), reason)
    need(len(set(columns.values())) == len(columns), reason)
    return dict(sorted(columns.items()))


def parse_settings(raw: Mapping[str, Any]) -> PilotSettings:
    """Validate a settings batch; every unknown key, role, origin or value fails closed."""
    need(isinstance(raw, Mapping) and raw.get("schema") == SETTINGS_SCHEMA, "settings_schema")
    need(_KEYS <= set(raw) <= _KEYS | _OPTIONAL_KEYS, "settings_unknown_or_missing_key")
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
    min_cell = count(
        raw.get("min_reportable_cell", DEFAULT_MIN_REPORTABLE_CELL),
        "settings_min_reportable_cell",
        MIN_REPORTABLE_CELL_FLOOR,
        1_000_000,
    )
    admitted, disputed = _row_state_values(raw.get("row_state_values", DEFAULT_ROW_STATE_VALUES))

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
        "min_reportable_cell": min_cell,
        "row_state_values": {"admitted": sorted(admitted), "disputed": sorted(disputed)},
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
        min_reportable_cell=min_cell,
        admitted_states=admitted,
        disputed_states=disputed,
        body=body,
    )


def _row_state_values(raw: object) -> tuple[frozenset[str], frozenset[str]]:
    """The mapped ``row_state`` column's closed value sets: admitted and disputed are disjoint.

    A value in neither set is quarantined as ``unknown_row_state``, so a column whose values
    are not declared here (for example a status column) admits nothing.
    """
    reason = "settings_row_state_values"
    need(isinstance(raw, Mapping) and set(raw) == {"admitted", "disputed"}, reason)
    assert isinstance(raw, Mapping)
    sets = []
    for side in ("admitted", "disputed"):
        values = raw[side]
        need(type(values) is list and 0 < len(values) <= 16, reason)
        assert type(values) is list
        need(all(type(v) is str and _REF.fullmatch(v) for v in values), reason)
        need(len(set(values)) == len(values), reason)
        sets.append(frozenset(values))
    need(not sets[0] & sets[1], reason)
    return sets[0], sets[1]


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


@contextlib.contextmanager
def locked(folder: Path) -> Iterator[None]:
    """An exclusive advisory lock on ``folder/.lock`` for one check-and-write sequence."""
    folder.mkdir(parents=True, exist_ok=True)
    fd = os.open(folder / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _private_folder(folder: Path) -> None:
    info = folder.lstat()
    need(
        stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & 0o077,
        "private_key_folder_required",
    )


def local_key(folder: Path, *, create: bool, missing: str) -> bytes:
    """A 32-byte key in ``folder/label.key`` (0600, owner-checked, in a 0700 folder).

    Created on first use only when ``create``; otherwise a missing key is refused with
    ``missing`` rather than silently re-keyed.
    """
    path = folder / "label.key"
    if not path.exists() and create:
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        _private_folder(folder)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "wb") as stream:
                stream.write(secrets.token_bytes(32))
                stream.flush()
                os.fsync(stream.fileno())
    need(path.exists(), missing)
    _private_folder(folder)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise ProfileError("private_key_required") from exc
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        need(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == os.geteuid()
            and info.st_nlink == 1
            and not info.st_mode & 0o077
            and info.st_size == 32,
            "private_key_required",
        )
        key = stream.read(33)
    need(len(key) == 32, "private_key_required")
    return key


def pin_identity(folder: Path, identity: str, *, create: bool) -> None:
    """Pin a settings-store identity into ``folder`` on first use; later it must match.

    A missing pin is created only when ``create`` (nothing has been committed against any store
    yet); otherwise it is itself a mismatch.
    """
    name = "settings-store"
    if not (folder / f"{name}.json").exists():
        need(create, "settings_store_mismatch")
        write_pinned(
            folder,
            name,
            {"schema": IDENTITY_SCHEMA, "identity": identity},
            "settings_store_mismatch",
        )
    pinned = read_pinned(folder, name, "settings_store_mismatch")
    need(
        isinstance(pinned, dict) and pinned == {"schema": IDENTITY_SCHEMA, "identity": identity},
        "settings_store_mismatch",
    )


def _numbered(folder: Path, prefix: str) -> list[int]:
    found = (
        sorted(
            int(p.stem[len(prefix) :])
            for p in folder.glob(f"{prefix}*.json")
            if re.fullmatch(r"\d{8}", p.stem[len(prefix) :])
        )
        if folder.is_dir()
        else []
    )
    return found


class SettingsBatchStore:
    """Write-once, consecutive settings revisions and custodian bindings per tenant/workflow."""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)

    def _base(self, tenant: str, workflow_ref: str) -> Path:
        return (
            self.directory / ref(tenant, "settings_tenant") / ref(workflow_ref, "settings_workflow")
        )

    def _folder(self, tenant: str, workflow_ref: str) -> Path:
        return self._base(tenant, workflow_ref) / "settings"

    def lock(self, tenant: str, workflow_ref: str) -> contextlib.AbstractContextManager[None]:
        """Held by every commit; the snapshot producer holds it for its final check and write."""
        return locked(self._base(tenant, workflow_ref))

    # ------------------------------------------------------------------ identity

    def identity(self, tenant: str, workflow_ref: str) -> str | None:
        """This store's identity for one tenant and workflow, or None before its first commit."""
        base = self._base(tenant, workflow_ref)
        if not (base / "identity.json").exists():
            return None
        body = read_pinned(base, "identity", "settings_store_tampered")
        need(
            isinstance(body, dict)
            and set(body) == {"schema", "store_id", "tenant", "workflow_ref"}
            and body["schema"] == IDENTITY_SCHEMA
            and body["tenant"] == tenant
            and body["workflow_ref"] == workflow_ref
            and type(body["store_id"]) is str
            and re.fullmatch(r"[0-9a-f]{32}", body["store_id"]),
            "settings_store_tampered",
        )
        parts: list[object] = [IDENTITY_SCHEMA, body["store_id"], tenant, workflow_ref]
        try:
            for item in (base, base / "identity.json", base / "identity.pin"):
                info = os.stat(item, follow_symlinks=False)
                parts += [os.path.realpath(item), info.st_dev, info.st_ino]
        except OSError as exc:
            raise ProfileError("settings_store_tampered") from exc
        return digest(parts)

    def _ensure_identity(self, tenant: str, workflow_ref: str) -> None:
        base = self._base(tenant, workflow_ref)
        if not (base / "identity.json").exists():
            write_pinned(
                base,
                "identity",
                {
                    "schema": IDENTITY_SCHEMA,
                    "store_id": secrets.token_hex(16),
                    "tenant": tenant,
                    "workflow_ref": workflow_ref,
                },
                "settings_store_tampered",
            )

    # ------------------------------------------------------------------ custodian bindings

    def custodians(self, tenant: str, workflow_ref: str) -> tuple[int, frozenset[str]]:
        """(binding revision, bound custodian actor IDs); (0, empty) before the first commit.

        The chain is re-verified on every read: each change is made by a custodian bound in
        the previous revision, and the set never becomes empty.
        """
        folder = self._base(tenant, workflow_ref) / "custodians"
        numbers = _numbered(folder, "bind-")
        need(numbers == list(range(1, len(numbers) + 1)), "custodian_binding_tampered")
        bound: frozenset[str] = frozenset()
        for number in numbers:
            body = read_pinned(folder, f"bind-{number:08d}", "custodian_binding_tampered")
            need(
                isinstance(body, dict)
                and body.get("schema") == BINDING_SCHEMA
                and body.get("binding_revision") == number
                and type(body.get("custodians")) is list
                and isinstance(body.get("change"), dict),
                "custodian_binding_tampered",
            )
            change, by = body["change"], body.get("by")
            target = change.get("actor_id")
            if number == 1:
                expected = {target}
                ok = change.get("kind") == "first_use" and by == target
            elif change.get("kind") == "bind":
                expected, ok = set(bound) | {target}, by in bound and target not in bound
            elif change.get("kind") == "unbind":
                expected, ok = set(bound) - {target}, by in bound and target in bound
            else:
                expected, ok = set(), False
            need(
                ok and expected and body["custodians"] == sorted(expected),
                "custodian_binding_tampered",
            )
            bound = frozenset(expected)
        return len(numbers), bound

    def _bind(
        self, tenant: str, workflow_ref: str, kind: str, target: str, by: str
    ) -> tuple[int, frozenset[str]]:
        number, bound = self.custodians(tenant, workflow_ref)
        members = (
            {target}
            if kind == "first_use"
            else (set(bound) | {target} if kind == "bind" else set(bound) - {target})
        )
        write_pinned(
            self._base(tenant, workflow_ref) / "custodians",
            f"bind-{number + 1:08d}",
            {
                "schema": BINDING_SCHEMA,
                "binding_revision": number + 1,
                "custodians": sorted(members),
                "change": {"kind": kind, "actor_id": target},
                "by": by,
                "basis": CUSTODIAN_BASIS,
            },
            "custodian_binding_conflict",
        )
        return self.custodians(tenant, workflow_ref)

    def _bound_actor(self, principal: Principal) -> str:
        # Exactly ``str``: a subclass could claim the custodian role through ``__eq__``.
        need(
            type(principal.role) is str and principal.role in PRINCIPAL_ROLES,
            "unknown_principal_role",
        )
        need(principal.role == CUSTODIAN, "custodian_required")
        return ref(principal.actor_id, "unknown_principal_role")

    def bind_custodian(
        self, tenant: str, workflow_ref: str, actor_id: str, *, principal: Principal
    ) -> frozenset[str]:
        """Bind another custodian; only a currently bound custodian may do so."""
        by = self._bound_actor(principal)
        target = ref(actor_id, "custodian_actor")
        with self.lock(tenant, workflow_ref):
            number, bound = self.custodians(tenant, workflow_ref)
            need(number > 0 and by in bound, "custodian_not_bound")
            if target in bound:
                return bound
            return self._bind(tenant, workflow_ref, "bind", target, by)[1]

    def unbind_custodian(
        self, tenant: str, workflow_ref: str, actor_id: str, *, principal: Principal
    ) -> frozenset[str]:
        """Remove a custodian binding; the last custodian cannot be removed."""
        by = self._bound_actor(principal)
        target = ref(actor_id, "custodian_actor")
        with self.lock(tenant, workflow_ref):
            number, bound = self.custodians(tenant, workflow_ref)
            need(number > 0 and by in bound, "custodian_not_bound")
            need(target in bound, "custodian_actor")
            need(len(bound) > 1, "last_custodian")
            return self._bind(tenant, workflow_ref, "unbind", target, by)[1]

    # ------------------------------------------------------------------ revisions

    def commit(self, raw: Mapping[str, Any], *, principal: Principal) -> PilotSettings:
        actor = self._bound_actor(principal)
        settings = parse_settings(raw)
        tenant, workflow_ref = settings.tenant, settings.workflow_ref
        folder = self._folder(tenant, workflow_ref)
        name = f"rev-{settings.settings_revision:08d}"
        with self.lock(tenant, workflow_ref):
            number, bound = self.custodians(tenant, workflow_ref)
            if number == 0:
                # Trust on first use, and only for a store that has never been committed to.
                need(not _numbered(folder, "rev-"), "custodian_binding_missing")
                self._ensure_identity(tenant, workflow_ref)
                number, bound = self._bind(tenant, workflow_ref, "first_use", actor, actor)
            need(actor in bound, "custodian_not_bound")
            if (folder / f"{name}.json").exists():
                existing = self.revision(tenant, workflow_ref, settings.settings_revision)
                assert existing is not None
                need(existing.body == settings.body, "settings_conflict")
                return existing
            if settings.settings_revision > 1:
                previous = self.revision(tenant, workflow_ref, settings.settings_revision - 1)
                need(previous is not None, "settings_revision_gap")
            write_pinned(
                folder,
                name,
                {
                    "schema": REVISION_SCHEMA,
                    "settings": settings.body,
                    "committed_by": {
                        "actor_id": actor,
                        "role": CUSTODIAN,
                        "basis": CUSTODIAN_BASIS,
                        "binding_revision": number,
                    },
                },
                "settings_conflict",
            )
        return settings

    def _revision_record(self, tenant: str, workflow_ref: str, revision: int) -> Any:
        folder = self._folder(tenant, workflow_ref)
        name = f"rev-{count(revision, 'settings_revision', 1):08d}"
        if not (folder / f"{name}.json").exists():
            return None
        record = read_pinned(folder, name, "settings_tampered")
        need(
            isinstance(record, dict)
            and set(record) == {"schema", "settings", "committed_by"}
            and record["schema"] == REVISION_SCHEMA
            and isinstance(record["committed_by"], dict)
            and type(record["committed_by"].get("actor_id")) is str,
            "settings_tampered",
        )
        return record

    def revision(self, tenant: str, workflow_ref: str, revision: int) -> PilotSettings | None:
        record = self._revision_record(tenant, workflow_ref, revision)
        if record is None:
            return None
        settings = parse_settings(record["settings"])
        need(
            settings.tenant == tenant
            and settings.workflow_ref == workflow_ref
            and settings.settings_revision == revision
            and settings.body == record["settings"],
            "settings_tampered",
        )
        return settings

    def committed_by(self, tenant: str, workflow_ref: str, revision: int) -> dict[str, Any]:
        """The audit entry of one revision: the committing actor ID and binding revision."""
        record = self._revision_record(tenant, workflow_ref, revision)
        need(record is not None, "settings_missing")
        return dict(record["committed_by"])

    def current(self, tenant: str, workflow_ref: str) -> PilotSettings | None:
        folder = self._folder(tenant, workflow_ref)
        revisions = _numbered(folder, "rev-")
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

    def fingerprint(self, columns: Iterable[str]) -> str:
        """Digest of the binding and of the mapped columns only.

        An unmapped column (for example a protected one that is present in the source) never
        reaches the digest, so changing it cannot be detected through a snapshot or receipt.
        """
        wanted = sorted(set(columns))
        return digest(
            [
                self.tenant,
                self.source_ref,
                self.source_revision,
                self.captured_at,
                [
                    {c: row[c] for c in wanted if c in row} if isinstance(row, Mapping) else None
                    for row in self.rows
                ],
            ]
        )


@dataclass(frozen=True)
class SourcePair:
    captures: tuple[SourceCapture, ...]

    def by_ref(self) -> dict[str, SourceCapture]:
        return {c.source_ref: c for c in self.captures}


def mapped_columns(settings: PilotSettings, role: str) -> tuple[str, ...]:
    """Every source column the settings map for one source role (allowlisted columns only)."""
    if role == DECISION:
        return tuple(
            sorted({*settings.decision_mapping.values(), *settings.feature_columns.values()})
        )
    return tuple(sorted(settings.correction_mapping.values()))


def map_row(row: Mapping[str, Any], columns: Mapping[str, str]) -> dict[str, Any]:
    """Project a source row onto logical fields. A missing mapped column is absent (None)."""
    need(isinstance(row, Mapping), "malformed_row")
    return {logical: row.get(column) for logical, column in columns.items()}
