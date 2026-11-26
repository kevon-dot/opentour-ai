"""A guided branch table assembled from supported, cited policy clauses."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from decimal import Decimal
from enum import StrEnum

from opentour_cell.pilot.guided_policy import (
    GuidedPolicySource,
    PolicyError,
    PolicySnapshot,
    PolicySpan,
)
from opentour_cell.pilot.intake_mcp import IntakePrincipal
from opentour_cell.pilot.operating_point import canonical, digest
from opentour_cell.pilot.repair_workflow import _record

_RULE = re.compile(r"(?:[-*] +)?(?:if|when) (.+), *(?:then )?([A-Z][A-Z0-9_]{0,47})\.?", re.I)
_PRESENCE = re.compile(r"([a-z][a-z0-9_]{0,63}) (?:is )?(missing|present)")
_COMPARISON = re.compile(
    r"([a-z][a-z0-9_]{0,63})\s*(<=|>=|<|>|below|at or above|at most|above)\s*"
    r"(-?[0-9]{1,12}(?:\.[0-9]{1,8})?)"
)
_OPERATORS = {"below": "<", "at most": "<=", "at or above": ">=", "above": ">"}
_FIRST_MATCH = re.compile(r"(?:[-*] +)?apply the first matching rule\.?", re.I)
_ACTION = re.compile(r"[A-Z][A-Z0-9_]{0,47}")
_FIELD = re.compile(r"[a-z][a-z0-9_]{0,63}")
MAP_VERSION = "opentour.pilot.guided-decision-map.v1"


class FieldKind(StrEnum):
    REFERENCE = "reference"
    NUMBER = "number"


@dataclass(frozen=True)
class Condition:
    field: str
    operator: str
    threshold: str | None = None

    def matches(self, fields: Mapping[str, str | Decimal | None]) -> bool:
        value = fields.get(self.field)
        if self.operator == "missing":
            return value is None
        if self.operator == "present":
            return value is not None
        if not isinstance(value, Decimal) or not value.is_finite() or self.threshold is None:
            return False
        threshold = Decimal(self.threshold)
        return {
            "<": value < threshold,
            "<=": value <= threshold,
            ">": value > threshold,
            ">=": value >= threshold,
        }[self.operator]


@dataclass(frozen=True)
class Branch:
    branch_ref: str
    conditions: tuple[Condition, ...]
    action: str
    citation: PolicySpan


@dataclass(frozen=True)
class GuidedMap:
    revision: str
    policy_revision: str
    grant_revision: str
    branches: tuple[Branch, ...]
    first_match_clause: PolicySpan | None
    unresolved: tuple[PolicySpan, ...]

    def draft_action(
        self, fields: Mapping[str, str | Decimal | None]
    ) -> tuple[str | None, tuple[PolicySpan, ...]]:
        """Return the supported action or the exact clauses needing a precedence decision."""
        matched = [
            branch
            for branch in self.branches
            if all(condition.matches(fields) for condition in branch.conditions)
        ]
        if not matched or self.unresolved:
            return None, self.unresolved
        if self.first_match_clause is None and len({branch.action for branch in matched}) > 1:
            return None, tuple(branch.citation for branch in matched)
        return matched[0].action, (matched[0].citation,)


def _condition(clause: str, field_kinds: Mapping[str, FieldKind]) -> Condition | None:
    presence = _PRESENCE.fullmatch(clause)
    if presence and presence[1] in field_kinds:
        return Condition(presence[1], presence[2])
    comparison = _COMPARISON.fullmatch(clause)
    if comparison and field_kinds.get(comparison[1]) is FieldKind.NUMBER:
        number = format(Decimal(comparison[3]).normalize(), "f")
        return Condition(comparison[1], _OPERATORS.get(comparison[2], comparison[2]), number)
    return None


def guided_draft(
    policy: PolicySnapshot,
    field_kinds: Mapping[str, FieldKind],
    actions: frozenset[str],
) -> GuidedMap:
    """Recognize simple supported clauses. Leave other selected rule statements open."""
    if (
        not field_kinds
        or not actions
        or any(
            not _FIELD.fullmatch(field) or not isinstance(kind, FieldKind)
            for field, kind in field_kinds.items()
        )
        or any(not _ACTION.fullmatch(action) for action in actions)
    ):
        raise PolicyError("guided_decision_schema_invalid")

    branches = []
    unresolved = []
    priority = None
    fenced = False
    for line_number, raw in enumerate(policy.lines, start=1):
        line = raw.strip()
        if line.startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            continue
        if _FIRST_MATCH.fullmatch(line):
            citation = policy.cite(line_number, line_number)
            if priority is not None:
                unresolved.append(citation)
            priority = citation
            continue
        if not re.match(r"(?:[-*] +)?(?:if|when)\b", line, re.I):
            continue
        citation = policy.cite(line_number, line_number)
        rule = _RULE.fullmatch(line)
        if rule is None or rule[2].upper() not in actions:
            unresolved.append(citation)
            continue
        predicates = []
        for condition_text in rule[1].lower().split(" and "):
            predicate = _condition(condition_text.strip(), field_kinds)
            if predicate is None:
                unresolved.append(citation)
                break
            predicates.append(predicate)
        else:
            action = rule[2].upper()
            reference = "branch-" + digest([[asdict(item) for item in predicates], action])[:32]
            branches.append(Branch(reference, tuple(predicates), action, citation))

    revision = digest(
        {
            "schema_version": MAP_VERSION,
            "policy_revision": policy.source_revision,
            "field_kinds": dict(field_kinds),
            "actions": sorted(actions),
            "branches": [asdict(branch) for branch in branches],
            "first_match_clause": asdict(priority) if priority is not None else None,
            "unresolved": [asdict(span) for span in unresolved],
        }
    )
    return GuidedMap(
        revision,
        policy.source_revision,
        policy.grant_revision,
        tuple(branches),
        priority,
        tuple(unresolved),
    )


class GuidedMapService:
    """Preview and save immutable drafts for the selected local decision schema."""

    def __init__(
        self,
        source: GuidedPolicySource,
        field_kinds: Mapping[str, FieldKind],
        actions: frozenset[str],
    ):
        self.source = source
        self.field_kinds = dict(field_kinds)
        self.actions = actions

    def preview(self, actor: IntakePrincipal, workflow_ref: str) -> GuidedMap:
        snapshot = self.source.read(actor, workflow_ref)
        draft = guided_draft(snapshot, self.field_kinds, self.actions)
        scoped = digest([actor.tenant, workflow_ref, snapshot.grant_revision, draft.revision])
        return replace(draft, revision=scoped)

    def save_draft(
        self, actor: IntakePrincipal, workflow_ref: str, expected_revision: str
    ) -> GuidedMap:
        draft = self.preview(actor, workflow_ref)
        if expected_revision != draft.revision:
            raise PolicyError("guided_map_stale_revision")
        authority = self.source.authority
        selection = authority.selected
        with authority.setup_grant(actor) as grant:
            self.source._require_workflow(workflow_ref, grant)
            if (
                grant.source_revision != draft.policy_revision
                or grant.revision != draft.grant_revision
            ):
                raise PolicyError("guided_map_stale_revision")
            scope = [selection.tenant, selection.engagement_epoch, workflow_ref, draft.revision]
            path = authority.path.parent / f"guided-map-{digest(scope)}.json"
            _record(
                path,
                {
                    "schema_version": MAP_VERSION,
                    "workflow_ref": workflow_ref,
                    "grant_revision": grant.revision,
                    "field_kinds": self.field_kinds,
                    "actions": sorted(self.actions),
                    "map": json.loads(canonical(asdict(draft))),
                },
            )
        return draft
