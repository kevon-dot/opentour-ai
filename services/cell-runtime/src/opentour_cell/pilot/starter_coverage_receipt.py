"""Owner-only per-branch coverage receipt for a frozen starter; it adds no store.

The receipt is a deterministic projection of the immutable frozen pack. Every
count is re-derived from the stored cases and the receipt holds when one differs
from the freeze. A family counts once, however many paraphrases it holds. The
evaluator handoff carries references and totals only: no branch, target,
suggestion, input or per-action count, because branch predicates plus per-branch
counts would reveal owner labels to anyone holding the evaluation inputs.
Starter evidence never earns training credit or live recurrence here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from opentour_cell.pilot.intake_admission import IntakeWork
from opentour_cell.pilot.intake_mcp import IntakeError, IntakePrincipal
from opentour_cell.pilot.operating_point import canonical, digest
from opentour_cell.pilot.starter_cases import ConfirmedBranch, ConfirmedStarterMap, StarterCases

if TYPE_CHECKING:
    from opentour_cell.pilot.starter_map_confirmation import StarterCaseSession

Json = dict[str, Any]
SCHEMA = "opentour.pilot.starter-coverage-receipt.v1"
HANDOFF = "opentour.pilot.starter-coverage-handoff.v1"
ORIGIN = "expert_reviewed_generated_starter"
NEXT_EVIDENCE = "collect_independent_reserved_family"
_SPLITS = {"development", "reserved", "excluded"}
_DISPOSITION = {
    "reserved": "reserved_evaluation",
    "development": "development",
    "excluded": "excluded_by_owner",
}


def _require(ok: object, reason: str = "starter_coverage_receipt_invalid") -> None:
    if not ok:
        raise IntakeError(reason)


def _material(branch: ConfirmedBranch) -> tuple[str, ...]:
    """The freeze's own coverage cells: explicit material actions, else all permitted."""
    return branch.allowed_actions if branch.coverage_actions is None else branch.coverage_actions


def _families(
    rows: list[Json], *, split: str | None = None, targets: set[str] | None = None
) -> set[str]:
    return {
        row["family_id"]
        for row in rows
        if (split is None or row["split_ref"] == split)
        and (targets is None or row["owner_adjudicated_target"] in targets)
    }


def _adjudication(case: Json) -> str:
    target = case["owner_adjudicated_target"]
    if target is None:
        return "unresolved"
    return "accept" if target == case["suggestion"] else "edit"


def _reserved_ref(pack_ref: str, family: str) -> str:
    return "starter-family-" + digest([pack_ref, family])[:40]


def build_receipt(scope: ConfirmedStarterMap, pack_ref: str, pack: Json) -> Json:
    """Pure projection. Raise rather than describe a pack that disagrees with its freeze."""
    frozen = pack.get("frozen")
    _require(frozen is not None, "starter_freeze_required")
    assert frozen is not None
    _require(frozen["state"] == "frozen_unscored" and frozen["evidence_origin"] == ORIGIN)
    branches = {b.branch_id: b for b in scope.branches}
    _require(branches and len(branches) == len(scope.branches))
    _require(all(_material(b) for b in scope.branches))
    cases: list[Json] = pack["cases"]
    _require(isinstance(cases, list) and cases)
    families: dict[str, Json] = {}
    case_rows = []
    for case in cases:
        branch, split, status = case["branch_id"], case["split_ref"], case["status"]
        _require(branch in branches and split in _SPLITS and status in {"resolved", "unresolved"})
        target = case["owner_adjudicated_target"]
        _require(
            case["adjudicator"] == scope.owner_id
            and (status == "resolved") == (target is not None)
            and (target is None or target in branches[branch].allowed_actions)
            and (status == "resolved" or split == "excluded")
            and case["case_origin"] in {"generated_starter", "owner_counterexample"}
        )
        family = families.setdefault(
            case["family_id"], {"split": split, "cases": 0, "origins": set(), "branches": set()}
        )
        _require(family["split"] == split, "starter_family_conflict")
        family["cases"] += 1
        family["origins"].add(case["case_origin"])
        family["branches"].add(branch)
        case_rows.append(
            {
                "case_id": case["case_id"],
                "family_id": case["family_id"],
                "branch_id": branch,
                "origin": case["case_origin"],
                "status": status,
                "split": split,
                "adjudication": _adjudication(case),
                "disposition": "excluded_unresolved"
                if status == "unresolved"
                else _DISPOSITION[split],
            }
        )
    _require(len({row["case_id"] for row in case_rows}) == len(case_rows))
    reserved_refs = tuple(
        _reserved_ref(pack_ref, family)
        for family in sorted(families)
        if families[family]["split"] == "reserved"
    )
    stored = {(b, a): n for b, a, n in frozen["coverage"]}
    _require(len(stored) == len(frozen["coverage"]))
    branch_rows = []
    held = []
    for branch in scope.branches:
        on_branch = [c for c in cases if c["branch_id"] == branch.branch_id]
        cells = []
        for action in _material(branch):
            count = len(_families(on_branch, split="reserved", targets={action}))
            _require(stored.pop((branch.branch_id, action), None) == count)
            cells.append({"action": action, "reserved_families": count})
        covering = _families(on_branch, split="reserved", targets=set(_material(branch)))
        reserved = _families(on_branch, split="reserved")
        empty = sum(cell["reserved_families"] == 0 for cell in cells)
        if empty and branch.critical:
            held.append(branch.branch_id)
        reserved_cases = sum(
            c["split_ref"] == "reserved" and c["status"] == "resolved" for c in on_branch
        )
        branch_rows.append(
            {
                "branch_id": branch.branch_id,
                "critical": branch.critical,
                "numerator": len(covering),
                "denominator": len(_families(on_branch)),
                "cells": cells,
                "reserved_cases": reserved_cases,
                "reserved_families_other_action": len(reserved - covering),
                "development_families": len(_families(on_branch, split="development")),
                "excluded_families": len(_families(on_branch, split="excluded")),
                "unresolved_cases": sum(c["status"] == "unresolved" for c in on_branch),
                "state": "covered"
                if not empty
                else "uncovered"
                if empty == len(cells)
                else "partially_covered",
                "verdict": "in_scope"
                if not empty
                else "held"
                if branch.critical
                else "out_of_scope",
                "per_branch_rates": "available" if reserved_cases else "unavailable",
                "next_evidence": NEXT_EVIDENCE if empty else None,
            }
        )
    # Every frozen cell was re-derived; a leftover stored cell is not this scope's freeze.
    _require(not stored)
    splits = {family: data["split"] for family, data in families.items()}
    totals = {
        "proposed": len(cases),
        "reviewed": sum(c["adjudicator"] is not None for c in cases),
        "resolved": sum(c["status"] == "resolved" for c in cases),
        "unresolved": sum(c["status"] == "unresolved" for c in cases),
        "development": sum(s == "development" for s in splits.values()),
        "reserved": sum(s == "reserved" for s in splits.values()),
        "excluded": sum(c["split_ref"] == "excluded" for c in cases),
    }
    _require(
        all(frozen[key] == value for key, value in totals.items())
        and tuple(frozen["reserved_family_refs"]) == reserved_refs
        and tuple(frozen["uncovered_critical"]) == tuple(sorted(held))
    )
    receipt: Json = {
        "schema_version": SCHEMA,
        "pack_ref": pack_ref,
        "workflow_ref": scope.work.workflow_ref,
        "run_ref": scope.work.run_ref,
        "policy_revision": scope.policy_revision,
        "settings_revision": scope.settings_revision,
        "map_ref": scope.map_ref,
        "freeze_ref": frozen["reference"],
        "freeze_revision": frozen["revision"],
        "state": frozen["state"],
        "evidence_origin": ORIGIN,
        "audit": {
            "pack_seal": digest(pack),
            "plan_digest": digest(pack["plan"]),
            "adjudicators": sorted({c["adjudicator"] for c in cases}),
            "edited_labels": sum(row["adjudication"] == "edit" for row in case_rows),
        },
        "totals": totals,
        "branches": branch_rows,
        "families": [
            {
                "family_id": family,
                "split": data["split"],
                "reserved_ref": _reserved_ref(pack_ref, family)
                if data["split"] == "reserved"
                else None,
                "cases": data["cases"],
                "counted_as": 1,
                "origins": sorted(data["origins"]),
                "branches": sorted(data["branches"]),
            }
            for family, data in sorted(families.items())
        ],
        "cases": case_rows,
        "duplicate_cases_collapsed": sum(data["cases"] - 1 for data in families.values()),
        "claims": {
            "all_branches_verdict": all(row["state"] == "covered" for row in branch_rows),
            "verdict_scope": [row["branch_id"] for row in branch_rows if row["state"] == "covered"],
            "held_branches": sorted(held),
            "training_credit": 0,
            "live_recurrence": 0,
        },
        "reserved_family_refs": list(reserved_refs),
    }
    receipt["receipt_digest"] = digest(receipt)
    receipt["receipt_ref"] = "starter-coverage-receipt-" + receipt["receipt_digest"][:40]
    return receipt


def verify_receipt(receipt: Json, scope: ConfirmedStarterMap, pack_ref: str, pack: Json) -> None:
    """Independent replay: the receipt must be exactly the projection of this frozen pack."""
    body = {k: v for k, v in receipt.items() if k not in {"receipt_digest", "receipt_ref"}}
    _require(
        receipt.get("receipt_digest") == digest(body)
        and receipt.get("receipt_ref") == "starter-coverage-receipt-" + digest(body)[:40]
        and canonical(build_receipt(scope, pack_ref, pack)) == canonical(receipt),
        "starter_coverage_receipt_mismatch",
    )


def handoff(receipt: Json) -> Json:
    """Evaluator-safe references and totals for OPE-539 and OPE-526."""
    totals = receipt["totals"]
    return {
        "schema_version": HANDOFF,
        "freeze_ref": receipt["freeze_ref"],
        "freeze_revision": receipt["freeze_revision"],
        "coverage_receipt_ref": receipt["receipt_ref"],
        "coverage_receipt_digest": receipt["receipt_digest"],
        "reserved_family_refs": list(receipt["reserved_family_refs"]),
        "reserved_families": totals["reserved"],
        "reserved_cases": sum(row["split"] == "reserved" for row in receipt["cases"]),
        "unresolved": totals["unresolved"],
        "excluded": totals["excluded"],
        "state": receipt["state"],
        "evidence_origin": receipt["evidence_origin"],
        "training_credit": 0,
    }


def owner_coverage_receipt(cases: StarterCases, actor: IntakePrincipal, work: IntakeWork) -> Json:
    """Named owner only, under the core's current review authority; read-only."""
    with cases._scope(actor, work, "review") as scope, cases._db() as db:
        pack = cases._load(db, scope)
        cases._validate_prior_freezes(db, scope)
        return build_receipt(scope, StarterCases._ref(scope), pack)


def session_coverage_receipt(session: StarterCaseSession) -> Json:
    """The installed owner entry: current case grant, map confirmation and owner binding."""
    with session._pin("review") as pinned:
        return owner_coverage_receipt(session.core, pinned.principal, pinned.scope.work)
