"""Construct decision-time examples for inspection of an authorized policy snapshot."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from itertools import islice, pairwise, product

from opentour_cell.pilot.map_review import MapBranch, MapReviewSnapshot


@dataclass(frozen=True)
class ProposedExample:
    inputs: tuple[tuple[str, str | Decimal | None], ...]
    action: str | None
    citations: tuple[str, ...]
    explanation: str


def branch_example(snapshot: MapReviewSnapshot, branch: MapBranch) -> ProposedExample:
    rows = tuple(row for row in snapshot.branches if row.disposition != "out_of_scope")
    if branch not in rows or branch.action is None or not branch.conditions:
        return ProposedExample(
            (), None, (branch.citation_ref,), "No supported example for this clause"
        )
    fields = sorted({condition.field for row in rows for condition in row.conditions})
    options: list[list[str | Decimal | None]] = []
    for field in fields:
        if snapshot.field_kinds.get(field) == "reference":
            values: list[str | Decimal | None] = ["example-reference", None]
        elif snapshot.field_kinds.get(field) == "number":
            boundaries = sorted(
                {
                    Decimal(c.threshold)
                    for row in rows
                    for c in row.conditions
                    if c.field == field and c.threshold is not None
                }
            )
            numbers = [Decimal(0), *boundaries]
            if boundaries:
                numbers.extend([boundaries[0] - 1, boundaries[-1] + 1])
                numbers.extend((low + high) / 2 for low, high in pairwise(boundaries))
            values = [*dict.fromkeys(numbers), None]
        else:
            return ProposedExample((), None, (), "Decision-time field type unavailable")
        conditions = [c for c in branch.conditions if c.field == field]
        options.append([v for v in values if all(c.matches({field: v}) for c in conditions)])
    held = None
    for combination in islice(product(*options), 512):
        inputs = tuple(zip(fields, combination, strict=True))
        selected = dict(inputs)
        matching = [r for r in rows if r.action and all(c.matches(selected) for c in r.conditions)]
        citations = tuple(r.citation_ref for r in matching)
        if any(r.action is None for r in rows):
            return ProposedExample(
                inputs, None, citations, "An operative clause needs clarification"
            )
        if any(r.disposition == "unresolved" for r in matching):
            held = ProposedExample(inputs, None, citations, "A matching rule is unresolved")
            continue
        if all(r.precedence is not None for r in matching):
            chosen = min(matching, key=lambda r: r.precedence or 0)
            if chosen.branch_ref == branch.branch_ref:
                return ProposedExample(
                    inputs, chosen.action, (chosen.citation_ref,), "Current priority"
                )
        elif len({r.action for r in matching}) == 1:
            return ProposedExample(inputs, branch.action, citations, "Matching policy predicates")
        else:
            held = ProposedExample(
                inputs, None, citations, "Overlapping rules need a priority decision"
            )
    return held or ProposedExample((), None, (branch.citation_ref,), "No reachable example found")
