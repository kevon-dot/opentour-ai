"""Discover one customer-authorized workplace change, evaluate it and queue human review."""

from __future__ import annotations

import argparse
import fcntl
import os
import re
import sqlite3
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Protocol

from opentour_cell.pilot import approval_inbox, comparison_plan, repair_workflow, source_intake
from opentour_cell.pilot.operating_point import canonical, digest
from opentour_cell.pilot.simple_repair import membership
from opentour_cell.pilot.training import LocalTrainingStore, _check_directory
from opentour_cell.pilot.training_data import identifier

Json = dict[str, Any]
SCHEMA = "opentour.pilot.autonomous-cycle.v1"


class Connector(Protocol):
    def discover_workplace(
        self,
        store: LocalTrainingStore,
        connection: Path,
        *,
        max_total_rows: int,
        expected_connection_revision: str,
    ) -> Json: ...
    def acquire_workplace(
        self,
        store: LocalTrainingStore,
        connection: Path,
        *,
        expected_pair_revision: str,
        max_total_rows: int,
        expected_connection_revision: str,
    ) -> Json: ...


def _require(value: object, reason: str) -> None:
    if not value:
        raise ValueError(reason)


def _reason(exc: Exception) -> str:
    raw = getattr(exc, "reason", str(exc))
    return (
        raw
        if isinstance(raw, str) and re.fullmatch(r"[a-z][a-z_]{2,95}", raw)
        else "private_workflow_unavailable"
    )


@contextmanager
def _once_at_a_time(directory: Path, organization: str, mandate: str) -> Iterator[None]:
    path = directory / ("cycle-" + digest([organization, mandate]) + ".lock")
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        st = os.fstat(fd)
        _require(
            stat.S_ISREG(st.st_mode)
            and st.st_uid == os.getuid()
            and st.st_nlink == 1
            and not st.st_mode & 0o077,
            "unsafe_cycle_lock",
        )
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _context(
    directory: Path, org: str, cfg: Json
) -> tuple[LocalTrainingStore, Any, Json, Json, Path]:
    from opentour_cell.pilot.learning_mandate import LearningMandates
    from opentour_cell.pilot.slack_adapter import _slack_id

    _require(
        directory.is_absolute() and directory.resolve() == directory, "private_directory_required"
    )
    _check_directory(directory / "binding")
    _require(
        set(cfg)
        == {
            "schema_version",
            "organization",
            "engagement",
            "mandate_id",
            "delegate",
            "connector",
            "connection_file",
            "agreement_file",
        }
        and cfg["schema_version"] == SCHEMA
        and org == cfg["organization"],
        "invalid_cycle_scope",
    )
    for name in ("organization", "engagement", "mandate_id", "delegate", "connector"):
        identifier(cfg[name])
    store = LocalTrainingStore(directory / "training.sqlite3", org, existing_only=True)
    _require(
        store.engagement_active() and store.engagement_id == cfg["engagement"],
        "engagement_unavailable",
    )
    ledger = LearningMandates(directory, org, cfg["engagement"])
    details = ledger.inspect(cfg["mandate_id"], actor=cfg["delegate"])
    scope = details["scope"]
    _require(not details["withdrawn"] and not details["expired"], "standing_mandate_unavailable")
    _require(
        scope.get("mode") == "autonomous"
        and scope["delegate"] == cfg["delegate"]
        and cfg["connector"] in scope["connectors"]
        and {"inspect", "evaluate"} <= set(scope["uses"])
        and time.time() < scope["expires_at"],
        "autonomous_mandate_required",
    )
    agreement = repair_workflow._private_json(
        directory / repair_workflow._name(cfg["agreement_file"])
    )
    _require(digest(agreement) == scope["agreement_revision"], "standing_agreement_changed")
    required = {
        "incumbent_revision",
        "terms",
        "rule",
        "handoff",
        "release_review",
        "slack_review",
        "connection_revision",
    }
    _require(set(agreement) == required, "invalid_standing_agreement")
    release, slack = agreement["release_review"], agreement["slack_review"]
    _require(
        isinstance(release, dict)
        and set(release) - {"provider"} == {"team", "channel", "audience", "reviewer"}
        and release.get("provider", "zapier_slack") in ("zapier_slack", "slack")
        and _slack_id(release["reviewer"], "UW")
        and _slack_id(release["team"], "T")
        and _slack_id(release["channel"], "CG")
        and (_slack_id(release["audience"], "UWG") or release["audience"] == release["channel"])
        and release["reviewer"] == scope["reviewer"]
        and release["audience"] == scope["audience"]
        and all(slack[k] == release[k] for k in ("team", "channel", "audience"))
        and slack["aggregate"] is True
        and time.time() < slack["expires_at"] <= scope["expires_at"],
        "review_scope_unavailable",
    )
    repair_workflow._publication_permission({"slack_review": slack, "audience": scope["audience"]})
    connection = directory / repair_workflow._name(cfg["connection_file"])
    workplace = repair_workflow._private_json(connection)
    _require(
        digest(workplace) == agreement["connection_revision"]
        and workplace["reader"]["provider"] == cfg["connector"]
        and workplace["scope"]["organization"] == org
        and workplace["scope"]["engagement"] == cfg["engagement"]
        and workplace["scope"]["workflow"] == scope["workflow"]
        and workplace["scope"]["field"] == scope["field"],
        "workplace_connection_outside_agreement",
    )
    return store, ledger, scope, agreement, connection


def _admit(
    directory: Path,
    cfg: Json,
    store: LocalTrainingStore,
    ledger: Any,
    scope: Json,
    connection: Path,
    connector: Connector,
    agreement: Json,
) -> tuple[Json, Json, Path]:
    # Both passes bound provider rows before reads. An ambiguous catalog has no content-read path.
    cap = scope["max_rows_per_run"]
    pin = agreement["connection_revision"]
    found = connector.discover_workplace(
        store, connection, max_total_rows=cap, expected_connection_revision=pin
    )
    _require(found.get("state") == "ready", "workplace_pair_unavailable")
    _require(
        found["sources"]["trace"]["source_id"] == scope["trace_source"]
        and found["sources"]["correction"]["source_id"] == scope["correction_source"],
        "unapproved_workplace_sources",
    )
    memo = directory / (
        "cycle-discovered-" + digest([cfg["mandate_id"], pin, found["pair_revision"]]) + ".json"
    )
    if os.path.lexists(memo):
        acquired = repair_workflow._private_json(memo)
    else:
        usage = ledger.inspect(cfg["mandate_id"], actor=cfg["delegate"])
        cap = min(cap, scope["max_total_rows"] - usage["cumulative_rows"])
        _require(not usage["withdrawn"] and not usage["expired"], "standing_mandate_unavailable")
        _require(
            usage["cumulative_runs"] < scope["max_runs"]
            and cap >= found["counts"]["trace_rows"] + found["counts"]["correction_rows"],
            "standing_source_budget_exhausted",
        )
        acquired = connector.acquire_workplace(
            store,
            connection,
            expected_pair_revision=found["pair_revision"],
            max_total_rows=cap,
            expected_connection_revision=pin,
        )
    _require(
        acquired["state"] == "ready" and acquired["pair_revision"] == found["pair_revision"],
        "source_changed_during_acquisition",
    )
    base = repair_workflow._private_json(Path(acquired["grant_path"]))
    _require(
        base["workflow"] == scope["workflow"]
        and base["field"] == scope["field"]
        and base["organization"] == store.organization
        and base["engagement"] == cfg["engagement"]
        and base["owner_id"] == scope["owner_id"]
        and base["trace_source"] == scope["trace_source"]
        and base["correction_source"] == scope["correction_source"]
        and set(base["permitted_features"]) == set(scope["fields"]),
        "workplace_outside_mandate",
    )
    counts = acquired["counts"]
    rows = counts["trace_rows"] + counts["correction_rows"]
    request = {
        "source_revision": acquired["snapshot_revision"],
        "connector": cfg["connector"],
        **{
            k: base[k]
            for k in (
                "trace_sha256",
                "correction_sha256",
                "workflow",
                "trace_source",
                "correction_source",
                "field",
            )
        },
        "fields": sorted(base["permitted_features"]),
        "rows": rows,
    }
    admission = ledger.authorize_source(cfg["mandate_id"], request, actor=cfg["delegate"])
    repair_workflow._record(memo, acquired)
    run_id = admission["run_id"]
    _require(
        isinstance(run_id, str) and re.fullmatch(r"learning-[0-9a-f]{40}", run_id),
        "invalid_delegated_run",
    )
    # Preserve the actual owner attestation on the connection. Additional capabilities come only
    # from the live, exact source-specific machine receipt, revalidated by source_intake.
    g = dict(
        base,
        expires_at=min(base["expires_at"], scope["expires_at"]),
        delegation={
            "authority_kind": "standing_customer_mandate",
            "run_id": run_id,
            "delegate": cfg["delegate"],
        },
    )
    g.pop("training_attestation", None)
    g.pop("training_expires_at", None)
    if "train" in scope["uses"] and admission.get("training_attestation"):
        g.update(
            training_attestation=admission["training_attestation"],
            training_expires_at=g["expires_at"],
        )
    grant_path = directory / ("cycle-source-" + run_id + ".json")
    repair_workflow._record(grant_path, g)
    return admission, acquired, grant_path


def _fresh_before_fit(store: LocalTrainingStore, revision: str) -> None:
    blind = source_intake.protected_inputs(store, revision, include_groups=True)
    with store._db() as db:
        for key in set(membership(blind, "case_id")) | set(membership(blind, "group_id")):
            used = db.execute(
                "SELECT split,used_by FROM split_keys WHERE fingerprint=?", (key,)
            ).fetchone()
            supplied = db.execute(
                "SELECT 1 FROM evaluation_keys e JOIN dataset_scope s ON s.dataset=e.dataset "
                "WHERE e.fingerprint=? AND s.baseline_result IS NOT NULL LIMIT 1",
                (key,),
            ).fetchone()
            _require(
                not supplied
                and (not used or (used["split"] == "heldout" and used["used_by"] is None)),
                "fresh_protected_cohort_required",
            )


def _model_permission(
    directory: Path,
    ledger: Any,
    scope: Json,
    cfg: Json,
    store: LocalTrainingStore,
    manifest: Json,
    intake: Json,
    run_id: str,
) -> str | None:
    if not scope["profile"]:
        return None
    rows = source_intake.development_rows(store, intake["intake_revision"], for_training=True)
    comparison_plan.validate_development_partition(store, rows)
    selected = repair_workflow._rule(intake, manifest, rows)
    residual = repair_workflow._development(rows, selected, manifest, permit_model=True)
    if residual["route"] != "model":
        return None
    _require(
        any(
            "class-" + digest([c["from_label"], c["to_label"]])[:24]
            == intake["confirmed_error_class_id"]
            and c["groups"] >= manifest["terms"].get("minimum_residual_errors", 2)
            for c in residual["residual_classes"]
        ),
        "outside_confirmed_recurring_error",
    )
    work = repair_workflow._stored_run(directory, run_id)
    dest = work / "model"
    if not dest.exists():
        _fresh_before_fit(store, intake["intake_revision"])
    reserved = ledger.reserve(
        cfg["mandate_id"],
        {
            "run_id": manifest["standing_delegation"]["run_id"],
            "plan_revision": digest(manifest),
            "intake_revision": intake["intake_revision"],
            "residual_revision": residual["residual_revision"],
            "max_seconds": scope["max_seconds_per_fit"],
        },
        actor=cfg["delegate"],
    )
    rid = reserved["reservation_id"]
    native = ledger.grant(rid, actor=cfg["delegate"])
    if dest.exists():
        from opentour_cell.pilot.open_weight import inspect_open_weight

        inspect_open_weight(dest, grant=native)
    # The actual fit entry point claims the reserved output and records its measured settlement.
    repair_workflow._record(repair_workflow._model_grant_path(directory, manifest), native)
    return str(rid)


def _execute(directory: Path, org: str, cfg: Json, connector: Connector, record: Json) -> Json:
    store, ledger, scope, agreement, connection = _context(directory, org, cfg)
    admission, acquired, grant = _admit(
        directory, cfg, store, ledger, scope, connection, connector, agreement
    )
    delegated, delegate = admission["run_id"], cfg["delegate"]
    run_id = "run-" + digest([org, cfg["mandate_id"], delegated])[:32]
    record.update(
        {
            "source_revision": acquired["snapshot_revision"],
            "run_id": run_id,
            "result_revision": None,
            "review_state": "not_requested",
            "authority_kind": "standing_customer_mandate",
        }
    )
    trace, outcomes = Path(acquired["trace_path"]), Path(acquired["correction_path"])
    proposal = source_intake.discover_intake(store, grant, trace, outcomes)
    classes = [
        c
        for c in proposal["suspected_error_classes"]
        if [c["from_label"], c["to_label"]] in scope["permitted_error_pairs"]
    ]
    if len(classes) != 1:
        return dict(
            record,
            state="held",
            reason="ambiguous_recurring_classes" if classes else "no_approved_recurring_class",
        )
    confirmation = ledger.confirm_class(
        delegated,
        actor=delegate,
        discovery_revision=proposal["discovery_revision"],
        candidate=classes[0],
    )
    frozen = directory / ("cycle-intake-" + run_id + ".json")
    data = source_intake.freeze_intake(
        store, grant, trace, outcomes, frozen, confirmation=confirmation
    )
    intake = source_intake.inspect_intake(store, data["intake_revision"])
    manifest = {
        "schema_version": repair_workflow.SCHEMA,
        "organization": org,
        "engagement": cfg["engagement"],
        "audience": scope["audience"],
        "intake_file": frozen.name,
        "intake_revision": intake["intake_revision"],
        "standing_delegation": {
            "mandate_id": cfg["mandate_id"],
            "run_id": delegated,
            "delegate": delegate,
        },
        **agreement,
    }
    manifest["release_review"] = {"provider": "zapier_slack", **agreement["release_review"]}
    if scope["profile"]:
        manifest["open_weight_profile"] = scope["profile"]
    repair_workflow._record(directory / ("cycle-plan-" + run_id + ".json"), manifest)
    repair_workflow.grant_slack_review(directory, org, manifest)
    work = repair_workflow._stored_run(directory, run_id)
    finished = work / "run.json"
    ledger.check_run(delegated, actor=delegate)
    if finished.exists():
        result = repair_workflow._private_json(finished)
        _require(result["plan_revision"] == digest(manifest), "existing_result_conflict")
    else:
        _model_permission(directory, ledger, scope, cfg, store, manifest, intake, run_id)
        result = repair_workflow.execute_managed(directory, org, manifest, run_id)
    ledger.check_run(delegated, actor=delegate)
    record.update(
        result_revision=result["result_revision"],
        cases=result["case_count"],
        outcome=result["outcome"],
    )
    if result["outcome"] in ("simple_repair", "model_candidate"):
        policy = {
            k: agreement["release_review"][k] for k in ("reviewer", "audience", "team", "channel")
        }
        if os.path.lexists(work / "release-decision.json"):
            decision = repair_workflow._private_json(work / "release-decision.json")
            evidence = repair_workflow.managed_review_evidence(
                directory, org, run_id, policy["audience"], policy["team"], policy["channel"]
            )
            _require(
                all(decision.get(k) == v for k, v in manifest["release_review"].items())
                and decision.get("organization") == org
                and decision.get("run_id") == run_id
                and decision.get("run_revision") == result["result_revision"]
                and decision.get("artifact_sha") == evidence["audit"]["candidate_digest"]
                and decision.get("decision") in ("approved", "changes_requested"),
                "review_decision_unavailable",
            )
            return dict(record, state="reviewed", reason=None, review_state=decision["decision"])
        pointer = approval_inbox.enqueue_ready(
            directory,
            org,
            run_id,
            run_revision=result["result_revision"],
            provider=manifest["release_review"]["provider"],
            **policy,
        )
        ledger.check_run(delegated, actor=delegate)
        return dict(
            record,
            state="pending_slack_review",
            reason=None,
            review_state=pointer["state"],
            review_receipt=pointer["receipt"],
        )
    return dict(record, state="held", reason=result["outcome"])


def cycle_once(
    directory: Path, organization: str, config: Json, *, connector: Connector | None = None
) -> Json:
    """Perform a bounded step. Only an approved connector can cross the private source boundary."""
    record: Json = {
        "source_revision": None,
        "run_id": None,
        "result_revision": None,
        "review_state": "not_requested",
    }
    try:
        if connector is None:
            from opentour_cell.pilot import workplace_context

            connector = workplace_context
        _check_directory(directory / "cycle")
        with _once_at_a_time(directory, organization, identifier(config.get("mandate_id"))):
            return _execute(directory, organization, config, connector, record)
    except (ValueError, TypeError, KeyError, OSError, RuntimeError, sqlite3.Error) as exc:
        return dict(record, state="held", reason=_reason(exc))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--organization", required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--once", action="store_true", required=True)
    args = parser.parse_args()
    try:
        _require(args.config.parent == args.directory, "local_cycle_configuration_required")
        config = repair_workflow._private_json(args.config)
        result = cycle_once(args.directory, args.organization, config)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        parser.exit(2, _reason(exc) + "\n")
    print(canonical(result))
    return 0 if result["state"] in ("pending_slack_review", "reviewed") else 2


if __name__ == "__main__":
    raise SystemExit(main())
