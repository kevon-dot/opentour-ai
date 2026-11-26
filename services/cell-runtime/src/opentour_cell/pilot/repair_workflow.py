"""Run the customer's prospectively declared repair plan on the approved local machine."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import stat
import tempfile
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

from opentour_cell.pilot import comparison_plan, simple_repair, source_intake
from opentour_cell.pilot.operating_point import canonical, digest
from opentour_cell.pilot.training import LocalTrainingStore, _check_directory, _check_file
from opentour_cell.pilot.training_data import identifier

SCHEMA = "opentour.pilot.managed-repair.v1"
MODEL_PROFILE = "smollm2_135m_lora_v1"
MODEL_AUTH = "opentour.pilot.open-weight-permission.v1"
HEX = re.compile(r"[0-9a-f]{64}")
ATTESTATION = re.compile(r"att-[0-9a-f]{16,64}")
Json = dict[str, Any]


def _require(ok: object, reason: str) -> None:
    if not ok:
        raise ValueError(reason)


def _name(value: Any) -> str:
    _require(
        isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,95}", value),
        "invalid_private_filename",
    )
    return str(value)


def _private_json(path: Path) -> Json:
    _check_file(path)
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
        st = os.fstat(stream.fileno())
        _require(
            stat.S_ISREG(st.st_mode)
            and st.st_uid == os.getuid()
            and st.st_nlink == 1
            and not st.st_mode & 0o077
            and st.st_size <= 16_777_216,
            "private_record_required",
        )
        payload = stream.read(16_777_217)
    _require(len(payload) <= 16_777_216, "private_file_outside_profile")
    result = json.loads(payload)
    _require(isinstance(result, dict), "private_object_required")
    return dict(result)


def _record(path: Path, value: Json) -> None:
    """Publish a complete private record once, or accept the identical earlier publication."""
    _check_directory(path)
    blob = (canonical(value) + "\n").encode()
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as tmp:
        tmp.write(blob)
        tmp.flush()
        os.fsync(tmp.fileno())
        temporary = Path(tmp.name)
    try:
        try:
            os.link(temporary, path)
        except FileExistsError:
            _require(_private_json(path) == value, "existing_result_conflict")
    finally:
        temporary.unlink()


def _directory(path: Path) -> Path:
    path.mkdir(mode=0o700, exist_ok=True)
    _check_directory(path / "private")
    return path


def _context(directory: Path, organization: str, manifest: Json) -> tuple[LocalTrainingStore, Json]:
    _require(
        manifest.get("schema_version") == SCHEMA and manifest.get("organization") == organization,
        "managed_plan_scope_mismatch",
    )
    _require(
        directory.is_absolute() and directory.resolve() == directory, "private_directory_required"
    )
    identifier(organization)
    identifier(manifest.get("audience"))
    _require(
        isinstance(manifest.get("terms"), dict) and isinstance(manifest.get("rule"), dict),
        "invalid_managed_plan",
    )
    _require(
        HEX.fullmatch(str(manifest.get("intake_revision")))
        and HEX.fullmatch(str(manifest.get("incumbent_revision"))),
        "unfrozen_managed_plan",
    )
    _check_file(directory / "training.sqlite3")
    store = LocalTrainingStore(directory / "training.sqlite3", organization, existing_only=True)
    _require(
        store.engagement_active() and store.engagement_id == manifest.get("engagement"),
        "engagement_unavailable",
    )
    _name(manifest.get("intake_file"))
    intake = source_intake.inspect_intake(store, manifest["intake_revision"])
    _require(
        intake.get("intake_revision") == manifest["intake_revision"]
        and intake.get("organization") == organization
        and intake.get("engagement") == store.engagement_id,
        "intake_scope_mismatch",
    )
    return store, intake


def _rule(intake: Json, manifest: Json, development: list[Json]) -> Json:
    selected = manifest["rule"]
    allowed = intake["allowed_features"]
    if "mapping" in selected:
        return simple_repair.declared_rule(
            feature=selected["feature"],
            allowed_features=allowed,
            mapping=selected["mapping"],
            fallback=selected.get("fallback", "review"),
        )
    _require(intake.get("training_authorized") is True, "learning_permission_required")
    return simple_repair.fit_rule(
        development,
        feature=selected["feature"],
        allowed_features=allowed,
        min_support=selected.get("min_support", 2),
        fallback=selected.get("fallback", "review"),
    )


def _development(
    development: list[Json], rule: Json, manifest: Json, *, permit_model: bool
) -> Json:
    return simple_repair.assess_development(
        development,
        rule,
        terms=manifest["terms"],
        model_authorized=permit_model,
        compute_authorized=permit_model,
    )


def inspect_managed(directory: Path, organization: str, manifest: Json) -> Json:
    """Check the evidence selection and readiness without scoring the protected answers."""
    _, intake = _context(directory, organization, manifest)
    _require(manifest["rule"].get("feature") in intake["allowed_features"], "field_not_permitted")
    _require(
        isinstance(intake.get("protected_count"), int) and intake["protected_count"] > 0,
        "no_protected_cases",
    )
    return {
        "state": "ready",
        "intake_revision": intake["intake_revision"],
        "development_count": intake["development_count"],
        "protected_count": intake["protected_count"],
    }


def _model_grant_path(directory: Path, manifest: Json) -> Path:
    return directory / ("model-grant-" + digest(manifest)[:32] + ".json")


def _model_grant(
    directory: Path, organization: str, manifest: Json, intake: Json, residual: Json
) -> Json:
    grant = _private_json(_model_grant_path(directory, manifest))
    _require(
        grant.get("schema_version") == MODEL_AUTH
        and grant.get("organization") == organization
        and grant.get("engagement") == manifest["engagement"]
        and grant.get("plan_revision") == digest(manifest)
        and grant.get("intake_revision") == intake["intake_revision"]
        and grant.get("residual_revision") == residual["residual_revision"]
        and grant.get("profile") == manifest.get("open_weight_profile") == MODEL_PROFILE,
        "open_weight_grant_scope_mismatch",
    )
    _require(
        type(grant.get("expires_at")) is int
        and time.time() < grant["expires_at"]
        and intake.get("training_authorized") is True,
        "open_weight_permission_unavailable",
    )
    if grant.get("authority_kind") == "standing_customer_mandate":
        from opentour_cell.pilot.learning_mandate import LearningMandates

        registry = LearningMandates(directory, organization, manifest["engagement"])
        issued = registry.grant(grant["reservation_id"], actor=grant["delegate"])
        _require(issued == grant, "delegated_model_grant_mismatch")
    return grant


def grant_model(
    directory: Path,
    organization: str,
    manifest: Json,
    *,
    trainer: str,
    payer: str,
    rights: str,
    destination: str,
    features: list[str],
    labels: list[str],
    max_seconds: int,
    expires_at: int,
) -> str:
    """The custodian issues this separate owner-local permission before an optional fit."""
    store, intake = _context(directory, organization, manifest)
    _require(
        manifest.get("open_weight_profile") == MODEL_PROFILE
        and intake.get("training_authorized") is True,
        "open_weight_training_not_enrolled",
    )
    _require(
        ATTESTATION.fullmatch(rights)
        and 0 < max_seconds <= 300
        and time.time() < expires_at < 2**63,
        "invalid_open_weight_authorization",
    )
    _require(
        features
        and set(features) <= set(intake["allowed_features"])
        and 2 <= len(set(labels)) <= 8
        and all(isinstance(item, str) and len(item) <= 80 for item in labels),
        "invalid_model_projection",
    )
    learning = source_intake.development_rows(store, manifest["intake_revision"], for_training=True)
    comparison_plan.validate_development_partition(store, learning)
    residual = _development(
        learning, _rule(intake, manifest, learning), manifest, permit_model=True
    )
    _require(residual["route"] == "model", "qualified_residual_required")
    _require(
        any(
            "class-" + digest([c["from_label"], c["to_label"]])[:24]
            == intake.get("confirmed_error_class_id")
            and c["groups"] >= manifest["terms"].get("minimum_residual_errors", 2)
            for c in residual["residual_classes"]
        ),
        "owner_confirmed_recurring_error_required",
    )
    grant = {
        "schema_version": MODEL_AUTH,
        "organization": organization,
        "engagement": store.engagement_id,
        "profile": MODEL_PROFILE,
        "plan_revision": digest(manifest),
        "intake_revision": intake["intake_revision"],
        "residual_revision": residual["residual_revision"],
        "rights_attestation": rights,
        "trainer": identifier(trainer),
        "payer": identifier(payer),
        "destination": identifier(destination),
        "max_seconds": max_seconds,
        "max_cost_cents": 0,
        "hourly_cost_cents": 0,
        "features": sorted(set(features)),
        "labels": sorted(set(labels)),
        "expires_at": expires_at,
        "model_authorized": True,
        "compute_authorized": True,
        "training_data_authorized": True,
        "model_use_authorized": True,
        "destination_authorized": True,
    }
    with store.engagement_guard(engagement_id=store.engagement_id):
        _record(_model_grant_path(directory, manifest), grant)
    return digest(grant)


def _model_arm(
    directory: Path,
    work: Path,
    manifest: Json,
    intake: Json,
    grant: Json,
    residual: Json,
    learning: list[Json],
    hidden_inputs: list[Json],
    check_permission: Callable[[], None],
) -> tuple[Json, dict[str, Any]]:
    from opentour_cell.pilot import open_weight

    def projection(row: Json) -> Json:
        return {
            "case_id": row["case_id"],
            "family_id": row["group_id"],
            "features": {field: row["features"][field] for field in grant["features"]},
        }

    development = [
        {**projection(row), "label": row["reference"]}
        for row in learning
        if row.get("reference") is not None
    ]
    protected = [projection(row) for row in hidden_inputs]
    base = directory / "model-base"
    python = os.environ.get("OPENTOUR_TRAINING_PYTHON")
    domain = LocalTrainingStore(
        directory / "training.sqlite3", manifest["organization"], existing_only=True
    )

    def dispatch() -> AbstractContextManager[None]:
        # SQLite WAL readers do not prevent a concurrent writer from withdrawing.
        return domain.engagement_guard(engagement_id=manifest["engagement"])

    if not (work / "model").exists():
        open_weight.inspect_open_weight_installation(
            work / "model",
            base,
            grant=grant,
            python=python,
            check_permission=check_permission,
            dispatch=dispatch,
        )

    registry = None
    if grant.get("authority_kind") == "standing_customer_mandate":
        from opentour_cell.pilot.learning_mandate import LearningMandates

        registry = LearningMandates(directory, manifest["organization"], manifest["engagement"])
        if not (work / "model").exists():
            registry.claim_fit(
                grant["reservation_id"], actor=grant["delegate"], bundle=work / "model"
            )
    model = open_weight.fit_open_weight(
        work / "model",
        development,
        model_dir=base,
        grant=grant,
        residual=residual,
        protected_fingerprints=open_weight.partition_fingerprints(protected),
        python=python,
        check_permission=check_permission,
        dispatch=dispatch,
    )
    check_permission()
    if registry is not None:
        registry.settle(
            grant["reservation_id"],
            actor=grant["delegate"],
            elapsed_seconds=math.ceil(model["training"]["elapsed_ms"] / 1000),
            artifact_sha256=model["artifact_sha256"],
        )
    _model_grant(directory, manifest["organization"], manifest, intake, residual)
    inferred = open_weight.predict_open_weight_batch(
        work / "model",
        [row["features"] for row in protected],
        model_dir=base,
        grant=grant,
        python=python,
        check_permission=check_permission,
        dispatch=dispatch,
    )
    return model, {
        row["case_id"]: decision for row, decision in zip(protected, inferred, strict=True)
    }


def _stored_run(directory: Path, run_id: str) -> Path:
    """A run identifier is generated by the managed queue and is never a filesystem path."""
    return _directory(_directory(directory / "managed-outputs") / _name(run_id))


def execute_managed(directory: Path, organization: str, manifest: Json, run_id: str) -> Json:
    store, intake = _context(directory, organization, manifest)
    learning = source_intake.development_rows(
        store, manifest["intake_revision"], for_training="mapping" not in manifest["rule"]
    )
    comparison_plan.validate_development_partition(store, learning)
    rule = _rule(intake, manifest, learning)
    hidden_inputs = source_intake.protected_inputs(
        store, manifest["intake_revision"], include_groups=True
    )
    work = _stored_run(directory, run_id)
    candidate = None
    predictions = None
    model_enabled = manifest.get("open_weight_profile") is not None
    if model_enabled:
        _require(
            manifest["open_weight_profile"] == MODEL_PROFILE, "unsupported_open_weight_profile"
        )
    with_permit = _development(learning, rule, manifest, permit_model=True)
    permission = None
    if (
        model_enabled
        and with_permit["route"] == "model"
        and _model_grant_path(directory, manifest).exists()
    ):
        permission = _model_grant(directory, organization, manifest, intake, with_permit)
    residual = (
        with_permit if permission else _development(learning, rule, manifest, permit_model=False)
    )

    def authorize() -> None:
        _require(store.engagement_active(), "engagement_unavailable")
        active_intake = source_intake.check_current_intake(
            directory,
            organization,
            store.engagement_id,
            manifest["intake_revision"],
            require_training=permission is not None,
        )
        if permission is not None:
            _model_grant(directory, organization, manifest, active_intake, residual)

    authorize()
    if permission is not None:
        learning = source_intake.development_rows(
            store, manifest["intake_revision"], for_training=True
        )
        candidate, predictions = _model_arm(
            directory,
            work,
            manifest,
            intake,
            permission,
            residual,
            learning,
            hidden_inputs,
            authorize,
        )
    measurements = None
    if "measurements_file" in manifest:
        measurements = _private_json(directory / _name(manifest["measurements_file"]))
        _require(
            digest(measurements) == manifest.get("measurements_revision"), "measurements_changed"
        )
    plan = comparison_plan.freeze_plan(
        organization=organization,
        engagement=store.engagement_id,
        audience=manifest["audience"],
        intake_revision=manifest["intake_revision"],
        protected_ids=[r["case_id"] for r in hidden_inputs],
        incumbent_revision=manifest["incumbent_revision"],
        rule=rule,
        development_decision=residual,
        terms=manifest["terms"],
        model_revision=candidate["artifact_sha256"] if candidate else None,
        model_budget_revision=digest(permission) if permission else None,
        model_predictions=predictions,
        measurements=measurements,
    )
    authorize()
    _record(work / "rule.json", rule)
    _record(work / "plan.json", plan)
    if residual["route"] == "hold":
        result = comparison_plan.development_hold(plan)
    else:
        frozen = source_intake.load_intake(
            store,
            directory / _name(manifest["intake_file"]),
            expected_revision=manifest["intake_revision"],
        )
        result = comparison_plan.run_comparison(
            store,
            plan,
            frozen,
            rule=rule,
            model_predictions=predictions,
            measurements=measurements,
            authorize=authorize,
        )
    authorize()
    _record(work / "comparison.json", result)
    outcome = result["outcome"]
    _require(
        outcome in {"simple_repair", "model_candidate", "hold", "no_change"},
        "invalid_comparison_outcome",
    )
    receipt = {
        "result_revision": digest(result),
        "case_count": result["protected_rows_evaluated"],
        "outcome": outcome,
    }
    private_receipt = {"run_id": run_id, "plan_revision": digest(manifest), **receipt}
    if permission is not None:
        private_receipt["model_permission_revision"] = digest(permission)
    if "release_review" in manifest:
        policy = manifest["release_review"]
        _require(
            isinstance(policy, dict)
            and set(policy) - {"provider"} == {"reviewer", "team", "channel", "audience"}
            and policy.get("provider", "slack") in {"slack", "zapier_slack"}
            and all(isinstance(v, str) and v for v in policy.values())
            and "slack_review" in manifest
            and all(
                policy[k] == manifest["slack_review"][k] for k in ("team", "channel", "audience")
            ),
            "inconsistent_release_policy",
        )
        private_receipt["release_review"] = policy
    if manifest.get("slack_review") is not None:
        summary = _make_review(manifest, intake, plan, result, run_id)
        _record(work / "review.json", summary)
        private_receipt["review_digest"] = digest(summary)
    with store.engagement_guard(engagement_id=store.engagement_id):
        active_intake = source_intake.check_current_intake(
            directory,
            organization,
            store.engagement_id,
            manifest["intake_revision"],
            require_training=permission is not None,
        )
        if permission is not None:
            _model_grant(directory, organization, manifest, active_intake, residual)
        _record(work / "run.json", private_receipt)
    return receipt


def _publication_permission(manifest: Json) -> Json:
    allowed = manifest.get("slack_review")
    _require(
        isinstance(allowed, dict)
        and set(allowed)
        == {"team", "channel", "audience", "expires_at", "owner_attestation", "aggregate"},
        "private_review_permission_required",
    )
    assert isinstance(allowed, dict)
    _require(
        allowed["aggregate"] is True
        and allowed["audience"] == manifest["audience"]
        and type(allowed["expires_at"]) is int
        and 0 < allowed["expires_at"] < 2**63
        and ATTESTATION.fullmatch(str(allowed["owner_attestation"])),
        "invalid_slack_review_permission",
    )
    for key in ("team", "channel", "audience"):
        identifier(allowed[key])
    return allowed


def grant_slack_review(directory: Path, organization: str, manifest: Json) -> str:
    store, intake = _context(directory, organization, manifest)
    permission = _publication_permission(manifest)
    _require(
        time.time() < permission["expires_at"] <= intake["expires_at"],
        "review_grant_outside_validity",
    )
    scope = {
        "organization": organization,
        "engagement": store.engagement_id,
        "plan_revision": digest(manifest),
        "intake_revision": intake["intake_revision"],
        **permission,
    }
    with store.engagement_guard(engagement_id=store.engagement_id):
        _record(directory / ("slack-review-" + digest(manifest) + ".json"), scope)
    return digest(scope)


def _make_review(manifest: Json, intake: Json, plan: Json, result: Json, run_id: str) -> Json:
    from opentour_cell.pilot.brief import decision_brief
    from opentour_cell.pilot.pilot_metrics import operating_verdict
    from opentour_cell.pilot.readout import INPUT_VERSION, evaluate_manifest
    from opentour_cell.pilot.slack_benchmark import select_benchmark

    scope = _publication_permission(manifest)
    terms = manifest["terms"]
    selected = (
        "model"
        if result["decision"]["winner"] == "model"
        or (result["decision"]["winner"] is None and "model" in result["arms"])
        else "simple"
    )

    def projection(arm: str) -> Json:
        cases = [
            {
                "case_id": r["case_id"],
                "reference": r["reference"],
                "current": r["incumbent"],
                "repair": r[arm],
            }
            for r in result["paired_cases"]
        ]
        return evaluate_manifest(
            {
                "schema_version": INPUT_VERSION,
                "error_ceiling_bps": terms["error_ceiling_bps"],
                "cases": cases,
            }
        )

    reports = {arm: projection(arm) for arm in ("simple", "model") if arm in result["arms"]}
    report = reports.get(selected) or projection("simple")
    observed = {
        "current": operating_verdict(
            report["current"], report["cohort_size"], terms["minimum_coverage_bps"]
        ),
        "repair": operating_verdict(
            report["repair"], report["cohort_size"], terms["minimum_coverage_bps"]
        ),
    }
    point = {
        "schema_version": "opentour.pilot.operating-point.v1",
        "agreement_revision": plan["revision"],
        "minimum_coverage_bps": terms["minimum_coverage_bps"],
        "error_ceiling_bps": terms["error_ceiling_bps"],
        **observed,
    }
    audit = {
        "run_id": run_id,
        "run_revision": digest(result),
        "mode": "managed_model" if selected == "model" else "managed_rule",
        "candidate_digest": result["arms"].get(selected, {}).get("revision")
        or plan["rule_revision"],
        "handoff_disposition": result["outcome"],
        "decision_reason": result["decision"]["reason"],
        "cohort_revision": plan["protected_membership_digest"],
        "aggregate_digest": digest(report),
        "brief_digest": hashlib.sha256(decision_brief(report).encode()).hexdigest(),
        "operating_point": point,
        "decision_quality_digest": digest(point),
        "comparisons": {("rule" if k == "simple" else k): v for k, v in reports.items()},
        "comparison_cohorts": {
            ("rule" if k == "simple" else k): plan["protected_membership_digest"] for k in reports
        },
    }
    benchmark = select_benchmark(result)
    if benchmark:
        audit["workflow_benchmark"] = benchmark
    return {
        "organization": manifest["organization"],
        "engagement": manifest["engagement"],
        "plan_revision": digest(manifest),
        "dataset_id": intake["dataset_id"],
        "dataset_revision": intake["intake_revision"],
        "scope": scope,
        "readout": report,
        "audit": audit,
    }


def _current_model_permission(
    directory: Path, organization: str, engagement: str, intake_revision: str, run: Json
) -> None:
    source_intake.check_current_intake(
        directory,
        organization,
        engagement,
        intake_revision,
        require_training=True,
    )
    grant = _private_json(directory / ("model-grant-" + run["plan_revision"][:32] + ".json"))
    _require(
        run.get("model_permission_revision") == digest(grant)
        and grant["organization"] == organization
        and grant["engagement"] == engagement
        and grant["intake_revision"] == intake_revision
        and grant["plan_revision"] == run["plan_revision"]
        and time.time() < grant["expires_at"],
        "model_use_permission_unavailable",
    )
    if grant.get("authority_kind") == "standing_customer_mandate":
        from opentour_cell.pilot.learning_mandate import LearningMandates

        issued = LearningMandates(directory, organization, engagement).grant(
            grant["reservation_id"],
            actor=grant["delegate"],
        )
        _require(issued == grant, "delegated_model_grant_mismatch")
    bundle = directory / "managed-outputs" / run["run_id"] / "model"
    _require(
        not bundle.with_name(".model.withdrawn").exists() and not (bundle / "WITHDRAWN").exists(),
        "open_weight_authority_withdrawn",
    )


def managed_review_evidence(
    directory: Path, organization: str, run_id: str, audience: str, team: str, channel: str
) -> Json:
    run_id = run_id.removeprefix("managed:")
    _require(re.fullmatch(r"run-[0-9a-f]{32}", run_id), "managed_review_unavailable")
    output_root = directory / "managed-outputs"
    _check_directory(output_root / "private")
    work = output_root / run_id
    receipt = _private_json(work / "run.json")
    _require(
        receipt["run_id"] == run_id and HEX.fullmatch(str(receipt["plan_revision"])),
        "managed_review_unavailable",
    )
    review = _private_json(work / "review.json")
    _require(
        receipt.get("review_digest") == digest(review)
        and receipt["result_revision"] == review["audit"]["run_revision"]
        and review["plan_revision"] == receipt["plan_revision"]
        and review["organization"] == organization,
        "managed_review_unavailable",
    )
    grant = _private_json(directory / ("slack-review-" + receipt["plan_revision"] + ".json"))
    expected = {
        "organization": organization,
        "engagement": review["engagement"],
        "plan_revision": receipt["plan_revision"],
        "intake_revision": review["dataset_revision"],
        **review["scope"],
    }
    _require(
        grant == expected
        and grant["aggregate"] is True
        and (grant["audience"], grant["team"], grant["channel"]) == (audience, team, channel)
        and time.time() < grant["expires_at"],
        "managed_review_unavailable",
    )
    if "model" in review["audit"]["comparisons"]:
        _current_model_permission(
            directory, organization, review["engagement"], review["dataset_revision"], receipt
        )
    else:
        source_intake.check_current_intake(
            directory, organization, review["engagement"], review["dataset_revision"]
        )
    result = {k: review[k] for k in ("dataset_id", "dataset_revision", "readout", "audit")}
    if "release_review" in receipt:
        result["release_review"] = receipt["release_review"]
    return result


def check_managed_review(
    directory: Path,
    organization: str,
    run_id: str,
    audience: str,
    team: str,
    channel: str,
    run_revision: str,
) -> None:
    evidence = managed_review_evidence(directory, organization, run_id, audience, team, channel)
    _require(evidence["audit"]["run_revision"] == run_revision, "managed_review_unavailable")


def managed_review_sources(
    directory: Path,
    organization: str,
    allowed_datasets: list[str],
    audience: str,
    team: str,
    channel: str,
) -> list[tuple[str, str]]:
    root = directory / "managed-outputs"
    if not root.exists():
        return []
    _check_directory(root / "private")
    items = []
    for path in sorted(root.iterdir()):
        if (
            not re.fullmatch(r"run-[0-9a-f]{32}", path.name)
            or path.is_symlink()
            or not path.is_dir()
        ):
            continue
        try:
            value = managed_review_evidence(
                directory, organization, path.name, audience, team, channel
            )
        except (ValueError, KeyError, TypeError, OSError):
            continue
        if value["dataset_id"] in allowed_datasets:
            items.append((value["dataset_id"], "managed:" + path.name))
    return items


def prepare_handoff(directory: Path, organization: str, manifest: Json, run_id: str) -> Json:
    from opentour_cell.pilot.repair_bundle import prepare_bundle_spec

    store, intake = _context(directory, organization, manifest)
    work = directory / "managed-outputs" / _name(run_id)
    receipt = _private_json(work / "run.json")
    comparison = _private_json(work / "comparison.json")
    _require(
        receipt.get("plan_revision") == digest(manifest)
        and receipt.get("result_revision") == digest(comparison),
        "managed_result_mismatch",
    )
    if comparison["decision"].get("winner") == "model":
        _current_model_permission(
            directory, organization, store.engagement_id, manifest["intake_revision"], receipt
        )
    if "release_review" in manifest and comparison["decision"].get("winner") in ("simple", "model"):
        from opentour_cell.pilot.release_gate import authorize_release

        arm = comparison["decision"]["winner"]
        authorize_release(
            directory,
            organization,
            run_id,
            receipt["result_revision"],
            comparison["arms"][arm]["revision"],
        )
    rule = _private_json(work / "rule.json")
    plan = _private_json(work / "plan.json")
    intake = source_intake.load_intake(
        store,
        directory / _name(manifest["intake_file"]),
        expected_revision=manifest["intake_revision"],
    )
    with store.engagement_guard(engagement_id=store.engagement_id):
        return prepare_bundle_spec(
            work,
            manifest=manifest,
            intake=intake,
            plan=plan,
            comparison=comparison,
            rule=rule,
            model=work / "model" if (work / "model").is_dir() else None,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--organization", required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("inspect")
    actions.add_parser(
        "permit-slack",
        help="Permit private aggregate disclosure to the declared workspace and audience.",
    )
    grant = actions.add_parser(
        "permit-model", help="Record separately authorized existing local compute."
    )
    grant.add_argument("--trainer", required=True)
    grant.add_argument("--payer", required=True)
    grant.add_argument("--rights", required=True)
    grant.add_argument("--destination", required=True)
    grant.add_argument("--feature", action="append", required=True)
    grant.add_argument("--label", action="append", required=True)
    grant.add_argument("--max-seconds", type=int, required=True)
    grant.add_argument("--expires-at", type=int, required=True)
    handoff = actions.add_parser("prepare-handoff")
    handoff.add_argument("--run-id", required=True)
    args = parser.parse_args()
    try:
        _require(
            args.directory.is_absolute() and args.manifest.parent == args.directory,
            "private_manifest_required",
        )
        manifest = _private_json(args.manifest)
        result: Json
        if args.action == "permit-model":
            revision = grant_model(
                args.directory,
                args.organization,
                manifest,
                trainer=args.trainer,
                payer=args.payer,
                rights=args.rights,
                destination=args.destination,
                features=args.feature,
                labels=args.label,
                max_seconds=args.max_seconds,
                expires_at=args.expires_at,
            )
            result = {"permission_revision": revision}
        elif args.action == "permit-slack":
            result = {
                "permission_revision": grant_slack_review(
                    args.directory, args.organization, manifest
                )
            }
        elif args.action == "prepare-handoff":
            result = prepare_handoff(args.directory, args.organization, manifest, args.run_id)
        else:
            result = inspect_managed(args.directory, args.organization, manifest)
    except (ValueError, TypeError, OSError, KeyError) as exc:
        issue = str(exc)
        parser.exit(
            2,
            (issue if re.fullmatch(r"[a-z_]{1,80}", issue) else "managed_repair_unavailable")
            + "\n",
        )
    print(canonical(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
