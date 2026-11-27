"""Propose cited map branches through a separately authorized authoring-inference profile.

This optional route extends the guided decision map (``guided_decision_map``). A proposal is a
draft that needs owner review. It is never an applied map, never saved as a guided map and never
attributed to the owner. Access to the workflow, its policy or its guided map grants nothing
here: a distinct authoring grant is checked before any policy text is disclosed to the profile,
before every attempt, and immediately before the proposal is committed or an existing proposal is
disclosed again. Absent, foreign, revoked or stale grants raise one fixed ``authoring_unavailable``
code with zero reads, zero profile calls and zero stored effects. The guided policy route never
imports or depends on this module.

Simulated here: the authoring grant port, the profile (a scripted generated stand-in in tests), the
attempt ledger (local SQLite) and the scorer's answer key (supplied by the caller, never reachable
by the service or the profile). NOT proven: any real inference, provider disclosure, cost, quota
enforcement by a provider, grant issuance or revocation by an authorized custodian, hidden
red-control or the OPE-527 harness. Tests over generated sentinel data prove software behavior
only and establish no provider or customer evidence. Owner editing and acceptance of a proposal
belong to the map-review route and are not implemented here.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol

from opentour_cell.pilot.guided_decision_map import Branch, Condition, FieldKind
from opentour_cell.pilot.guided_policy import (
    HEADING,
    GuidedPolicySource,
    PolicyError,
    PolicySnapshot,
    PolicySpan,
    operative_lines,
)
from opentour_cell.pilot.intake_mcp import IntakePrincipal
from opentour_cell.pilot.operating_point import canonical, digest

PROPOSAL_VERSION = "opentour.pilot.authoring-proposal.v1"
PROPOSER_PREFIX = "authoring-inference:"
TIER_STAND_IN = "generated"
TIER_UNATTESTED = "unattested"
EXTERNAL_GATE = "real_inference_receipt_unsatisfied"
SPAN_RESOURCE = "policy_span"
FORBIDDEN_RESOURCES = frozenset(
    {"held_out_answer_key", "customer_credential", "trainer_access", "training_authority"}
)
_OPERATORS = frozenset({"<", "<=", ">", ">=", "missing", "present"})
_COMPARISONS = frozenset({"<", "<=", ">", ">="})
_COMPARISON_TEXT = re.compile(
    r"([a-z][a-z0-9_]{0,63})\s*(<=|>=|<|>|below|at or above|at most|above)\s*"
    r"(-?[0-9]{1,12}(?:\.[0-9]{1,8})?)"
)
_PRESENCE_TEXT = re.compile(r"([a-z][a-z0-9_]{0,63}) (?:is )?(missing|present)")
_WORD_OPERATORS = {"below": "<", "at most": "<=", "at or above": ">=", "above": ">"}
_FIELD = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_ACTION = re.compile(r"[A-Z][A-Z0-9_]{0,47}\Z")
_NUMBER = re.compile(r"-?[0-9]{1,12}(?:\.[0-9]{1,8})?")
_MAX_BRANCHES = 64
_MAX_CONDITIONS = 6


class AuthoringError(ValueError):
    """Fixed refusal code that never echoes policy text, grants or model output."""


@dataclass(frozen=True)
class AuthoringGrant:
    """A grant for one profile, tenant and workflow. It is separate from workflow access."""

    tenant: str
    workflow_ref: str
    profile_id: str
    schema_id: str
    revision: str
    permitted_fields: frozenset[str]
    quota_attempts: int


class AuthoringGrants(Protocol):
    def current(self, actor: IntakePrincipal, workflow_ref: str) -> AuthoringGrant | None:
        """Return the live grant, or None when absent, revoked, expired or foreign."""


@dataclass(frozen=True)
class SpanText:
    citation: PolicySpan
    text: str


@dataclass(frozen=True)
class AuthoringRequest:
    """The only material a profile receives: schema vocabulary and cited policy spans."""

    schema_id: str
    attempt: int
    spans: tuple[SpanText, ...]
    fields: Mapping[str, str]
    actions: tuple[str, ...]
    prior_issue_codes: tuple[str, ...]


@dataclass(frozen=True)
class ProfileReply:
    payload: Mapping[str, Any]
    cost_micro: int


class AuthoringTools:
    """Tools offered to a profile. Only cited policy spans resolve; everything else is refused."""

    def __init__(self, snapshot: PolicySnapshot):
        self._snapshot = snapshot

    def retrieve(self, resource: str, citation: PolicySpan | None = None) -> str:
        if resource != SPAN_RESOURCE or citation is None:
            # Answer keys, credentials, trainer access and training authority are all refused.
            raise AuthoringError("authoring_resource_forbidden")
        try:
            return self._snapshot.passage(citation)
        except PolicyError:
            raise AuthoringError("authoring_resource_forbidden") from None


class AuthoringProfile(Protocol):
    profile_id: str
    schema_id: str
    stand_in: bool  # True for a generated deterministic stand-in; False for any real profile

    def infer(self, request: AuthoringRequest, tools: AuthoringTools) -> ProfileReply: ...


@dataclass(frozen=True)
class Issue:
    code: str
    citation: PolicySpan | None = None


@dataclass(frozen=True)
class AuthoringProposal:
    """An owner-editable draft. ``applied`` is always False; no apply path exists here."""

    proposal_ref: str
    workflow_ref: str
    policy_revision: str
    profile_id: str
    schema_id: str
    grant_revision: str
    proposed_by: str
    requested_by: str
    state: str
    applied: bool
    evidence_tier: str
    stand_in: bool
    external_gate: str
    attempts: int
    cost_micro: int
    branches: tuple[Branch, ...]
    issues: tuple[Issue, ...]


@dataclass(frozen=True)
class AuthoringLimits:
    max_attempts_per_proposal: int = 3
    max_cost_micro_per_attempt: int = 1_000_000


def _branch_ref(conditions: tuple[Condition, ...], action: str) -> str:
    return "branch-" + digest([[asdict(item) for item in conditions], action])[:32]


def _number(text: str) -> str | None:
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    if not value.is_finite() or not _NUMBER.fullmatch(text):
        return None
    number = format(value, "f")
    return number.rstrip("0").rstrip(".") if "." in number else number


def _validate_branch(
    raw: object,
    snapshot: PolicySnapshot,
    usable_lines: frozenset[int],
    field_kinds: Mapping[str, FieldKind],
    permitted: frozenset[str],
    actions: frozenset[str],
) -> Branch | Issue:
    if not isinstance(raw, Mapping) or set(raw) != {"conditions", "action", "citation"}:
        return Issue("branch_schema_invalid")
    action, conditions_raw, cite_raw = raw["action"], raw["conditions"], raw["citation"]
    if not isinstance(action, str) or not _ACTION.match(action) or action not in actions:
        return Issue("action_unsupported")
    if (
        not isinstance(cite_raw, Mapping)
        or set(cite_raw) != {"line_start", "line_end"}
        or any(type(cite_raw[key]) is not int for key in cite_raw)
    ):
        return Issue("citation_invalid")
    try:
        citation = snapshot.cite(cite_raw["line_start"], cite_raw["line_end"])
    except PolicyError:
        return Issue("citation_invalid")
    if not set(range(citation.line_start, citation.line_end + 1)) <= usable_lines:
        return Issue("citation_invalid", citation)
    if (
        not isinstance(conditions_raw, Sequence)
        or isinstance(conditions_raw, str)
        or not 1 <= len(conditions_raw) <= _MAX_CONDITIONS
    ):
        return Issue("branch_schema_invalid", citation)
    passage = snapshot.passage(citation).lower()
    if not re.search(rf"\b{action}\b", passage, re.I):
        return Issue("answer_unsupported", citation)
    conditions = []
    for item in conditions_raw:
        if not isinstance(item, Mapping) or not set(item) <= {"field", "operator", "threshold"}:
            return Issue("branch_schema_invalid", citation)
        field, operator, threshold = item.get("field"), item.get("operator"), item.get("threshold")
        if not isinstance(field, str) or not _FIELD.match(field) or field not in field_kinds:
            return Issue("predicate_unsupported", citation)
        if field not in permitted:
            return Issue("field_not_permitted", citation)
        if not isinstance(operator, str) or operator not in _OPERATORS:
            return Issue("predicate_unsupported", citation)
        normalized: str | None = None
        if operator in _COMPARISONS:
            if field_kinds[field] is not FieldKind.NUMBER or not isinstance(threshold, str):
                return Issue("predicate_unsupported", citation)
            normalized = _number(threshold)
            if normalized is None:
                return Issue("predicate_unsupported", citation)
            stated = {
                (match[1], _WORD_OPERATORS.get(match[2], match[2]), _number(match[3]))
                for match in _COMPARISON_TEXT.finditer(passage)
            }
            if (field, operator, normalized) not in stated:
                return Issue("answer_unsupported", citation)  # field, direction and number
        elif threshold is not None:
            return Issue("predicate_unsupported", citation)
        else:
            stated_presence = {(m[1], m[2]) for m in _PRESENCE_TEXT.finditer(passage)}
            if (field, operator) not in stated_presence:
                return Issue("answer_unsupported", citation)
        conditions.append(Condition(field, operator, normalized))
    frozen = tuple(conditions)
    return Branch(_branch_ref(frozen, action), frozen, action, citation)


def _validate_reply(
    payload: Mapping[str, Any],
    schema_id: str,
    snapshot: PolicySnapshot,
    field_kinds: Mapping[str, FieldKind],
    permitted: frozenset[str],
    actions: frozenset[str],
) -> tuple[tuple[Branch, ...], tuple[Issue, ...]]:
    raw_branches = payload.get("branches") if isinstance(payload, Mapping) else None
    if (
        not isinstance(payload, Mapping)
        or set(payload) != {"schema_id", "branches"}
        or payload["schema_id"] != schema_id
        or not isinstance(raw_branches, Sequence)
        or isinstance(raw_branches, str)
        or len(raw_branches) > _MAX_BRANCHES
    ):
        return (), (Issue("reply_schema_invalid"),)
    usable = frozenset(
        number for number, line in operative_lines(snapshot.lines) if not HEADING.match(line)
    )
    branches: dict[str, Branch] = {}
    issues: list[Issue] = []
    for raw in raw_branches:
        result = _validate_branch(raw, snapshot, usable, field_kinds, permitted, actions)
        if isinstance(result, Issue):
            issues.append(result)
        else:
            branches.setdefault(result.branch_ref, result)
    return tuple(branches.values()), tuple(issues)


def _conflicts(branches: tuple[Branch, ...]) -> tuple[Issue, ...]:
    """Identical predicates with different actions cannot both be proposed without a decision."""
    seen: dict[tuple[Condition, ...], Branch] = {}
    issues = []
    for branch in branches:
        earlier = seen.setdefault(branch.conditions, branch)
        if earlier.action != branch.action:
            issues.append(Issue("conflict_unresolved", branch.citation))
    return tuple(issues)


def _proposal_json(proposal: AuthoringProposal) -> str:
    return canonical(asdict(proposal))


def _proposal_from_json(text: str) -> AuthoringProposal:
    value = json.loads(text)

    def span(item: Mapping[str, Any]) -> PolicySpan:
        return PolicySpan(**item)

    branches = tuple(
        Branch(
            item["branch_ref"],
            tuple(Condition(**cond) for cond in item["conditions"]),
            item["action"],
            span(item["citation"]),
        )
        for item in value["branches"]
    )
    issues = tuple(
        Issue(item["code"], span(item["citation"]) if item["citation"] else None)
        for item in value["issues"]
    )
    return AuthoringProposal(**{**value, "branches": branches, "issues": issues})


class AuthoringService:
    """Propose branches only while a distinct, current authoring grant authorizes it."""

    def __init__(
        self,
        source: GuidedPolicySource,
        grants: AuthoringGrants,
        profile: AuthoringProfile,
        field_kinds: Mapping[str, FieldKind],
        actions: frozenset[str],
        ledger: Path,
        limits: AuthoringLimits = AuthoringLimits(),
    ):
        self.source = source
        self.grants = grants
        self.profile = profile
        self.field_kinds = dict(field_kinds)
        self.actions = actions
        self.limits = limits
        self.ledger = ledger
        ledger.parent.mkdir(mode=0o700, exist_ok=True)
        with self._db() as db:
            db.executescript(
                "CREATE TABLE IF NOT EXISTS attempts (tenant TEXT, grant_revision TEXT, "
                "workflow_ref TEXT, key TEXT, n INTEGER, outcome TEXT, cost_micro INTEGER, "
                "PRIMARY KEY (tenant, grant_revision, workflow_ref, key, n));"
                "CREATE TABLE IF NOT EXISTS proposals (tenant TEXT, workflow_ref TEXT, "
                "key TEXT, intent TEXT, body TEXT, PRIMARY KEY (tenant, workflow_ref, key));"
            )

    def _db(self) -> Any:
        db = sqlite3.connect(self.ledger, isolation_level=None)
        os.chmod(self.ledger, 0o600)
        return closing(db)

    def _grant(self, actor: IntakePrincipal, workflow_ref: str) -> AuthoringGrant:
        grant = self.grants.current(actor, workflow_ref)
        if (
            grant is None
            or grant.tenant != actor.tenant
            or grant.workflow_ref != workflow_ref
            or grant.quota_attempts < 0
        ):
            raise AuthoringError("authoring_unavailable")
        if (grant.profile_id, grant.schema_id) != (self.profile.profile_id, self.profile.schema_id):
            raise AuthoringError("authoring_profile_unsupported")
        return grant

    def _require_current(
        self, actor: IntakePrincipal, workflow_ref: str, held: AuthoringGrant
    ) -> None:
        if self._grant(actor, workflow_ref) != held:
            raise AuthoringError("authoring_unavailable")

    def propose(
        self, actor: IntakePrincipal, workflow_ref: str, idempotency_key: str
    ) -> AuthoringProposal:
        try:
            return self._propose(actor, workflow_ref, idempotency_key)
        except sqlite3.Error:
            raise AuthoringError("authoring_ledger_unavailable") from None

    def _propose(
        self, actor: IntakePrincipal, workflow_ref: str, idempotency_key: str
    ) -> AuthoringProposal:
        grant = self._grant(actor, workflow_ref)  # before any policy text is read
        try:
            snapshot = self.source.read(actor, workflow_ref)
        except PolicyError:
            raise AuthoringError("authoring_unavailable") from None
        permitted = grant.permitted_fields & self.field_kinds.keys()
        intent = digest([workflow_ref, snapshot.source_revision, grant.profile_id, grant.schema_id])
        with self._db() as db:
            row = db.execute(
                "SELECT intent, body FROM proposals WHERE tenant=? AND workflow_ref=? AND key=?",
                (actor.tenant, workflow_ref, idempotency_key),
            ).fetchone()
        if row is not None:
            if row[0] != intent:
                raise AuthoringError("authoring_idempotency_conflict")
            self._require_current(actor, workflow_ref, grant)  # before disclosing it again
            return _proposal_from_json(row[1])

        spans = tuple(
            SpanText(snapshot.cite(number, number), line)
            for number, line in operative_lines(snapshot.lines)
            if not HEADING.match(line)
        )
        tools = AuthoringTools(snapshot)
        scope = (actor.tenant, grant.revision, workflow_ref, idempotency_key)
        with self._db() as db:
            mine = db.execute(
                "SELECT count(*), coalesce(sum(cost_micro),0) FROM attempts "
                "WHERE tenant=? AND grant_revision=? AND workflow_ref=? AND key=?",
                scope,
            ).fetchone()
        attempts, cost = int(mine[0]), int(mine[1])
        branches: tuple[Branch, ...] = ()
        issues: tuple[Issue, ...] = (Issue("attempts_exhausted"),)
        prior: tuple[str, ...] = ()
        while True:
            self._require_current(actor, workflow_ref, grant)
            reserved = self._reserve(scope, grant.quota_attempts)
            if isinstance(reserved, str):
                if reserved == "quota" and attempts == 0:
                    raise AuthoringError("authoring_quota_exhausted")
                break
            attempts = reserved
            self._finish(scope, attempts, "dispatched", 0)  # outcome unknown if we crash now
            request = AuthoringRequest(
                grant.schema_id,
                attempts,
                spans,
                {name: self.field_kinds[name].value for name in sorted(permitted)},
                tuple(sorted(self.actions)),
                prior,
            )
            try:
                reply = self.profile.infer(request, tools)
            except AuthoringError:
                self._finish(scope, attempts, "refused", 0)
                raise
            spent = reply.cost_micro
            if type(spent) is not int or not 0 <= spent <= self.limits.max_cost_micro_per_attempt:
                self._finish(scope, attempts, "cost_invalid", 0)
                raise AuthoringError("authoring_cost_invalid")
            cost += spent
            branches, issues = _validate_reply(
                reply.payload, grant.schema_id, snapshot, self.field_kinds, permitted, self.actions
            )
            issues += _conflicts(branches)
            self._finish(scope, attempts, "valid" if not issues else "invalid", spent)
            if not issues:
                break
            prior = tuple(sorted({issue.code for issue in issues}))

        self._require_current(actor, workflow_ref, grant)  # before committing effects
        proposal = AuthoringProposal(
            "proposal-" + digest([*scope, intent])[:32],
            workflow_ref,
            snapshot.source_revision,
            grant.profile_id,
            grant.schema_id,
            grant.revision,
            PROPOSER_PREFIX + grant.profile_id,
            actor.actor,
            "needs_owner_review",
            False,
            TIER_STAND_IN if self.profile.stand_in else TIER_UNATTESTED,
            self.profile.stand_in,
            EXTERNAL_GATE,  # a proposal alone never satisfies the real-inference gate
            attempts,
            cost,
            branches,
            issues,
        )
        body = _proposal_json(proposal)
        with self._db() as db:
            db.execute(
                "INSERT OR IGNORE INTO proposals VALUES (?,?,?,?,?)",
                (actor.tenant, workflow_ref, idempotency_key, intent, body),
            )
            stored = db.execute(
                "SELECT body FROM proposals WHERE tenant=? AND workflow_ref=? AND key=?",
                (actor.tenant, workflow_ref, idempotency_key),
            ).fetchone()[0]
        return _proposal_from_json(stored)

    def _reserve(self, scope: tuple[str, ...], quota: int) -> int | str:
        """Atomically count and persist one attempt before dispatch.

        Returns the attempt number, or "quota" / "bound" when no attempt may be dispatched.
        """
        try:
            with self._db() as db:
                db.execute("BEGIN IMMEDIATE")
                try:
                    used = db.execute(
                        "SELECT count(*) FROM attempts WHERE tenant=? AND grant_revision=?",
                        scope[:2],
                    ).fetchone()[0]
                    mine = db.execute(
                        "SELECT count(*) FROM attempts WHERE tenant=? AND grant_revision=? "
                        "AND workflow_ref=? AND key=?",
                        scope,
                    ).fetchone()[0]
                    if mine >= self.limits.max_attempts_per_proposal:
                        db.execute("ROLLBACK")
                        return "bound"
                    if used >= quota:
                        db.execute("ROLLBACK")
                        return "quota"
                    db.execute(
                        "INSERT INTO attempts VALUES (?,?,?,?,?,?,?)",
                        (*scope, mine + 1, "reserved", 0),
                    )
                    db.execute("COMMIT")
                    return int(mine) + 1
                except BaseException:
                    if db.in_transaction:
                        db.execute("ROLLBACK")
                    raise
        except sqlite3.Error:
            raise AuthoringError("authoring_ledger_unavailable") from None

    def _finish(self, scope: tuple[str, ...], n: int, outcome: str, cost: int) -> None:
        with self._db() as db:
            db.execute(
                "UPDATE attempts SET outcome=?, cost_micro=? WHERE tenant=? AND grant_revision=? "
                "AND workflow_ref=? AND key=? AND n=?",
                (outcome, cost, *scope, n),
            )


@dataclass(frozen=True)
class ProposalScore:
    proposed: int
    grounded: int
    expected: int
    matched: int
    attempts: int
    cost_micro: int


def score_proposal(
    proposal: AuthoringProposal, answer_key: frozenset[tuple[tuple[Condition, ...], str]]
) -> ProposalScore:
    """Grade a proposal against a held-out key. Only the scorer ever holds the key.

    Every returned branch already passed grounding validation, so ``grounded`` is the validated
    count; ``matched`` counts branches equal to an expected (conditions, action) pair.
    """
    actual = {(branch.conditions, branch.action) for branch in proposal.branches}
    return ProposalScore(
        len(proposal.branches),
        len(proposal.branches),
        len(answer_key),
        len(actual & answer_key),
        proposal.attempts,
        proposal.cost_micro,
    )
