"""Actor-bound private review of cited guided maps, separately from source ownership."""

from __future__ import annotations

import hmac
import json
import os
import re
import sqlite3
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, dataclass, replace
from decimal import Decimal
from pathlib import Path
from typing import Any

from opentour_cell.pilot.guided_decision_map import (
    Condition,
    FieldKind,
    GuidedMap,
    guided_draft,
)
from opentour_cell.pilot.guided_policy import (
    GuidedPolicySource,
    PolicySnapshot,
    PolicySpan,
    _read_policy,
)
from opentour_cell.pilot.intake_local import (
    LocalStarterAuthority,
    StarterSelection,
    VerifiedStarterGrant,
)
from opentour_cell.pilot.intake_mcp import IntakeError, IntakePrincipal, require_ref
from opentour_cell.pilot.onboarding_mcp import _read_private
from opentour_cell.pilot.operating_point import canonical, digest
from opentour_cell.pilot.receipt_recovery import _open_db
from opentour_cell.pilot.training import _check_file

Json = dict[str, Any]
SELECTION = "opentour.pilot.map-review-selection.v1"
GRANT = "opentour.pilot.map-review-grant.v1"
_FILE = r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}\.(?:json|md|txt)"
_GRANT_KEYS = {
    "schema_version",
    "grant_ref",
    "tenant",
    "engagement_epoch",
    "workflow_ref",
    "starter_grant_revision",
    "policy_resource_ref",
    "source_revision",
    "owner_ref",
    "custodian_ref",
    "expires_at",
    "editors",
    "readers",
    "field_kinds",
    "actions",
}
_DISPOSITIONS = {"accepted", "corrected", "unresolved", "out_of_scope"}


def _require(ok: object, reason: str = "map_review_unavailable") -> None:
    if not ok:
        raise IntakeError(reason)


def citation_ref(span: PolicySpan) -> str:
    return "map-citation-" + digest(asdict(span))[:40]


@dataclass(frozen=True)
class BranchEdit:
    branch_ref: str
    disposition: str
    reason: str
    action: str | None = None
    conditions: tuple[Condition, ...] | None = None


@dataclass(frozen=True)
class MapSettingsEdit:
    trigger: str | None
    fallback_action: str | None
    reason: str


@dataclass(frozen=True)
class MapBranch:
    branch_ref: str
    conditions: tuple[Condition, ...]
    action: str | None
    citation: PolicySpan
    citation_ref: str
    disposition: str = "needs_review"
    reason: str | None = None
    actor_ref: str | None = None
    editor_ref: str | None = None
    source_support: str = "source"
    precedence: int | None = None
    fallback: str = "Unresolved when no supported rule matches"
    confirmed: bool = False


@dataclass(frozen=True)
class MapReviewSnapshot:
    workflow_ref: str
    source_revision: str
    source_digest: str
    map_revision: str
    review_revision: str
    owner_ref: str
    branches: tuple[MapBranch, ...]
    unresolved: tuple[PolicySpan, ...]
    question: str | None
    can_set_precedence: bool
    can_edit: bool
    confirmed_ref: str | None
    changed_branch_refs: tuple[str, ...]
    actions: tuple[str, ...]
    field_kinds: dict[str, str]
    version: int
    trigger: str = "Trigger has not been specified"
    precedence_citation: PolicySpan | None = None
    precedence_reason: str | None = None
    precedence_actor_ref: str | None = None
    fallback_action: str | None = None
    settings_reason: str | None = None
    settings_actor_ref: str | None = None
    settings_editor_ref: str | None = None
    settings_ref: str | None = None
    settings_support: str | None = None
    settings_trigger: str | None = None

    def expected_action(
        self, fields: Mapping[str, str | Decimal | None]
    ) -> tuple[str | None, tuple[str, ...]]:
        unknown = [r for r in self.branches if r.action is None and r.disposition != "out_of_scope"]
        if unknown:
            return None, tuple(r.citation_ref for r in unknown)
        matched = [
            r
            for r in self.branches
            if r.disposition != "out_of_scope" and all(c.matches(fields) for c in r.conditions)
        ]
        citations = tuple(r.citation_ref for r in matched)
        if not matched:
            if (
                self.confirmed_ref
                and self.fallback_action is not None
                and self.settings_ref
                and all(r.confirmed or r.disposition == "out_of_scope" for r in self.branches)
            ):
                return self.fallback_action, (self.settings_ref,)
            return None, citations
        if any(not r.confirmed for r in matched):
            return None, citations
        if all(r.precedence is not None for r in matched):
            chosen = min(matched, key=lambda r: r.precedence or 0)
            return chosen.action, (chosen.citation_ref,)
        actions = {r.action for r in matched}
        return (matched[0].action if len(actions) == 1 else None), citations


@dataclass(frozen=True)
class MapDiff:
    branch_ref: str
    before: MapBranch | None
    after: MapBranch | None


class StaleMapError(IntakeError):
    def __init__(
        self,
        current: MapReviewSnapshot,
        diff: tuple[MapDiff, ...],
        previous: MapReviewSnapshot | None = None,
    ):
        super().__init__("map_review_stale_revision")
        self.current, self.diff = current, diff
        self.previous = previous


@dataclass(frozen=True)
class _Context:
    capability: Json
    source: PolicySnapshot | None
    draft: GuidedMap | None
    can_edit: bool


class MapReviewService:
    def __init__(self, directory: Path, tenant: str, selection: Json):
        self.directory = directory.absolute()
        _require(self.directory.resolve(strict=True) == self.directory)
        self.tenant, self.selection = require_ref(tenant), selection
        _require(
            set(selection)
            == {
                "schema_version",
                "workflow_ref",
                "starter_selection_file",
                "policy_file",
                "grant_file",
            }
            and selection["schema_version"] == SELECTION
        )
        self.workflow_ref = require_ref(selection["workflow_ref"])
        for field in ("starter_selection_file", "policy_file", "grant_file"):
            _require(isinstance(selection[field], str) and re.fullmatch(_FILE, selection[field]))
        _require(
            len({selection[f] for f in ("starter_selection_file", "policy_file", "grant_file")})
            == 3
        )
        self.authority = LocalStarterAuthority(
            self.directory / selection["starter_selection_file"], read_only=True
        )
        _require(self.authority.selected.tenant == self.tenant)
        self.source = GuidedPolicySource(self.authority, self.directory / selection["policy_file"])
        self.path = self.directory / "map-reviews.sqlite3"
        self.scope = digest([tenant, self.authority.selected.engagement_epoch, self.workflow_ref])

    def _capability(self, actor: IntakePrincipal, starter: VerifiedStarterGrant) -> Json:
        _require(actor.tenant == self.tenant)
        _require(
            _read_private(self.directory / "map-review-selection.json", 8192) == self.selection
        )
        envelope = _read_private(self.directory / self.selection["grant_file"], 32768)
        _require(set(envelope) == {"grant", "seal_sha256"})
        cap, seal = envelope["grant"], envelope["seal_sha256"]
        _require(isinstance(cap, dict) and set(cap) == _GRANT_KEYS)
        _require(isinstance(seal, str) and re.fullmatch(r"[0-9a-f]{64}", seal))
        key = _read_private(self.directory / "starter-intake-key.json", 4096)["key"]
        _require(
            hmac.compare_digest(
                seal, hmac.digest(bytes.fromhex(key), canonical(cap).encode(), "sha256").hex()
            )
        )
        _require(cap["schema_version"] == GRANT)
        selected = self.authority.selected
        _require(
            (
                cap["tenant"],
                cap["engagement_epoch"],
                cap["workflow_ref"],
                cap["starter_grant_revision"],
                cap["policy_resource_ref"],
                cap["source_revision"],
                cap["owner_ref"],
                cap["custodian_ref"],
            )
            == (
                self.tenant,
                selected.engagement_epoch,
                self.workflow_ref,
                starter.revision,
                starter.policy_resource_ref,
                starter.source_revision,
                selected.owner_id,
                starter.custodian_id,
            )
        )
        require_ref(cap["grant_ref"])
        _require(
            not os.path.lexists(self.directory / f"map-review-withdrawn-{cap['grant_ref']}.json")
        )
        _require(type(cap["expires_at"]) is int and int(time.time()) < cap["expires_at"] < 2**63)
        editors, readers = cap["editors"], cap["readers"]
        _require(
            isinstance(editors, dict)
            and 0 < len(editors) <= 32
            and isinstance(readers, list)
            and len(readers) <= 64
        )
        for person, ref in editors.items():
            require_ref(person)
            require_ref(ref)
        for person in readers:
            require_ref(person)
        _require(len(set(readers)) == len(readers))
        _require(actor.actor in editors or actor.actor in readers)
        fields, actions = cap["field_kinds"], cap["actions"]
        _require(
            isinstance(fields, dict)
            and 0 < len(fields) <= 32
            and isinstance(actions, list)
            and 0 < len(actions) <= 64
        )
        for name, kind in fields.items():
            _require(isinstance(name, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name))
            FieldKind(kind)
        _require(
            all(isinstance(a, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,47}", a) for a in actions)
            and len(set(actions)) == len(actions)
        )
        return dict(cap)

    @contextmanager
    def _guard(
        self, actor: IntakePrincipal, *, write: bool = False, read_source: bool = True
    ) -> Iterator[_Context]:
        admitted = self._capability(actor, self.authority._read_grant())
        _require(not write or actor.actor in admitted["editors"], "map_review_read_only")
        with (
            _open_db(self.directory / "training.sqlite3") if not write else nullcontext(),
            self.authority.setup_grant(self.authority.principal, read_only=not write) as starter,
        ):
            cap = self._capability(actor, starter)
            _require(cap == admitted, "map_review_permission_changed")
            editable = actor.actor in cap["editors"]
            _require(not write or editable, "map_review_read_only")
            self.source._require_workflow(self.workflow_ref, starter)
            _require(
                self.authority._read_grant() == starter
                and self._capability(actor, starter) == cap
                and time.time() < starter.expires_at,
                "map_review_permission_changed",
            )
            source = _read_policy(self.source.path, starter) if read_source else None
            draft = (
                guided_draft(
                    source,
                    {k: FieldKind(v) for k, v in cap["field_kinds"].items()},
                    frozenset(cap["actions"]),
                )
                if source
                else None
            )
            if draft is not None:
                _require(
                    len({b.branch_ref for b in draft.branches}) == len(draft.branches),
                    "ambiguous_map_branches",
                )
            yield _Context(cap, source, draft, editable)
            _require(self._capability(actor, starter) == cap, "map_review_permission_changed")
            _require(
                self.authority._read_grant() == starter
                and time.time() < starter.expires_at
                and StarterSelection.read(self.authority.path) == self.authority.selected,
                "map_review_permission_changed",
            )
            if source is not None:
                _require(
                    _read_policy(self.source.path, starter) == source, "map_review_source_changed"
                )

    def _current(self, actor: IntakePrincipal, ctx: _Context) -> None:
        starter = self.authority._read_grant()
        _require(
            self._capability(actor, starter) == ctx.capability
            and time.time() < starter.expires_at
            and StarterSelection.read(self.authority.path) == self.authority.selected,
            "map_review_permission_changed",
        )
        if ctx.source is not None:
            _require(
                _read_policy(self.source.path, starter) == ctx.source, "map_review_source_changed"
            )

    def entry(self, actor: IntakePrincipal) -> str | None:
        try:
            with self._guard(actor, read_source=False) as ctx:
                return digest([self.scope, actor.actor, ctx.capability])
        except (ValueError, OSError, sqlite3.Error, KeyError, TypeError):
            return None

    @contextmanager
    def _db(
        self, *, write: bool = False, before_commit: Callable[[], None] | None = None
    ) -> Iterator[sqlite3.Connection | None]:
        if not write:
            with _open_db(self.path) as db:
                yield db
            return
        try:
            os.close(
                os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            )
        except FileExistsError:
            pass
        _check_file(self.path)
        db = sqlite3.connect(
            self.path.as_uri() + "?mode=rw", uri=True, timeout=10, isolation_level=None
        )
        try:
            db.execute("PRAGMA trusted_schema=OFF")
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "CREATE TABLE IF NOT EXISTS maps (scope TEXT,version INTEGER,ref TEXT UNIQUE,"
                "body TEXT,seal TEXT,PRIMARY KEY(scope,version))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS operations (scope TEXT,actor TEXT,operation TEXT,"
                "intent TEXT,ref TEXT,PRIMARY KEY(scope,actor,operation))"
            )
            db.execute("CREATE TABLE IF NOT EXISTS heads (scope TEXT PRIMARY KEY,ref TEXT)")
            yield db
            if before_commit is not None:
                before_commit()
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _records(self, db: sqlite3.Connection | None) -> list[Json]:
        if db is None:
            return []
        required = {"maps", "operations", "heads"}
        present = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not present:
            # A rejected first save may leave an empty file after its DDL rolls back.
            return []
        _require(required <= present, "map_review_receipt_invalid")
        rows = db.execute(
            "SELECT version,ref,body,seal FROM maps WHERE scope=? ORDER BY version", (self.scope,)
        ).fetchall()
        result = []
        for version, ref, raw, seal in rows:
            body = json.loads(raw)
            _require(
                digest(body) == seal
                and ref == "map-review-" + seal[:40]
                and body["version"] == version
                and body["scope"] == self.scope,
                "map_review_receipt_invalid",
            )
            result.append(body | {"ref": ref})
        head = db.execute("SELECT ref FROM heads WHERE scope=?", (self.scope,)).fetchone()
        _require(
            (not result and head is None)
            or (result and head is not None and head[0] == result[-1]["ref"]),
            "map_review_receipt_invalid",
        )
        refs = {r["ref"] for r in result}
        _require(
            all(
                r[0] in refs
                for r in db.execute("SELECT ref FROM operations WHERE scope=?", (self.scope,))
            ),
            "map_review_receipt_invalid",
        )
        return result

    @staticmethod
    def _row(value: Json) -> MapBranch:
        row = dict(value)
        row["conditions"] = tuple(Condition(**c) for c in row["conditions"])
        row["citation"] = PolicySpan(**row["citation"])
        return MapBranch(**row)

    @staticmethod
    def _base(ctx: _Context) -> tuple[MapBranch, ...]:
        assert ctx.draft is not None
        rows = [
            MapBranch(b.branch_ref, b.conditions, b.action, b.citation, citation_ref(b.citation))
            for b in ctx.draft.branches
        ]
        rows.extend(
            MapBranch(
                "unresolved-" + digest(asdict(s))[:32],
                (),
                None,
                s,
                citation_ref(s),
                disposition="unresolved",
            )
            for s in ctx.draft.unresolved
        )
        return tuple(rows)

    @staticmethod
    def _same_source(record: Json, ctx: _Context) -> bool:
        return bool(
            record["source_revision"] == ctx.capability["source_revision"]
            and record["starter_grant_revision"] == ctx.capability["starter_grant_revision"]
            and record["schema_digest"]
            == digest([ctx.capability["field_kinds"], ctx.capability["actions"]])
        )

    @staticmethod
    def _overlap(left: MapBranch, right: MapBranch) -> bool:
        by_field: dict[str, list[Condition]] = {}
        for condition in (*left.conditions, *right.conditions):
            by_field.setdefault(condition.field, []).append(condition)
        for conditions in by_field.values():
            if any(c.operator == "missing" for c in conditions):
                if any(c.operator != "missing" for c in conditions):
                    return False
                continue
            lows = [
                (Decimal(c.threshold), c.operator == ">=")
                for c in conditions
                if c.operator in {">", ">="} and c.threshold is not None
            ]
            highs = [
                (Decimal(c.threshold), c.operator == "<=")
                for c in conditions
                if c.operator in {"<", "<="} and c.threshold is not None
            ]
            if lows and highs:
                low = max(v for v, _ in lows)
                high = min(v for v, _ in highs)
                if low > high or (
                    low == high
                    and (
                        any(v == low and not inclusive for v, inclusive in lows)
                        or any(v == high and not inclusive for v, inclusive in highs)
                    )
                ):
                    return False
        return True

    def _snapshot(
        self, actor: IntakePrincipal, ctx: _Context, record: Json | None
    ) -> MapReviewSnapshot:
        assert ctx.source is not None and ctx.draft is not None
        same = record is not None and self._same_source(record, ctx)
        rows = (
            tuple(self._row(r) for r in record["branches"]) if same and record else self._base(ctx)
        )
        changed = ()
        if record is not None and not same:
            old = {r["branch_ref"]: self._row(r) for r in record["branches"]}
            inherited = []
            affected = set(old) - {r.branch_ref for r in rows}
            for row in rows:
                before = old.get(row.branch_ref)
                if (
                    before is not None
                    and record["owner_ref"] == ctx.capability["owner_ref"]
                    and before.source_support == "source"
                    and before.conditions == row.conditions
                    and before.action == row.action
                    and before.citation.passage_sha256 == row.citation.passage_sha256
                ):
                    row = replace(
                        row,
                        disposition=before.disposition,
                        reason=before.reason,
                        actor_ref=before.actor_ref,
                        editor_ref=before.editor_ref,
                    )
                else:
                    affected.add(row.branch_ref)
                inherited.append(row)
            rows = tuple(inherited)
            changed = tuple(sorted(affected))
        settings = record.get("settings") if same and record else None
        fallback = settings["fallback_action"] if settings else None
        rows = tuple(
            replace(
                r,
                fallback="Owner-asserted fallback: " + fallback
                if fallback is not None
                else "Unresolved when no supported rule matches",
            )
            for r in rows
        )
        precedence = record["precedence"] if same and record else None
        if precedence is None and ctx.draft.first_match_clause is not None:
            precedence = [b.branch_ref for b in rows if b.action is not None]
        rows = tuple(
            replace(
                r,
                precedence=precedence.index(r.branch_ref) + 1
                if precedence and r.branch_ref in precedence
                else None,
                confirmed=r.disposition in {"accepted", "corrected"},
            )
            for r in rows
        )
        if record is not None and not same:
            prior_rows = tuple(self._row(r) for r in record["branches"])
            if record["precedence"] is not None:
                prior_rows = tuple(
                    replace(
                        r,
                        precedence=record["precedence"].index(r.branch_ref) + 1
                        if r.branch_ref in record["precedence"]
                        else None,
                    )
                    for r in prior_rows
                )
            prior_by_ref = {r.branch_ref: r for r in prior_rows}
            affected = set(changed)
            for row in rows:
                before = prior_by_ref.get(row.branch_ref)
                if before is not None and self._blocking_rules(
                    before, prior_rows
                ) != self._blocking_rules(row, rows):
                    affected.add(row.branch_ref)
            rows = tuple(
                replace(
                    r,
                    disposition="needs_review",
                    reason=None,
                    actor_ref=None,
                    editor_ref=None,
                    confirmed=False,
                )
                if r.branch_ref in affected and r.disposition != "out_of_scope"
                else r
                for r in rows
            )
            changed = tuple(sorted(affected))
        active = [r for r in rows if r.action is not None and r.disposition != "out_of_scope"]
        conflict = not precedence and any(
            a.action != b.action and self._overlap(a, b)
            for i, a in enumerate(active)
            for b in active[i + 1 :]
        )
        unknown = any(r.action is None and r.disposition != "out_of_scope" for r in rows)
        unresolved = tuple(
            r.citation for r in rows if r.disposition in {"unresolved", "needs_review"}
        )
        confirmed = (
            record["ref"]
            if same
            and record
            and not conflict
            and not unknown
            and any(r.confirmed for r in rows)
            and all(r.disposition != "needs_review" for r in rows)
            else None
        )
        token = digest(
            [
                self.scope,
                actor.actor,
                ctx.capability,
                ctx.source.normalized_sha256,
                ctx.draft.revision,
                record["ref"] if record else None,
            ]
        )
        return MapReviewSnapshot(
            self.workflow_ref,
            ctx.source.source_revision,
            ctx.source.normalized_sha256,
            ctx.draft.revision,
            token,
            ctx.capability["owner_ref"],
            rows,
            unresolved,
            "Which matching branch takes precedence?"
            if conflict
            else "Clarify or leave the unsupported clause unresolved."
            if unknown
            else None,
            bool(conflict),
            ctx.can_edit,
            confirmed,
            changed,
            tuple(ctx.capability["actions"]),
            dict(ctx.capability["field_kinds"]),
            record["version"] if record else 0,
            trigger=settings["trigger"]
            if settings and settings["trigger"] is not None
            else "Trigger has not been specified",
            precedence_citation=ctx.draft.first_match_clause
            if not (same and record and record["precedence"] is not None)
            else None,
            precedence_reason=record["precedence_reason"] if same and record else None,
            precedence_actor_ref=record["precedence_actor_ref"] if same and record else None,
            fallback_action=fallback,
            settings_reason=settings["reason"] if settings else None,
            settings_actor_ref=settings["actor_ref"] if settings else None,
            settings_editor_ref=settings["editor_ref"] if settings else None,
            settings_ref="map-assertion-" + digest(settings)[:40] if settings else None,
            settings_support="owner_assertion" if settings else None,
            settings_trigger=settings["trigger"] if settings else None,
        )

    def _blocking_rules(self, row: MapBranch, rows: tuple[MapBranch, ...]) -> set[str]:
        """Rules that can change this branch's action domain, ignoring disjoint rank changes."""
        result = set()
        for other in rows:
            if (
                other.branch_ref == row.branch_ref
                or other.disposition == "out_of_scope"
                or other.action is None
                or other.action == row.action
                or not self._overlap(row, other)
            ):
                continue
            if row.precedence is None or other.precedence is None:
                relation = "unordered"
            elif other.precedence < row.precedence:
                relation = "earlier"
            else:
                continue
            result.add(
                digest(
                    [
                        other.branch_ref,
                        [asdict(c) for c in other.conditions],
                        other.action,
                        relation,
                    ]
                )
            )
        return result

    def preview(self, actor: IntakePrincipal) -> MapReviewSnapshot:
        with self._guard(actor) as ctx, self._db() as db:
            records = self._records(db)
            return self._snapshot(actor, ctx, records[-1] if records else None)

    @contextmanager
    def disclosure(
        self, actor: IntakePrincipal, *, expected_revision: str
    ) -> Iterator[MapReviewSnapshot]:
        with self._guard(actor) as ctx, self._db() as db:
            records = self._records(db)
            current = self._snapshot(actor, ctx, records[-1] if records else None)
            _require(current.review_revision == expected_revision, "map_review_stale_revision")
            self._current(actor, ctx)
            yield current

    def citation(self, actor: IntakePrincipal, *, expected_revision: str, citation_ref: str) -> str:
        with self._guard(actor) as ctx, self._db() as db:
            records = self._records(db)
            current = self._snapshot(actor, ctx, records[-1] if records else None)
            _require(current.review_revision == expected_revision, "map_review_stale_revision")
            spans = [r.citation for r in current.branches if r.citation_ref == citation_ref]
            if ctx.draft and ctx.draft.first_match_clause:
                span = ctx.draft.first_match_clause
                if globals()["citation_ref"](span) == citation_ref:
                    spans.append(span)
            _require(bool(spans), "map_citation_unavailable")
            assert ctx.source is not None
            return ctx.source.passage(spans[0])

    def _stale(
        self,
        actor: IntakePrincipal,
        ctx: _Context,
        records: list[Json],
        expected: str,
        current: MapReviewSnapshot,
    ) -> StaleMapError:
        # A stale exception carries disclosure data and bypasses normal context exit.
        self._current(actor, ctx)
        initial = self._snapshot(actor, ctx, None)
        prior: MapReviewSnapshot | None = initial
        if initial.review_revision != expected:
            prior = next(
                (
                    self._snapshot(actor, ctx, r)
                    for r in records
                    if self._same_source(r, ctx)
                    and self._snapshot(actor, ctx, r).review_revision == expected
                ),
                None,
            )
        old = {r.branch_ref: r for r in prior.branches} if prior is not None else {}
        new = {r.branch_ref: r for r in current.branches}
        diff = tuple(
            MapDiff(ref, old.get(ref), new.get(ref))
            for ref in sorted(old.keys() | new.keys())
            if old.get(ref) != new.get(ref)
        )
        return StaleMapError(current, diff, prior)

    @staticmethod
    def _edit(row: MapBranch, edit: BranchEdit, ctx: _Context, actor: IntakePrincipal) -> MapBranch:
        _require(
            edit.disposition in _DISPOSITIONS and bool(edit.reason) and len(edit.reason) <= 1000,
            "invalid_map_edit",
        )
        _require(
            edit.disposition == "corrected" or (edit.action is None and edit.conditions is None),
            "invalid_map_edit",
        )
        action = edit.action if edit.action is not None else row.action
        conditions = edit.conditions if edit.conditions is not None else row.conditions
        if edit.disposition in {"accepted", "corrected"}:
            _require(
                action in ctx.capability["actions"] and 0 < len(conditions) <= 32,
                "unsupported_map_rule",
            )
        for c in conditions:
            _require(c.field in ctx.capability["field_kinds"], "unsupported_map_field")
            _require(
                c.operator in {"missing", "present", "<", "<=", ">", ">="},
                "unsupported_map_predicate",
            )
            if c.operator in {"missing", "present"}:
                _require(c.threshold is None, "unsupported_map_predicate")
            else:
                _require(
                    ctx.capability["field_kinds"][c.field] == "number"
                    and isinstance(c.threshold, str)
                    and re.fullmatch(r"-?[0-9]{1,12}(?:\.[0-9]{1,8})?", c.threshold),
                    "unsupported_map_predicate",
                )
        return replace(
            row,
            action=action,
            conditions=conditions,
            disposition=edit.disposition,
            reason=edit.reason,
            actor_ref=actor.actor,
            editor_ref=ctx.capability["editors"][actor.actor],
            source_support="owner_assertion"
            if edit.disposition == "corrected"
            else row.source_support,
            confirmed=edit.disposition in {"accepted", "corrected"},
        )

    def save(
        self,
        actor: IntakePrincipal,
        *,
        expected_revision: str,
        operation_ref: str,
        changes: tuple[BranchEdit, ...],
        precedence: tuple[str, ...] | None = None,
        precedence_reason: str | None = None,
        settings: MapSettingsEdit | None = None,
    ) -> MapReviewSnapshot:
        require_ref(operation_ref)
        _require(changes or precedence is not None or settings is not None, "empty_map_edit")
        _require(
            len(changes) <= 128 and len({c.branch_ref for c in changes}) == len(changes),
            "invalid_map_edit",
        )
        intent = digest(
            [
                expected_revision,
                [asdict(c) for c in changes],
                precedence,
                precedence_reason,
                asdict(settings) if settings is not None else None,
            ]
        )
        with (
            self._guard(actor, write=True) as ctx,
            self._db(write=True, before_commit=lambda: self._current(actor, ctx)) as db,
        ):
            assert db is not None and ctx.source is not None and ctx.draft is not None
            records = self._records(db)
            current = self._snapshot(actor, ctx, records[-1] if records else None)
            replay = db.execute(
                "SELECT intent,ref FROM operations WHERE scope=? AND actor=? AND operation=?",
                (self.scope, actor.actor, operation_ref),
            ).fetchone()
            if replay is not None:
                _require(replay[0] == intent, "map_review_operation_conflict")
                prior = next(r for r in records if r["ref"] == replay[1])
                if (
                    prior["ref"] != records[-1]["ref"]
                    or not self._same_source(prior, ctx)
                    or prior["capability_revision"] != digest(ctx.capability)
                    or prior["editor_ref"] != ctx.capability["editors"][actor.actor]
                ):
                    raise self._stale(actor, ctx, records, expected_revision, current)
                return current
            if current.review_revision != expected_revision:
                raise self._stale(actor, ctx, records, expected_revision, current)
            rows = {r.branch_ref: r for r in current.branches}
            for edit in changes:
                _require(edit.branch_ref in rows, "map_review_branch_changed")
                rows[edit.branch_ref] = self._edit(rows[edit.branch_ref], edit, ctx, actor)
            previous = records[-1] if records and self._same_source(records[-1], ctx) else None
            order = previous["precedence"] if previous else None
            order_reason = previous["precedence_reason"] if previous else None
            order_actor = previous["precedence_actor_ref"] if previous else None
            assertion = previous.get("settings") if previous else None
            if settings is not None:
                _require(
                    (
                        settings.trigger is None
                        or isinstance(settings.trigger, str)
                        and 0 < len(settings.trigger) <= 160
                        and settings.trigger == settings.trigger.strip()
                        and settings.trigger.isprintable()
                    )
                    and (
                        settings.fallback_action is None
                        or settings.fallback_action in ctx.capability["actions"]
                    )
                    and isinstance(settings.reason, str)
                    and 0 < len(settings.reason) <= 1000
                    and settings.reason == settings.reason.strip()
                    and settings.reason.isprintable(),
                    "invalid_map_settings",
                )
                assertion = asdict(settings) | {
                    "actor_ref": actor.actor,
                    "editor_ref": ctx.capability["editors"][actor.actor],
                }
            if precedence is not None:
                wanted = {
                    r.branch_ref
                    for r in rows.values()
                    if r.action is not None and r.disposition != "out_of_scope"
                }
                _require(
                    set(precedence) == wanted
                    and len(precedence) == len(wanted)
                    and precedence_reason
                    and len(precedence_reason) <= 1000,
                    "invalid_map_precedence",
                )
                order, order_reason = list(precedence), precedence_reason
                order_actor = actor.actor
            body = dict(
                scope=self.scope,
                version=(records[-1]["version"] + 1 if records else 1),
                parent=records[-1]["ref"] if records else None,
                owner_ref=ctx.capability["owner_ref"],
                source_revision=ctx.source.source_revision,
                source_digest=ctx.source.normalized_sha256,
                starter_grant_revision=ctx.capability["starter_grant_revision"],
                schema_digest=digest([ctx.capability["field_kinds"], ctx.capability["actions"]]),
                map_revision=ctx.draft.revision,
                capability_revision=digest(ctx.capability),
                actor_ref=actor.actor,
                editor_ref=ctx.capability["editors"][actor.actor],
                recorded_at=int(time.time()),
                branches=[asdict(r) for r in rows.values()],
                precedence=order,
                precedence_reason=order_reason,
                precedence_actor_ref=order_actor,
                settings=assertion,
            )
            seal = digest(body)
            ref = "map-review-" + seal[:40]
            # Revalidate before durable effects, while the authority guard is held.
            starter = self.authority._read_grant()
            _require(
                self._capability(actor, starter) == ctx.capability
                and _read_policy(self.source.path, starter) == ctx.source,
                "map_review_permission_changed",
            )
            db.execute(
                "INSERT INTO maps VALUES(?,?,?,?,?)",
                (self.scope, body["version"], ref, canonical(body), seal),
            )
            db.execute(
                "INSERT INTO operations VALUES(?,?,?,?,?)",
                (self.scope, actor.actor, operation_ref, intent, ref),
            )
            db.execute(
                "INSERT INTO heads VALUES(?,?) ON CONFLICT(scope) DO UPDATE SET ref=excluded.ref",
                (self.scope, ref),
            )
            return self._snapshot(actor, ctx, body | {"ref": ref})


def open_map_review(
    directory: Path, tenant: str, workflow_ref: str | None = None
) -> MapReviewService:
    selection = _read_private(directory / "map-review-selection.json", 8192)
    service = MapReviewService(directory, tenant, selection)
    _require(workflow_ref is None or service.workflow_ref == workflow_ref)
    return service
