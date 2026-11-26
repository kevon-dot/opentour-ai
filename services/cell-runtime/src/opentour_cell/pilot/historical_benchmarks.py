"""Materialize an authorized historical benchmark using the existing comparison worker.

Only a preregistered declared comparator and recorded incumbent decisions are
supported here. This path has no model, fit, publication or release operation.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from opentour_cell.pilot import comparison_plan, repair_workflow, source_intake
from opentour_cell.pilot.intake_admission import IntakeWork
from opentour_cell.pilot.intake_local import HistoricalSelection, LocalHistoricalAuthority
from opentour_cell.pilot.intake_mcp import HistoricalPair, IntakeError, IntakePrincipal
from opentour_cell.pilot.operating_point import canonical, digest
from opentour_cell.pilot.workflow_benchmarks import MaterializedBenchmark

Json = dict[str, Any]
POLICY = "opentour.pilot.historical-benchmark-policy.v1"
BASELINE_POLICY = "opentour.pilot.historical-baseline-policy.v1"
BASELINE_WORKER = "opentour.pilot.historical-baseline-worker.v1"


def _require(condition: object, reason: str = "historical_benchmark_unavailable") -> None:
    if not condition:
        raise IntakeError(reason)


class LocalHistoricalBenchmark:
    """Worker-only composition; paths are local owner configuration, never wire fields."""

    def __init__(self, selection: Path, traces: Path, corrections: Path, agreement: Path):
        self.authority = LocalHistoricalAuthority(selection)
        self.directory = selection.parent
        _require(
            all(
                path.is_absolute() and path.parent == self.directory
                for path in (traces, corrections, agreement)
            )
            and len(
                {selection, traces, corrections, agreement, self.directory / "training.sqlite3"}
            )
            == 5
        )
        self.traces, self.corrections, self.agreement = traces, corrections, agreement

    def _authorize(self, actor: IntakePrincipal, work: IntakeWork) -> Json:
        authority = self.authority
        selected = authority.selected
        with authority.guard(actor):
            request = HistoricalPair(
                selected.pair_ref,
                selected.source_revision,
                selected.correction_revision,
                selected.policy_revision,
                "benchmark-worker",
            )
            _require(
                actor == authority.principal
                and work.mode == "historical"
                and work.workflow_ref == selected.workflow_ref
                and work.resource == authority.historical(actor, request)
                and work.revisions
                == (
                    selected.source_revision,
                    selected.correction_revision,
                    selected.policy_revision,
                )
                and work.resource.engagement_epoch == selected.engagement_epoch
            )
            policy = repair_workflow._private_json(self.agreement)
            _require(digest(policy) == selected.policy_revision, "benchmark_policy_changed")
            baseline = policy.get("schema_version") == BASELINE_POLICY
            _require(
                set(policy)
                == (
                    {"schema_version", "incumbent_revision", "terms"}
                    if baseline
                    else {"schema_version", "incumbent_revision", "terms", "rule"}
                )
                and policy["schema_version"] in {POLICY, BASELINE_POLICY}
                and isinstance(policy["incumbent_revision"], str)
                and re.fullmatch("[a-f0-9]{64}", policy["incumbent_revision"])
                and (
                    baseline
                    or (
                        isinstance(policy["rule"], dict)
                        and set(policy["rule"]) == {"feature", "mapping", "fallback"}
                    )
                ),
                "historical_declared_policy_required",
            )
            terms = comparison_plan._terms(policy["terms"])
            _require(
                (
                    {"minimum_verified_cases", "minimum_slice_cases"}
                    if baseline
                    else {
                        "minimum_development_cases",
                        "minimum_verified_cases",
                        "minimum_residual_errors",
                        "minimum_slice_cases",
                    }
                )
                <= set(terms),
                "benchmark_thresholds_required",
            )
            return policy

    def _paths(self, operation_ref: str) -> tuple[Path, str, Path]:
        _require(re.fullmatch(r"benchmark-operation-[a-f0-9]{40}", operation_ref))
        key = digest(operation_ref)
        run = "historical-benchmark-" + key[:40]
        return (
            self.directory / ("benchmark-intake-" + key + ".json"),
            run,
            self.directory / "managed-outputs" / run,
        )

    @contextmanager
    def _commit_authority(self, actor: IntakePrincipal, work: IntakeWork) -> Iterator[None]:
        """Pin evaluation while caller holds training DB; do not re-enter its writer."""
        authority, selected = self.authority, self.authority.selected
        with authority.mandates.guard_evaluation_source(
            selected.mandate_id, selected.source_run_id, actor=actor.actor
        ) as grant:
            source = grant["source_request"]
            _require(
                actor == authority.principal
                and HistoricalSelection.read(authority.path) == selected
                and grant["mandate_revision"] == selected.mandate_revision
                and grant["agreement_revision"] == selected.policy_revision
                and grant["delegation_id"] == work.resource.resource_id
                and work.resource.engagement_epoch == selected.engagement_epoch
                and source["workflow"] == work.workflow_ref == selected.workflow_ref
                and source["trace_source"] == selected.trace_source
                and source["correction_source"] == selected.correction_source
                and source["trace_sha256"] == selected.source_revision
                and source["correction_sha256"] == selected.correction_revision
                and digest(repair_workflow._private_json(self.agreement))
                == selected.policy_revision
            )
            yield

    def _manifest(self, work: IntakeWork, policy: Json, intake_path: Path, revision: str) -> Json:
        baseline = policy["schema_version"] == BASELINE_POLICY
        return {
            "schema_version": BASELINE_WORKER if baseline else repair_workflow.SCHEMA,
            "organization": self.authority.selected.tenant,
            "engagement": work.resource.engagement_epoch,
            "audience": self.authority.selected.actor,
            "intake_file": intake_path.name,
            "intake_revision": revision,
            "incumbent_revision": policy["incumbent_revision"],
            "terms": policy["terms"],
            **({} if baseline else {"rule": policy["rule"]}),
        }

    def materialize(
        self, actor: IntakePrincipal, work: IntakeWork, *, operation_ref: str
    ) -> MaterializedBenchmark | None:
        policy = self._authorize(actor, work)
        previous = self.readback(actor, work, operation_ref=operation_ref)
        if previous is not None:
            return previous
        intake_path, run, output = self._paths(operation_ref)
        selected = self.authority.selected
        grant = self.directory / f"cycle-source-{selected.source_run_id}.json"
        frozen = source_intake.freeze_intake(
            self.authority.store, grant, self.traces, self.corrections, intake_path
        )
        terms = policy["terms"]
        baseline = policy["schema_version"] == BASELINE_POLICY
        _require(
            (baseline or len(frozen["development"]) >= terms["minimum_development_cases"])
            and len(frozen["protected"]) >= terms["minimum_verified_cases"],
            "benchmark_cases_required",
        )
        _require(
            all(
                row["incumbent"]["status"] != "missing"
                for row in (
                    frozen["protected"]
                    if baseline
                    else (*frozen["development"], *frozen["protected"])
                )
            ),
            "recorded_baseline_predictions_required",
        )
        # Only the preregistered declared rule is admitted: no fit/model profile,
        # Slack review, handoff or release fields enter this manifest.
        manifest = self._manifest(work, policy, intake_path, frozen["intake_revision"])
        self._authorize(actor, work)
        _require(repair_workflow._stored_run(self.directory, run) == output)
        repair_workflow._record(output / "benchmark-manifest.json", manifest)
        if baseline:
            plan = comparison_plan.freeze_recorded_baseline(
                organization=actor.tenant,
                engagement=work.resource.engagement_epoch,
                audience=actor.actor,
                intake_revision=frozen["intake_revision"],
                protected_ids=[row["case_id"] for row in frozen["protected"]],
                incumbent_revision=policy["incumbent_revision"],
                terms=terms,
            )
            repair_workflow._record(output / "plan.json", plan)

            def current() -> None:
                self._authorize(actor, work)

            result = comparison_plan.run_recorded_baseline(
                self.authority.store,
                plan,
                frozen,
                authorize=current,
                commit_authority=lambda: self._commit_authority(actor, work),
            )
            repair_workflow._record(output / "comparison.json", result)
            self._authorize(actor, work)
            repair_workflow._record(
                output / "run.json",
                {
                    "run_id": run,
                    "plan_revision": digest(manifest),
                    "result_revision": digest(result),
                    "case_count": result["protected_rows_evaluated"],
                    "outcome": result["outcome"],
                },
            )
        else:
            result = repair_workflow.execute_managed(self.directory, actor.tenant, manifest, run)
            _require(result["case_count"] > 0, "baseline_comparison_not_evaluated")
        return self.readback(actor, work, operation_ref=operation_ref)

    def readback(
        self, actor: IntakePrincipal, work: IntakeWork, *, operation_ref: str
    ) -> MaterializedBenchmark | None:
        """Reconcile actual immutable records; never map, score, fit or publish again."""
        policy = self._authorize(actor, work)
        intake_path, run, output = self._paths(operation_ref)
        if not (output / "run.json").exists():
            return None
        manifest = repair_workflow._private_json(output / "benchmark-manifest.json")
        _require(manifest == self._manifest(work, policy, intake_path, manifest["intake_revision"]))
        marker = source_intake.inspect_intake(self.authority.store, manifest["intake_revision"])
        _require(marker["protected_count"] >= policy["terms"]["minimum_verified_cases"])
        selected = self.authority.selected
        source_grant = source_intake._scope(
            self.authority.store,
            self.directory / f"cycle-source-{selected.source_run_id}.json",
            expected_delegation={
                "authority_kind": "standing_customer_mandate",
                "run_id": selected.source_run_id,
                "delegate": actor.actor,
            },
        )
        _require(marker["authority_revision"] == digest(source_grant))
        plan = repair_workflow._private_json(output / "plan.json")
        baseline = policy["schema_version"] == BASELINE_POLICY
        if baseline:
            comparison_plan.check_recorded_baseline_plan(plan)
        else:
            comparison_plan._check_plan(plan)
        _require(
            plan["organization"] == actor.tenant
            and plan["engagement"] == work.resource.engagement_epoch
            and plan["audience"] == self.authority.selected.actor
            and plan["intake_revision"] == manifest["intake_revision"]
            and plan["terms"] == policy["terms"]
            and plan["incumbent_revision"] == policy["incumbent_revision"]
            and (
                baseline
                or (
                    plan["model_revision"] is None
                    and plan["rule_revision"]
                    == repair_workflow._rule(marker, manifest, [])["revision"]
                )
            )
            and plan["protected_membership_digest"] == marker["protected_membership_digest"],
        )
        private_result = repair_workflow._private_json(output / "comparison.json")
        receipt = repair_workflow._private_json(output / "run.json")
        _require(
            receipt["run_id"] == run
            and receipt["plan_revision"] == digest(manifest)
            and receipt["result_revision"] == digest(private_result)
            and receipt["case_count"] == private_result["protected_rows_evaluated"] > 0
            and private_result["protected_membership_digest"] == plan["protected_membership_digest"]
        )
        # The comparison's actual protected registry is the authority for a
        # completed score, not caller claims or an orphaned output file.
        with self.authority.store._db() as db:
            row = db.execute(
                "SELECT result FROM protected_comparisons WHERE revision=?", (plan["revision"],)
            ).fetchone()
            _require(row is not None and row["result"])
            result = (
                comparison_plan.stored_recorded_baseline(row["result"], plan)
                if baseline
                else comparison_plan._stored(row["result"], plan)
            )
            _require(canonical(result) == canonical(private_result))
        self._authorize(actor, work)
        return MaterializedBenchmark(
            "scored",
            "protected-plan-" + plan["revision"],
            plan["revision"],
            work.revisions[0],
            "protected-result-" + digest(result),
            digest(result),
        )
