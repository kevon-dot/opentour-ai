"""A frozen one-feature lookup and a protected-label-free development decision."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from typing import Any

from opentour_cell.pilot.operating_point import digest
from opentour_cell.pilot.readout import INPUT_VERSION, ManifestError, evaluate_manifest

Json = dict[str, Any]
RULE_VERSION = "opentour.pilot.simple-rule.v1"
MAX_ROWS = 5_000
_RESERVED = {
    "reference",
    "label",
    "outcome",
    "outcome_id",
    "adjudicator_id",
    "verified_at",
    "decision_at",
    "event_id",
    "organization",
    "case_id",
    "group_id",
    "customer_id",
    "material_error",
    "protected",
    "split",
    "incumbent",
    "correction",
    "target",
}


class RepairError(ValueError):
    """Operator-safe fixed reason for a held repair workflow."""


def identifier(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value):
        raise RepairError("invalid_scope_or_identifier")
    return value


def _text(value: Any) -> str:
    if not isinstance(value, str) or not (0 < len(value) <= 256) or value.strip() != value:
        raise RepairError("invalid_rule_value")
    if not value.isprintable():
        raise RepairError("invalid_rule_value")
    return value


def revision(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch("[a-f0-9]{64}", value):
        raise RepairError("invalid_revision")
    return value


def decision(value: Any) -> Json:
    if isinstance(value, dict):
        if set(value) == {"status"} and value["status"] in ("missing", "review"):
            return {"status": value["status"]}
        if set(value) == {"status", "label"} and value["status"] == "automatic":
            return {"status": "automatic", "label": _text(value["label"])}
    raise RepairError("invalid_method_decision")


def fraction_bps(terms: Json, key: str) -> int:
    value = terms.get(key)
    if type(value) is not int or not 0 <= value <= 10_000:
        raise RepairError("invalid_operating_limits")
    return value


def row_bound(value: Any, *, minimum: int = 1) -> int:
    if type(value) is not int or not minimum <= value <= MAX_ROWS:
        raise RepairError("invalid_sample_requirement")
    return value


def validate_features(feature: str, allowed: Any) -> list[str]:
    if not isinstance(allowed, (list, tuple)) or not 1 <= len(allowed) <= 64:
        raise RepairError("decision_time_feature_required")
    fields = sorted({identifier(f) for f in allowed})
    if feature not in fields or _RESERVED.intersection(x.lower() for x in fields):
        raise RepairError("decision_time_feature_required")
    return fields


def _rows(rows: Any, feature: str) -> list[Json]:
    if not isinstance(rows, list) or len(rows) > MAX_ROWS:
        raise RepairError("invalid_development_rows")
    selected = []
    ids = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("features"), dict):
            raise RepairError("invalid_development_rows")
        case_id, group_id = identifier(row.get("case_id")), identifier(row.get("group_id"))
        if case_id in ids:
            raise RepairError("duplicate_case")
        ids.add(case_id)
        raw = row["features"].get(feature)
        value = None if raw is None else _text(raw)
        reference = row.get("reference")
        selected.append(
            {
                "case_id": case_id,
                "group_id": group_id,
                "value": value,
                "reference": None if reference is None else _text(reference),
                "incumbent": decision(row.get("incumbent")),
            }
        )
    return sorted(selected, key=lambda row: row["case_id"])


def _votes(rows: list[Json]) -> tuple[dict[str, Counter[str]], dict[tuple[str, str], str]]:
    # An independent group casts at most one consistent vote per observed feature value.
    labels: defaultdict[tuple[str, str], set[str]] = defaultdict(set)
    for row in rows:
        if row["reference"] is not None and row["value"] is not None:
            labels[row["value"], row["group_id"]].add(row["reference"])
    winners = {key: next(iter(v)) for key, v in labels.items() if len(v) == 1}
    counts: defaultdict[str, Counter[str]] = defaultdict(Counter)
    for (value, _), label in winners.items():
        counts[value][label] += 1
    return dict(counts), winners


def _winner(counts: Counter[str], minimum: int) -> str | None:
    top = counts.most_common(2)
    if not top or top[0][1] < minimum or (len(top) == 2 and top[0][1] == top[1][1]):
        return None
    return top[0][0]


def _make_rule(
    feature: str,
    allowed: Any,
    mapping: Json,
    fallback: str,
    minimum: int,
    source: str,
    development: str | None,
) -> Json:
    fields = validate_features(feature, allowed)
    if (
        fallback not in ("review", "incumbent")
        or not isinstance(mapping, dict)
        or len(mapping) > MAX_ROWS
    ):
        raise RepairError("invalid_rule_mapping_or_fallback")
    lookup = {_text(k): _text(v) for k, v in mapping.items()}
    rule = {
        "schema_version": RULE_VERSION,
        "source": source,
        "feature": feature,
        "allowed_features": fields,
        "mapping": lookup,
        "fallback": fallback,
        "min_support": row_bound(minimum),
        "development_revision": development,
    }
    return dict(rule, revision=digest(rule))


def declared_rule(
    *,
    feature: str,
    allowed_features: list[str],
    mapping: Json,
    fallback: str = "review",
    allowed_labels: list[str] | None = None,
) -> Json:
    rule = _make_rule(feature, allowed_features, mapping, fallback, 1, "owner_declared", None)
    if allowed_labels is not None and not set(rule["mapping"].values()) <= {
        _text(v) for v in allowed_labels
    }:
        raise RepairError("unpermitted_output_label")
    return rule


def fit_rule(
    development: list[Json],
    *,
    feature: str,
    allowed_features: list[str],
    min_support: int = 2,
    fallback: str = "review",
) -> Json:
    validate_features(feature, allowed_features)
    minimum = row_bound(min_support)
    selected = _rows(development, feature)
    tally, _ = _votes(selected)
    mapping = {
        key: label
        for key, counts in tally.items()
        if (label := _winner(counts, minimum)) is not None
    }
    return _make_rule(
        feature,
        allowed_features,
        mapping,
        fallback,
        minimum,
        "development_lookup",
        digest(selected),
    )


def validate_rule(rule: Any) -> Json:
    if not isinstance(rule, dict) or set(rule) != {
        "schema_version",
        "source",
        "feature",
        "allowed_features",
        "mapping",
        "fallback",
        "min_support",
        "development_revision",
        "revision",
    }:
        raise RepairError("invalid_frozen_rule")
    if rule["schema_version"] != RULE_VERSION or rule["source"] not in (
        "owner_declared",
        "development_lookup",
    ):
        raise RepairError("invalid_frozen_rule")
    dev = rule["development_revision"]
    if rule["source"] == "development_lookup":
        revision(dev)
    elif dev is not None or rule["min_support"] != 1:
        raise RepairError("invalid_frozen_rule")
    if (
        _make_rule(
            rule["feature"],
            rule["allowed_features"],
            rule["mapping"],
            rule["fallback"],
            rule["min_support"],
            rule["source"],
            dev,
        )
        != rule
    ):
        raise RepairError("rule_revision_mismatch")
    return rule


def _output(label: str | None, fallback: str, incumbent: Json | None) -> Json:
    if label is not None:
        return {"status": "automatic", "label": label}
    if fallback == "incumbent":
        return decision(incumbent) if incumbent is not None else {"status": "missing"}
    return {"status": "review"}


def apply_frozen_rule(rule: Json, features: Json, incumbent: Json | None = None) -> Json:
    """Apply a previously validated artifact in a bounded case loop."""
    if not isinstance(features, dict):
        raise RepairError("invalid_decision_time_features")
    value = features.get(rule["feature"])
    label = None if value is None else rule["mapping"].get(_text(value))
    return _output(label, rule["fallback"], incumbent)


def predict_rule(rule: Json, features: Json, incumbent: Json | None = None) -> Json:
    return apply_frozen_rule(validate_rule(rule), features, incumbent)


def membership(rows: Iterable[Json], key: str) -> list[str]:
    return sorted({digest(["case" if key == "case_id" else "family", row[key]]) for row in rows})


def assess_development(
    development: list[Json],
    rule: Json,
    *,
    terms: Json,
    model_authorized: bool = False,
    compute_authorized: bool = False,
) -> Json:
    validate_rule(rule)
    if type(model_authorized) is not bool or type(compute_authorized) is not bool:
        raise RepairError("explicit_permissions_required")
    floor, ceiling = (
        fraction_bps(terms, "minimum_coverage_bps"),
        fraction_bps(terms, "error_ceiling_bps"),
    )
    minimum = row_bound(terms.get("minimum_development_cases", 2))
    recurrence = row_bound(terms.get("minimum_residual_errors", 2), minimum=2)
    rows = _rows(development, rule["feature"])
    dev_revision = digest(rows)
    if rule["source"] == "development_lookup" and rule["development_revision"] != dev_revision:
        raise RepairError("rule_development_revision_mismatch")
    votes, individual = _votes(rows)
    unseen: dict[tuple[str, str], str | None] = {}
    comparisons = []
    classes: defaultdict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
    for row in rows:
        value, group = row["value"], row["group_id"]
        label = rule["mapping"].get(value)
        if rule["source"] == "development_lookup" and value is not None:
            key = value, group
            if key not in unseen:
                tally = votes.get(value, Counter()).copy()
                if key in individual:
                    tally[individual[key]] -= 1
                unseen[key] = _winner(tally, rule["min_support"])
            label = unseen[key]
        new = _output(label, rule["fallback"], row["incumbent"])
        comparisons.append(
            {
                "case_id": row["case_id"],
                "reference": row["reference"],
                "current": row["incumbent"],
                "repair": new,
            }
        )
        ref = row["reference"]
        if ref is not None and (
            (new["status"] == "automatic" and new["label"] != ref)
            or (
                new["status"] != "automatic"
                and row["incumbent"]["status"] == "automatic"
                and row["incumbent"]["label"] != ref
            )
        ):
            current = row["incumbent"]
            from_label = current.get("label", current["status"])
            classes[from_label, ref].append((row["case_id"], group))
    try:
        report = evaluate_manifest(
            {"schema_version": INPUT_VERSION, "error_ceiling_bps": ceiling, "cases": comparisons}
        )
    except ManifestError:
        raise RepairError("invalid_incumbent_comparison") from None
    arm = report["repair"]
    # One recurring business error must span distinct families. A bag of unrelated
    # one-off mistakes cannot authorize training. Stable ordering resolves equal counts.
    ordered_classes = sorted(
        classes, key=lambda pair: (-len({g for _, g in classes[pair]}), -len(classes[pair]), pair)
    )
    residual_class = ordered_classes[0] if ordered_classes else None
    residuals = classes[residual_class] if residual_class is not None else []
    residual_groups = len({group for _, group in residuals})
    group_count = len({r["group_id"] for r in rows if r["reference"] is not None})
    repaired = (
        arm["observed_ceiling_met"] is True and arm["automatic"] * 10_000 >= len(rows) * floor
    )
    known = (
        report["known_references"] == len(rows)
        and not report["current"]["missing"]
        and not arm["missing"]
    )
    safe = report["paired"]["regressions"] == 0
    if not known or group_count < minimum:
        route, reason = "hold", "insufficient_independent_development_evidence"
    elif repaired and safe and report["paired"]["fixes"] > 0:
        route, reason = "simple", "simple_repair_meets_development_limits"
    elif repaired and safe and report["observed_comparison"] == "equivalent":
        route, reason = "no_change", "incumbent_tied_on_same_automatic_cases"
    elif residual_groups >= recurrence and model_authorized and compute_authorized:
        route, reason = "model", "recurring_development_residual_with_grants"
    elif residual_groups >= recurrence:
        route, reason = "hold", "recurring_residual_requires_model_and_compute_rights"
    else:
        route, reason = "hold", "simple_repair_unproven_and_residual_support_insufficient"
    record = {
        "schema_version": "opentour.pilot.development-decision.v1",
        "route": route,
        "reason": reason,
        "rule_revision": rule["revision"],
        "development_revision": dev_revision,
        "terms_revision": digest(terms),
        "validation": "leave_one_group_out"
        if rule["source"] == "development_lookup"
        else "owner_declared",
        "development_case_keys": membership(rows, "case_id"),
        "development_group_keys": membership(rows, "group_id"),
        "independent_groups": group_count,
        "residual_count": len(residuals),
        "residual_class": list(residual_class) if residual_class is not None else None,
        "residual_classes": [
            {
                "from_label": a,
                "to_label": b,
                "count": len(classes[a, b]),
                "groups": len({g for _, g in classes[a, b]}),
            }
            for a, b in ordered_classes
        ],
        "residual_groups": residual_groups,
        "residual_membership_digest": digest(sorted(c for c, _ in residuals)),
        "model_authorized": model_authorized,
        "compute_authorized": compute_authorized,
        "alternatives": [
            "unchanged_incumbent",
            "deterministic_lookup",
            "bounded_model_if_authorized",
        ],
        "observed_comparison": report["observed_comparison"],
        "paired": report["paired"],
        "feasibility": "fixed_lookup_on_permitted_decision_time_field",
        "inference_fields": [rule["feature"]],
        "fallback": rule["fallback"],
        "fit_calls": 0,
    }
    identity = digest(record)
    return dict(record, revision=identity, residual_revision=identity)
