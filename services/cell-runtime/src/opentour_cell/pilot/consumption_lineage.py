"""Resolve a workflow claim to its recorded consumed fields and derive impact sets (OPE-544).

This is a reader over write-once, digest-pinned (unsigned), customer-local consumed-field
manifests written at decision time (OPE-416). It never recreates provenance from current
documents and never guesses an edge: a claim or change that cannot be linked through recorded
edges returns ``unknown`` / ``hold``. All joins happen locally; nothing here performs I/O
beyond the manifest directory.

The producer record shape (``opentour.pilot.consumed-field-manifest.v1``) is a reader-side
proposal until OPE-416 publishes its writer; see ``validate_manifest`` for the exact fields.

Status vocabulary (this module's reading of a spec that permits "unknown or hold"; a contract
gap for the coordinator, not a settled product decision): ``unknown`` means no recorded edge
links the claim or consumer; ``hold`` means an edge exists but is unusable (an unrecorded
dependency, or two manifests claiming the same claim). Neither is ever completed by a guess.

Integrity: ``digest_self_consistent_only``. A manifest is pinned by a plain, unkeyed sha256
of its own bytes. That is tamper evidence only (an edit of one file is noticed); it is not
authentication, and anyone who can rewrite both the manifest and its pin can forge one. No
signing or key-custody contract exists for this reader yet (OPE-566). The resolution receipt
(``Resolution.to_json``) and the impact receipt (``ImpactSet.to_json``) carry
``integrity: digest_self_consistent_only`` inside their digested body, so no consumer reads
more into them. The redacted denial receipt carries no such field.

Time authority: grant expiry is judged on the worker's own clock (``_worker_now``), never on
a caller-supplied time. ``Request.now`` can only narrow the window: the grant is treated as
expired once either clock reaches ``expires_at``, so back-dating cannot revive an expired
grant and future-dating cannot extend one.

Simulated and NOT proven: everything here is a reader over generated sentinel manifests in a
local directory with an in-test grant lookup. It proves software behavior only. It establishes
no provider, installed-witness, staged-packet (OPE-529) or customer evidence, and it does not
show that OPE-416's real writer produces this shape.
"""

from __future__ import annotations

import hashlib
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
INTEGRITY = "digest_self_consistent_only"

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


_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$")


def _time(value: object, reason: str) -> datetime:
    # Full UTC timestamps only: a date-only value such as "2026-01-01Z" is not a point in time.
    # Exactly ``str``: a subclass can override ``__str__`` and an impostor can fake
    # ``__class__``; either is a refused time, never a TypeError.
    text = value if type(value) is str else ""
    _need(_TIMESTAMP.fullmatch(text), reason)
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as exc:
        raise LineageError(reason) from exc
    return parsed.astimezone(UTC)


def _ref(value: object, reason: str) -> str:
    # Exactly ``str``, returned as itself. A subclass whose buffer passes ``_REF`` can still
    # tell a different story through ``__str__``/``__format__``, which is what names files and
    # folders; it must never reach a path, a persisted record or a tenant folder.
    text = value if type(value) is str else ""
    _need(_REF.fullmatch(text), reason)
    return text


def _segments(path: str) -> tuple[str, ...]:
    return tuple(path.split("/"))


def paths_overlap(left: str, right: str) -> bool:
    """A change to a path touches a consumed path when either is a prefix of the other."""
    a, b = _segments(left), _segments(right)
    n = min(len(a), len(b))
    return a[:n] == b[:n]


# --------------------------------------------------------------------------- manifest


def _count(value: object, reason: str) -> int:
    # Exactly ``int``: not ``bool`` and not a subclass, whose comparisons can lie (an int
    # subclass of -5 can answer ``>= 0`` with True and then persist as -5).
    _need(type(value) is int, reason)
    number: int = value  # type: ignore[assignment]
    _need(number >= 0, reason)
    return number


def _choice(value: object, allowed: frozenset[str], reason: str) -> str:
    # Exactly ``str``: a subclass can claim membership through ``__eq__``/``__hash__`` while
    # its buffer, which is what gets persisted, says something else.
    text = value if type(value) is str else ""
    _need(text in allowed, reason)
    return text


def _keyed(value: object, reason: str) -> dict[str, Any]:
    # One read of a mapping, whose keys must be exactly ``str``: a key subclass can match an
    # expected name through ``__eq__``/``__hash__`` while persisting as another.
    _need(isinstance(value, Mapping), reason)
    mapping: Mapping[Any, Any] = value  # type: ignore[assignment]
    copy: dict[str, Any] = dict(mapping)
    _need(all(type(k) is str for k in copy), reason)
    return copy


def validate_manifest(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Return a normalized copy of a manifest or raise ``LineageError``.

    Required: schema, manifest_ref, tenant, workflow_ref, engagement_epoch, grant_epoch,
    partition, rule_kind, case_ref, decision_time, result_ref, candidate_ref, example_ref,
    correction_version, policy_revision, claim_refs, consumed[], and optional
    unknown_dependencies[], descendants[], later_relationships[].

    Every key and string field is exactly ``str`` and every integer field exactly ``int``. A
    ``TypeError``, ``ValueError``, ``RecursionError``, ``KeyError`` or ``AttributeError``
    raised while reading ``raw`` becomes ``LineageError("manifest_malformed")``. That covers a
    ``Mapping`` whose ``__iter__`` yields a key its ``__getitem__`` lacks, or whose
    ``__iter__`` raises ``AttributeError``. Any other exception that a caller-supplied object
    raises from its own methods is not mapped.
    """
    try:
        return _validate(raw)
    except (TypeError, ValueError, RecursionError, KeyError, AttributeError) as exc:
        raise LineageError("manifest_malformed") from exc


def _validate(given: Mapping[str, Any]) -> dict[str, Any]:
    _need(isinstance(given, Mapping), "manifest_schema")
    raw = _keyed(given, "manifest_unknown_key")
    _need(raw.get("schema") == MANIFEST_SCHEMA, "manifest_schema")
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
        out[key] = _count(raw.get(key), f"manifest_{key}")
    out["partition"] = _choice(raw.get("partition"), PARTITIONS, "manifest_partition")
    out["rule_kind"] = _choice(raw.get("rule_kind"), RULE_KINDS, "manifest_rule_kind")
    stamp = raw.get("decision_time")
    decision = _time(stamp, "manifest_decision_time")
    out["decision_time"] = stamp
    claims: Any = raw.get("claim_refs")
    _need(isinstance(claims, list) and claims, "manifest_claim_refs")
    out["claim_refs"] = sorted({_ref(c, "manifest_claim_refs") for c in claims})
    consumed: Any = raw.get("consumed")
    _need(isinstance(consumed, list), "manifest_consumed")
    seen: set[tuple[str, str]] = set()
    entries: list[dict[str, Any]] = []
    for given_item in consumed:
        item = _keyed(given_item, "manifest_consumed_entry")
        _need(set(item) == {
            "source_ref", "path", "source_revision", "available_at", "span"
        }, "manifest_consumed_entry")  # fmt: skip
        source = _ref(item["source_ref"], "manifest_consumed_entry")
        path = item["path"]
        _need(type(path) is str and _PATH.fullmatch(path), "manifest_consumed_path")
        revision = _count(item["source_revision"], "manifest_consumed_revision")
        # A decision may only have consumed what existed when it was made.
        available = item["available_at"]
        _need(
            _time(available, "manifest_consumed_time") <= decision,
            "input_after_decision",
        )
        span = item["span"]
        _need(
            span is None or (type(span) is str and 0 < len(span) <= 64),
            "manifest_consumed_span",
        )
        _need((source, path) not in seen, "manifest_consumed_duplicate")
        seen.add((source, path))
        entries.append({
            "source_ref": source, "path": path, "source_revision": revision,
            "available_at": available, "span": span,
        })  # fmt: skip
    out["consumed"] = sorted(entries, key=lambda e: (e["source_ref"], e["path"]))
    unknown = raw.get("unknown_dependencies", [])
    _need(isinstance(unknown, list), "manifest_unknown_dependencies")
    out["unknown_dependencies"] = sorted(
        {
            "*" if type(u) is str and u == "*" else _ref(u, "manifest_unknown_dependencies")
            for u in unknown
        }
    )
    known = {out["result_ref"], out["candidate_ref"], out["example_ref"]}
    descendants = raw.get("descendants", [])
    _need(isinstance(descendants, list), "manifest_descendants")
    nodes: list[dict[str, Any]] = []
    names = set(known)
    for given_node in descendants:
        node = _keyed(given_node, "manifest_descendant")
        _need(set(node) == {"ref", "kind", "derived_from"}, "manifest_descendant")
        ref = _ref(node["ref"], "manifest_descendant")
        kind = _choice(node["kind"], ARTIFACT_KINDS, "manifest_descendant")
        _need(ref not in names, "manifest_descendant")
        _need(isinstance(node["derived_from"], list), "manifest_descendant")
        parents = sorted({_ref(p, "manifest_descendant") for p in node["derived_from"]})
        names.add(ref)
        nodes.append({"ref": ref, "kind": kind, "derived_from": parents})
    for node in nodes:
        _need(set(node["derived_from"]) <= names - {node["ref"]}, "manifest_descendant_parent")
    out["descendants"] = sorted(nodes, key=lambda n: n["ref"])
    later = raw.get("later_relationships", [])
    _need(isinstance(later, list), "manifest_later_relationships")
    rels = []
    for given_rel in later:
        rel = _keyed(given_rel, "manifest_later_relationship")
        _need(
            set(rel) == {"kind", "ref", "available_at", "relationship_version"},
            "manifest_later_relationship",
        )
        rel_ref = _ref(rel["ref"], "manifest_later_relationship")
        # A "later" relationship must really be later than the decision it attaches to.
        rel_at = rel["available_at"]
        _need(
            _time(rel_at, "manifest_later_relationship") > decision,
            "manifest_later_relationship",
        )
        rel_kind, version = rel["kind"], rel["relationship_version"]
        _need(
            type(rel_kind) is str and type(version) is int,
            "manifest_later_relationship",
        )
        rels.append({
            "kind": rel_kind, "ref": rel_ref, "available_at": rel_at,
            "relationship_version": version,
        })  # fmt: skip
    out["later_relationships"] = sorted(rels, key=lambda r: (r["ref"], r["relationship_version"]))
    return out


def _same(left: object, right: object) -> bool:
    """Equal with exactly the same types at every level (dict keys included); no ``__eq__``
    of a subclass is ever consulted."""
    if type(left) is not type(right):
        return False
    if type(left) is dict:
        a: dict[Any, Any] = left
        b: dict[Any, Any] = right  # type: ignore[assignment]
        return (
            all(type(k) is str for k in a)
            and all(type(k) is str for k in b)
            and sorted(a) == sorted(b)
            and all(_same(a[k], b[k]) for k in a)
        )
    if type(left) is list:
        xs: list[Any] = left
        ys: list[Any] = right  # type: ignore[assignment]
        return len(xs) == len(ys) and all(_same(x, y) for x, y in zip(xs, ys, strict=True))
    return left == right  # same exact type, and ``put`` passes the re-read (builtin) first


def _canonical(manifest: Mapping[str, Any]) -> str:
    """The exact bytes ``put`` may write: only a record that every later read accepts as
    exactly itself. Anything else is refused before a file or folder exists, so a write can
    never wedge its tenant's reads."""
    try:
        body = _body(manifest)
        again = validate_manifest(json.loads(body))
    except (TypeError, ValueError, RecursionError) as exc:
        raise LineageError("manifest_not_canonical") from exc
    _need(_same(again, manifest), "manifest_not_canonical")
    return body


def manifest_digest(manifest: Mapping[str, Any]) -> str:
    return digest(manifest)


def _body(manifest: Mapping[str, Any]) -> str:
    return json.dumps(manifest, sort_keys=True, separators=(",", ":"))


def _pin(body: str) -> str:
    """Plain unkeyed sha256: ``digest_self_consistent_only`` (tamper evidence, not a seal)."""
    return hashlib.sha256(body.encode()).hexdigest()


class ManifestStore:
    """Write-once, tenant-scoped manifest directory; survives restart by re-reading files.

    Each manifest is stored under its tenant's own folder with a sidecar pin (plain sha256 of
    the exact bytes) written first. Reads verify the pin, so an edit to the manifest file alone
    is detected. Integrity is ``digest_self_consistent_only``: tamper evidence against an edit
    of one file, not tamper-proofing or authentication. Anyone who can rewrite both the
    manifest and its pin can forge a manifest. Tenants never share files, so one tenant's
    refs, corruption or writes cannot affect or probe another's.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def _folder(self, tenant: str) -> Path:
        return self.directory / _ref(tenant, "manifest_tenant")

    @staticmethod
    def _slurp(path: Path) -> str:
        try:
            return path.read_text()
        except (OSError, UnicodeDecodeError) as exc:
            raise LineageError("manifest_unreadable") from exc

    @staticmethod
    def _create(folder: Path, name: str, body: str) -> None:
        """Atomically create ``name`` unless it exists; an existing file must be identical."""
        target = folder / name
        handle, temporary = tempfile.mkstemp(dir=folder, prefix=".tmp-")
        try:
            with os.fdopen(handle, "w") as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, target)  # atomic create-if-absent
            except FileExistsError:
                _need(ManifestStore._slurp(target) == body, "manifest_conflict")
        finally:
            os.unlink(temporary)

    def put(self, raw: Mapping[str, Any]) -> str:
        """Write once. Identical replay is idempotent; different bytes for a ref conflict."""
        manifest = validate_manifest(raw)
        body = _canonical(manifest)  # before any folder or file is created
        folder = self._folder(manifest["tenant"])
        folder.mkdir(parents=True, exist_ok=True)
        name = f"manifest-{manifest['manifest_ref']}"
        # Pin first: a crash leaves only an orphan pin, which a retry accepts as identical.
        self._create(folder, f"{name}.sha256", _pin(body))
        self._create(folder, f"{name}.json", body)
        return manifest_digest(manifest)

    def all(self, tenant: str) -> list[dict[str, Any]]:
        """Every manifest of one tenant whose (unsigned) digest pin matches its bytes.

        Any defect raises a typed LineageError.
        """
        folder = self._folder(tenant)
        found = []
        for path in sorted(folder.glob("manifest-*.json")) if folder.is_dir() else []:
            body = self._slurp(path)
            pin = path.with_suffix(".sha256")
            _need(pin.is_file() and self._slurp(pin) == _pin(body), "manifest_tampered")
            try:
                loaded = json.loads(body)
            except (ValueError, RecursionError) as exc:
                raise LineageError("manifest_unreadable") from exc
            manifest = validate_manifest(loaded)
            _need(
                path.name == f"manifest-{manifest['manifest_ref']}.json"
                and manifest["tenant"] == tenant
                and _body(manifest) == body,
                "manifest_tampered",
            )
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


def _worker_now() -> datetime:
    """The worker's own clock: the only authority on whether a grant has expired.

    Deliberately not a parameter of ``ConsumptionReader`` or a field of ``Request``, so no
    caller of the production entry point can supply or replace it. Tests pin it by patching
    this module attribute (a test-only seam).
    """
    return datetime.now(UTC)


@dataclass(frozen=True)
class Request:
    """What the actor clicked: bound to the grant epoch observed at click time.

    ``now`` is caller-supplied and untrusted. It may only make a grant expire sooner; expiry
    itself is judged on the worker clock (``_worker_now``).
    """

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
            "integrity": INTEGRITY,
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
        body = {
            "schema": IMPACT_SCHEMA,
            "status": self.status,
            **self.semantic(),
            "integrity": INTEGRITY,
        }
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
        """Checks run in a fixed order and the first failure is the reason returned.

        The order is identity and role, then revocation, then time format, then expiry (worker
        clock), then grant epoch. No contract specifies this order, so tests do not pin it.
        """
        actor = request.actor
        grant = self.grants(actor.tenant, request.workflow_ref)
        # Unknown workflow, foreign tenant and wrong role all look identical to the caller.
        if grant is None or grant.tenant != actor.tenant or actor.role not in ROLES:
            return _denial(request, "unavailable", what)
        if grant.roles.get(actor.actor_id) != actor.role:
            return _denial(request, "unavailable", what)
        if grant.revoked:
            return _denial(request, "grant_revoked", what)
        try:
            claimed = _time(request.now, "request_time")
            expires = _time(grant.expires_at, "grant_time")
        except LineageError:
            return _denial(request, "invalid_time", what)
        # The worker clock decides. A caller time can only narrow the window (take the later
        # of the two instants), so back-dating cannot skip expiry and future-dating extends
        # nothing.
        expired = max(_worker_now(), claimed) >= expires
        if expired:
            return _denial(request, "grant_expired", what)
        if grant.grant_epoch != request.grant_epoch:
            return _denial(request, "grant_stale", what)
        return grant

    def _scoped(self, grant: GrantState) -> list[dict[str, Any]]:
        return [
            m
            for m in self.store.all(grant.tenant)
            if m["workflow_ref"] == grant.workflow_ref and m["grant_epoch"] <= grant.grant_epoch
        ]

    def resolve_claim(self, request: Request, claim_ref: str) -> Resolution | Denial:
        granted = self._authorize(request, claim_ref)
        if isinstance(granted, Denial):
            return granted
        try:
            scoped = self._scoped(granted)
        except LineageError as exc:
            return _denial(request, exc.reason, claim_ref)  # fail closed, no content
        matches = [m for m in scoped if claim_ref in m["claim_refs"]]
        if not matches:
            return _finish(Resolution("unknown", "linkage_missing", claim_ref=claim_ref))
        if len(matches) > 1:
            # Two pinned (unsigned) manifests claim the same claim: never pick a winner by order.
            return _finish(Resolution("hold", "ambiguous_linkage", claim_ref=claim_ref))
        m = matches[0]
        if m["partition"] == "protected":
            # Protected roots expose authorized aggregate status only: no refs, digests,
            # versions, times, counts or dependency names, whatever else is true of them.
            return _finish(Resolution("protected", "aggregate_only", claim_ref=claim_ref))
        base = {
            "claim_ref": claim_ref,
            "manifest_ref": m["manifest_ref"],
            "manifest_digest": manifest_digest(m),
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
        try:
            scoped = self._scoped(granted)
        except LineageError as exc:
            return _denial(request, exc.reason, [change.source_ref, change.path])
        dirty: dict[str, str] = {}
        unknown: set[str] = set()
        every: set[str] = set()
        protected: dict[str, set[str]] = {"dirty": set(), "unknown": set(), "unaffected": set()}
        for m in scoped:
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
    # Any artifact the recorded edges do not ground in a root (no parent, only unrecorded
    # ancestry, or an unreachable cycle) has no usable linkage: unknown, never unaffected. That
    # doubt flows down to everything derived from it, even from a grounded second parent.
    unknown.update(set(nodes) - reach)
    frontier = list(unknown)
    while frontier:
        current = frontier.pop()
        for ref, parents in nodes.items():
            if current in parents and ref not in unknown:
                unknown.add(ref)
                frontier.append(ref)
    return dirty, unknown - set(dirty)
