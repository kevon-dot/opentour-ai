"""The one worker entry point for mixed-source intake, called by the UI route and the MCP tool.

`MixedIntakeWorker.call(actor, operation, arguments)` is the single entry. `mcp_dispatch` (served
over local stdio by `main`) and `ui_dispatch` (the route table the UI host mounts) are thin
wrappers that differ only in how they encode a result or a refusal.

Authority comes only from worker-held records: the private worker record selects the tenant,
the workflow, the actors and their roles, each connection and its grant (roots, owner-declared
roles, rendition grants, formula attestations and authority epochs). A caller may only choose
among granted connections, purposes and roots, and may only narrow the scope by deselecting.

OPE-505: a frozen scoped starter is dispatched through the production starter admission
(`BenchmarkIntake.submit_intake` with a `Starter` request) under the owner's sealed OPE-426
setup grant. The grant, not this module, names the policy resource and revision; a frozen
starter that does not contain exactly that eligible policy occurrence is held, not submitted.

Simulated/not proven: every result here is local software evidence on generated sentinel
sources. An `mcp-resource` connection runs over a generated connector transport supplied by the
launcher; no installed customer connector exists, so no native integration coverage is claimed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import stat
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from opentour_cell.pilot.intake_admission import DurableIntake
from opentour_cell.pilot.intake_local import LocalStarterAuthority
from opentour_cell.pilot.intake_mcp import AdmissionHold, Starter
from opentour_cell.pilot.intake_mcp import IntakeError as AdmissionError
from opentour_cell.pilot.mcp import serve_tools, tool_error, tool_result
from opentour_cell.pilot.mixed_intake.adapters import (
    AdapterError,
    AdapterUnavailable,
    LocalDirectoryAdapter,
    McpResourceAdapter,
    McpTransport,
    SourceAdapter,
    public_ref,
)
from opentour_cell.pilot.mixed_intake.model import Json, Limits, Role, digest
from opentour_cell.pilot.mixed_intake.service import (
    PURPOSES,
    Binding,
    FaultHook,
    IntakeError,
    MixedIntakeService,
)
from opentour_cell.pilot.mixed_intake.store import IntakeStore
from opentour_cell.pilot.onboarding_mcp import _read_private
from opentour_cell.pilot.workflow_benchmarks import BenchmarkIntake

RECORD_VERSION = "opentour.pilot.mixed-intake-worker.v1"
RECORD_FILE = "mixed-intake-worker.json"
STORE_FILE = "mixed-intake.sqlite3"
SERVER_NAME = "opentour-mixed-intake-v1"
MAX_RECORD_BYTES = 65536
MAX_LIST = 64
REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
ROLES = frozenset({"owner", "observer"})
KINDS = frozenset({"local-directory", "mcp-resource"})
GENERATED_TRANSPORT = "mcp-resource-generated-transport"
ADMITTED = "ope505_admitted"
# Stated on every result. Nothing this worker produces is a provider or customer witness.
EVIDENCE: Json = {
    "evidence_tier": "simulated_unwitnessed",
    "installed_connector_coverage": False,
}


class WorkerRefused(Exception):
    """A fail-closed refusal with an UPPER_SNAKE code that names no object."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _refuse(condition: object, code: str = "INVALID_WORKER_RECORD") -> None:
    if not condition:
        raise WorkerRefused(code)


def _ref(value: object, code: str = "INVALID_WORKER_RECORD") -> str:
    _refuse(type(value) is str and REF.fullmatch(value) is not None, code)
    assert type(value) is str
    return value


def _refs(value: object, code: str = "INVALID_WORKER_RECORD") -> tuple[str, ...]:
    _refuse(type(value) is list and len(value) <= MAX_LIST, code)
    assert type(value) is list
    items = tuple(_ref(item, code) for item in value)
    _refuse(len(set(items)) == len(items), code)
    return items


def _object(value: object, keys: set[str], code: str = "INVALID_WORKER_RECORD") -> Json:
    _refuse(type(value) is dict and set(value) == keys, code)
    assert type(value) is dict
    return value


def _absolute(value: object) -> Path:
    _refuse(type(value) is str and value.isprintable() and os.path.isabs(value))
    assert type(value) is str
    return Path(value)


# ------------------------------------------------------------------ worker-held record


@dataclass(frozen=True)
class ConnectionGrant:
    """What the owner granted for one connection. Callers can only narrow it."""

    kind: str
    manifest: Path | None
    root: Path | None
    purposes: tuple[str, ...]
    selected_roots: tuple[str, ...]
    deselected: tuple[str, ...]
    source_roles: Mapping[str, str]
    rendition_grants: Mapping[str, tuple[str, ...]]
    formula_attested: tuple[str, ...]
    authority_epochs: Mapping[str, int]
    required_strata: tuple[str, ...]
    page_size: int

    @classmethod
    def parse(cls, value: object) -> ConnectionGrant:
        keys = {
            "kind",
            "purposes",
            "selected_roots",
            "deselected",
            "source_roles",
            "rendition_grants",
            "formula_attested",
            "authority_epochs",
            "required_strata",
            "page_size",
        }
        _refuse(type(value) is dict)
        assert type(value) is dict
        kind = _ref(value.get("kind"))
        _refuse(kind in KINDS)
        local = kind == "local-directory"
        raw = _object(value, keys | ({"manifest", "root"} if local else set()))
        purposes = _refs(raw["purposes"])
        _refuse(purposes and set(purposes) <= PURPOSES)
        roots = _refs(raw["selected_roots"])
        _refuse(roots)
        roles = raw["source_roles"]
        _refuse(type(roles) is dict and len(roles) <= 4096)
        source_roles = {_ref(k): _ref(v) for k, v in roles.items()}
        _refuse(set(source_roles.values()) <= {r.value for r in Role})
        renditions = raw["rendition_grants"]
        _refuse(type(renditions) is dict and len(renditions) <= 4096)
        rendition_grants = {_ref(k): _refs(v) for k, v in renditions.items()}
        epochs = raw["authority_epochs"]
        _refuse(type(epochs) is dict and len(epochs) <= 8)
        authority_epochs: dict[str, int] = {}
        for name, epoch in epochs.items():
            _refuse(type(epoch) is int and epoch >= 0)
            authority_epochs[_ref(name)] = epoch
        strata = _refs(raw["required_strata"])
        _refuse(set(strata) <= {r.value for r in Role})
        page_size = raw["page_size"]
        _refuse(type(page_size) is int and 1 <= page_size <= 100)
        return cls(
            kind,
            _absolute(raw["manifest"]) if local else None,
            _absolute(raw["root"]) if local else None,
            purposes,
            roots,
            _refs(raw["deselected"]),
            source_roles,
            rendition_grants,
            _refs(raw["formula_attested"]),
            authority_epochs,
            strata,
            page_size,
        )


@dataclass(frozen=True)
class WorkerRecord:
    tenant: str
    workflow_ref: str
    actors: Mapping[str, str]
    connections: Mapping[str, ConnectionGrant]
    starter_selection: Path | None
    revision: str = field(default="")

    @classmethod
    def read(cls, path: Path) -> WorkerRecord:
        try:
            raw = _read_private(path, MAX_RECORD_BYTES)
        except (OSError, ValueError) as error:
            raise WorkerRefused("WORKER_RECORD_UNAVAILABLE") from error
        body = _object(
            raw,
            {
                "schema_version",
                "tenant",
                "workflow_ref",
                "actors",
                "connections",
                "starter_selection",
            },
        )
        _refuse(body["schema_version"] == RECORD_VERSION)
        actors = body["actors"]
        _refuse(type(actors) is dict and 0 < len(actors) <= MAX_LIST)
        roles = {_ref(actor): role for actor, role in actors.items()}
        _refuse(all(type(role) is str and role in ROLES for role in roles.values()))
        connections = body["connections"]
        _refuse(type(connections) is dict and 0 < len(connections) <= MAX_LIST)
        selection = body["starter_selection"]
        return cls(
            _ref(body["tenant"]),
            _ref(body["workflow_ref"]),
            roles,
            {_ref(ref): ConnectionGrant.parse(grant) for ref, grant in connections.items()},
            None if selection is None else _absolute(selection),
            digest(body),
        )


# ------------------------------------------------------------------ OPE-505 production sink


_ADMISSION_INDEXES = (
    "CREATE UNIQUE INDEX IF NOT EXISTS ope505_admitted_draft ON ledger("
    "json_extract(payload, '$.draft_key')) WHERE kind = 'ope505_admitted'",
    "CREATE UNIQUE INDEX IF NOT EXISTS ope505_admitted_run ON ledger("
    "json_extract(payload, '$.run_ref')) WHERE kind = 'ope505_admitted'",
)


def ensure_admission_indexes(store: IntakeStore) -> None:
    """One admission per draft and per OPE-505 run, enforced by the worker store itself.

    Additive and idempotent (`IF NOT EXISTS`), like the store's own schema. A store that
    already holds a duplicate admission fails to build the index and is refused.
    """
    try:
        with store.tx() as db:
            for statement in _ADMISSION_INDEXES:
                db.execute(statement)
    except sqlite3.IntegrityError as error:
        raise WorkerRefused("WORKER_STORE_INCONSISTENT") from error


def _admitted_rows(db: sqlite3.Connection) -> list[Json]:
    rows = db.execute("SELECT payload FROM ledger WHERE kind=? ORDER BY id", (ADMITTED,)).fetchall()
    return [json.loads(row["payload"]) for row in rows]


def _admitted_on(db: sqlite3.Connection, draft_key: str) -> Json | None:
    row = db.execute(
        "SELECT payload FROM ledger WHERE kind=? AND json_extract(payload, '$.draft_key')=?",
        (ADMITTED, draft_key),
    ).fetchone()
    return None if row is None else dict(json.loads(row["payload"]))


def _ancestors(db: sqlite3.Connection, draft_key: str) -> set[str]:
    """The local supersedes chain above `draft_key` (cycle-safe)."""
    seen: set[str] = set()
    current: str | None = draft_key
    while current is not None:
        row = db.execute("SELECT supersedes FROM drafts WHERE draft_key=?", (current,)).fetchone()
        current = None if row is None else row["supersedes"]
        if current is None or current in seen or current == draft_key:
            break
        seen.add(current)
    return seen


class StarterBenchmarkSink:
    """`DraftSink` bound to the production OPE-505 starter admission.

    `create_draft` submits one `Starter` intake through `BenchmarkIntake`, which durably admits
    the run and creates or reuses its versioned benchmark draft before any dispatch. The request
    values come from the owner's sealed setup grant; the frozen payload only has to contain that
    exact eligible policy occurrence, and the principal must be the draft's own tenant.

    Lineage follows the local supersedes chain only. A superseded draft is never dispatched.
    The sealed grant, not the draft, fixes the OPE-505 run identity (the grant pins
    `creation_ref` and both revisions), so one OPE-505 run is bound to one draft, and within one
    OPE-505 workflow a draft may only follow an admitted draft from its own supersedes chain:
    a successor under the same grant, or any draft outside that chain, is held before anything
    is submitted. A chain successor under a fresh owner grant becomes the next OPE-505 version,
    and is recorded only if OPE-505's own parent run is the chain's admitted run.

    The final checks and the ledger write run in one `BEGIN IMMEDIATE` transaction, backed by
    unique indexes, so concurrent dispatchers (the UI host and the stdio launcher on one
    directory) record at most one admission per draft and per run; the loser re-reads and is
    held, or reads the same admission when it dispatched the same draft. `enqueue_job` creates
    nothing: the admitted run is the OPE-505 job, and this call only confirms it was recorded.
    """

    def __init__(
        self,
        selection: Path,
        tenant: str,
        store: IntakeStore,
        *,
        check_current: Callable[[], None] | None = None,
    ):
        self.selection = selection
        self.tenant = tenant
        self.store = store
        self.check_current = check_current or (lambda: None)
        ensure_admission_indexes(store)

    def _open(self) -> tuple[LocalStarterAuthority, BenchmarkIntake]:
        # Reopened on every dispatch so withdrawal or expiry of the grant takes effect.
        authority = LocalStarterAuthority(self.selection)
        principal = authority.principal
        _refuse(
            type(principal.tenant) is str and principal.tenant == self.tenant,
            "OPE505_TENANT_MISMATCH",
        )
        with authority.guard(principal):
            admission = BenchmarkIntake(DurableIntake(self.selection.parent, authority))
        return authority, admission

    def admitted(self, draft_key: str) -> Json | None:
        with self.store.tx() as db:
            return _admitted_on(db, draft_key)

    def _draft_tenant(self, session_id: object) -> str | None:
        with self.store.tx() as db:
            row = db.execute(
                "SELECT tenant FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
        return None if row is None else str(row["tenant"])

    @staticmethod
    def _lineage(
        db: sqlite3.Connection,
        draft_key: str,
        identity: str,
        workflow: str,
        link: Json,
        run_ref: str | None = None,
    ) -> Json | None:
        """Hold unless `draft_key` may be admitted; return the admitted chain parent, if any."""
        superseded = db.execute(
            "SELECT 1 FROM drafts WHERE supersedes=? AND draft_key!=? LIMIT 1",
            (draft_key, draft_key),
        ).fetchone()
        if superseded is not None:
            raise IntakeError("DRAFT_SUPERSEDED")
        chain = _ancestors(db, draft_key)
        others = [e for e in _admitted_rows(db) if e["draft_key"] != draft_key]
        for entry in others:
            if entry.get("run_identity") == identity or entry["run_ref"] == run_ref:
                if entry["draft_key"] in chain:
                    raise IntakeError("OPE505_SUCCESSOR_NEEDS_NEW_SETUP_GRANT", link)
                raise IntakeError("OPE505_RUN_BOUND_TO_OTHER_DRAFT")
        latest = None
        for entry in others:
            if entry.get("workflow_identity") == workflow:
                latest = entry
        if latest is not None and latest["draft_key"] not in chain:
            # Never let a draft outside the chain become the next version of another's run.
            raise IntakeError("OPE505_RUN_BOUND_TO_OTHER_DRAFT")
        return latest

    def _current(self) -> None:
        try:
            self.check_current()
        except WorkerRefused as refused:
            raise IntakeError(refused.code) from refused

    def create_draft(self, draft_key: str, payload: Json) -> None:
        if self.admitted(draft_key) is not None:
            return  # recorded: a replay after a crash before the draft was marked dispatched
        predecessor = payload.get("supersedes")
        prior = self.admitted(predecessor) if type(predecessor) is str else None
        link: Json = {
            "predecessor_draft_key": predecessor if type(predecessor) is str else None,
            "predecessor_run_ref": prior["run_ref"] if prior else None,
        }
        try:
            _refuse(
                self._draft_tenant(payload["session_id"]) == self.tenant,
                "OPE505_TENANT_MISMATCH",
            )
            with self.store.tx() as db:
                superseded = db.execute(
                    "SELECT 1 FROM drafts WHERE supersedes=? LIMIT 1", (draft_key,)
                ).fetchone()
            _refuse(superseded is None, "DRAFT_SUPERSEDED")
            authority, admission = self._open()
            self._current()  # the record may have changed while the grant was opening
            principal = authority.principal
            with authority.setup_grant(principal) as grant:
                policy_ref, revision = grant.policy_resource_ref, grant.source_revision
                setup_revision, creation_ref = grant.setup_policy_revision, grant.creation_ref
            covered = any(
                item["role"] == Role.POLICY.value
                and item["native_id"] == policy_ref
                and item["revision"] == revision
                for item in payload["included"]
            )
            _refuse(covered, "OPE505_GRANT_DOES_NOT_COVER_POLICY")
            epoch = authority.selected.engagement_epoch
            workflow = digest(["ope505-starter-workflow", principal.tenant, epoch, creation_ref])
            identity = digest(
                [
                    "ope505-starter-run",
                    principal.tenant,
                    epoch,
                    policy_ref,
                    revision,
                    setup_revision,
                    creation_ref,
                ]
            )
            with self.store.tx() as db:
                self._lineage(db, draft_key, identity, workflow, link)
            request = Starter(
                policy_resource_ref=policy_ref,
                source_revision=revision,
                setup_policy_revision=setup_revision,
                # OPE-505 deduplicates by intent, so this key cannot split or merge runs. It
                # keeps retries of one draft on one OPE-505 key row and keeps distinct drafts on
                # distinct keys, so a reused key can never silently stand for another intent.
                idempotency_key="mixed-intake-" + draft_key[:48],
                creation_ref=creation_ref,
                workflow_ref=None,
            )
            receipt = admission.submit_intake(principal, request)
            if isinstance(receipt, AdmissionHold):
                raise WorkerRefused("OPE505_HOLD")
            status = admission.get_run(principal, receipt.run_ref)
            with admission.intake.admitted_work(principal, receipt.run_ref) as work:
                parent_run = work.benchmark_parent_run
        except WorkerRefused as refused:
            raise IntakeError(refused.code) from refused
        except (AdmissionError, OSError, sqlite3.Error, ValueError) as error:
            raise IntakeError("OPE505_UNAVAILABLE") from error
        benchmark = status.benchmark
        # One serialized transaction re-checks everything that a concurrent dispatcher, a record
        # change or OPE-505's own deduplication may have changed, then records the admission.
        with self.store.tx() as db:
            db.execute("BEGIN IMMEDIATE")
            self._current()
            if _admitted_on(db, draft_key) is not None:
                return  # another dispatcher of this same draft recorded it first
            parent = self._lineage(db, draft_key, identity, workflow, link, receipt.run_ref)
            expected = parent["run_ref"] if parent is not None else None
            if parent_run != expected:
                raise IntakeError("OPE505_LINEAGE_MISMATCH")
            self.store.log(
                db,
                str(payload["session_id"]),
                ADMITTED,
                {
                    "draft_key": draft_key,
                    "run_identity": identity,
                    "workflow_identity": workflow,
                    "run_ref": receipt.run_ref,
                    "correlation_ref": receipt.correlation_ref,
                    "run_state": receipt.state,
                    "workflow_ref": status.workflow_ref,
                    "benchmark_ref": benchmark.benchmark_ref if benchmark else None,
                    "benchmark_version": benchmark.version if benchmark else None,
                    "benchmark_state": benchmark.benchmark_state if benchmark else None,
                    **link,
                    "lineage_parent_draft_key": parent["draft_key"] if parent else None,
                    # OPE-505's own lineage field for the benchmark this run admitted.
                    "ope505_parent_run": parent_run,
                },
            )

    def enqueue_job(self, job_key: str, draft_key: str) -> None:
        if self.admitted(draft_key) is None:
            raise IntakeError("OPE505_ADMISSION_NOT_RECORDED")


# ------------------------------------------------------------------ the entry point


@dataclass(frozen=True)
class Operation:
    role: str
    required: frozenset[str]
    optional: frozenset[str] = frozenset()


SCOPE = frozenset({"connection_ref", "purpose"})
NARROW = frozenset({"roots", "deselect"})
SESSION = frozenset({"session_id"})
OPERATIONS: dict[str, Operation] = {
    "mixed_intake_preview": Operation("owner", SCOPE, NARROW),
    "mixed_intake_trial": Operation("owner", SCOPE | {"max_items"}, NARROW),
    "mixed_intake_open": Operation("owner", SCOPE, NARROW),
    "mixed_intake_run": Operation("owner", SESSION, frozenset({"max_pages", "incremental"})),
    "mixed_intake_status": Operation("observer", SESSION),
    "mixed_intake_inventory": Operation("owner", SESSION),
    "mixed_intake_citation": Operation("owner", SESSION | {"native_id", "revision", "locator"}),
    "mixed_intake_freeze_starter": Operation("owner", SESSION),
    "mixed_intake_triage_packet": Operation("owner", SESSION),
}


def _int(value: object, low: int, high: int) -> int:
    _refuse(type(value) is int and low <= value <= high, "INVALID_ARGUMENTS")
    assert type(value) is int
    return value


def _locator(value: object) -> Json:
    _refuse(type(value) is dict and 0 < len(value) <= 16, "INVALID_ARGUMENTS")
    assert type(value) is dict
    _refuse(all(type(key) is str for key in value), "INVALID_ARGUMENTS")
    try:
        encoded = json.dumps(value, allow_nan=False, sort_keys=True)
    except (TypeError, ValueError) as error:
        raise WorkerRefused("INVALID_ARGUMENTS") from error
    _refuse(len(encoded) <= 1024, "INVALID_ARGUMENTS")
    return value


class MixedIntakeWorker:
    """One private worker directory: its record, its customer-local store, its connections."""

    def __init__(
        self,
        directory: Path,
        *,
        transports: Mapping[str, Callable[[], McpTransport]] | None = None,
        fault: FaultHook | None = None,
    ):
        _refuse(directory.is_absolute(), "PRIVATE_DIRECTORY_REQUIRED")
        info = directory.lstat()
        _refuse(
            stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o077,
            "PRIVATE_DIRECTORY_REQUIRED",
        )
        self.directory = directory
        self.record_path = directory / RECORD_FILE
        WorkerRecord.read(self.record_path)  # refuse to start without a valid record
        self.store = IntakeStore(directory / STORE_FILE)
        ensure_admission_indexes(self.store)
        self.transports = dict(transports or {})
        self.fault = fault

    # -------------------------------------------------------------- authority

    def _authorize(self, actor: object, operation: object) -> tuple[WorkerRecord, Operation]:
        record = WorkerRecord.read(self.record_path)  # current authority on every call
        _refuse(type(actor) is str and actor in record.actors, "ACTOR_NOT_AUTHORIZED")
        assert type(actor) is str
        spec = OPERATIONS.get(operation) if type(operation) is str else None
        _refuse(spec is not None, "UNKNOWN_OPERATION")
        assert spec is not None
        role = record.actors[actor]
        _refuse(role == "owner" or spec.role == role, "OWNER_ROLE_REQUIRED")
        return record, spec

    @staticmethod
    def _binding(record: WorkerRecord, scope: Json) -> tuple[str, Binding]:
        connection = _ref(scope.get("connection_ref"), "INVALID_ARGUMENTS")
        grant = record.connections.get(connection)
        _refuse(grant is not None, "CONNECTION_NOT_GRANTED")
        assert grant is not None
        purpose = _ref(scope.get("purpose"), "INVALID_ARGUMENTS")
        _refuse(purpose in grant.purposes, "PURPOSE_NOT_GRANTED")
        roots = (
            _refs(scope["roots"], "INVALID_ARGUMENTS") if "roots" in scope else grant.selected_roots
        )
        _refuse(roots and set(roots) <= set(grant.selected_roots), "ROOT_NOT_GRANTED")
        narrowed = _refs(scope.get("deselect", []), "INVALID_ARGUMENTS")
        binding = Binding(
            tenant=record.tenant,
            workflow_ref=record.workflow_ref,
            purpose=purpose,
            selected_roots=roots,
            deselected=tuple(sorted({*grant.deselected, *narrowed})),
            source_roles=grant.source_roles,
            rendition_grants=grant.rendition_grants,
            formula_attested=grant.formula_attested,
            authority_epochs=grant.authority_epochs,
            required_strata=grant.required_strata,
            limits=Limits(),
            page_size=grant.page_size,
        )
        return connection, binding

    def _adapter(self, record: WorkerRecord, connection: str) -> SourceAdapter:
        grant = record.connections[connection]
        adapter: SourceAdapter
        if grant.kind == "local-directory":
            assert grant.manifest is not None and grant.root is not None
            adapter = LocalDirectoryAdapter(grant.manifest, grant.root)
        else:
            factory = self.transports.get(connection)
            if factory is None:
                raise AdapterUnavailable()
            adapter = McpResourceAdapter(factory(), connection=connection)
        info = adapter.describe()
        _refuse(
            type(info.tenant) is str
            and info.tenant == record.tenant
            and type(info.connection) is str
            and info.connection == connection,
            "CONNECTION_MISMATCH",
        )
        return adapter

    def _session(self, record: WorkerRecord, session_id: str) -> tuple[str, Binding]:
        opened = self.store.ledger(session_id, "worker_session")
        _refuse(opened, "UNKNOWN_SESSION")
        scope = opened[0]["scope"]
        connection, binding = self._binding(record, scope)
        # The grant is re-derived from the current record; a changed grant cannot be resumed.
        _refuse(binding.digest() == opened[0]["binding_digest"], "GRANT_CHANGED")
        return connection, binding

    def _sink(self, record: WorkerRecord) -> StarterBenchmarkSink | None:
        if record.starter_selection is None:
            return None

        def unchanged() -> None:
            # No lock covers the record file. This narrows the window to the instants between
            # this re-read and the sink's commit; it cannot close it.
            try:
                now = WorkerRecord.read(self.record_path)
            except WorkerRefused as refused:
                raise WorkerRefused("WORKER_RECORD_CHANGED") from refused
            _refuse(now.revision == record.revision, "WORKER_RECORD_CHANGED")

        return StarterBenchmarkSink(
            record.starter_selection, record.tenant, self.store, check_current=unchanged
        )

    # -------------------------------------------------------------- the single entry

    def call(self, actor: object, operation: object, arguments: object) -> Json:
        """The one entry point. The UI route and the MCP tool both call exactly this."""
        record, spec = self._authorize(actor, operation)
        assert type(operation) is str
        _refuse(type(arguments) is dict, "INVALID_ARGUMENTS")
        assert type(arguments) is dict
        keys = set(arguments)
        _refuse(spec.required <= keys <= spec.required | spec.optional, "INVALID_ARGUMENTS")
        service = MixedIntakeService(self.store)
        try:
            result = self._operate(record, service, operation, arguments)
        except AdapterUnavailable:
            result = {"status": "adapter_unavailable", "base_starter_blocked": False}
        except AdapterError as error:
            raise WorkerRefused(error.code.upper()) from error
        except IntakeError as error:
            raise WorkerRefused(error.code) from error
        return {"operation": operation, **result, **EVIDENCE}

    def _operate(
        self, record: WorkerRecord, service: MixedIntakeService, operation: str, args: Json
    ) -> Json:
        if operation in ("mixed_intake_preview", "mixed_intake_trial", "mixed_intake_open"):
            connection, binding = self._binding(record, args)
            adapter = self._adapter(record, connection)
            transport = {"connector_transport": adapter.describe().transport}
            if operation == "mixed_intake_preview":
                return {**service.preview(binding, adapter), **transport}
            if operation == "mixed_intake_trial":
                count = _int(args["max_items"], 1, 5)
                return {**service.trial(binding, adapter, count), **transport}
            session_id = service.open_session(binding, adapter)
            if not self.store.ledger(session_id, "worker_session"):
                scope = {k: args[k] for k in sorted(SCOPE | NARROW) if k in args}
                with self.store.tx() as db:
                    self.store.log(
                        db,
                        session_id,
                        "worker_session",
                        {"scope": scope, "binding_digest": binding.digest()},
                    )
            return {"session_id": session_id, **transport}
        session_id = _ref(args["session_id"], "INVALID_ARGUMENTS")
        connection, _current = self._session(record, session_id)
        if operation == "mixed_intake_run":
            adapter = self._adapter(record, connection)
            options: dict[str, Any] = {}
            if "max_pages" in args:
                options["max_pages"] = _int(args["max_pages"], 1, 10_000)
            if "incremental" in args:
                _refuse(type(args["incremental"]) is bool, "INVALID_ARGUMENTS")
                options["incremental"] = args["incremental"]
            summary = service.run(session_id, adapter, **options)
            return {**summary, "connector_transport": adapter.describe().transport}
        if operation == "mixed_intake_status":
            return service.handle("intake.status", {"session_id": session_id})
        if operation == "mixed_intake_inventory":
            return service.handle("intake.inventory", {"session_id": session_id})
        if operation == "mixed_intake_citation":
            return service.handle(
                "intake.citation",
                {
                    "session_id": session_id,
                    "native_id": _ref(args["native_id"], "INVALID_ARGUMENTS"),
                    "revision": _ref(args["revision"], "INVALID_ARGUMENTS"),
                    "locator": _locator(args["locator"]),
                },
            )
        if operation == "mixed_intake_freeze_starter":
            return self._freeze(service, session_id)
        return self._triage(service, session_id)

    def _freeze(self, service: MixedIntakeService, session_id: str) -> Json:
        # Admission is committed first; dispatch is a separate step that may be retried.
        frozen = service.freeze_starter(session_id, fault=self.fault)
        return {
            "session_id": session_id,
            "draft_key": frozen["draft_key"],
            "created": frozen["created"],
            "supersedes": frozen["supersedes"],
            "broader_claim_held": frozen["broader_claim_held"],
            "missing_strata": frozen["missing_strata"],
            "exclusions": frozen["exclusions"],
            # Only this session's own draft is dispatched, under its current authority.
            "ope505": self._dispatch(session_id, frozen["draft_key"]),
        }

    def _session_tenant(self, session_id: str) -> str | None:
        with self.store.tx() as db:
            row = db.execute(
                "SELECT tenant FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
        return None if row is None else str(row["tenant"])

    def _dispatch(self, session_id: str, draft_key: str) -> Json:
        """Submit one draft to OPE-505 under the current authority of `session_id`.

        The record is re-read immediately before submit: the session's connection grant and
        binding must still be current, its tenant must be the record's tenant, and the sink
        re-opens the owner's setup grant. A draft whose authority changed is held, not sent.
        """
        service = MixedIntakeService(self.store)
        try:
            record = WorkerRecord.read(self.record_path)
            self._session(record, session_id)
            _refuse(self._session_tenant(session_id) == record.tenant, "GRANT_CHANGED")
        except WorkerRefused as refused:
            hold = IntakeError(refused.code)
        else:
            sink = self._sink(record)
            if sink is None:
                return {"state": "not_configured", "reason": "starter_selection_absent"}
            try:
                MixedIntakeService(self.store, sink=sink).dispatch_draft(draft_key)
                admitted = sink.admitted(draft_key)
                if admitted is not None:
                    hidden = {"kind", "run_identity", "workflow_identity"}
                    return {
                        "state": "admitted",
                        **{k: v for k, v in admitted.items() if k not in hidden},
                    }
                hold = IntakeError("OPE505_ADMISSION_NOT_RECORDED")
            except IntakeError as error:
                hold = error
        service.record_hold(session_id, draft_key, hold)
        return {"state": "held", "reason": hold.code}

    def dispatch_pending(self) -> Json:
        """Background recovery: dispatch every pending draft that its own session still may.

        Each draft is re-validated against the current record and its own session's tenant;
        a held draft is skipped and recorded, and never stops the others.
        """
        with self.store.tx() as db:
            rows = db.execute(
                "SELECT draft_key, session_id FROM drafts WHERE dispatched=0 ORDER BY rowid"
            ).fetchall()
        admitted: list[str] = []
        held: list[Json] = []
        for row in rows:
            key = str(row["draft_key"])
            result = self._dispatch(str(row["session_id"]), key)
            if result["state"] == "admitted":
                admitted.append(key)
            elif result["state"] == "held":
                held.append({"draft_key": key, "reason": result["reason"]})
        return {"admitted": admitted, "held": held}

    def _triage(self, service: MixedIntakeService, session_id: str) -> Json:
        """Dispositions as the worker triaged them, ready for an independent reviewer."""
        inventory = service.inventory(session_id, "owner")
        rows = []
        for occ in self.store.latest(session_id):
            restricted = occ.extraction_state in ("denied", "withdrawn")
            rows.append(
                {
                    "ref": public_ref(occ.tenant, occ.native_id) if restricted else occ.native_id,
                    "format": occ.format,
                    "role": occ.source_role,
                    "disposition": occ.extraction_state,
                    "reason": occ.reason,
                    "next_actor": occ.next_actor,
                }
            )
        rows.sort(key=lambda row: str(row["ref"]))
        return {
            "kind": "triage_disposition_packet",
            "session_id": session_id,
            "status": inventory["status"],
            "fingerprint": inventory["fingerprint"],
            "dispositions": rows,
            # The worker never fills this in. Only a separately recorded independent reviewer can.
            "independent_comparison": {
                "status": "outstanding",
                "reviewer": None,
                "reviewer_dispositions": None,
                "agreement": None,
            },
        }


# ------------------------------------------------------------------ the two surfaces


_REF_SCHEMA = {"type": "string", "minLength": 1, "maxLength": 128, "pattern": REF.pattern}
_REFS_SCHEMA = {"type": "array", "maxItems": MAX_LIST, "items": _REF_SCHEMA}
_PROPERTY_SCHEMAS: Json = {
    "connection_ref": _REF_SCHEMA,
    "purpose": {"type": "string", "enum": sorted(PURPOSES)},
    "roots": _REFS_SCHEMA,
    "deselect": _REFS_SCHEMA,
    "max_items": {"type": "integer", "minimum": 1, "maximum": 5},
    "session_id": _REF_SCHEMA,
    "max_pages": {"type": "integer", "minimum": 1, "maximum": 10_000},
    "incremental": {"type": "boolean"},
    "native_id": _REF_SCHEMA,
    "revision": _REF_SCHEMA,
    "locator": {"type": "object", "maxProperties": 16},
}
_DESCRIPTIONS = {
    "mixed_intake_preview": "Preview a granted source scope without reading any content.",
    "mixed_intake_trial": "Evaluate a few objects privately; nothing is retained or admitted.",
    "mixed_intake_open": "Open an intake session bound to the granted scope.",
    "mixed_intake_run": "Scan the session's scope, page by page, resumably.",
    "mixed_intake_status": "Read the shared, name-free status of a session.",
    "mixed_intake_inventory": "Read the customer-local owner inventory of a session.",
    "mixed_intake_citation": "Inspect one cited span at its pinned revision.",
    "mixed_intake_freeze_starter": "Freeze a scoped starter and admit its OPE-505 benchmark.",
    "mixed_intake_triage_packet": "Export triage dispositions for an independent reviewer.",
}
READ_ONLY = frozenset(
    {
        "mixed_intake_preview",
        "mixed_intake_trial",
        "mixed_intake_status",
        "mixed_intake_inventory",
        "mixed_intake_citation",
        "mixed_intake_triage_packet",
    }
)
TOOLS = [
    {
        "name": name,
        "description": _DESCRIPTIONS[name],
        "inputSchema": {
            "$id": f"urn:opentour:pilot:mixed-intake:v1:{name}",
            "type": "object",
            "additionalProperties": False,
            "properties": {k: _PROPERTY_SCHEMAS[k] for k in sorted(op.required | op.optional)},
            "required": sorted(op.required),
        },
        "annotations": {
            "readOnlyHint": name in READ_ONLY,
            "idempotentHint": True,
            "destructiveHint": False,
            "openWorldHint": False,
        },
    }
    for name, op in OPERATIONS.items()
]
UI_ROUTES = {
    f"POST /v1/mixed-intake/{name.removeprefix('mixed_intake_').replace('_', '-')}": name
    for name in OPERATIONS
}
_HTTP_STATUS = {
    "INVALID_ARGUMENTS": 400,
    "UNKNOWN_ROUTE": 404,
    "UNKNOWN_SESSION": 404,
    "GRANT_CHANGED": 409,
}


def _resolve_actor(worker: MixedIntakeWorker, actor: object) -> str:
    """The trusted launcher or UI host selects the actor; it must be in the current record."""
    record = WorkerRecord.read(worker.record_path)
    _refuse(type(actor) is str and actor in record.actors, "ACTOR_NOT_AUTHORIZED")
    assert type(actor) is str
    return actor


def mcp_dispatch(worker: MixedIntakeWorker, actor: object) -> Callable[[str, Json], Json]:
    selected = _resolve_actor(worker, actor)

    def call(name: str, arguments: Json) -> Json:
        try:
            return tool_result(worker.call(selected, name, arguments))
        except WorkerRefused as refused:
            return tool_error(refused.code)
        except (OSError, sqlite3.Error):
            return tool_error("WORKER_UNAVAILABLE")

    return call


def ui_dispatch(
    worker: MixedIntakeWorker, actor: object
) -> Callable[[str, Json], tuple[int, Json]]:
    """The route contract the UI host mounts after it authenticates the actor."""
    selected = _resolve_actor(worker, actor)

    def route(request_line: str, arguments: Json) -> tuple[int, Json]:
        operation = UI_ROUTES.get(request_line) if type(request_line) is str else None
        if operation is None:
            return 404, {"error": "UNKNOWN_ROUTE"}
        try:
            return 200, worker.call(selected, operation, arguments)
        except WorkerRefused as refused:
            return _HTTP_STATUS.get(refused.code, 403), {"error": refused.code}
        except (OSError, sqlite3.Error):
            return 503, {"error": "WORKER_UNAVAILABLE"}

    return route


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--actor", required=True)
    args = parser.parse_args(argv)
    try:
        # No installed connector transport exists; an mcp-resource connection is unavailable.
        worker = MixedIntakeWorker(args.directory)
        call = mcp_dispatch(worker, args.actor)
    except (WorkerRefused, OSError, sqlite3.Error):
        print("private_mixed_intake_worker_unavailable", file=sys.stderr)
        return 2
    try:
        return serve_tools(SERVER_NAME, TOOLS, call, sys.stdin.buffer, sys.stdout.buffer)
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
