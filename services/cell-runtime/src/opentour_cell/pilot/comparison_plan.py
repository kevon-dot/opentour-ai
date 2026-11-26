"""Freeze all candidate predictions before protected evaluation and reserve the cohort once."""

from __future__ import annotations

import json
import re
import sqlite3
import time
from collections.abc import Callable
from itertools import combinations
from typing import Any

from opentour_cell.pilot.operating_point import canonical, digest
from opentour_cell.pilot.pilot_metrics import (
    amount,
    arm_metrics,
    economic_comparison,
    economics,
    operating_verdict,
)
from opentour_cell.pilot.readout import INPUT_VERSION, evaluate_manifest
from opentour_cell.pilot.simple_repair import (
    MAX_ROWS,
    RepairError,
    apply_frozen_rule,
    assess_development,
    decision,
    fraction_bps,
    identifier,
    membership,
    revision,
    row_bound,
    validate_rule,
)
from opentour_cell.pilot.training import LocalTrainingStore

Json = dict[str, Any]
PLAN = "opentour.pilot.protected-plan.v1"
RESULT = "opentour.pilot.protected-comparison.v1"
_REGISTRY = """CREATE TABLE IF NOT EXISTS protected_comparisons(
 revision TEXT PRIMARY KEY, reserved_at INTEGER NOT NULL, result TEXT)"""


def _terms(value: Any) -> Json:
    allowed = {
        "minimum_coverage_bps",
        "error_ceiling_bps",
        "minimum_development_cases",
        "minimum_residual_errors",
        "minimum_verified_cases",
        "minimum_slice_cases",
        "critical_slices",
        "material_slices",
        "metric",
        "max_cost_per_1000",
        "max_latency_ms",
    }
    if not isinstance(value, dict) or not value.keys() <= allowed:
        raise RepairError("invalid_protected_terms")
    fraction_bps(value, "minimum_coverage_bps")
    fraction_bps(value, "error_ceiling_bps")
    for key in (
        "minimum_development_cases",
        "minimum_residual_errors",
        "minimum_verified_cases",
        "minimum_slice_cases",
    ):
        if key in value:
            row_bound(value[key], minimum=2 if key == "minimum_residual_errors" else 1)
    for key in ("critical_slices", "material_slices"):
        items = value.get(key, [])
        if (
            not isinstance(items, list)
            or len(items) > 32
            or len({identifier(v) for v in items}) != len(items)
        ):
            raise RepairError("invalid_agreed_slice")
    if "metric" in value:
        identifier(value["metric"])
    if "max_cost_per_1000" in value:
        cap = value["max_cost_per_1000"]
        if (
            not isinstance(cap, dict)
            or set(cap) != {"currency", "amount"}
            or not isinstance(cap["currency"], str)
            or not re.fullmatch("[A-Z]{3}", cap["currency"])
        ):
            raise RepairError("invalid_cost_limit")
        amount(cap["amount"])
    if "max_latency_ms" in value:
        amount(value["max_latency_ms"])
    return value


def _prediction_revision(ids: list[str], predictions: Json | None) -> str | None:
    if predictions is None:
        return None
    if not isinstance(predictions, dict) or set(predictions) != set(ids):
        raise RepairError("model_prediction_population_mismatch")
    for value in predictions.values():
        decision(value)
    return digest(predictions)


def freeze_plan(
    *,
    organization: str,
    engagement: str,
    audience: str,
    intake_revision: str,
    protected_ids: list[str],
    incumbent_revision: str,
    rule: Json,
    development_decision: Json,
    terms: Json,
    model_revision: str | None = None,
    model_predictions: Json | None = None,
    measurements: Json | None = None,
    model_budget_revision: str | None = None,
) -> Json:
    for item in (organization, engagement, audience, incumbent_revision):
        identifier(item)
    revision(intake_revision)
    validate_rule(rule)
    _terms(terms)
    dev = development_decision
    stripped = {k: v for k, v in dev.items() if k not in ("revision", "residual_revision")}
    if (
        dev.get("revision") != digest(stripped)
        or dev.get("residual_revision") != dev["revision"]
        or dev.get("rule_revision") != rule["revision"]
        or dev.get("terms_revision") != digest(terms)
        or dev.get("route") not in ("simple", "model", "no_change", "hold")
    ):
        raise RepairError("unverified_development_decision")
    if not isinstance(protected_ids, list) or len(protected_ids) > MAX_ROWS:
        raise RepairError("invalid_protected_population")
    ids = sorted(identifier(i) for i in protected_ids)
    if len(ids) != len(set(ids)) or set(dev["development_case_keys"]).intersection(
        digest(["case", i]) for i in ids
    ):
        raise RepairError("protected_development_overlap")
    if dev["route"] == "model":
        revision(model_revision)
        revision(model_budget_revision)
        if (
            model_predictions is None
            or not dev["model_authorized"]
            or not dev["compute_authorized"]
        ):
            raise RepairError("frozen_model_and_approval_required")
    elif any(v is not None for v in (model_revision, model_predictions, model_budget_revision)):
        raise RepairError("model_not_on_approved_route")
    result = {
        "schema_version": PLAN,
        "organization": organization,
        "engagement": engagement,
        "audience": audience,
        "intake_revision": intake_revision,
        "protected_membership_digest": digest(ids),
        "protected_count": len(ids),
        "incumbent_revision": incumbent_revision,
        "rule_revision": rule["revision"],
        "model_revision": model_revision,
        "model_predictions_revision": _prediction_revision(ids, model_predictions),
        "model_budget_revision": model_budget_revision,
        "development_decision": dev,
        "terms": terms,
        "measurements_revision": digest(measurements) if measurements is not None else None,
    }
    return {**result, "revision": digest(result)}


def _check_plan(plan: Json) -> None:
    if plan.get("schema_version") != PLAN or plan.get("revision") != digest(
        {k: v for k, v in plan.items() if k != "revision"}
    ):
        raise RepairError("protected_plan_revision_mismatch")


def development_hold(plan: Json) -> Json:
    _check_plan(plan)
    dev = plan["development_decision"]
    if dev["route"] != "hold":
        raise RepairError("development_hold_required")
    result = {
        "schema_version": RESULT,
        "organization": plan["organization"],
        "engagement": plan["engagement"],
        "audience": plan["audience"],
        "plan_revision": plan["revision"],
        "intake_revision": plan["intake_revision"],
        "protected_membership_digest": plan["protected_membership_digest"],
        "outcome": "hold",
        "arms": {},
        "comparisons": {},
        "paired_cases": [],
        "protected_rows_evaluated": 0,
        "decision": {
            "disposition": "hold",
            "winner": None,
            "fallback": "incumbent",
            "reason": dev["reason"],
        },
    }
    return dict(result, evidence_revision=digest(result))


def _check_inputs(
    store: LocalTrainingStore,
    plan: Json,
    intake: Json,
    rule: Json,
    model_predictions: Json | None,
    measurements: Json | None,
) -> list[Json]:
    _check_plan(plan)
    validate_rule(rule)
    if (
        plan["organization"] != store.organization
        or plan["engagement"] != store.engagement_id
        or intake.get("organization") != plan["organization"]
        or intake.get("engagement") != plan["engagement"]
    ):
        raise RepairError("protected_scope_mismatch")
    if (
        intake.get("intake_revision") != plan["intake_revision"]
        or digest({k: v for k, v in intake.items() if k != "intake_revision"})
        != plan["intake_revision"]
    ):
        raise RepairError("protected_intake_revision_mismatch")
    rows = intake["protected"]
    if not isinstance(rows, list) or len(rows) != plan["protected_count"]:
        raise RepairError("protected_population_mismatch")
    ids = sorted(identifier(row["case_id"]) for row in rows)
    if (
        digest(ids) != plan["protected_membership_digest"]
        or digest(ids) != intake["protected_membership_digest"]
    ):
        raise RepairError("protected_population_mismatch")
    dev = plan["development_decision"]
    if (
        rule["revision"] != plan["rule_revision"]
        or _prediction_revision(ids, model_predictions) != plan["model_predictions_revision"]
        or (digest(measurements) if measurements is not None else None)
        != plan["measurements_revision"]
    ):
        raise RepairError("protected_artifact_revision_mismatch")
    if rule["feature"] not in intake["allowed_features"] or not set(
        rule["allowed_features"]
    ) <= set(intake["allowed_features"]):
        raise RepairError("rule_feature_not_permitted")
    if set(membership(rows, "group_id")).intersection(dev["development_group_keys"]):
        raise RepairError("protected_development_overlap")
    if rule["source"] == "development_lookup" or plan["model_revision"] is not None:
        if (
            not intake.get("training_allowed")
            or not intake.get("training_expires_at")
            or intake["training_expires_at"] <= time.time()
        ):
            raise RepairError("learning_permission_expired")
    if plan["model_revision"] is not None:
        approved = intake.get("error_class")
        recurrence = plan["terms"].get("minimum_residual_errors", 2)
        if not isinstance(approved, dict) or not any(
            c["from_label"] == approved.get("from_label")
            and c["to_label"] == approved.get("to_label")
            and c["groups"] >= recurrence
            for c in dev["residual_classes"]
        ):
            raise RepairError("owner_confirmed_recurring_error_required")
    actual = assess_development(
        intake["development"],
        rule,
        terms=plan["terms"],
        model_authorized=dev["model_authorized"],
        compute_authorized=dev["compute_authorized"],
    )
    if actual["revision"] != dev["revision"]:
        raise RepairError("protected_development_revision_mismatch")
    slices = set(plan["terms"].get("critical_slices", [])) | set(
        plan["terms"].get("material_slices", [])
    )
    if not slices <= set(intake.get("allowed_slices", [])):
        raise RepairError("unpermitted_slice")
    _live(intake)
    return sorted(rows, key=lambda item: item["case_id"])


def _live(intake: Json) -> None:
    if type(intake.get("expires_at")) is not int or intake["expires_at"] <= time.time():
        raise RepairError("protected_intake_expired")


def validate_development_partition(store: LocalTrainingStore, development: list[Json]) -> None:
    """Deny earlier protected rows or families before fitting even a low-cost repair."""
    with store._db() as db:
        for key in (*membership(development, "case_id"), *membership(development, "group_id")):
            prior = db.execute(
                "SELECT split FROM split_keys WHERE fingerprint=?", (key,)
            ).fetchone()
            if prior is not None and prior["split"] != "train":
                raise RepairError("development_contains_previously_protected_data")


def _reserve(db: sqlite3.Connection, plan: Json, rows: list[Json]) -> None:
    db.execute(_REGISTRY)
    if db.execute(
        "SELECT 1 FROM protected_comparisons WHERE revision=?", (plan["revision"],)
    ).fetchone():
        return
    keys = set(membership(rows, "case_id")) | set(membership(rows, "group_id"))
    for key in (
        *plan["development_decision"]["development_case_keys"],
        *plan["development_decision"]["development_group_keys"],
    ):
        prior = db.execute("SELECT split FROM split_keys WHERE fingerprint=?", (key,)).fetchone()
        if prior is not None and prior["split"] != "train":
            raise RepairError("development_contains_previously_protected_data")
        db.execute("INSERT OR IGNORE INTO split_keys(fingerprint,split) VALUES(?,'train')", (key,))
    claim = "protect-" + plan["revision"]
    for key in sorted(keys):
        # These are the same fingerprints, split ledger and reservation column used by
        # the v1 fit. A previously scored supervised or supplied alias makes a new test invalid.
        prior = db.execute(
            "SELECT split,used_by FROM split_keys WHERE fingerprint=?", (key,)
        ).fetchone()
        supplied = db.execute(
            "SELECT 1 FROM evaluation_keys e JOIN dataset_scope s ON s.dataset=e.dataset "
            "WHERE e.fingerprint=? AND s.baseline_result IS NOT NULL LIMIT 1",
            (key,),
        ).fetchone()
        if supplied or (prior and (prior["split"] != "heldout" or prior["used_by"] is not None)):
            raise RepairError("fresh_protected_cohort_required")
        db.execute(
            "INSERT INTO split_keys(fingerprint,split,used_by) VALUES(?,'heldout',?) "
            "ON CONFLICT(fingerprint) DO UPDATE SET used_by=excluded.used_by",
            (key, claim),
        )
    db.execute(
        "INSERT INTO protected_comparisons VALUES(?,?,NULL)", (plan["revision"], time.time_ns())
    )


def _pair(rows: list[Json], a: str, b: str, ceiling: int) -> Json:
    return evaluate_manifest(
        {
            "schema_version": INPUT_VERSION,
            "error_ceiling_bps": ceiling,
            "cases": [
                {
                    "case_id": row["case_id"],
                    "reference": row["reference"],
                    "current": row[a],
                    "repair": row[b],
                }
                for row in rows
            ],
        }
    )


def _checks(rows: list[Json], terms: Json, m: Json, critical: Json) -> str | None:
    if not rows or m["observed_error_rate"]["denominator"] < terms.get("minimum_verified_cases", 1):
        return "insufficient_protected_support"
    if m["missing"] or m["unknown_reference_automatic"]:
        return "incomplete_protected_evidence"
    if m["operating_point"]["combined"] != "met":
        return "operating_point_missed_or_undetermined"
    if any(v["status"] != "met" for v in critical.values()):
        return "critical_slice_failed_or_unverified"
    econ = m["economics"]
    if "max_cost_per_1000" in terms and econ["cost_cap"] != "met":
        return "cost_limit_failed_or_unverified"
    if "max_latency_ms" in terms and econ["latency_cap"] != "met":
        return "latency_limit_failed_or_unverified"
    return None


def _slice_results(rows: list[Json], arm: str, terms: Json) -> Json:
    result = {}
    for name in terms.get("critical_slices", []):
        selected = [r for r in rows if name in r["slices"]]
        groups = {r["group_id"] for r in selected if r["reference"] is not None}
        pair = _pair(selected, "incumbent", arm, terms["error_ceiling_bps"])
        gate = operating_verdict(pair["repair"], len(selected), terms["minimum_coverage_bps"])
        if len(groups) < terms.get("minimum_slice_cases", 2):
            status = "undetermined"
        elif (
            pair["paired"]["regressions"]
            or pair["repair"]["missing"]
            or not pair["changes"]["same_automatic_cases"]
            or pair["observed_comparison"] == "insufficient_evidence"
        ):
            status = "missed"
        else:
            status = gate["combined"]
        result[name] = {
            "cases": len(selected),
            "independent_groups": len(groups),
            "status": status,
            "operating_point": gate,
            "paired": pair["paired"],
        }
    return result


def _pick(arms: Json, pairs: Json, terms: Json) -> Json:
    def outcome(disposition: str, winner: str | None, reason: str) -> Json:
        return {
            "disposition": disposition,
            "winner": winner,
            "fallback": "incumbent",
            "reason": reason,
        }

    reasons = []
    eligible = []
    for name in arms:
        if name == "incumbent":
            continue
        match = pairs["incumbent_vs_" + name]
        if arms[name]["hold_reason"]:
            reasons.append(arms[name]["hold_reason"])
            continue
        if match["observed_comparison"] == "equivalent":
            reasons.append("equivalent_to_incumbent")
            continue
        if match["observed_comparison"] != "improved":
            reasons.append(match["observed_comparison"])
            continue
        costs = match["economics"]
        if "max_cost_per_1000" not in terms and costs.get("observed_cost_increased") is True:
            reasons.append("quality_cost_tradeoff_requires_owner_choice")
            continue
        eligible.append(name)
    if "model" in eligible and "simple" in eligible:
        incremental = pairs["simple_vs_model"]
        if (
            "max_cost_per_1000" not in terms
            and incremental["economics"].get("observed_cost_increased") is True
        ):
            return outcome("recommend", "simple", "adequate_simple_repair_at_lower_observed_cost")
        selected = "model" if incremental["observed_comparison"] == "improved" else "simple"
    elif eligible:
        selected = eligible[0]
    else:
        if (
            reasons
            and all(r == "equivalent_to_incumbent" for r in reasons)
            and not arms["incumbent"]["hold_reason"]
        ):
            return outcome("no_change", "incumbent", "no_confirmed_improvement")
        return outcome("hold", None, reasons[0] if reasons else "insufficient_protected_support")
    return outcome("recommend", selected, "protected_improvement_at_agreed_limits")


def _score(
    plan: Json,
    intake: Json,
    inputs: list[Json],
    rule: Json,
    model: Json | None,
    measurements: Json | None,
    reserved_at: int,
    saved_stages: Json | None = None,
) -> Json:
    started = time.time_ns()
    terms = plan["terms"]
    rows = []
    arms = {"incumbent": plan["incumbent_revision"], "simple": rule["revision"]}
    if model is not None:
        arms["model"] = plan["model_revision"]
    if measurements is not None and (
        not isinstance(measurements, dict) or not measurements.keys() <= arms.keys()
    ):
        raise RepairError("measurements_arm_outside_plan")
    for row in inputs:
        baseline = decision(row["incumbent"])
        calculated = {
            "case_id": row["case_id"],
            "group_id": identifier(row["group_id"]),
            "slices": row.get("slices", []),
            "reference": row["reference"],
            "incumbent": baseline,
            "simple": apply_frozen_rule(rule, row["features"], baseline),
        }
        if model is not None:
            calculated["model"] = decision(model[row["case_id"]])
        rows.append(calculated)
    results = {}
    for name, rev in arms.items():
        arm = _pair(rows, name, name, terms["error_ceiling_bps"])["current"]
        metrics = arm_metrics(arm, rows, name, terms)
        metrics["economics"] = economics(rows, (measurements or {}).get(name), terms)
        critical = _slice_results(rows, name, terms)
        results[name] = {
            "revision": rev,
            "prediction_digest": digest({r["case_id"]: r[name] for r in rows}),
            "metrics": metrics,
            "critical_slices": critical,
            "hold_reason": _checks(rows, terms, metrics, critical),
        }
    pairs = {}
    for a, b in combinations(arms, 2):
        pair = _pair(rows, a, b, terms["error_ceiling_bps"])
        pairs[a + "_vs_" + b] = {k: pair[k] for k in ("observed_comparison", "paired", "changes")}
        pairs[a + "_vs_" + b]["equal_coverage"] = (
            pair["current"]["automatic"] == pair["repair"]["automatic"]
        )
        pairs[a + "_vs_" + b]["economics"] = economic_comparison(
            results[a]["metrics"]["economics"], results[b]["metrics"]["economics"]
        )
    selected = _pick(results, pairs, terms)
    winner = selected["winner"]
    outcome = {
        "simple": "simple_repair",
        "model": "model_candidate",
        "incumbent": "no_change",
        None: "hold",
    }[winner]
    reports = [{k: v for k, v in r.items() if k not in ("group_id", "slices")} for r in rows]
    result = {
        "schema_version": RESULT,
        "organization": plan["organization"],
        "engagement": plan["engagement"],
        "audience": plan["audience"],
        "plan_revision": plan["revision"],
        "intake_revision": plan["intake_revision"],
        "protected_membership_digest": plan["protected_membership_digest"],
        "protected_rows_evaluated": len(rows),
        "terms": terms,
        "source_revision": digest(intake["sources"]),
        "authority_revision": intake["authority_revision"],
        "policy_version": intake["policy_version"],
        "source_schema_version": intake["source_schema_version"],
        "exclusions": intake["excluded_counts"],
        "split": intake["split"],
        "metric": terms.get("metric", "verified_wrong_automatic"),
        "independent_reference_digest": digest(
            [
                [
                    r["case_id"],
                    r["reference"],
                    r.get("outcome_id"),
                    r.get("adjudicator_id"),
                    r.get("verified_at"),
                ]
                for r in inputs
            ]
        ),
        "stages": saved_stages
        if saved_stages is not None
        else {
            "protected_reserved_unix_ns": reserved_at,
            "score_started_unix_ns": started,
            "score_completed_unix_ns": time.time_ns(),
        },
        "arms": results,
        "comparisons": pairs,
        "paired_cases": reports,
        "outcome": outcome,
        "decision": selected,
    }
    return dict(result, evidence_revision=digest(result))


def _stored(record: str, plan: Json) -> Json:
    try:
        result: Json = json.loads(record)
        if (
            result["schema_version"] != RESULT
            or result["plan_revision"] != plan["revision"]
            or any(
                result[k] != plan[k]
                for k in (
                    "organization",
                    "engagement",
                    "audience",
                    "intake_revision",
                    "protected_membership_digest",
                )
            )
        ):
            raise RepairError("protected_result_digest_mismatch")
        if result["evidence_revision"] != digest(
            {k: v for k, v in result.items() if k != "evidence_revision"}
        ):
            raise RepairError("protected_result_digest_mismatch")
        return result
    except (TypeError, KeyError, json.JSONDecodeError):
        raise RepairError("protected_result_digest_mismatch") from None


def run_comparison(
    store: LocalTrainingStore,
    plan: Json,
    intake: Json,
    *,
    rule: Json,
    model_predictions: Json | None = None,
    measurements: Json | None = None,
    authorize: Callable[[], None] | None = None,
    replay: bool = False,
) -> Json:
    """Reserve before scoring. A failed run can only resume its identical frozen comparison."""
    _check_plan(plan)
    if plan["organization"] != store.organization or plan["engagement"] != store.engagement_id:
        raise RepairError("protected_scope_mismatch")
    if authorize is None:
        from opentour_cell.pilot.source_intake import check_current_intake

        def current_authority() -> None:
            check_current_intake(
                store.path.parent,
                store.organization,
                plan["engagement"],
                plan["intake_revision"],
                require_training=(
                    rule.get("source") == "development_lookup" or plan["model_revision"] is not None
                ),
            )
    else:
        current_authority = authorize
    current_authority()
    if plan["development_decision"]["route"] == "hold":
        return development_hold(plan)
    rows = _check_inputs(store, plan, intake, rule, model_predictions, measurements)
    # Commit reservations separately. A runtime error after unblinding cannot release labels
    # for another plan. The second serialized transaction computes and persists at most once.
    if not replay:
        with store._db(write=True) as db:
            _live(intake)
            _reserve(db, plan, rows)
    with store._db(write=not replay) as db:
        _live(intake)
        if (
            replay
            and db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='protected_comparisons'"
            ).fetchone()
            is None
        ):
            raise RepairError("frozen_result_required")
        record = db.execute(
            "SELECT reserved_at,result FROM protected_comparisons WHERE revision=?",
            (plan["revision"],),
        ).fetchone()
        if record is None or (replay and not record["result"]):
            raise RepairError("frozen_result_required")
        if record["result"] and not replay:
            result = _stored(record["result"], plan)
        else:
            result = _score(
                plan,
                intake,
                rows,
                rule,
                model_predictions,
                measurements,
                record["reserved_at"],
                _stored(record["result"], plan)["stages"] if replay else None,
            )
            if replay:
                if _stored(record["result"], plan) != result:
                    raise RepairError("protected_result_repeat_mismatch")
            else:
                db.execute(
                    "UPDATE protected_comparisons SET result=? WHERE revision=?",
                    (canonical(result), plan["revision"]),
                )
        _live(intake)
    current_authority()
    return result
