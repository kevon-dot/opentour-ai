"""A guided branch table assembled from supported, cited policy clauses."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from datetime import date
from decimal import Decimal
from enum import StrEnum

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
from opentour_cell.pilot.repair_workflow import _record

_RULE = re.compile(r"(?:if|when) (.+), *(?:then )?([A-Z][A-Z0-9_]{0,47})\.?", re.I)
_PRESENCE = re.compile(r"([a-z][a-z0-9_]{0,63}) (?:is )?(missing|present)")
_COMPARISON = re.compile(
    r"([a-z][a-z0-9_]{0,63})\s*(<=|>=|<|>|below|at or above|at most|above)\s*"
    r"(-?[0-9]{1,12}(?:\.[0-9]{1,8})?)"
)
_OPERATORS = {"below": "<", "at most": "<=", "at or above": ">=", "above": ">"}
_FIRST_MATCH = re.compile(r"apply the first matching rule\.?", re.I)
_EFFECTIVE_FROM = re.compile(r"effective from ([0-9]{4}-[0-9]{2}-[0-9]{2})\.?", re.I)
_LIST_MARKER = re.compile(r"^(?:[-+*]|[0-9]{1,9}[.)]) +")
_HORIZONTAL_RULE = re.compile(r"(?:-{3,}|\*{3,}|_{3,})")
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
class EffectiveFrom:
    iso_date: str
    citation: PolicySpan

    def applies(self, decision_date: date | None) -> bool:
        return type(decision_date) is date and decision_date.isoformat() >= self.iso_date


@dataclass(frozen=True)
class GuidedMap:
    revision: str
    policy_revision: str
    grant_revision: str
    branches: tuple[Branch, ...]
    first_match_clause: PolicySpan | None
    unresolved: tuple[PolicySpan, ...]
    effective_from: EffectiveFrom | None = None

    def draft_action(
        self, fields: Mapping[str, str | Decimal | None], *, decision_date: date | None = None
    ) -> tuple[str | None, tuple[PolicySpan, ...]]:
        """Return the supported action or the exact clauses needing a precedence decision."""
        if self.effective_from and not self.effective_from.applies(decision_date):
            return None, (self.effective_from.citation,)
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
        number = format(Decimal(comparison[3]), "f")
        if "." in number:
            number = number.rstrip("0").rstrip(".")
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
    effective_dates: list[EffectiveFrom] = []
    for line_number, raw in operative_lines(policy.lines):
        if HEADING.match(raw) or _HORIZONTAL_RULE.fullmatch(raw.strip()):
            continue
        line = _LIST_MARKER.sub("", raw.strip(), count=1)
        citation = policy.cite(line_number, line_number)
        effective = _EFFECTIVE_FROM.fullmatch(line)
        if effective:
            try:
                valid_from = date.fromisoformat(effective[1]).isoformat()
            except ValueError:
                unresolved.append(citation)
            else:
                effective_dates.append(EffectiveFrom(valid_from, citation))
            continue
        if _FIRST_MATCH.fullmatch(line):
            if priority is not None:
                unresolved.append(citation)
            priority = citation
            continue
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

    effective_from = effective_dates[0] if len(effective_dates) == 1 else None
    if len(effective_dates) > 1:
        unresolved.extend(effective.citation for effective in effective_dates)
    meaning = {
        "schema_version": MAP_VERSION,
        "policy_revision": policy.source_revision,
        "field_kinds": dict(field_kinds),
        "actions": sorted(actions),
        "branches": [asdict(branch) for branch in branches],
        "first_match_clause": asdict(priority) if priority is not None else None,
        "unresolved": [asdict(span) for span in unresolved],
    }
    if effective_from:
        meaning["effective_from"] = asdict(effective_from)
    return GuidedMap(
        digest(meaning),
        policy.source_revision,
        policy.grant_revision,
        tuple(branches),
        priority,
        tuple(unresolved),
        effective_from,
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
            saved = asdict(draft)
            if draft.effective_from is None:
                del saved["effective_from"]
            _record(
                path,
                {
                    "schema_version": MAP_VERSION,
                    "workflow_ref": workflow_ref,
                    "grant_revision": grant.revision,
                    "field_kinds": self.field_kinds,
                    "actions": sorted(self.actions),
                    "map": json.loads(canonical(saved)),
                },
            )
        return draft
