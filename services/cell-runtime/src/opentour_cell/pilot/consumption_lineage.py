"""Resolve a workflow claim to its recorded consumed fields and derive impact sets (OPE-544).

This is a reader over immutable, customer-local consumed-field manifests written at
decision time (OPE-416). It never recreates provenance from current documents and never
guesses an edge: a claim or change that cannot be linked through recorded edges returns
``unknown`` / ``hold``. All joins happen locally; nothing here performs I/O beyond the
manifest directory.

The producer record shape (``opentour.pilot.consumed-field-manifest.v1``) is a reader-side
proposal until OPE-416 publishes its writer; see ``validate_manifest`` for the exact fields.

Status vocabulary (this module's reading of a spec that permits "unknown or hold"; a contract
gap for the coordinator, not a settled product decision): ``unknown`` means no recorded edge
links the claim or consumer; ``hold`` means an edge exists but is unusable (an unrecorded
dependency, or two manifests claiming the same claim). Neither is ever completed by a guess.

Simulated and NOT proven: everything here is a reader over generated sentinel manifests in a
local directory with an in-test grant lookup. It proves software behavior only. It establishes
no provider, installed-witness, staged-packet (OPE-529) or customer evidence, and it does not
show that OPE-416's real writer produces this shape.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from opentour_cell.pilot.operating_point import digest

MANIFEST_SCHEMA = "opentour.pilot.consumed-field-manifest.v1"
RESOLUTION_SCHEMA = "opentour.pilot.claim-resolution.v1"
IMPACT_SCHEMA = "opentour.pilot.impact-set.v1"
RECEIPT_SCHEMA = "opentour.pilot.lineage-receipt.v1"

OWNER = "workflow_owner"
REVIEWER = "reviewer"
ROLES = frozenset({OWNER, REVIEWER})
PARTITIONS = frozenset({"development", "protected"})
RULE_KINDS = frozenset({"simple_rule", "local_adapter"})
ARTIFACT_KINDS = frozenset({"dataset", "model", "proposal", "comparator", "frozen_result"})

_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:\-]{0,127}$")
_PATH = re.compile(r"^[A-Za-z0-9_.\-]+(/[A-Za-z0-9_.\-]+)*$")


class LineageError(Exception):
    """Write-side failure with a stable machine reason."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _need(condition: object, reason: str) -> None:
    if not condition:
        raise LineageError(reason)


def _time(value: object, reason: str) -> datetime:
    _need(isinstance(value, str) and value.endswith("Z"), reason)
    try:
        parsed = datetime.fromisoformat(str(value)[:-1] + "+00:00")
    except ValueError as exc:
        raise LineageError(reason) from exc
    return parsed.astimezone(UTC)


def _ref(value: object, reason: str) -> str:
    _need(isinstance(value, str) and _REF.fullmatch(value), reason)
    return str(value)


def _segments(path: str) -> tuple[str, ...]:
    return tuple(path.split("/"))


def paths_overlap(left: str, right: str) -> bool:
    """A change to a path touches a consumed path when either is a prefix of the other."""
    a, b = _segments(left), _segments(right)
    n = min(len(a), len(b))
    return a[:n] == b[:n]


# --------------------------------------------------------------------------- manifest


def validate_manifest(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Return a normalized copy of a manifest or raise ``LineageError``.

    Required: schema, manifest_ref, tenant, workflow_ref, engagement_epoch, grant_epoch,
    partition, rule_kind, case_ref, decision_time, result_ref, candidate_ref, example_ref,
    correction_version, policy_revision, claim_refs, consumed[], and optional
    unknown_dependencies[], descendants[], later_relationships[].
    """
    _need(isinstance(raw, Mapping) and raw.get("schema") == MANIFEST_SCHEMA, "manifest_schema")
    allowed = {
        "schema", "manifest_ref", "tenant", "workflow_ref", "engagement_epoch", "grant_epoch",
        "partition", "rule_kind", "case_ref", "decision_time", "result_ref", "candidate_ref",
        "example_ref", "correction_version", "policy_revision", "claim_refs", "consumed",
        "unknown_dependencies", "descendants", "later_relationships",
    }  # fmt: skip
    _need(set(raw) <= allowed, "manifest_unknown_key")
    out: dict[str, Any] = {"schema": MANIFEST_SCHEMA}
    for key in (
        "manifest_ref", "tenant", "workflow_ref", "case_ref", "result_ref", "candidate_ref",
        "example_ref", "policy_revision",
    ):  # fmt: skip
        out[key] = _ref(raw.get(key), f"manifest_{key}")
    for key in ("engagement_epoch", "grant_epoch", "correction_version"):
        value = raw.get(key)
        _need(
            isinstance(value, int) and not isinstance(value, bool) and value >= 0, f"manifest_{key}"
        )
        out[key] = value
    _need(raw.get("partition") in PARTITIONS, "manifest_partition")
    _need(raw.get("rule_kind") in RULE_KINDS, "manifest_rule_kind")
    out["partition"], out["rule_kind"] = raw["partition"], raw["rule_kind"]
    decision = _time(raw.get("decision_time"), "manifest_decision_time")
    out["decision_time"] = raw["decision_time"]
    claims: Any = raw.get("claim_refs")
    _need(isinstance(claims, list) and claims, "manifest_claim_refs")
    out["claim_refs"] = sorted({_ref(c, "manifest_claim_refs") for c in claims})
    consumed: Any = raw.get("consumed")
    _need(isinstance(consumed, list), "manifest_consumed")
    seen: set[tuple[str, str]] = set()
    entries: list[dict[str, Any]] = []
    for item in consumed:
        _need(isinstance(item, Mapping) and set(item) == {
            "source_ref", "path", "source_revision", "available_at", "span"
        }, "manifest_consumed_entry")  # fmt: skip
        source = _ref(item["source_ref"], "manifest_consumed_entry")
        path = item["path"]
        _need(isinstance(path, str) and _PATH.fullmatch(path), "manifest_consumed_path")
        revision = item["source_revision"]
        _need(
            isinstance(revision, int) and not isinstance(revision, bool) and revision >= 0,
            "manifest_consumed_revision",
        )
        # A decision may only have consumed what existed when it was made.
        _need(
            _time(item["available_at"], "manifest_consumed_time") <= decision,
            "input_after_decision",
        )
        span = item["span"]
        _need(
            span is None or (isinstance(span, str) and 0 < len(span) <= 64),
            "manifest_consumed_span",
        )
        _need((source, path) not in seen, "manifest_consumed_duplicate")
        seen.add((source, path))
        entries.append(dict(item))
    out["consumed"] = sorted(entries, key=lambda e: (e["source_ref"], e["path"]))
    unknown = raw.get("unknown_dependencies", [])
    _need(isinstance(unknown, list), "manifest_unknown_dependencies")
    out["unknown_dependencies"] = sorted(
        {"*" if u == "*" else _ref(u, "manifest_unknown_dependencies") for u in unknown}
    )
    known = {out["result_ref"], out["candidate_ref"], out["example_ref"]}
    descendants = raw.get("descendants", [])
    _need(isinstance(descendants, list), "manifest_descendants")
    nodes: list[dict[str, Any]] = []
    names = set(known)
    for node in descendants:
        _need(
            isinstance(node, Mapping) and set(node) == {"ref", "kind", "derived_from"},
            "manifest_descendant",
        )
        ref = _ref(node["ref"], "manifest_descendant")
        _need(node["kind"] in ARTIFACT_KINDS and ref not in names, "manifest_descendant")
        _need(isinstance(node["derived_from"], list), "manifest_descendant")
        parents = sorted({_ref(p, "manifest_descendant") for p in node["derived_from"]})
        names.add(ref)
        nodes.append({"ref": ref, "kind": node["kind"], "derived_from": parents})
    for node in nodes:
        _need(set(node["derived_from"]) <= names - {node["ref"]}, "manifest_descendant_parent")
    out["descendants"] = sorted(nodes, key=lambda n: n["ref"])
    later = raw.get("later_relationships", [])
    _need(isinstance(later, list), "manifest_later_relationships")
    rels = []
    for rel in later:
        _need(
            isinstance(rel, Mapping)
            and set(rel) == {"kind", "ref", "available_at", "relationship_version"},
            "manifest_later_relationship",
        )
        _ref(rel["ref"], "manifest_later_relationship")
        _time(rel["available_at"], "manifest_later_relationship")
        _need(
            isinstance(rel["kind"], str) and isinstance(rel["relationship_version"], int),
            "manifest_later_relationship",
        )
        rels.append(dict(rel))
    out["later_relationships"] = sorted(rels, key=lambda r: (r["ref"], r["relationship_version"]))
    return out


def manifest_digest(manifest: Mapping[str, Any]) -> str:
    return digest(manifest)


class ManifestStore:
    """Immutable write-once manifest directory; survives restart by re-reading files."""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def _file(self, ref: str) -> Path:
        return self.directory / f"manifest-{ref}.json"

    def put(self, raw: Mapping[str, Any]) -> str:
        """Write once. Identical replay is idempotent; different bytes for a ref conflict."""
        manifest = validate_manifest(raw)
        path = self._file(manifest["manifest_ref"])
        body = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
        if path.exists():
            _need(path.read_text() == body, "manifest_conflict")
            return manifest_digest(manifest)
        handle, temporary = tempfile.mkstemp(dir=self.directory, prefix=".tmp-")
        try:
            with os.fdopen(handle, "w") as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)  # atomic create-if-absent
            except FileExistsError:
                _need(path.read_text() == body, "manifest_conflict")
        finally:
            os.unlink(temporary)
        return manifest_digest(manifest)

    def all(self) -> list[dict[str, Any]]:
        found = []
        for path in sorted(self.directory.glob("manifest-*.json")):
            manifest = validate_manifest(json.loads(path.read_text()))
            _need(path.name == f"manifest-{manifest['manifest_ref']}.json", "manifest_tampered")
            found.append(manifest)
        return found


# --------------------------------------------------------------------------- authority


@dataclass(frozen=True)
class Actor:
    tenant: str
    actor_id: str
    role: str


@dataclass(frozen=True)
class GrantState:
    tenant: str
    workflow_ref: str
    grant_epoch: int
    revoked: bool
    expires_at: str
    roles: Mapping[str, str]  # actor_id -> role


GrantLookup = Callable[[str, str], "GrantState | None"]


@dataclass(frozen=True)
class Request:
    """What the actor clicked: bound to the grant epoch observed at click time."""

    actor: Actor
    workflow_ref: str
    grant_epoch: int
    now: str


@dataclass(frozen=True)
class Denial:
    reason: str
    receipt: dict[str, Any]

    status: str = "denied"


def _denial(request: Request, reason: str, what: object) -> Denial:
    # Redacted: reason code and request digest only; no refs, tenants of others or content.
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "status": "denied",
        "reason": reason,
        "request_digest": digest(
            [request.actor.tenant, request.actor.actor_id, request.workflow_ref, what]
        ),
    }
    return Denial(reason, receipt)


# --------------------------------------------------------------------------- results


@dataclass(frozen=True)
class FieldLink:
    source_ref: str
    path: str
    source_revision: int
    available_at: str
    span: str | None  # withheld for non-owner roles


@dataclass(frozen=True)
class Resolution:
    status: str  # resolved | unknown | hold | protected
    reason: str | None = None
    claim_ref: str | None = None
    manifest_ref: str | None = None
    manifest_digest: str | None = None
    result_ref: str | None = None
    candidate_ref: str | None = None
    example_ref: str | None = None
    correction_version: int | None = None
    policy_revision: str | None = None
    decision_time: str | None = None
    inputs: tuple[FieldLink, ...] = ()
    input_count: int = 0
    unknown_dependencies: tuple[str, ...] = ()
    later_relationships: tuple[str, ...] = ()
    reader_digest: str = ""

    def to_json(self) -> dict[str, Any]:
        body = {
            "schema": RESOLUTION_SCHEMA,
            "status": self.status,
            "reason": self.reason,
            "claim_ref": self.claim_ref,
            "manifest_ref": self.manifest_ref,
            "manifest_digest": self.manifest_digest,
            "result_ref": self.result_ref,
            "candidate_ref": self.candidate_ref,
            "example_ref": self.example_ref,
            "correction_version": self.correction_version,
            "policy_revision": self.policy_revision,
            "decision_time": self.decision_time,
            "input_count": self.input_count,
            "inputs": [vars(link) for link in self.inputs],
            "unknown_dependencies": list(self.unknown_dependencies),
            "later_relationships": list(self.later_relationships),
        }
        return {**body, "reader_digest": digest(body)}


@dataclass(frozen=True)
class Change:
    """A change to one source field/revision. ``new_revision=None`` means withdrawn."""

    source_ref: str
    path: str | None  # None: the whole source object
    new_revision: int | None


@dataclass(frozen=True)
class ImpactSet:
    status: str  # computed | protected_aggregate_only
    dirty: tuple[tuple[str, str], ...]  # (ref, reason), development only
    unaffected: tuple[str, ...]
    unknown: tuple[str, ...]
    protected_counts: Mapping[str, int] = field(default_factory=dict)

    def semantic(self) -> dict[str, Any]:
        return {
            "dirty": [list(d) for d in self.dirty],
            "unaffected": list(self.unaffected),
            "unknown": list(self.unknown),
            "protected_counts": dict(sorted(self.protected_counts.items())),
        }

    def to_json(self) -> dict[str, Any]:
        body = {"schema": IMPACT_SCHEMA, "status": self.status, **self.semantic()}
        return {**body, "reader_digest": digest(body)}

    @property
    def digest(self) -> str:
        return digest(self.semantic())


# --------------------------------------------------------------------------- reader


class ConsumptionReader:
    """Reverse lookup and impact set over recorded forward consumption edges only."""

    def __init__(self, store: ManifestStore, grants: GrantLookup) -> None:
        self.store = store
        self.grants = grants

    def _authorize(self, request: Request, what: object) -> Denial | GrantState:
        actor = request.actor
        grant = self.grants(actor.tenant, request.workflow_ref)
        # Unknown workflow, foreign tenant and wrong role all look identical to the caller.
        if grant is None or grant.tenant != actor.tenant or actor.role not in ROLES:
            return _denial(request, "unavailable", what)
        if grant.roles.get(actor.actor_id) != actor.role:
            return _denial(request, "unavailable", what)
        if grant.revoked:
            return _denial(request, "grant_revoked", what)
        if _time(request.now, "request_time") >= _time(grant.expires_at, "grant_time"):
            return _denial(request, "grant_expired", what)
        if grant.grant_epoch != request.grant_epoch:
            return _denial(request, "grant_stale", what)
        return grant

    def _scoped(self, grant: GrantState) -> list[dict[str, Any]]:
        return [
            m for m in self.store.all()
            if m["tenant"] == grant.tenant
            and m["workflow_ref"] == grant.workflow_ref
            and m["grant_epoch"] <= grant.grant_epoch
        ]  # fmt: skip

    def resolve_claim(self, request: Request, claim_ref: str) -> Resolution | Denial:
        granted = self._authorize(request, claim_ref)
        if isinstance(granted, Denial):
            return granted
        matches = [m for m in self._scoped(granted) if claim_ref in m["claim_refs"]]
        if not matches:
            return _finish(Resolution("unknown", "linkage_missing", claim_ref=claim_ref))
        if len(matches) > 1:
            # Two sealed manifests claim the same claim: never pick a winner by order.
            return _finish(Resolution("hold", "ambiguous_linkage", claim_ref=claim_ref))
        m = matches[0]
        sealed = manifest_digest(m)
        base = {
            "claim_ref": claim_ref,
            "manifest_ref": m["manifest_ref"],
            "manifest_digest": sealed,
            "result_ref": m["result_ref"],
            "candidate_ref": m["candidate_ref"],
            "example_ref": m["example_ref"],
            "correction_version": m["correction_version"],
            "policy_revision": m["policy_revision"],
            "decision_time": m["decision_time"],
            "unknown_dependencies": tuple(m["unknown_dependencies"]),
        }
        if m["unknown_dependencies"]:
            # A claim needing an unrecorded link is held, never completed from guesses.
            return _finish(Resolution("hold", "unknown_dependency", **base))
        if m["partition"] == "protected":
            return _finish(
                Resolution("protected", "aggregate_only", input_count=len(m["consumed"]), **base)
            )
        links = tuple(
            FieldLink(
                e["source_ref"], e["path"], e["source_revision"], e["available_at"],
                e["span"] if request.actor.role == OWNER else None,
            )
            for e in m["consumed"]
        )  # fmt: skip
        later = (
            tuple(r["ref"] for r in m["later_relationships"]) if request.actor.role == OWNER else ()
        )
        return _finish(
            Resolution(
                "resolved",
                None,
                inputs=links,
                input_count=len(links),
                later_relationships=later,
                **base,
            )
        )

    def impact(self, request: Request, change: Change) -> ImpactSet | Denial:
        granted = self._authorize(request, [change.source_ref, change.path, change.new_revision])
        if isinstance(granted, Denial):
            return granted
        dirty: dict[str, str] = {}
        unknown: set[str] = set()
        every: set[str] = set()
        protected: dict[str, set[str]] = {"dirty": set(), "unknown": set(), "unaffected": set()}
        for m in self._scoped(granted):
            nodes = _consumers(m)
            reasons, doubts = _classify(m, nodes, change)
            for ref in nodes:
                if m["partition"] == "protected":
                    bucket = (
                        "dirty" if ref in reasons else "unknown" if ref in doubts else "unaffected"
                    )
                    protected[bucket].add(f"{m['manifest_ref']}:{ref}")
                    continue
                every.add(ref)
                if ref in reasons:
                    dirty.setdefault(ref, reasons[ref])
                elif ref in doubts:
                    unknown.add(ref)
        unknown -= set(dirty)
        return ImpactSet(
            "computed",
            tuple(sorted(dirty.items())),
            tuple(sorted(every - set(dirty) - unknown)),
            tuple(sorted(unknown)),
            {k: len(v) for k, v in protected.items()},
        )


def _finish(resolution: Resolution) -> Resolution:
    return resolution


def _consumers(manifest: Mapping[str, Any]) -> dict[str, list[str]]:
    """Consumer ref -> the refs it derives from (empty for direct field consumers)."""
    nodes: dict[str, list[str]] = {
        manifest["result_ref"]: [],
        manifest["candidate_ref"]: [],
        manifest["example_ref"]: [],
    }
    for node in manifest["descendants"]:
        nodes[node["ref"]] = list(node["derived_from"])
    return nodes


def _classify(
    manifest: Mapping[str, Any], nodes: Mapping[str, list[str]], change: Change
) -> tuple[dict[str, str], set[str]]:
    """Dirty reasons and unknown refs for one manifest, closed over recorded edges."""
    direct: str | None = None
    for entry in manifest["consumed"]:
        if entry["source_ref"] != change.source_ref:
            continue
        if change.path is not None and not paths_overlap(entry["path"], change.path):
            continue
        if change.new_revision is None:
            direct = "source_withdrawn"
        elif change.new_revision > entry["source_revision"]:
            direct = "consumed_revision_superseded"
        # equal: already current; lower: stale/replayed change. Neither dirties anything.
    doubt = any(u in ("*", change.source_ref) for u in manifest["unknown_dependencies"])
    roots = [manifest["result_ref"], manifest["candidate_ref"], manifest["example_ref"]]
    reach: set[str] = set(roots)
    frontier = list(roots)
    while frontier:  # closure over recorded derived_from edges only
        current = frontier.pop()
        for ref, parents in nodes.items():
            if current in parents and ref not in reach:
                reach.add(ref)
                frontier.append(ref)
    dirty: dict[str, str] = {}
    unknown: set[str] = set()
    if direct is not None:
        for ref in reach:
            dirty[ref] = direct if ref in roots else "descendant_of_changed_field"
    if doubt:
        unknown.update(reach)
    # An artifact with no recorded parents has no linkage at all: unknown, never unaffected.
    unknown.update(node["ref"] for node in manifest["descendants"] if not node["derived_from"])
    return dirty, unknown - set(dirty)
