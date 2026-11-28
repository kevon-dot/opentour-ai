"""Mixed-source intake worker service: preview, scoped scan, normalize, seal, starter draft.

Simulated/not proven: this runs against generated sentinel sources through a local adapter and a
generated MCP transport. It proves the worker contract, not any installed customer connector,
provider permission semantics, OCR quality or the live OPE-505 benchmark worker.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from opentour_cell.pilot.mixed_intake import readers
from opentour_cell.pilot.mixed_intake.adapters import (
    ADAPTER_REVISIONS,
    AdapterError,
    AdapterUnavailable,
    ConnectorInfo,
    Resource,
    SourceAdapter,
    public_ref,
)
from opentour_cell.pilot.mixed_intake.model import (
    INVENTORY_VERSION,
    SCHEMA_VERSION,
    Deadline,
    ExtractionState,
    Json,
    Limits,
    NextActor,
    Occurrence,
    ReaderFailure,
    ReadResult,
    Role,
    Span,
    digest,
    sha256,
)
from opentour_cell.pilot.mixed_intake.store import IntakeStore

PURPOSES = frozenset({"policy", "historical_decision_traces", "independently_verified_outcomes"})
ACCEPTED_ROLES = frozenset(r.value for r in Role)
FaultHook = Callable[[str], None]


class IntakeError(Exception):
    """Fail-closed hold with an UPPER_SNAKE code and no object names.

    `link` may carry opaque refs (draft keys, run refs) that a recorded hold keeps, never names.
    """

    def __init__(self, code: str, link: Mapping[str, str | None] | None = None):
        super().__init__(code)
        self.code = code
        self.link = dict(link or {})


class DraftSink(Protocol):
    """Port to the OPE-505 workflow benchmark draft. Keys make both calls idempotent."""

    def create_draft(self, draft_key: str, payload: Json) -> None: ...

    def enqueue_job(self, job_key: str, draft_key: str) -> None: ...


@dataclass(frozen=True)
class Binding:
    """Session binding: tenant, workflow, roots, source roles, purpose and authority epochs."""

    tenant: str
    workflow_ref: str
    purpose: str
    selected_roots: tuple[str, ...]
    deselected: tuple[str, ...] = ()
    source_roles: Mapping[str, str] = field(default_factory=dict)
    rendition_grants: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    formula_attested: tuple[str, ...] = ()
    authority_epochs: Mapping[str, int] = field(default_factory=lambda: {"access": 1})
    required_strata: tuple[str, ...] = ()
    limits: Limits = field(default_factory=Limits)
    page_size: int = 3

    def validate(self) -> None:
        if (
            self.purpose not in PURPOSES
            or not self.tenant
            or not self.selected_roots
            or any(r not in ACCEPTED_ROLES for r in self.source_roles.values())
            or any(r not in ACCEPTED_ROLES for r in self.required_strata)
            or self.page_size < 1
        ):
            raise IntakeError("INVALID_BINDING")

    def as_json(self) -> Json:
        return {
            "tenant": self.tenant,
            "workflow_ref": self.workflow_ref,
            "purpose": self.purpose,
            "selected_roots": sorted(self.selected_roots),
            "deselected": sorted(self.deselected),
            "source_roles": dict(sorted(self.source_roles.items())),
            "rendition_grants": {k: sorted(v) for k, v in sorted(self.rendition_grants.items())},
            "formula_attested": sorted(self.formula_attested),
            "authority_epochs": dict(sorted(self.authority_epochs.items())),
            "required_strata": sorted(self.required_strata),
            "limits": self.limits.visible(),
            "page_size": self.page_size,
        }

    @classmethod
    def from_json(cls, v: Json) -> Binding:
        return cls(
            v["tenant"],
            v["workflow_ref"],
            v["purpose"],
            tuple(v["selected_roots"]),
            tuple(v["deselected"]),
            v["source_roles"],
            {k: tuple(x) for k, x in v["rendition_grants"].items()},
            tuple(v["formula_attested"]),
            v["authority_epochs"],
            tuple(v["required_strata"]),
            Limits(**v["limits"]),
            v["page_size"],
        )

    def digest(self) -> str:
        return digest(self.as_json())


class _Ctx:
    """Per-page working context. Mutations here become durable only when the page commits."""

    def __init__(
        self,
        sid: str,
        binding: Binding,
        state: Json,
        adapter: SourceAdapter,
        info: ConnectorInfo,
        store: IntakeStore,
        clock: Callable[[], str],
        dclock: Callable[[], float],
    ):
        self.sid, self.binding, self.state = sid, binding, state
        self.adapter, self.info, self.store = adapter, info, store
        self.clock, self.dclock = clock, dclock
        self.occurrences: list[Occurrence] = []
        self.touches: list[tuple[Occurrence, Resource]] = []
        self.enqueue: list[tuple[str, int, list[str]]] = []
        self.ledger: list[tuple[str, Json]] = []


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class MixedIntakeService:
    """The one worker service behind both the UI and MCP surfaces."""

    def __init__(
        self,
        store: IntakeStore,
        *,
        connectors: Mapping[str, Callable[[], SourceAdapter]] | None = None,
        sink: DraftSink | None = None,
        clock: Callable[[], str] = _now,
        deadline_clock: Callable[[], float] = time.monotonic,
    ):
        self.store = store
        self.connectors = dict(connectors or {})
        self.sink = sink
        self.clock = clock
        self.dclock = deadline_clock

    # ------------------------------------------------------------------ sessions

    def open_session(self, binding: Binding, adapter: SourceAdapter) -> str:
        binding.validate()
        info = adapter.describe()
        if info.tenant != binding.tenant:
            raise IntakeError("TENANT_MISMATCH")
        epochs = binding.authority_epochs
        if "provider_access" in epochs and info.access_epoch != epochs["provider_access"]:
            raise IntakeError("EPOCH_MISMATCH")
        sid = digest(["session", binding.digest(), info.connection])[:32]
        state: Json = {
            "connection": info.connection,
            "seeded": False,
            "pass": 0,
            "seen": {},
            "listed": [],
            "pass_seen": [],
            "withdrawn": [],
            "epochs": dict(binding.authority_epochs),
            "token": None,
            "pass_mode": "full",
        }
        with self.store.tx() as db:
            db.execute(
                "INSERT OR IGNORE INTO sessions(session_id, tenant, binding, binding_digest, "
                "status, state) VALUES (?,?,?,?,?,?)",
                (
                    sid,
                    binding.tenant,
                    json.dumps(binding.as_json()),
                    binding.digest(),
                    "open",
                    json.dumps(state),
                ),
            )
        return sid

    def _load(self, sid: str) -> tuple[Binding, Json, str, str | None]:
        with self.store.tx() as db:
            row = db.execute("SELECT * FROM sessions WHERE session_id=?", (sid,)).fetchone()
        if row is None:
            raise IntakeError("UNKNOWN_SESSION")
        return (
            Binding.from_json(json.loads(row["binding"])),
            json.loads(row["state"]),
            row["status"],
            row["fingerprint"],
        )

    def _save_state(
        self, db: sqlite3.Connection, sid: str, state: Json, status: str | None = None
    ) -> None:
        db.execute("UPDATE sessions SET state=? WHERE session_id=?", (json.dumps(state), sid))
        if status is not None:
            db.execute("UPDATE sessions SET status=? WHERE session_id=?", (status, sid))

    # ------------------------------------------------------------------ preview and trial

    def preview(self, binding: Binding, adapter: SourceAdapter) -> Json:
        """Metadata-only preview: scope, type support, proposed roles, volume, granted ops."""
        binding.validate()
        info = adapter.describe()
        if info.tenant != binding.tenant:
            raise IntakeError("TENANT_MISMATCH")
        before = adapter.counters.total_reads()
        rows: list[Json] = []
        denied = 0
        seen: set[str] = set()
        queue = [(root, 0) for root in binding.selected_roots]
        seen.update(binding.selected_roots)
        while queue:
            parent, depth = queue.pop(0)
            cursor: str | None = None
            while True:
                page = adapter.list_children(parent, cursor, binding.page_size)
                for res in page.resources:
                    if res.access != "granted":
                        denied += 1
                        continue
                    if res.id in binding.deselected:
                        rows.append(self._preview_row(res, binding, info, "deselected"))
                    elif res.kind == "folder":
                        if res.id not in seen and depth + 1 <= binding.limits.max_folder_depth:
                            seen.add(res.id)
                            queue.append((res.id, depth + 1))
                    elif res.kind == "shortcut":
                        rows.append(self._preview_row(res, binding, info, "shortcut_not_followed"))
                    else:
                        rows.append(self._preview_row(res, binding, info, "selected"))
                if page.next_cursor is None:
                    break
                cursor = page.next_cursor
        selected = [r for r in rows if r["selection"] == "selected"]
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "preview",
            "content_reads": adapter.counters.total_reads() - before,
            "capability_matrix": info.capabilities.matrix(),
            "limits_reported": info.capabilities.limits_reported(),
            "limits": binding.limits.visible(),
            "selected_roots": list(binding.selected_roots),
            "entries": sorted(rows, key=lambda r: r["native_id"]),
            "estimated_bytes": sum(r["size"] or 0 for r in selected),
            "estimated_objects": len(selected),
            "type_support": dict(Counter(r["support"] for r in selected)),
            "denied_children": denied,
            "granted_operations": [
                op for op, on in info.capabilities.matrix().items() if on and op != "snapshot_only"
            ],
        }

    @staticmethod
    def _preview_row(res: Resource, binding: Binding, info: ConnectorInfo, sel: str) -> Json:
        ext = readers.extension(res.name)
        granted = binding.rendition_grants.get(res.id, ())
        if res.kind == "native":
            ok = info.capabilities.rendition and any(
                o.format == "markdown" and "markdown" in granted for o in res.renditions
            )
            support = "supported_via_rendition" if ok else "needs_rendition"
        elif ext in {"md", "markdown", "txt", "csv", "jsonl", "ndjson", "docx", "xlsx", "pdf"}:
            support = "supported"
        else:
            support = "unverified_until_read"
        return {
            "native_id": res.id,
            "name": res.name,
            "kind": res.kind,
            "size": res.size,
            "revision": res.revision,
            "selection": sel,
            "support": support,
            "proposed_role": binding.source_roles.get(res.id, Role.UNKNOWN.value),
        }

    def trial(self, binding: Binding, adapter: SourceAdapter, max_items: int) -> Json:
        """Limited private trial: evaluate a few objects, retain and admit nothing."""
        binding.validate()
        info = adapter.describe()
        trial_state: Json = {"pass": 0, "withdrawn": [], "epochs": dict(binding.authority_epochs)}
        ctx = _Ctx(
            "trial", binding, trial_state, adapter, info, self.store, self.clock, self.dclock
        )
        results: list[Json] = []
        queue = list(binding.selected_roots)
        while queue and len(results) < max_items:
            parent = queue.pop(0)
            cursor: str | None = None
            while len(results) < max_items:
                page = adapter.list_children(parent, cursor, binding.page_size)
                for res in page.resources:
                    if res.kind == "folder" and res.id not in binding.deselected:
                        if res.access == "granted":
                            queue.append(res.id)
                    elif (
                        res.kind in ("file", "native")
                        and res.access == "granted"
                        and res.id not in binding.deselected
                        and len(results) < max_items
                    ):
                        occ = self._evaluate(ctx, res, [parent], parent)
                        results.append({"native_id": res.id, "state": occ.extraction_state})
                if page.next_cursor is None:
                    break
                cursor = page.next_cursor
        return {"kind": "trial", "committed": False, "results": results}

    # ------------------------------------------------------------------ scanning

    def run(
        self,
        sid: str,
        adapter: SourceAdapter,
        *,
        max_pages: int | None = None,
        fault: FaultHook | None = None,
        incremental: bool = False,
    ) -> Json:
        binding, state, _status, _fp = self._load(sid)
        info = adapter.describe()
        if info.tenant != binding.tenant:
            raise IntakeError("TENANT_MISMATCH")
        epochs = binding.authority_epochs
        if "provider_access" in epochs and info.access_epoch != epochs["provider_access"]:
            raise IntakeError("EPOCH_MISMATCH")
        ctx = _Ctx(sid, binding, state, adapter, info, self.store, self.clock, self.dclock)
        if incremental and info.capabilities.delta and state["token"] is not None:
            self._run_delta(ctx, fault)
            return self._finish(ctx, complete=True, pages=0, mode="delta")
        if not state["seeded"] or incremental:
            self._seed(ctx)
        processed = 0
        deadline = Deadline(binding.limits.max_run_seconds, self.dclock)
        while True:
            item = self._next_item(sid)
            if item is None:
                break
            if max_pages is not None and processed >= max_pages:
                return self._finish(ctx, complete=False, pages=processed, mode="full")
            try:
                deadline.check()
            except ReaderFailure:
                return self._finish(ctx, complete=False, pages=processed, mode="full")
            self._page(ctx, item, fault)
            processed += 1
        return self._finish(ctx, complete=True, pages=processed, mode="full")

    def run_route(self, sid: str, connection: str, **options: object) -> Json:
        """Run through a registered connector. An unavailable adapter never raises upward."""
        try:
            adapter = self.connectors[connection]()
            return self.run(sid, adapter, **options)  # type: ignore[arg-type]
        except (AdapterUnavailable, KeyError):
            with self.store.tx() as db:
                self.store.log(db, sid, "route_unavailable", {"connection": connection})
            return {
                "session_id": sid,
                "status": "adapter_unavailable",
                "complete": False,
                "base_starter_blocked": False,
            }

    def _seed(self, ctx: _Ctx) -> None:
        state = ctx.state
        state["pass"] += 1
        state.update(seeded=True, seen={}, listed=[], pass_seen=[], pass_mode="full")
        with self.store.tx() as db:
            db.execute("DELETE FROM frontier WHERE session_id=?", (ctx.sid,))
            for seq, root in enumerate(sorted(ctx.binding.selected_roots)):
                state["seen"][root] = [root]
                db.execute(
                    "INSERT INTO frontier(session_id, seq, parent_id, cursor, depth, ancestors) "
                    "VALUES (?,?,?,?,?,?)",
                    (ctx.sid, seq, root, "", 0, json.dumps([root])),
                )
            self._save_state(db, ctx.sid, state, "open")

    def _next_item(self, sid: str) -> Json | None:
        with self.store.tx() as db:
            row = db.execute(
                "SELECT * FROM frontier WHERE session_id=? AND done=0 ORDER BY seq LIMIT 1",
                (sid,),
            ).fetchone()
        return dict(row) if row else None

    def replay_page(
        self, sid: str, adapter: SourceAdapter, parent: str, cursor: str | None
    ) -> Json:
        """Re-deliver an already applied page. It must be a no-op."""
        binding, state, _s, _f = self._load(sid)
        key = digest([sid, state["pass"], parent, cursor or ""])
        with self.store.tx() as db:
            known = db.execute(
                "SELECT 1 FROM pages WHERE session_id=? AND page_key=?", (sid, key)
            ).fetchone()
            if known:
                self.store.log(db, sid, "page_replay_noop", {"page_key": key})
                return {"replayed": True, "admitted": 0}
        raise IntakeError("PAGE_NOT_APPLIED")

    def _page(self, ctx: _Ctx, item: Json, fault: FaultHook | None) -> None:
        state = ctx.state
        parent, cursor = item["parent_id"], item["cursor"]
        key = digest([ctx.sid, state["pass"], parent, cursor])
        with self.store.tx() as db:
            if db.execute(
                "SELECT 1 FROM pages WHERE session_id=? AND page_key=?", (ctx.sid, key)
            ).fetchone():
                db.execute(
                    "UPDATE frontier SET done=1 WHERE session_id=? AND parent_id=? AND cursor=?",
                    (ctx.sid, parent, cursor),
                )
                self.store.log(db, ctx.sid, "page_replay_noop", {"page_key": key})
                return
        page = ctx.adapter.list_children(parent, cursor or None, ctx.binding.page_size)
        ancestors: list[str] = json.loads(item["ancestors"])
        for res in page.resources:
            self._visit(ctx, res, parent, item["depth"], ancestors)
        with self.store.tx() as db:
            for occ in ctx.occurrences:
                self._admit(db, ctx, occ)
            for occ, res in ctx.touches:
                self._touch(db, ctx, occ, res)
        if fault:
            fault("after_page_admission")
        with self.store.tx() as db:
            for kind, payload in ctx.ledger:
                self.store.log(db, ctx.sid, kind, payload)
            seq = db.execute(
                "SELECT COALESCE(MAX(seq),0)+1 FROM frontier WHERE session_id=?", (ctx.sid,)
            ).fetchone()[0]
            if page.next_cursor is not None:
                db.execute(
                    "INSERT OR IGNORE INTO frontier(session_id, seq, parent_id, cursor, depth, "
                    "ancestors) VALUES (?,?,?,?,?,?)",
                    (ctx.sid, seq, parent, page.next_cursor, item["depth"], item["ancestors"]),
                )
                seq += 1
            for folder, depth, chain in ctx.enqueue:
                db.execute(
                    "INSERT OR IGNORE INTO frontier(session_id, seq, parent_id, cursor, depth, "
                    "ancestors) VALUES (?,?,?,?,?,?)",
                    (ctx.sid, seq, folder, "", depth, json.dumps(chain)),
                )
                seq += 1
            db.execute(
                "UPDATE frontier SET done=1 WHERE session_id=? AND parent_id=? AND cursor=?",
                (ctx.sid, parent, cursor),
            )
            summary = {"entries": len(page.resources), "admitted": len(ctx.occurrences)}
            db.execute(
                "INSERT INTO pages(session_id, page_key, summary) VALUES (?,?,?)",
                (ctx.sid, key, json.dumps(summary)),
            )
            self.store.log(
                db,
                ctx.sid,
                "cursor",
                {"parent": parent, "cursor": cursor, "next": page.next_cursor, **summary},
            )
            self._save_state(db, ctx.sid, state)
        ctx.occurrences, ctx.touches, ctx.enqueue, ctx.ledger = [], [], [], []

    # ------------------------------------------------------------------ visiting

    def _visit(
        self, ctx: _Ctx, res: Resource, parent: str, depth: int, ancestors: list[str]
    ) -> None:
        b, state = ctx.binding, ctx.state
        scoped_root = ancestors[0]
        if res.id not in state["listed"]:
            state["listed"].append(res.id)
        if res.access != "granted":
            ctx.occurrences.append(
                self._occ(
                    ctx,
                    res,
                    ancestors,
                    ExtractionState.DENIED,
                    "provider_denied",
                    NextActor.CUSTOMER_ADMIN,
                    "unknown",
                )
            )
            return
        if res.id in state["withdrawn"] or any(a in state["withdrawn"] for a in ancestors):
            # No admission, no record of the object: only an opaque counter survives.
            ctx.ledger.append(("withdrawn_scope_skipped", {}))
            return
        if res.id in b.deselected:
            ctx.occurrences.append(
                self._occ(
                    ctx,
                    res,
                    ancestors,
                    ExtractionState.DESELECTED,
                    "owner_deselected",
                    NextActor.NONE,
                    "unknown",
                )
            )
            return
        if res.kind == "folder":
            if res.id in state["seen"]:
                ctx.ledger.append(("cycle_edge", {"from": parent, "to": res.id}))
            elif depth + 1 > b.limits.max_folder_depth:
                ctx.occurrences.append(
                    self._occ(
                        ctx,
                        res,
                        ancestors,
                        ExtractionState.OVER_DEPTH,
                        "folder_depth",
                        NextActor.OWNER,
                        "folder",
                        limit="max_folder_depth",
                    )
                )
            else:
                chain = [*ancestors, res.id]
                state["seen"][res.id] = chain
                ctx.enqueue.append((res.id, depth + 1, chain))
            return
        if res.kind == "shortcut":
            target = res.target_id
            inside = target is not None and (
                target in state["seen"] or target in state["listed"] or target in b.selected_roots
            )
            if inside:
                ctx.ledger.append(("shortcut_edge", {"from": parent, "to": target}))
            else:
                ctx.occurrences.append(
                    self._occ(
                        ctx,
                        res,
                        ancestors,
                        ExtractionState.DENIED,
                        "shortcut_outside_grant",
                        NextActor.CUSTOMER_ADMIN,
                        "shortcut",
                    )
                )
            return
        # leaf object
        state["pass_seen"].append(res.id)
        existing = ctx.store.occurrence(ctx.sid, res.id, res.revision)
        if existing is not None and existing.eval_key == self._eval_key(ctx, res):
            if (existing.display_name, existing.path) != (res.name, res.path):
                ctx.touches.append((existing, res))
            ctx.ledger.append(("revisit_skipped", {"family": existing.family_id}))
            return
        ctx.occurrences.append(self._evaluate(ctx, res, ancestors, scoped_root))

    @staticmethod
    def _eval_key(ctx: _Ctx, res: Resource) -> str:
        b = ctx.binding
        return digest(
            [
                b.rendition_grants.get(res.id, ()),
                res.id in b.formula_attested,
                b.limits.visible(),
                b.source_roles.get(res.id, "unknown"),
                sorted(readers.READER_REVISIONS.items()),
            ]
        )

    def _occ(
        self,
        ctx: _Ctx,
        res: Resource,
        ancestors: list[str],
        state: ExtractionState,
        reason: str,
        actor: NextActor,
        fmt: str,
        *,
        data: bytes | None = None,
        result: ReadResult | None = None,
        parser: str = "none",
        limit: str | None = None,
        export: tuple[str, str] | None = None,
    ) -> Occurrence:
        b, info = ctx.binding, ctx.info
        restricted = state in (ExtractionState.DENIED, ExtractionState.WITHDRAWN)
        content = sha256(data) if data is not None else None
        scoped_root = ancestors[0] if ancestors else ""
        return Occurrence(
            occurrence_id=digest(["occ", b.tenant, info.connection, res.id, res.revision]),
            family_id=digest(["family", b.tenant, info.connection, res.id]),
            content_identity=(
                digest(["content", b.tenant, info.connection, scoped_root, content])
                if content and not restricted
                else None
            ),
            tenant=b.tenant,
            connection=info.connection,
            scoped_root=scoped_root,
            native_id=res.id,
            native_revision=res.revision,
            content_digest=None if restricted else content,
            parser_revision=parser,
            access_epoch=int(ctx.state["epochs"]["access"]),
            as_of=info.as_of,
            source_role=b.source_roles.get(res.id, Role.UNKNOWN.value),
            extraction_state=state.value,
            reason=reason,
            next_actor=actor.value,
            display_name=None if restricted else res.name,
            path=None if restricted else res.path,
            format=fmt,
            original_revision=res.original_revision,
            original_digest=res.original_digest,
            export_revision=export[0] if export else None,
            export_digest=export[1] if export else None,
            unavailable_spans=result.unavailable_spans if result else 0,
            notes=list(result.notes) if result else [],
            spans=[s.as_json() for s in result.spans] if result else [],
            limit=limit,
            ancestors=list(ancestors),
            eval_key=self._eval_key(ctx, res),
        )

    def _evaluate(
        self, ctx: _Ctx, res: Resource, ancestors: list[str], scoped_root: str
    ) -> Occurrence:
        b, info = ctx.binding, ctx.info
        limits = b.limits
        chain = ancestors if ancestors else [scoped_root]
        if res.size is not None and res.size > limits.max_object_bytes:
            return self._occ(
                ctx,
                res,
                chain,
                ExtractionState.OVERSIZED,
                "object_byte_limit",
                NextActor.OWNER,
                "unknown",
                limit="bytes",
            )
        granted = b.rendition_grants.get(res.id, ())
        offered = {o.format for o in res.renditions}
        try:
            if res.kind == "native":
                if info.capabilities.rendition and "markdown" in granted and "markdown" in offered:
                    rend = ctx.adapter.read_rendition(res.id, "markdown")
                    return self._parse(
                        ctx,
                        res,
                        chain,
                        rend.data,
                        "native-markdown.md",
                        export=(rend.revision, sha256(rend.data)),
                    )
                return self._occ(
                    ctx,
                    res,
                    chain,
                    ExtractionState.NEEDS_RENDITION,
                    "native_document_needs_authorized_rendition",
                    NextActor.OWNER,
                    "native",
                )
            data = ctx.adapter.read(res.id, res.revision)
        except AdapterError as error:
            return self._occ(
                ctx,
                res,
                chain,
                ExtractionState.CORRUPT,
                error.code.lower(),
                NextActor.PROVIDER,
                "unknown",
            )
        occ = self._parse(ctx, res, chain, data, res.name)
        if (
            occ.extraction_state == ExtractionState.NEEDS_RENDITION.value
            and occ.format == "pdf"
            and "ocr-text" in granted
            and "ocr-text" in offered
            and info.capabilities.rendition
        ):
            try:
                rend = ctx.adapter.read_rendition(res.id, "ocr-text")
            except AdapterError:
                return occ
            text = readers.decode_utf8(rend.data)
            spans = tuple(
                Span(
                    {"kind": "ocr", "line": n},
                    line,
                    None,
                    ("ocr_text_requires_review",),
                )
                for n, line in enumerate(text.splitlines(), start=1)
                if line.strip()
            )
            result = ReadResult(
                ExtractionState.NEEDS_REVIEW,
                spans,
                "ocr_rendition_requires_explicit_review",
                NextActor.OWNER,
            )
            return self._occ(
                ctx,
                res,
                chain,
                result.state,
                result.reason,
                result.next_actor,
                "ocr-text",
                data=data,
                result=result,
                parser=readers.READER_REVISIONS["ocr-text"],
                export=(rend.revision, sha256(rend.data)),
            )
        return occ

    def _parse(
        self,
        ctx: _Ctx,
        res: Resource,
        ancestors: list[str],
        data: bytes,
        name: str | None,
        *,
        export: tuple[str, str] | None = None,
    ) -> Occurrence:
        b = ctx.binding
        fmt = readers.sniff(data, name)
        label = {"cfb": "unknown", "zip": "zip"}.get(fmt, fmt)
        variant = "attested" if res.id in b.formula_attested else "plain"
        scope = ancestors[0] if ancestors else ""
        content = sha256(data)
        # Cached work is keyed by the reader revisions that produced it, so an upgraded reader
        # never inherits an older reader's disposition.
        revision = f"{label}|{digest(sorted(readers.READER_REVISIONS.items()))}"
        cache_key = (b.tenant, scope, content, revision, variant + (":r" if export else ""))
        row = None
        if ctx.sid != "trial":
            with self.store.tx() as db:
                row = db.execute(
                    "SELECT payload FROM parse_cache WHERE tenant=? AND scope=? "
                    "AND content_digest=? AND parser_revision=? AND variant=?",
                    cache_key,
                ).fetchone()
        if row is not None:
            payload = json.loads(row["payload"])
            result = ReadResult(
                ExtractionState(payload["state"]),
                tuple(Span.from_json(s) for s in payload["spans"]),
                payload["reason"],
                NextActor(payload["next_actor"]),
                tuple(payload["notes"]),
                payload["limit"],
            )
            fmt_out = payload["format"]
            ctx.ledger.append(("parse_reuse", {"identity_scope": scope, "digest": content[:12]}))
        else:
            deadline = Deadline(b.limits.max_seconds, ctx.dclock)
            fmt_out, result = readers.read_object(
                data, name, b.limits, deadline, attested_formulas=res.id in b.formula_attested
            )
            if result.state != ExtractionState.TIMED_OUT and ctx.sid != "trial":
                payload = {
                    "state": result.state.value,
                    "spans": [s.as_json() for s in result.spans],
                    "reason": result.reason,
                    "next_actor": result.next_actor.value,
                    "notes": list(result.notes),
                    "limit": result.limit,
                    "format": fmt_out,
                }
                with self.store.tx() as db:
                    db.execute(
                        "INSERT OR IGNORE INTO parse_cache VALUES (?,?,?,?,?,?)",
                        (*cache_key, json.dumps(payload)),
                    )
        parser = readers.READER_REVISIONS.get(fmt_out, "opentour.reader.none@1.0.0")
        if export:
            fmt_out = "native-markdown"
            parser = readers.READER_REVISIONS["native-markdown"]
        return self._occ(
            ctx,
            res,
            ancestors,
            result.state,
            result.reason,
            result.next_actor,
            fmt_out,
            data=data,
            result=result,
            parser=parser,
            limit=result.limit,
            export=export,
        )

    # ------------------------------------------------------------------ admission

    def _admit(self, db: sqlite3.Connection, ctx: _Ctx, occ: Occurrence) -> None:
        semantic = digest(occ.semantic())
        row = db.execute(
            "SELECT payload_digest, payload FROM occurrences WHERE session_id=? "
            "AND occurrence_id=?",
            (ctx.sid, occ.occurrence_id),
        ).fetchone()
        if row is not None:
            if row["payload_digest"] == semantic:
                self.store.log(
                    db, ctx.sid, "duplicate_admission_attempt", {"family": occ.family_id}
                )
                return
            old = Occurrence(**json.loads(row["payload"]))
            if old.eval_key != occ.eval_key:
                db.execute(
                    "UPDATE occurrences SET payload=?, payload_digest=? WHERE session_id=? "
                    "AND occurrence_id=?",
                    (json.dumps(occ.as_json()), semantic, ctx.sid, occ.occurrence_id),
                )
                self.store.log(db, ctx.sid, "re_evaluated", {"family": occ.family_id})
                return
            self.store.log(db, ctx.sid, "disputed_same_revision", {"family": occ.family_id})
            return
        db.execute(
            "INSERT INTO occurrences VALUES (?,?,?,?,?,?)",
            (
                ctx.sid,
                occ.occurrence_id,
                occ.native_id,
                occ.native_revision,
                json.dumps(occ.as_json()),
                semantic,
            ),
        )
        prior = db.execute(
            "SELECT o.native_id, o.revision FROM latest l JOIN occurrences o ON "
            "o.session_id=l.session_id AND o.occurrence_id=l.occurrence_id "
            "WHERE l.session_id=? AND l.native_id=?",
            (ctx.sid, occ.native_id),
        ).fetchone()
        db.execute(
            "INSERT OR REPLACE INTO latest VALUES (?,?,?)",
            (ctx.sid, occ.native_id, occ.occurrence_id),
        )
        self.store.log(
            db,
            ctx.sid,
            "admission",
            {
                "family": occ.family_id,
                "state": occ.extraction_state,
                "revision": occ.native_revision,
            },
        )
        if prior is not None and prior["revision"] != occ.native_revision:
            self._invalidate(db, ctx.sid, occ.native_id, prior["revision"], occ.native_revision)

    def _invalidate(
        self,
        db: sqlite3.Connection,
        sid: str,
        native_id: str,
        old: str,
        new: str | None,
        reason: str = "edit",
    ) -> list[str]:
        hit: list[str] = []
        for row in db.execute(
            "SELECT consumer_id, depends FROM consumers WHERE session_id=? AND valid=1", (sid,)
        ).fetchall():
            if [native_id, old] in json.loads(row["depends"]):
                hit.append(row["consumer_id"])
        for consumer in hit:
            db.execute(
                "UPDATE consumers SET valid=0 WHERE session_id=? AND consumer_id=?",
                (sid, consumer),
            )
        self.store.log(
            db,
            sid,
            "invalidation",
            {"native_id": native_id, "from": old, "to": new, "reason": reason, "consumers": hit},
        )
        return hit

    def _touch(self, db: sqlite3.Connection, ctx: _Ctx, occ: Occurrence, res: Resource) -> None:
        occ.display_name, occ.path = res.name, res.path
        db.execute(
            "UPDATE occurrences SET payload=? WHERE session_id=? AND occurrence_id=?",
            (json.dumps(occ.as_json()), ctx.sid, occ.occurrence_id),
        )
        self.store.log(db, ctx.sid, "rename", {"family": occ.family_id, "new_family": False})

    # ------------------------------------------------------------------ delta / seal

    def _run_delta(self, ctx: _Ctx, fault: FaultHook | None) -> None:
        state = ctx.state
        change = ctx.adapter.changes(state["token"])
        pending_ctx = ctx
        ancestors_of = state["seen"]
        for res in change.upserts:
            parent_chain = ancestors_of.get(res.parent_id or "")
            if parent_chain is None:
                pending_ctx.ledger.append(("out_of_scope_change_ignored", {}))
                continue
            self._visit(pending_ctx, res, res.parent_id or "", len(parent_chain) - 1, parent_chain)
        with self.store.tx() as db:
            for occ in ctx.occurrences:
                self._admit(db, ctx, occ)
            for occ, res in ctx.touches:
                self._touch(db, ctx, occ, res)
            known = {o.native_id: o for o in self.store.latest(ctx.sid)}
            for tomb in change.tombstones:
                prior = known.get(tomb.id)
                if prior is None:
                    continue
                gone = Occurrence(
                    **{
                        **prior.as_json(),
                        "extraction_state": "deleted",
                        "reason": "explicit_tombstone",
                        "spans": [],
                        "occurrence_id": digest(["tomb", prior.occurrence_id, tomb.revision]),
                    }
                )
                gone.native_revision = tomb.revision
                self._admit(db, ctx, gone)
                self._invalidate(db, ctx.sid, tomb.id, prior.native_revision, None, "deleted")
            for kind, payload in ctx.ledger:
                self.store.log(db, ctx.sid, kind, payload)
            state["token"] = change.next_token
            self._save_state(db, ctx.sid, state)
        ctx.occurrences, ctx.touches, ctx.ledger = [], [], []
        if fault:
            fault("after_delta_admission")

    def _finish(self, ctx: _Ctx, *, complete: bool, pages: int, mode: str) -> Json:
        state = ctx.state
        info = ctx.info
        status = "open"
        fingerprint: str | None = None
        if complete:
            if mode == "full":
                self._reconcile_absence(ctx)
                state["token"] = self._head_token(ctx)
            fingerprint = digest(
                {
                    "binding": ctx.binding.digest(),
                    "schema": SCHEMA_VERSION,
                    "capabilities": info.capabilities.matrix(),
                    "adapter": ADAPTER_REVISIONS.get(
                        "mcp-resource" if "mcp" in info.transport else "local-directory"
                    ),
                    "readers": sorted(readers.READER_REVISIONS.items()),
                    "map": self.semantic_manifest(ctx.sid),
                    "epochs": state["epochs"],
                }
            )
            status = "sealed"
        else:
            status = "incomplete"
        with self.store.tx() as db:
            db.execute(
                "UPDATE sessions SET fingerprint=? WHERE session_id=?", (fingerprint, ctx.sid)
            )
            self._save_state(db, ctx.sid, state, status)
        return {
            "session_id": ctx.sid,
            "status": status,
            "complete": complete,
            "pages_processed": pages,
            "mode": mode,
            "fingerprint": fingerprint,
        }

    def _head_token(self, ctx: _Ctx) -> str | None:
        if not ctx.info.capabilities.delta:
            return None
        try:
            return ctx.adapter.changes(None).next_token
        except AdapterError:
            return None

    def _reconcile_absence(self, ctx: _Ctx) -> None:
        """Declare disappearance only after a complete authorized listing of the same scope."""
        if ctx.state["pass"] < 2:
            return
        seen = set(ctx.state["pass_seen"])
        listed = set(ctx.state["listed"])
        with self.store.tx() as db:
            for occ in self.store.latest(ctx.sid):
                if occ.native_id in seen or occ.native_id in listed:
                    continue
                if occ.extraction_state in (
                    ExtractionState.DELETED.value,
                    ExtractionState.WITHDRAWN.value,
                ):
                    continue
                gone = Occurrence(
                    **{
                        **occ.as_json(),
                        "extraction_state": "deleted",
                        "reason": "absent_in_complete_authorized_reconciliation",
                        "spans": [],
                        "occurrence_id": digest(["absent", occ.occurrence_id, ctx.state["pass"]]),
                    }
                )
                self._admit(db, ctx, gone)
                self._invalidate(db, ctx.sid, occ.native_id, occ.native_revision, None, "absent")

    # ------------------------------------------------------------------ grants / consumers

    def withdraw_grant(self, sid: str, resource_id: str) -> Json:
        """Withdraw a grant: no new admissions, content and permission detail purged."""
        binding, state, _s, _f = self._load(sid)
        if resource_id not in state["withdrawn"]:
            state["withdrawn"].append(resource_id)
            state["epochs"]["access"] = int(state["epochs"]["access"]) + 1
        purged = 0
        with self.store.tx() as db:
            for occ in self.store.latest(sid):
                if occ.native_id != resource_id and resource_id not in occ.ancestors:
                    continue
                if occ.extraction_state == ExtractionState.WITHDRAWN.value:
                    continue
                redacted = Occurrence(
                    **{
                        **occ.as_json(),
                        "extraction_state": ExtractionState.WITHDRAWN.value,
                        "reason": "grant_withdrawn",
                        "spans": [],
                        "display_name": None,
                        "path": None,
                        "content_digest": None,
                        "content_identity": None,
                        "export_digest": None,
                        "original_digest": None,
                        "notes": [],
                        "access_epoch": state["epochs"]["access"],
                    }
                )
                db.execute(
                    "UPDATE occurrences SET payload=?, payload_digest=? WHERE session_id=? "
                    "AND occurrence_id=?",
                    (
                        json.dumps(redacted.as_json()),
                        digest(redacted.semantic()),
                        sid,
                        occ.occurrence_id,
                    ),
                )
                self._invalidate(db, sid, occ.native_id, occ.native_revision, None, "withdrawn")
                purged += 1
            db.execute(
                "DELETE FROM parse_cache WHERE tenant=? AND scope=?", (binding.tenant, resource_id)
            )
            self.store.log(
                db, sid, "grant_withdrawn", {"ref": public_ref(binding.tenant, resource_id)}
            )
            self._save_state(db, sid, state, "open")
        return {"withdrawn": True, "purged_occurrences": purged}

    def register_consumer(self, sid: str, consumer_id: str, depends: list[tuple[str, str]]) -> None:
        with self.store.tx() as db:
            db.execute(
                "INSERT OR REPLACE INTO consumers VALUES (?,?,?,1)",
                (sid, consumer_id, json.dumps([list(d) for d in depends])),
            )

    def consumer_validity(self, sid: str) -> dict[str, bool]:
        with self.store.tx() as db:
            rows = db.execute(
                "SELECT consumer_id, valid FROM consumers WHERE session_id=?", (sid,)
            ).fetchall()
        return {r["consumer_id"]: bool(r["valid"]) for r in rows}

    # ------------------------------------------------------------------ views

    def semantic_manifest(self, sid: str) -> list[Json]:
        return sorted(
            (o.semantic() for o in self.store.latest(sid)), key=lambda s: str(s["native_id"])
        )

    def counts(self, sid: str) -> Json:
        occs = self.store.latest(sid)
        eligible = [o for o in occs if o.extraction_state == ExtractionState.ELIGIBLE.value]
        ledger = self.store.ledger(sid)
        kinds = Counter(e["kind"] for e in ledger)
        lanes: dict[str, set[str]] = {}
        for o in eligible:
            lanes.setdefault(o.source_role, set()).add(str(o.content_identity))
        return {
            "occurrences": len(occs),
            "eligible_occurrences": len(eligible),
            "distinct_source_content_identities": len(
                {o.content_identity for o in eligible if o.content_identity}
            ),
            "independent_families_by_lane": {k: len(v) for k, v in sorted(lanes.items())},
            "per_state": dict(sorted(Counter(o.extraction_state for o in occs).items())),
            "unavailable_cells": sum(o.unavailable_spans for o in occs),
            "duplicate_admission_attempts": kinds["duplicate_admission_attempt"],
            "duplicate_admissions": len(self.store.all_occurrences(sid))
            - len({o.occurrence_id for o in self.store.all_occurrences(sid)}),
            "parse_reuse_hits": kinds["parse_reuse"],
            "page_replays_noop": kinds["page_replay_noop"],
            "cycle_edges": kinds["cycle_edge"],
            "revisits_skipped": kinds["revisit_skipped"],
        }

    def inventory(self, sid: str, view: str = "owner") -> Json:
        """`owner` is customer-local and names permitted items; `shared` carries no names."""
        binding, _state, status, fingerprint = self._load(sid)
        rows: list[Json] = []
        for o in self.store.latest(sid):
            restricted = o.extraction_state in ("denied", "withdrawn")
            ref = public_ref(o.tenant, o.native_id)
            row: Json = {
                "ref": ref if restricted or view == "shared" else o.native_id,
                "state": o.extraction_state,
                "reason": o.reason,
                "next_actor": o.next_actor,
                "role": o.source_role,
                "format": o.format,
            }
            if view == "owner":
                row.update(
                    family_id=o.family_id,
                    content_identity=o.content_identity,
                    content_digest=o.content_digest,
                    native_revision=o.native_revision,
                    parser_revision=o.parser_revision,
                    access_epoch=o.access_epoch,
                    as_of=o.as_of,
                    tenant=o.tenant,
                    connection=o.connection,
                    scoped_root=o.scoped_root,
                    name=o.display_name,
                    path=o.path,
                    original_revision=o.original_revision,
                    original_digest=o.original_digest,
                    export_revision=o.export_revision,
                    export_digest=o.export_digest,
                    lineage=[s["locator"] for s in o.spans],
                    limit=o.limit,
                )
            rows.append(row)
        rows.sort(key=lambda r: (str(r["ref"]), str(r["state"])))
        return {
            "schema_version": INVENTORY_VERSION,
            "view": view,
            "session_id": sid,
            "status": status,
            "fingerprint": fingerprint,
            "binding": {k: v for k, v in binding.as_json().items() if k == "purpose"},
            "counts": self.counts(sid),
            "items": rows,
            "limits": binding.limits.visible(),
        }

    def inspect_citation(self, sid: str, native_id: str, revision: str, locator: Json) -> Json:
        """Customer-local only: return the span text at the pinned revision, or fail closed."""
        occ = self.store.occurrence(sid, native_id, revision)
        if occ is None:
            raise IntakeError("REVISION_NOT_PINNED")
        if occ.extraction_state not in ("eligible", "needs_review"):
            raise IntakeError("CITATION_UNAVAILABLE")
        for span in occ.spans:
            if span["locator"] == locator:
                if not span["available"]:
                    raise IntakeError("CITATION_UNAVAILABLE")
                return {
                    "text": span["text"],
                    "data": span["data"],
                    "native_revision": occ.native_revision,
                    "content_digest": occ.content_digest,
                    "export_revision": occ.export_revision,
                    "export_digest": occ.export_digest,
                    "parser_revision": occ.parser_revision,
                    "family_id": occ.family_id,
                }
        raise IntakeError("LOCATOR_NOT_FOUND")

    # ------------------------------------------------------------------ starter / OPE-505

    def freeze_starter(self, sid: str, *, fault: FaultHook | None = None) -> Json:
        """Freeze a scoped starter from usable policy, with every exclusion visible."""
        binding, state, status, fingerprint = self._load(sid)
        if status != "sealed" or fingerprint is None:
            raise IntakeError("SCAN_INCOMPLETE")
        occs = self.store.latest(sid)
        eligible = [o for o in occs if o.extraction_state == "eligible"]
        policy = [o for o in eligible if o.source_role == Role.POLICY.value]
        if not policy:
            raise IntakeError("NO_USABLE_POLICY")
        present = {o.source_role for o in eligible}
        missing = sorted(set(binding.required_strata) - present)
        exclusions = [
            {
                "ref": public_ref(o.tenant, o.native_id)
                if o.extraction_state in ("denied", "withdrawn")
                else o.native_id,
                "state": o.extraction_state,
                "reason": o.reason,
                "next_actor": o.next_actor,
            }
            for o in occs
            if o.extraction_state != "eligible"
        ]
        included = sorted(
            (
                {
                    "family_id": o.family_id,
                    "native_id": o.native_id,
                    "revision": o.native_revision,
                    "content_identity": o.content_identity,
                    "role": o.source_role,
                }
                for o in eligible
            ),
            key=lambda i: str(i["native_id"]),
        )
        evidence = digest(
            [[i["family_id"], i["revision"], i["content_identity"], i["role"]] for i in included]
        )
        draft_key = digest(["ope505-draft", binding.tenant, binding.workflow_ref, evidence])
        payload = {
            "scope": "scoped_starter",
            "session_id": sid,
            "snapshot_fingerprint": fingerprint,
            "workflow_ref": binding.workflow_ref,
            "included": included,
            "exclusions": exclusions,
            "missing_strata": missing,
            "broader_claim_held": bool(missing),
            "hold_reason": "required_historical_stratum_missing_coverage_unknown"
            if missing
            else "",
        }
        with self.store.tx() as db:
            existing = db.execute("SELECT 1 FROM drafts WHERE draft_key=?", (draft_key,)).fetchone()
            prior = db.execute(
                "SELECT draft_key FROM drafts WHERE session_id=? ORDER BY rowid DESC LIMIT 1",
                (sid,),
            ).fetchone()
            supersedes = prior["draft_key"] if prior and not existing else None
            if not existing:
                db.execute(
                    "INSERT INTO drafts(draft_key, session_id, payload, supersedes) "
                    "VALUES (?,?,?,?)",
                    (draft_key, sid, json.dumps({**payload, "supersedes": supersedes}), supersedes),
                )
                self.store.log(db, sid, "draft_admitted", {"draft_key": draft_key})
        if fault:
            fault("after_intake_admission_before_dispatch")
        # A freeze dispatches only its own draft; other pending drafts never block or ride along.
        dispatched = int(self.dispatch_draft(draft_key))
        return {
            "draft_key": draft_key,
            "created": not existing,
            "supersedes": supersedes,
            "dispatched": dispatched,
            **{k: payload[k] for k in ("broader_claim_held", "missing_strata", "exclusions")},
        }

    def dispatch_draft(self, draft_key: str) -> bool:
        """Dispatch one admitted draft exactly once. A sink hold propagates as `IntakeError`."""
        if self.sink is None:
            return False
        with self.store.tx() as db:
            row = db.execute(
                "SELECT payload FROM drafts WHERE draft_key=? AND dispatched=0", (draft_key,)
            ).fetchone()
        if row is None:
            return False
        self.sink.create_draft(draft_key, json.loads(row["payload"]))
        job_key = digest(["ope505-job", draft_key])
        self.sink.enqueue_job(job_key, draft_key)
        with self.store.tx() as db:
            db.execute("UPDATE drafts SET dispatched=1 WHERE draft_key=?", (draft_key,))
            db.execute("INSERT OR IGNORE INTO jobs VALUES (?,?)", (job_key, draft_key))
        return True

    def dispatch_pending(self) -> int:
        """Dispatch admitted drafts exactly once per key. Safe to call after any crash.

        A held draft stays pending with its typed hold recorded; it never stops the others.
        """
        if self.sink is None:
            return 0
        with self.store.tx() as db:
            rows = db.execute(
                "SELECT draft_key, session_id FROM drafts WHERE dispatched=0 ORDER BY rowid"
            ).fetchall()
        sent = 0
        for row in rows:
            try:
                sent += self.dispatch_draft(row["draft_key"])
            except IntakeError as error:
                self.record_hold(row["session_id"], row["draft_key"], error)
        return sent

    def record_hold(self, sid: str, draft_key: str, error: IntakeError) -> None:
        """Record a dispatch hold once per distinct reason, keyed by the draft."""
        entry: Json = {"draft_key": draft_key, "reason": error.code, **error.link}
        with self.store.tx() as db:
            last = db.execute(
                "SELECT payload FROM ledger WHERE kind='dispatch_held' "
                "AND json_extract(payload, '$.draft_key')=? ORDER BY id DESC LIMIT 1",
                (draft_key,),
            ).fetchone()
            if last is None or json.loads(last["payload"]) != entry:
                self.store.log(db, sid, "dispatch_held", entry)

    # ------------------------------------------------------------------ surfaces

    def handle(self, tool: str, args: Json) -> Json:
        """One dispatch used verbatim by the UI and the MCP surface."""
        if tool == "intake.status":
            return self.inventory(str(args["session_id"]), "shared")
        if tool == "intake.inventory":
            return self.inventory(str(args["session_id"]), "owner")
        if tool == "intake.counts":
            return self.counts(str(args["session_id"]))
        if tool == "intake.citation":
            return self.inspect_citation(
                str(args["session_id"]),
                str(args["native_id"]),
                str(args["revision"]),
                args["locator"],
            )
        raise IntakeError("UNKNOWN_TOOL")
