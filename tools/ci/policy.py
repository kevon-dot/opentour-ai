"""Validate the finite bootstrap workflow and deny production identity admission."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

CHECKS = (
    "lint",
    "typecheck",
    "python-unit",
    "web-unit",
    "browser-fixtures",
    "contract",
    "local-integration",
    "spec-traceability",
    "secret-scan",
    "dependency-scan",
    "iac-policy",
)


def workflow_errors(workflow: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if workflow.get("permissions") != {"contents": "read"}:
        errors.append("WORKFLOW_PERMISSION")
    events = workflow.get("on", {})
    if set(events) != {"pull_request", "push", "workflow_dispatch"} or events["pull_request"]:
        errors.append("UNTRUSTED_TRIGGER_OR_PATH_FILTER")
    jobs = workflow.get("jobs", {})
    if set(jobs) != set(CHECKS):
        errors.append("REQUIRED_CHECK_SET")
    for name, job in jobs.items():
        if set(job) != {"name", "runs-on", "timeout-minutes", "permissions", "steps"}:
            errors.append("UNEXPECTED_JOB_CAPABILITY")
        if job.get("name") != name or job.get("if") or job.get("continue-on-error"):
            errors.append("REQUIRED_JOB_CAN_SKIP_OR_SUCCEED_ON_ERROR")
        if job.get("permissions") != {"contents": "read"}:
            errors.append("JOB_PERMISSION")
        if job.get("runs-on") not in {"ubuntu-24.04", "macos-26"}:
            errors.append("UNAPPROVED_RUNNER")
        if job.get("environment") or job.get("secrets") or job.get("container"):
            errors.append("PRIVILEGED_JOB")
        steps = job.get("steps", [])
        if len(steps) != 4:
            errors.append("UNEXPECTED_STEPS")
        if not any(s.get("run") == "python -m tools.ci.run " + name for s in steps):
            errors.append("MISSING_ACTUAL_CHECK")
        for step in steps:
            if "env" in step or ("if" in step and "upload-artifact@" not in step.get("uses", "")):
                errors.append("REQUIRED_STEP_CAN_SKIP_OR_CHANGE_ENVIRONMENT")
            if step.get("continue-on-error"):
                errors.append("IGNORED_FAILURE")
            if "uses" in step and not re.fullmatch(r"actions/[\w-]+@[a-f0-9]{40}", step["uses"]):
                errors.append("UNPINNED_ACTION")
            if "run" in step and "${{" in step["run"]:
                errors.append("UNTRUSTED_COMMAND_INTERPOLATION")
            if "checkout@" in step.get("uses", "") and (
                step.get("with", {}).get("persist-credentials") is not False
            ):
                errors.append("PERSISTED_CHECKOUT_CREDENTIAL")
        upload = [s for s in steps if "upload-artifact@" in s.get("uses", "")]
        if len(upload) != 1 or upload[0].get("with", {}).get("path") != "evidence/ci/publish":
            errors.append("UNSCREENED_ARTIFACT_PATH")
        elif upload[0].get("with", {}).get("retention-days") != 7:
            errors.append("ARTIFACT_RETENTION")
        if upload and upload[0].get("if") != (
            "${{ always() && steps.verify.outputs.publish_ready == 'true' }}"
        ):
            errors.append("UNSCREENED_UPLOAD_CONDITION")
        actual = [s for s in steps if s.get("run") == "python -m tools.ci.run " + name]
        if len(actual) != 1 or actual[0].get("id") != "verify":
            errors.append("MISSING_VERIFIED_PUBLISH_STEP")
    serialized = json.dumps(workflow)
    if (
        re.search(r"\bsecrets\b", serialized)
        or "id-token" in serialized
        or "pull_request_target" in serialized
    ):
        errors.append("PRIVILEGED_CREDENTIAL_ROUTE")
    return sorted(set(errors))


def production_identity_allowed(claims: dict[str, Any], policy: dict[str, Any]) -> bool:
    """No production principal is admitted by bootstrap. Never an AWS verifier."""
    del claims
    if policy.get("production_oidc_profiles") != []:
        raise ValueError("Production trust needs its owning reviewed implementation")
    return False


def check(root: Path) -> dict[str, Any]:
    # JSON is the deliberately chosen strict YAML subset, parsed identically by CI and tests.
    workflow = json.loads((root / ".github/workflows/ci.yml").read_text())
    policy = json.loads((root / ".github/ci-policy.json").read_text())
    errors = workflow_errors(workflow)
    if tuple(policy.get("required_checks", [])) != CHECKS:
        errors.append("POLICY_CHECK_SET")
    if policy.get("production_oidc_profiles") != []:
        errors.append("UNADMITTED_PRODUCTION_PROFILE")
    return {
        "status": "FAIL" if errors else "PASS",
        "errors": errors,
        "kind": "simulated",
        "product_acceptance": "NOT_RUN",
        "production_identity_admitted": False,
    }
