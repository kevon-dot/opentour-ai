"""Bounded, deterministic suggestions from confirmed presence/numeric predicates."""

from __future__ import annotations

import itertools
import math
from decimal import Decimal

from opentour_cell.pilot.intake_mcp import IntakeError
from opentour_cell.pilot.map_review import MapReviewSnapshot
from opentour_cell.pilot.operating_point import digest
from opentour_cell.pilot.starter_cases import ProposedCase

PROFILE = "guided-presence-number-v1"
MAX_CANDIDATES = 96
Scalar = str | int | float | None


def _require(ok: object, reason: str) -> None:
    if not ok:
        raise IntakeError(reason)


def _number(value: Decimal) -> int | float:
    _require(value.is_finite(), "guided_authoring_number_unavailable")
    if value == value.to_integral():
        return int(value)
    converted = float(value)
    _require(
        math.isfinite(converted) and Decimal(str(converted)) == value,
        "guided_authoring_number_unavailable",
    )
    return converted


class GuidedCaseAuthoring:
    """No credentials, invocation, labels, splits, evaluator output or live evidence."""

    def propose(self, snapshot: MapReviewSnapshot) -> tuple[ProposedCase, ...]:
        _require(
            snapshot.confirmed_ref is not None
            and snapshot.settings_trigger is not None
            and snapshot.settings_support == "owner_assertion",
            "guided_authoring_confirmed_map_required",
        )
        active = tuple(row for row in snapshot.branches if row.disposition != "out_of_scope")
        _require(
            active
            and snapshot.question is None
            and all(row.confirmed and row.action is not None and row.conditions for row in active),
            "guided_authoring_unresolved_map",
        )
        fields = sorted({condition.field for row in active for condition in row.conditions})
        _require(0 < len(fields) <= 16, "guided_authoring_profile_exceeded")
        values: list[tuple[str | Decimal | None, ...]] = []
        for field in fields:
            conditions = [c for row in active for c in row.conditions if c.field == field]
            kind = snapshot.field_kinds.get(field)
            _require(kind in {"reference", "number"}, "guided_authoring_schema_unavailable")
            if kind == "reference":
                _require(
                    all(c.operator in {"missing", "present"} for c in conditions),
                    "guided_authoring_predicate_unsupported",
                )
                values.append((None, "generated-present"))
                continue
            thresholds = {Decimal(c.threshold) for c in conditions if c.threshold is not None}
            _require(all(t.is_finite() for t in thresholds), "guided_authoring_number_unavailable")
            _require(
                all(c.operator in {"missing", "present", "<", "<=", ">", ">="} for c in conditions),
                "guided_authoring_predicate_unsupported",
            )
            precision = max([2, *(max(0, -int(t.as_tuple().exponent)) for t in thresholds)])
            _require(precision <= 8, "guided_authoring_number_unavailable")
            quantum = Decimal(1).scaleb(-precision)
            candidates = sorted(
                {
                    value
                    for threshold in thresholds
                    for value in (threshold - quantum, threshold, threshold + quantum)
                }
            )
            if not candidates:
                candidates = [Decimal(0)]
            choices: tuple[str | Decimal | None, ...] = tuple(candidates)
            if any(c.operator in {"missing", "present"} for c in conditions):
                choices = (None, *choices)
            values.append(choices)
        _require(
            math.prod(len(v) for v in values) <= MAX_CANDIDATES, "guided_authoring_profile_exceeded"
        )
        by_citation = {row.citation_ref: row for row in active}
        cases: dict[str, ProposedCase] = {}
        covered = set()
        for row_values in itertools.product(*values):
            example = dict(zip(fields, row_values, strict=True))
            action, citations = snapshot.expected_action(example)
            _require(
                action is not None and len(citations) == 1 and citations[0] in by_citation,
                "guided_authoring_uncovered_input",
            )
            assert action is not None
            branch = by_citation[citations[0]]
            _require(action == branch.action, "guided_authoring_source_changed")
            inputs: tuple[tuple[str, Scalar], ...] = tuple(
                (field, _number(value) if isinstance(value, Decimal) else value)
                for field, value in example.items()
            )
            identity = digest(dict(inputs))
            family = "guided-family-" + digest([PROFILE, branch.branch_ref])[:32]
            cases.setdefault(
                identity,
                ProposedCase(
                    "guided-case-" + identity[:32],
                    family,
                    family,
                    branch.branch_ref,
                    inputs,
                    action,
                    "Generated predicate example; named-owner adjudication is required.",
                    branch.citation_ref,
                ),
            )
            covered.add(branch.branch_ref)
        # Never silently omit a material branch because the profile cannot reach it.
        _require(covered == {row.branch_ref for row in active}, "guided_authoring_uncovered_branch")
        return tuple(cases[key] for key in sorted(cases))


def guided_case_provider() -> GuidedCaseAuthoring:
    """The normal no-inference provider; permission stays in StarterCaseSession."""
    return GuidedCaseAuthoring()
