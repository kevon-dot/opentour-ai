"""Recompute only the affected mind-map families and prove parity with a clean rebuild.

Simulated: a SQLite-backed local store, generated sentinel families and in-process workers.
Not proven: provider behavior, use of the OPE-544 consumption manifest, million-scale
fixtures, any speed claim beyond the registered local scenario, or authorization of a fit.

Scope (OPE-525). OPE-544's consumed-field manifest reader and reverse lookup are on ``main``
(``opentour_cell.pilot.consumption_lineage``), but this module does not consume them yet.
Write-time consumption capture (OPE-416), the writer those manifests depend on, is not on
``main``. This module therefore takes declared consumption (``Family.consumes``) as typed
input and builds its own reverse index from it; swap the index source for the OPE-544 reader
once OPE-416 writes real manifests. Nothing here authorizes a fit: plans and manifests always
carry ``fit_authorized = False`` and rows are never exposed, only digests, so protected
per-case answers cannot leak through a manifest.

The cache key binds ``TOOL_VERSION``, ``VERIFIER_VERSION`` (the verifier/finality rule a
built artifact was checked under) and ``ARTIFACT_SCHEMA_VERSION``. The same versions are in
the stored registry fingerprint, so bumping any of them forces a bounded full rebuild with no
reuse of earlier jobs or snapshot results.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from opentour_cell.pilot.operating_point import canonical, digest

TOOL_VERSION = "opentour.recompute.v1"
MANIFEST_VERSION = "opentour.recompute.manifest.v1"
VERIFIER_VERSION = "opentour.recompute.verifier-finality.v1"
ARTIFACT_SCHEMA_VERSION = "opentour.recompute.family-artifact.v1"
DEVELOPMENT = "development"
PROTECTED = "protected"
BUILT, HELD, BLOCKED = "built", "held", "blocked"
SPLITS = (DEVELOPMENT, PROTECTED)

IndexLookup = Callable[["ChangeEvent", str], set[str]]


class RecomputeError(Exception):
    """Closed-vocabulary failure; ``code`` is safe to show and never carries row values."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class PublishConflict(RecomputeError):
    """The parent snapshot, change cursor or authority moved before publish."""


class LeaseLost(RecomputeError):
    """A newer lease holder fenced this worker."""


class SimulatedCrash(Exception):
    """Test hook: process death mid-recompute (deliberately not a RecomputeError)."""


@dataclass(frozen=True, order=True)
class FieldRef:
    source_ref: str  # exact source or policy revision
    field_path: str

    def key(self) -> str:
        return f"{self.source_ref}#{self.field_path}"


@dataclass(frozen=True)
class Family:
    family_id: str
    tenant: str
    purpose: str
    split: str
    consumes: tuple[FieldRef, ...]
    parent_id: str | None = None  # synthetic child: inherits its parent's consumption
    lineage_complete: bool = True
    revision: str = "family-r1"

    def __post_init__(self) -> None:
        if self.split not in SPLITS:
            raise RecomputeError("INVALID_SPLIT")


@dataclass(frozen=True)
class ChangeEvent:
    event_id: str
    revision: int
    kind: str  # upsert | withdrawal | policy | family_withdrawal | merge | grant
    source_ref: str = ""
    field_path: str = ""
    value: Any = None
    family_ids: tuple[str, ...] = ()  # family_withdrawal / merge
    tenant: str = ""
    purpose: str = ""
    epoch: int | None = None  # grant: new authority epoch, None = revoked

    def to_json(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id, "revision": self.revision, "kind": self.kind,
            "source_ref": self.source_ref, "field_path": self.field_path, "value": self.value,
            "family_ids": list(self.family_ids), "tenant": self.tenant,
            "purpose": self.purpose, "epoch": self.epoch,
        }  # fmt: skip

    @staticmethod
    def from_json(raw: Mapping[str, Any]) -> ChangeEvent:
        data = dict(raw)
        data["family_ids"] = tuple(data["family_ids"])
        return ChangeEvent(**data)


@dataclass(frozen=True)
class SourceState:
    """Point-in-time inputs, withdrawals, split merges and live grants."""

    values: Mapping[str, tuple[int, Any]] = field(default_factory=dict)  # key -> (rev, value)
    withdrawn_families: frozenset[str] = frozenset()
    merges: frozenset[frozenset[str]] = frozenset()
    grants: Mapping[tuple[str, str], int | None] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "values": {k: list(v) for k, v in sorted(self.values.items())},
            "withdrawn": sorted(self.withdrawn_families),
            "merges": sorted(sorted(m) for m in self.merges),
            "grants": [[t, p, e] for (t, p), e in sorted(self.grants.items())],
        }

    @staticmethod
    def from_json(raw: Mapping[str, Any]) -> SourceState:
        return SourceState(
            values={k: (v[0], v[1]) for k, v in raw["values"].items()},
            withdrawn_families=frozenset(raw["withdrawn"]),
            merges=frozenset(frozenset(m) for m in raw["merges"]),
            grants={(t, p): e for t, p, e in raw["grants"]},
        )


def apply_event(state: SourceState, event: ChangeEvent) -> SourceState:
    """Pure transition. A lower revision of the same field never overwrites a newer one."""
    values = dict(state.values)
    grants = dict(state.grants)
    withdrawn, merges = set(state.withdrawn_families), set(state.merges)
    key = FieldRef(event.source_ref, event.field_path).key()
    if event.kind in ("upsert", "withdrawal"):
        current = values.get(key)
        if current is None or event.revision > current[0]:
            values[key] = (event.revision, event.value if event.kind == "upsert" else None)
    elif event.kind == "family_withdrawal":
        withdrawn.update(event.family_ids)
    elif event.kind == "merge":
        merges.add(frozenset(event.family_ids))
    elif event.kind == "grant":
        grants[(event.tenant, event.purpose)] = event.epoch
    elif event.kind != "policy":
        raise RecomputeError("UNKNOWN_EVENT_KIND")
    return SourceState(values, frozenset(withdrawn), frozenset(merges), grants)


@dataclass(frozen=True)
class FamilyResult:
    family_id: str
    tenant: str
    split: str
    status: str
    reasons: tuple[str, ...]
    digest: str
    cache_key: str
    grant_epoch: int | None

    def to_json(self) -> dict[str, Any]:
        return {
            "family_id": self.family_id, "tenant": self.tenant, "split": self.split,
            "status": self.status, "reasons": list(self.reasons), "digest": self.digest,
            "cache_key": self.cache_key, "grant_epoch": self.grant_epoch,
        }  # fmt: skip

    @staticmethod
    def from_json(raw: Mapping[str, Any]) -> FamilyResult:
        data = dict(raw)
        data["reasons"] = tuple(data["reasons"])
        return FamilyResult(**data)


class Registry:
    """Immutable family set plus the reverse index (field -> families, parent -> children)."""

    def __init__(self, families: Iterable[Family]):
        self.families: dict[str, Family] = {}
        for fam in families:
            if fam.family_id in self.families:
                raise RecomputeError("DUPLICATE_FAMILY")
            self.families[fam.family_id] = fam
        self.children: dict[str, list[str]] = defaultdict(list)
        for fam in self.families.values():
            if fam.parent_id is not None:
                if fam.parent_id not in self.families:
                    raise RecomputeError("UNKNOWN_PARENT")
                self.children[fam.parent_id].append(fam.family_id)
        # Development and protected indexes are separate: development roots are queried alone.
        self.by_field: dict[str, dict[str, set[str]]] = {s: {} for s in SPLITS}
        self.by_source: dict[str, dict[str, set[str]]] = {s: {} for s in SPLITS}
        self.by_scope: dict[tuple[str, str], set[str]] = defaultdict(set)
        for fam in self.families.values():
            self.by_scope[(fam.tenant, fam.purpose)].add(fam.family_id)
            for ref in self.effective_consumes(fam.family_id):
                self.by_field[fam.split].setdefault(ref.key(), set()).add(fam.family_id)
                self.by_source[fam.split].setdefault(ref.source_ref, set()).add(fam.family_id)

    def ancestry(self, family_id: str) -> set[str]:
        chain: set[str] = set()
        cursor: str | None = family_id
        while cursor is not None and cursor not in chain:
            chain.add(cursor)
            cursor = self.families[cursor].parent_id
        return chain

    def effective_consumes(self, family_id: str) -> tuple[FieldRef, ...]:
        refs = {ref for anc in self.ancestry(family_id) for ref in self.families[anc].consumes}
        return tuple(sorted(refs))

    def descendants(self, family_ids: Iterable[str]) -> set[str]:
        out: set[str] = set()
        stack = list(family_ids)
        while stack:
            current = stack.pop()
            if current not in out:
                out.add(current)
                stack.extend(self.children.get(current, ()))
        return out

    def lookup(self, event: ChangeEvent, split: str) -> set[str]:
        """Families of one split that directly consume what the event touches."""
        if event.kind in ("upsert", "withdrawal"):
            key = FieldRef(event.source_ref, event.field_path).key()
            return set(self.by_field[split].get(key, ()))
        if event.kind == "policy":
            return set(self.by_source[split].get(event.source_ref, ()))
        if event.kind == "grant":
            scope = self.by_scope.get((event.tenant, event.purpose), set())
            return {f for f in scope if self.families[f].split == split}
        if event.kind in ("family_withdrawal", "merge"):
            return {
                f
                for f in event.family_ids
                if f in self.families and self.families[f].split == split
            }
        raise RecomputeError("UNKNOWN_EVENT_KIND")


def evaluate_family(registry: Registry, family_id: str, state: SourceState) -> FamilyResult:
    """The single pure build rule used by both the incremental and the clean path."""
    fam = registry.families[family_id]
    epoch = state.grants.get((fam.tenant, fam.purpose))
    chain = registry.ancestry(family_id)
    reasons: set[str] = set()
    if chain & state.withdrawn_families:
        reasons.add("family_withdrawn")
    if epoch is None:
        reasons.add("grant_revoked")
    for merged in state.merges:
        splits = {registry.families[m].split for m in merged if m in registry.families}
        if chain & merged and len(splits) > 1:
            reasons.add("split_merge_ambiguous")
    inputs: list[list[Any]] = []
    for ref in registry.effective_consumes(family_id):
        rev, value = state.values.get(ref.key(), (0, None))
        inputs.append([ref.source_ref, ref.field_path, rev, value])
    if any(row[3] is None for row in inputs):
        reasons.add("input_missing_or_withdrawn")
    if reasons:
        return FamilyResult(
            family_id, fam.tenant, fam.split, BLOCKED, tuple(sorted(reasons)), "", "", epoch
        )
    if not all(registry.families[c].lineage_complete for c in chain):
        return FamilyResult(
            family_id, fam.tenant, fam.split, HELD, ("lineage_incomplete",), "", "", epoch
        )
    key = digest({
        "tenant": fam.tenant, "purpose": fam.purpose, "epoch": epoch, "inputs": inputs,
        "family": fam.family_id, "family_revision": fam.revision, "parent": fam.parent_id,
        "split": fam.split, "tool": TOOL_VERSION, "verifier": VERIFIER_VERSION,
        "artifact_schema": ARTIFACT_SCHEMA_VERSION,
    })  # fmt: skip
    content = digest({"cache_key": key, "kind": "family-artifact"})
    return FamilyResult(family_id, fam.tenant, fam.split, BUILT, (), content, key, epoch)


def full_rebuild(registry: Registry, state: SourceState) -> dict[str, FamilyResult]:
    """Independent clean rebuild: no index, no cache, no prior snapshot."""
    return {f: evaluate_family(registry, f, state) for f in sorted(registry.families)}


def semantic_digest(results: Mapping[str, FamilyResult]) -> str:
    """Over sorted stable ids; results carry no timestamps, receipt ids or execution order."""
    return digest([results[f].to_json() for f in sorted(results)])


@dataclass(frozen=True)
class Plan:
    parent_revision: int
    cursor: int
    dirty: tuple[str, ...]
    reusable: tuple[str, ...]
    blocked: tuple[str, ...]
    unknown: tuple[str, ...]
    reasons: Mapping[str, tuple[str, ...]]
    mode: str  # incremental | bounded_full_rebuild
    fit_authorized: bool = False  # preparation never authorizes a fit


def make_plan(
    registry: Registry,
    prior: Mapping[str, FamilyResult],
    parent_revision: int,
    events: Iterable[ChangeEvent],
    state: SourceState,
    cursor: int,
    max_fanout: int | None = None,
    lookup: IndexLookup | None = None,
    registry_changed: bool = False,
) -> Plan:
    find = lookup or registry.lookup
    reasons: dict[str, set[str]] = defaultdict(set)
    for event in events:
        for split in SPLITS:
            for fam in find(event, split):
                reasons[fam].add(f"change:{event.kind}")
    for root in list(reasons):  # children inherit the root's consumption and fences
        for child in registry.descendants([root]) - {root}:
            reasons[child].add("ancestor_dirty")
    for fam_id, result in prior.items():  # live grant recheck on every reuse
        if fam_id not in registry.families:
            continue  # family left the registry; its prior result is dropped at publish
        purpose = registry.families[fam_id].purpose
        if state.grants.get((result.tenant, purpose)) != result.grant_epoch:
            reasons[fam_id].add("grant_epoch_changed")
    for fam_id in registry.families:
        if fam_id not in prior:
            reasons[fam_id].add("no_prior_result")
    mode = "incremental"
    if registry_changed:  # family set, revision, consumption or a build version moved
        mode = "bounded_full_rebuild"
        for fam_id in registry.families:
            reasons[fam_id].add("registry_changed")
    elif max_fanout is not None and len(reasons) > max_fanout:
        mode = "bounded_full_rebuild"  # excessive fan-out: rebuild the full authorized scope
        for fam_id in registry.families:
            reasons[fam_id].add("fanout_fallback")
    dirty = sorted(reasons)
    blocked = sorted(f for f in dirty if evaluate_family(registry, f, state).status == BLOCKED)
    unknown = sorted(f for f, fam in registry.families.items() if not fam.lineage_complete)
    reusable = sorted(set(registry.families) - set(dirty) - set(unknown))
    return Plan(
        parent_revision,
        cursor,
        tuple(dirty),
        tuple(reusable),
        tuple(blocked),
        tuple(unknown),
        {k: tuple(sorted(v)) for k, v in sorted(reasons.items())},
        mode,
    )


@dataclass(frozen=True)
class Manifest:
    revision: int
    parent_revision: int | None
    parent_digest: str | None
    results: Mapping[str, FamilyResult]

    @property
    def semantic(self) -> str:
        return semantic_digest(self.results)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = defaultdict(int)
        for r in self.results.values():
            out[r.status] += 1
        return dict(sorted(out.items()))

    def to_json(self) -> dict[str, Any]:
        # Digests and statuses only: no row values, so protected answers cannot appear here.
        return {
            "version": MANIFEST_VERSION, "revision": self.revision,
            "parent_revision": self.parent_revision, "parent_digest": self.parent_digest,
            "semantic_digest": self.semantic, "fit_authorized": False,
            "results": [self.results[f].to_json() for f in sorted(self.results)],
        }  # fmt: skip


SCHEMA = """
create table if not exists meta(k text primary key, v text not null);
create table if not exists events(cursor integer primary key autoincrement, event_id text unique,
  payload text not null, state text not null default 'pending');
create table if not exists snapshots(revision integer primary key, parent integer,
  manifest text not null, semantic text not null);
create table if not exists jobs(cache_key text primary key, family_id text not null,
  result text not null);
create table if not exists lease(k text primary key, holder text, token integer, expires integer);
create table if not exists claims(claim_id text primary key, family_id text not null,
  stale integer not null default 0);
create table if not exists admissions(family_id text, revision integer,
  primary key(family_id, revision));
"""


@dataclass(frozen=True)
class RunReport:
    plan: Plan
    jobs_started: int
    cache_hits: int
    manifest: Manifest


class _Tx:
    def __init__(self, db: sqlite3.Connection, lock: threading.RLock):
        self.db, self.lock = db, lock

    def __enter__(self) -> None:
        self.lock.acquire()
        self.db.execute("begin immediate")

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        try:
            self.db.execute("rollback" if exc_type else "commit")
        finally:
            self.lock.release()


def registry_fingerprint(registry: Registry) -> str:
    """Digest of every input that feeds a result besides source state.

    Family fields (revision, split, lineage, consumption) plus the tool, verifier/finality and
    artifact-schema versions: a version bump invalidates every prior result, not just new jobs.
    """
    return digest({
        "versions": [TOOL_VERSION, VERIFIER_VERSION, ARTIFACT_SCHEMA_VERSION],
        "families": [
            [
                f.family_id, f.tenant, f.purpose, f.split, f.parent_id, f.lineage_complete,
                f.revision, [ref.key() for ref in f.consumes],
            ]
            for f in (registry.families[k] for k in sorted(registry.families))
        ],
    })  # fmt: skip


class RecomputeService:
    """Durable change cursor + dirty queue with fenced, compare-and-set publication."""

    def __init__(
        self, path: str, registry: Registry, batch_size: int = 8, max_fanout: int | None = None
    ):
        self.registry = registry
        self.batch_size = batch_size
        self.max_fanout = max_fanout
        self._lock = threading.RLock()
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    def _tx(self) -> _Tx:
        return _Tx(self.db, self._lock)

    def source_state(self) -> SourceState:
        row = self.db.execute("select v from meta where k='state'").fetchone()
        return SourceState() if row is None else SourceState.from_json(json.loads(row[0]))

    def _put_state(self, state: SourceState) -> None:
        self.db.execute(
            "insert or replace into meta values('state',?)", (canonical(state.to_json()),)
        )

    def _put_registry(self) -> None:
        self.db.execute(
            "insert or replace into meta values('registry',?)",
            (registry_fingerprint(self.registry),),
        )

    def _registry_changed(self) -> bool:
        row = self.db.execute("select v from meta where k='registry'").fetchone()
        return row is None or row[0] != registry_fingerprint(self.registry)

    def revision(self) -> int:
        return int(self.db.execute("select max(revision) from snapshots").fetchone()[0] or 0)

    def cursor(self) -> int:
        return int(self.db.execute("select max(cursor) from events").fetchone()[0] or 0)

    def job_count(self) -> int:
        return int(self.db.execute("select count(*) from jobs").fetchone()[0])

    def snapshot(self, revision: int | None = None) -> Manifest | None:
        rev = self.revision() if revision is None else revision
        row = self.db.execute("select manifest from snapshots where revision=?", (rev,)).fetchone()
        if row is None:
            return None
        raw = json.loads(row[0])
        results = {r["family_id"]: FamilyResult.from_json(r) for r in raw["results"]}
        return Manifest(raw["revision"], raw["parent_revision"], raw["parent_digest"], results)

    def pending_events(self) -> list[ChangeEvent]:
        rows = self.db.execute("select payload from events where state='pending' order by cursor")
        return [ChangeEvent.from_json(json.loads(p[0])) for p in rows]

    def _save_snapshot(self, manifest: Manifest) -> None:
        self.db.execute(
            "insert into snapshots values(?,?,?,?)",
            (
                manifest.revision,
                manifest.parent_revision,
                canonical(manifest.to_json()),
                manifest.semantic,
            ),
        )

    def bootstrap(self, state: SourceState) -> Manifest:
        with self._tx():
            if self.revision():
                raise RecomputeError("ALREADY_BOOTSTRAPPED")
            results = full_rebuild(self.registry, state)
            for r in results.values():
                if r.status == BUILT:
                    self.db.execute(
                        "insert or ignore into jobs values(?,?,?)",
                        (r.cache_key, r.family_id, canonical(r.to_json())),
                    )
            manifest = Manifest(1, None, None, results)
            self._save_snapshot(manifest)
            self._put_state(state)
            self._put_registry()
            return manifest

    def submit(self, events: Iterable[ChangeEvent]) -> dict[str, int]:
        """Persist events, advance source state and stale dependent claims in one transaction.

        Duplicate event ids are ignored entirely. A newer upsert supersedes older pending
        upserts of the same field; withdrawals, merges and grants are never coalesced away.
        """
        accepted = duplicates = 0
        with self._tx():
            state = self.source_state()
            touched: set[str] = set()
            for event in events:
                if self.db.execute(
                    "select 1 from events where event_id=?", (event.event_id,)
                ).fetchone():
                    duplicates += 1
                    continue
                if event.kind == "upsert":
                    self.db.execute(
                        "update events set state='superseded' where state='pending' and "
                        "json_extract(payload,'$.kind')='upsert' and "
                        "json_extract(payload,'$.source_ref')=? and "
                        "json_extract(payload,'$.field_path')=? and "
                        "json_extract(payload,'$.revision')<?",
                        (event.source_ref, event.field_path, event.revision),
                    )
                self.db.execute(
                    "insert into events(event_id,payload) values(?,?)",
                    (event.event_id, canonical(event.to_json())),
                )
                state = apply_event(state, event)
                for split in SPLITS:
                    touched |= self.registry.lookup(event, split)
                accepted += 1
            self._put_state(state)
            for fam in self.registry.descendants(touched):  # stale immediately, before recompute
                self.db.execute("update claims set stale=1 where family_id=?", (fam,))
        return {"accepted": accepted, "duplicates": duplicates}

    def register_claim(self, claim_id: str, family_id: str) -> None:
        self.db.execute(
            "insert or ignore into claims(claim_id,family_id) values(?,?)", (claim_id, family_id)
        )

    def current_claims(self) -> list[str]:
        return [
            r[0] for r in self.db.execute("select claim_id from claims where stale=0 order by 1")
        ]

    def verify_claim(self, claim_id: str) -> bool:
        """Re-verify a stale claim; only possible against a built, fully caught-up family."""
        snap = self.snapshot()
        row = self.db.execute(
            "select family_id from claims where claim_id=?", (claim_id,)
        ).fetchone()
        if row is None or snap is None or snap.results[row[0]].status != BUILT:
            return False
        state = self.source_state()
        if (
            evaluate_family(self.registry, row[0], state).cache_key
            != snap.results[row[0]].cache_key
        ):
            return False
        self.db.execute("update claims set stale=0 where claim_id=?", (claim_id,))
        return True

    def acquire_lease(self, holder: str, now: int, ttl: int = 30) -> int:
        with self._tx():
            row = self.db.execute(
                "select holder, token, expires from lease where k='run'"
            ).fetchone()
            if row and row[2] > now and row[0] != holder:
                raise LeaseLost("LEASE_HELD")
            token = (row[1] if row else 0) + 1
            self.db.execute(
                "insert or replace into lease values('run',?,?,?)", (holder, token, now + ttl)
            )
            return int(token)

    def _check_lease(self, token: int) -> None:
        row = self.db.execute("select token from lease where k='run'").fetchone()
        if row is None or row[0] != token:
            raise LeaseLost("LEASE_LOST")

    def admit(self, family_id: str) -> bool:
        """Promotion gate: published, built, live-authorized, current and not stale. Idempotent."""
        snap, state = self.snapshot(), self.source_state()
        if snap is None or family_id not in snap.results:
            return False
        fam, res = self.registry.families[family_id], snap.results[family_id]
        if res.status != BUILT or state.grants.get((fam.tenant, fam.purpose)) != res.grant_epoch:
            return False
        if evaluate_family(self.registry, family_id, state).cache_key != res.cache_key:
            return False  # a newer change superseded the published artifact
        if self.db.execute(
            "select 1 from claims where family_id=? and stale=1", (family_id,)
        ).fetchone():
            return False
        self.db.execute("insert or ignore into admissions values(?,?)", (family_id, snap.revision))
        return True

    def admission_count(self) -> int:
        return int(self.db.execute("select count(*) from admissions").fetchone()[0])

    def run(
        self,
        worker: str,
        now: int,
        crash_after: int | None = None,
        before_publish: Callable[[], None] | None = None,
        lookup: IndexLookup | None = None,
    ) -> RunReport:
        token = self.acquire_lease(worker, now)
        snap = self.snapshot()
        if snap is None:
            raise RecomputeError("NOT_BOOTSTRAPPED")
        state, cursor = self.source_state(), self.cursor()
        changed = self._registry_changed()
        plan = make_plan(
            self.registry, snap.results, snap.revision, self.pending_events(), state, cursor,
            self.max_fanout, lookup, changed,
        )  # fmt: skip
        built: dict[str, FamilyResult] = {}
        started = hits = 0
        for start in range(0, len(plan.dirty), self.batch_size):
            self._check_lease(token)
            with self._tx():  # one durable checkpoint per batch
                for fam_id in plan.dirty[start : start + self.batch_size]:
                    result = evaluate_family(self.registry, fam_id, state)
                    if result.status == BUILT:
                        seen = self.db.execute(
                            "select 1 from jobs where cache_key=?", (result.cache_key,)
                        )
                        if seen.fetchone():
                            hits += 1
                        else:  # the only place a generator job is admitted
                            self.db.execute(
                                "insert into jobs values(?,?,?)",
                                (result.cache_key, fam_id, canonical(result.to_json())),
                            )
                            started += 1
                    built[fam_id] = result
            if crash_after is not None and started + hits >= crash_after:
                raise SimulatedCrash
        if before_publish:
            before_publish()
        return RunReport(plan, started, hits, self._publish(plan, snap, built, token, changed))

    def _publish(
        self, plan: Plan, parent: Manifest, built: Mapping[str, FamilyResult], token: int,
        registry_changed: bool,
    ) -> Manifest:
        with self._tx():
            self._check_lease(token)  # inside the publish transaction: no check-then-act window
            state = self.source_state()
            if self.revision() != plan.parent_revision:
                raise PublishConflict("PARENT_MOVED")
            if self.cursor() != plan.cursor:
                raise PublishConflict("LATE_CHANGE")  # change after the checkpoint: replan
            results = {} if registry_changed else dict(parent.results)
            results.update(built)
            for fam_id, res in results.items():  # authority recheck at the publish point
                fam = self.registry.families[fam_id]
                if (
                    res.status == BUILT
                    and state.grants.get((fam.tenant, fam.purpose)) != res.grant_epoch
                ):
                    raise PublishConflict("AUTHORITY_MOVED")
            manifest = Manifest(parent.revision + 1, parent.revision, parent.semantic, results)
            self._save_snapshot(manifest)
            self._put_registry()
            self.db.execute(
                "update events set state='done' where state='pending' and cursor<=?", (plan.cursor,)
            )
            return manifest
