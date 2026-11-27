"""Rebuild the owner-operated Slack pilot kit from the current immutable Git tree."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import runpy
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILDER = "tools/build_slack_pilot_kit.py"
ARCHIVE_HELPER = "tools/build_pilot_kit.py"
CELL = "services/cell-runtime"
VERSION = "1.0.0-rc.4"
PYTHON = (3, 12, 16)
MODULES = (
    "opentour_cell/__init__.py",
    "opentour_cell/pilot/__init__.py",
    "opentour_cell/pilot/brief.py",
    "opentour_cell/pilot/comparison_plan.py",
    "opentour_cell/pilot/managed_launch.py",
    "opentour_cell/pilot/release_gate.py",
    "opentour_cell/pilot/workplace_context.py",
    "opentour_cell/pilot/cohort_lease.py",
    "opentour_cell/pilot/qualification.py",
    "opentour_cell/pilot/autonomous_cycle.py",
    "opentour_cell/pilot/cycle_worker.py",
    "opentour_cell/pilot/approval_inbox.py",
    "opentour_cell/pilot/learning_mandate.py",
    "opentour_cell/pilot/zapier_approval.py",
    "opentour_cell/pilot/approval_reconciler.py",
    "opentour_cell/pilot/owner_settings.py",
    "opentour_cell/pilot/reviewer_assignment.py",
    "opentour_cell/pilot/managed_run.py",
    "opentour_cell/pilot/intake_mcp.py",
    "opentour_cell/pilot/intake_local.py",
    "opentour_cell/pilot/intake_admission.py",
    "opentour_cell/pilot/open_weight.py",
    "opentour_cell/pilot/pilot_metrics.py",
    "opentour_cell/pilot/repair_bundle.py",
    "opentour_cell/pilot/handoff_readback.py",
    "opentour_cell/pilot/local_handoff_permissions.py",
    "opentour_cell/pilot/recipient_delivery.py",
    "opentour_cell/pilot/recipient_handoff_simulator.py",
    "opentour_cell/pilot/release_authority.py",
    "opentour_cell/pilot/release_outbox.py",
    "opentour_cell/pilot/repair_workflow.py",
    "opentour_cell/pilot/simple_repair.py",
    "opentour_cell/pilot/source_intake.py",
    "opentour_cell/pilot/workflow_mcp.py",
    "opentour_cell/pilot/custody.py",
    "opentour_cell/pilot/readout.py",
    "opentour_cell/pilot/mcp.py",
    "opentour_cell/pilot/onboarding_mcp.py",
    "opentour_cell/pilot/operating_point.py",
    "opentour_cell/pilot/receipt_recovery.py",
    "opentour_cell/pilot/scoped_query_plan.py",
    "opentour_cell/pilot/slack_adapter.py",
    "opentour_cell/pilot/live_authority.py",
    "opentour_cell/pilot/live_evidence.py",
    "opentour_cell/pilot/verifier_form.py",
    "opentour_cell/pilot/live_reader.py",
    "opentour_cell/pilot/live_readiness.py",
    "opentour_cell/pilot/local_decision_context.py",
    "opentour_cell/pilot/training_readiness.py",
    "opentour_cell/pilot/readiness_selection.py",
    "opentour_cell/pilot/slack_views.py",
    "opentour_cell/pilot/status_questions.py",
    "opentour_cell/pilot/starter_setup.py",
    "opentour_cell/pilot/starter_cases.py",
    "opentour_cell/pilot/starter_case_authoring.py",
    "opentour_cell/pilot/starter_map_confirmation.py",
    "opentour_cell/pilot/starter_benchmark_bridge.py",
    "opentour_cell/pilot/starter_benchmark_worker.py",
    "opentour_cell/pilot/starter_completion.py",
    "opentour_cell/pilot/starter_evaluation_authority.py",
    "opentour_cell/pilot/starter_evaluation_session.py",
    "opentour_cell/pilot/starter_evaluation_worker.py",
    "opentour_cell/pilot/starter_reconnect.py",
    "opentour_cell/pilot/starter_local_baseline.py",
    "opentour_cell/pilot/starter_map_comparator.py",
    "opentour_cell/pilot/starter_score_inspection.py",
    "opentour_cell/pilot/starter_scoring.py",
    "opentour_cell/pilot/model_connector.py",
    "opentour_cell/pilot/model_registry.py",
    "opentour_cell/pilot/guided_policy.py",
    "opentour_cell/pilot/guided_decision_map.py",
    "opentour_cell/pilot/map_review.py",
    "opentour_cell/pilot/map_inspection.py",
    "opentour_cell/pilot/workflow_benchmarks.py",
    "opentour_cell/pilot/historical_benchmarks.py",
    "opentour_cell/pilot/historical_worker.py",
    "opentour_cell/pilot/slack_charts.py",
    "opentour_cell/pilot/slack_benchmark.py",
    "opentour_cell/pilot/text_classifier.py",
    "opentour_cell/pilot/training.py",
    "opentour_cell/pilot/training_data.py",
)
ASSETS = {
    "pilot-starter-cases.md": "docs/pilot-starter-cases.md",
    "pilot-guided-case-authoring.md": "docs/pilot-guided-case-authoring.md",
    "pilot-starter-map-confirmation.md": "docs/pilot-starter-map-confirmation.md",
    "pilot-starter-benchmark-worker.md": "docs/pilot-starter-benchmark-worker.md",
    "pilot-starter-score-completion.md": "docs/pilot-starter-score-completion.md",
    "pilot-starter-evaluation-authority.md": "docs/pilot-starter-evaluation-authority.md",
    "pilot-starter-evaluation-session.md": "docs/pilot-starter-evaluation-session.md",
    "pilot-starter-evaluation-worker.md": "docs/pilot-starter-evaluation-worker.md",
    "pilot-starter-model-reconnect.md": "docs/pilot-starter-model-reconnect.md",
    "pilot-starter-scoring.md": "docs/pilot-starter-scoring.md",
    "README.md": f"{CELL}/docs/slack-workspace-kit.md",
    "operator-guide.md": f"{CELL}/docs/pilot-kit.md",
    "pilot-recipient-delivery.md": f"{CELL}/docs/pilot-recipient-delivery.md",
    "pilot-recipient-handoff-simulator.md": f"{CELL}/docs/pilot-recipient-handoff-simulator.md",
    "pilot-release-outbox.md": f"{CELL}/docs/pilot-release-outbox.md",
    "pilot-learning-mandate.md": f"{CELL}/docs/pilot-learning-mandate.md",
    "templates/pilot-learning-mandate.template.json": (
        f"{CELL}/examples/pilot-learning-mandate.template.json"
    ),
    "integrations/zapier/slack-approval-signer.cjs": (
        f"{CELL}/integrations/zapier/slack-approval-signer.cjs"
    ),
    "pilot-workplace-context.md": f"{CELL}/docs/pilot-workplace-context.md",
    **{
        f"integrations/zapier/{name}": f"{CELL}/integrations/zapier/{name}"
        for name in (
            "approval-recipe-v1.json",
            "import-approval-recipe.cjs",
            "reconciliation-recipe-v1.json",
            "handoff-recipe-v1.json",
        )
    },
    "pilot-cohort-lease.md": f"{CELL}/docs/pilot-cohort-lease.md",
    "pilot-qualification.md": f"{CELL}/docs/pilot-qualification.md",
    "pilot-zapier-approval.md": f"{CELL}/docs/pilot-zapier-approval.md",
    "pilot-approval-reconciliation.md": f"{CELL}/docs/pilot-approval-reconciliation.md",
    "pilot-approval-schedule.md": f"{CELL}/docs/pilot-approval-schedule.md",
    "pilot-owner-settings.md": f"{CELL}/docs/pilot-owner-settings.md",
    "pilot-reviewer-replacement.md": f"{CELL}/docs/pilot-reviewer-replacement.md",
    "pilot-reviewer-replacement-integration.md": (
        f"{CELL}/docs/pilot-reviewer-replacement-integration.md"
    ),
    "pilot-autonomous-cycle.md": f"{CELL}/docs/pilot-autonomous-cycle.md",
    "pilot-managed-repairs.md": f"{CELL}/docs/pilot-managed-repairs.md",
    "protected-comparison.md": f"{CELL}/docs/protected-comparison.md",
    "pilot-managed-mcp.md": f"{CELL}/docs/pilot-managed-mcp.md",
    "pilot-source-intake.md": f"{CELL}/docs/pilot-source-intake.md",
    "pilot-historical-benchmarks.md": f"{CELL}/docs/pilot-historical-benchmarks.md",
    "pilot-historical-worker.md": f"{CELL}/docs/pilot-historical-worker.md",
    "pilot-map-review.md": "docs/pilot-map-review.md",
    "pilot-open-weight.md": "docs/pilot-open-weight.md",
    "pilot-repair-handoff.md": f"{CELL}/docs/pilot-repair-handoff.md",
    "generated/prepare-repair-example.py": f"{CELL}/examples/prepare-repair-example.py",
    "generated/pilot-source-trace.jsonl": f"{CELL}/examples/pilot-source-trace.json",
    "generated/pilot-source-correction.jsonl": f"{CELL}/examples/pilot-source-correction.json",
    "generated/pilot-source-grant.template.json": (
        f"{CELL}/examples/pilot-source-grant.template.json"
    ),
    "training/profiles/smollm2_135m_lora_v1.json": "training/profiles/smollm2_135m_lora_v1.json",
    "training/profiles/smollm2_135m_lora_v1.requirements.txt": (
        "training/profiles/smollm2_135m_lora_v1.requirements.txt"
    ),
    "training/src/opentour_training/training/open_weight_worker.py": (
        "training/src/opentour_training/training/open_weight_worker.py"
    ),
    "manifest-guide.md": f"{CELL}/docs/pilot-readout.md",
    "pilot-onboarding-mcp.md": f"{CELL}/docs/pilot-onboarding-mcp.md",
    "pilot-mcp.md": f"{CELL}/docs/pilot-mcp.md",
    "pilot-operating-point.md": f"{CELL}/docs/pilot-operating-point.md",
    "pilot-receipt-recovery.md": f"{CELL}/docs/pilot-receipt-recovery.md",
    "generated/readout-input.json": f"{CELL}/examples/pilot-evaluation.json",
    "generated/coverage-floor.json": f"{CELL}/examples/pilot-coverage-floor.json",
    "generated/operating-agreement.template.json": (
        f"{CELL}/examples/pilot-operating-agreement.template.json"
    ),
    "templates/pilot-receipt.blank.json": f"{CELL}/examples/pilot-receipt.blank.json",
    "integrations/slack/manifest.json": f"{CELL}/integrations/slack/manifest.json",
    "integrations/slack/requirements.txt": f"{CELL}/integrations/slack/requirements.txt",
    "integrations/slack/README.md": f"{CELL}/integrations/slack/README.md",
    "integrations/slack/worker.example.json": f"{CELL}/integrations/slack/worker.example.json",
}
for asset in (
    "health.py",
    "manifest.example.json",
    "opentour-health.service",
    "opentour-worker.service",
    "README.md",
):
    ASSETS[f"integrations/worker-host/{asset}"] = f"{CELL}/integrations/worker-host/{asset}"

ENTRY = """import importlib
import sys

COMMANDS = {
    "qualification": ("qualification", "main"),
    "cohort-lease": ("cohort_lease", "main"),
    "data": ("training", "main"),
    "source": ("source_intake", "main"),
    "workplace": ("workplace_context", "main"),
    "workplace-context": ("workplace_context", "main"),
    "cycle": ("autonomous_cycle", "main"),
    "learning-mandate": ("learning_mandate", "main"),
    "cycle-worker": ("cycle_worker", "main"),
    "slack-launch-authorize": ("managed_launch", "main"),
    "zapier-approval": ("zapier_approval", "main"),
    "approval-reconciler": ("approval_reconciler", "main"),
    "recipient-handoff-simulator": ("recipient_handoff_simulator", "main"),
    "owner-settings": ("owner_settings", "main"),
    "reviewer-assignment": ("reviewer_assignment", "main"),
    "repair": ("repair_workflow", "main"),
    "bundle": ("repair_bundle", "main"),
    "mcp-intake": ("intake_local", "main"),
    "live-reader": ("live_reader", "main"),
    "historical-worker": ("historical_worker", "main"),
    "starter-benchmark-worker": ("starter_benchmark_worker", "main"),
    "starter-evaluation-worker": ("starter_evaluation_worker", "main"),
    "mcp-workflow": ("workflow_mcp", "main"),
    "workflow-authorize": ("workflow_mcp", "authorize_main"),
    "workflow-inspect": ("workflow_mcp", "inspect_main"),
    "workflow-worker": ("workflow_mcp", "worker_main"),
    "evaluate": ("readout", "main"),
    "custody": ("custody", "main"),
    "mcp-onboard": ("onboarding_mcp", "main"),
    "onboarding-authorize": ("onboarding_mcp", "authorize_main"),
    "mcp-readout": ("mcp", "main"),
    "slack": ("slack_adapter", "main"),
}
if sys.version_info[:3] != (3, 12, 16):
    print("Use the supported Python 3.12.16 runtime.", file=sys.stderr)
    raise SystemExit(2)
if sys.argv[1:] == ["--version"]:
    print(VERSION)
    raise SystemExit(0)
if sys.argv[1:] in ([], ["-h"], ["--help"]):
    print("Usage: python opentour-slack.pyz "
          "{" + "|".join(COMMANDS) + "} ...")
    raise SystemExit(0)
command = COMMANDS.get(sys.argv[1])
if command is None:
    print("Unknown command. Use --help for supported commands.", file=sys.stderr)
    raise SystemExit(2)
sys.argv = [sys.argv[0] + " " + sys.argv[1], *sys.argv[2:]]
module, entry = command
raise SystemExit(getattr(importlib.import_module("opentour_cell.pilot." + module), entry)())
"""


def _git(*args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(ROOT), *args])


def _json(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def build(revision: str) -> bytes:
    def read(path: str) -> bytes:
        return _git("show", f"{revision}:{path}")

    # Reuse the existing deterministic ZIP writer after verifying both scripts at the source SHA.
    for path in (BUILDER, ARCHIVE_HELPER):
        if (ROOT / path).read_bytes() != read(path):
            raise ValueError("Build from the recorded version of both packaging scripts.")
    archive = runpy.run_path(str(ROOT / ARCHIVE_HELPER))["_archive"]
    modules = {name: read(f"{CELL}/src/{name}") for name in MODULES}
    version = f"OpenTour Slack workspace kit {VERSION} ({revision})"
    modules["__main__.py"] = (f"VERSION = {version!r}\n" + ENTRY).encode()
    files = {name: read(path) for name, path in ASSETS.items()}
    for name in ("generated/pilot-source-trace.jsonl", "generated/pilot-source-correction.jsonl"):
        records = json.loads(files[name])
        files[name] = b"".join(
            (json.dumps(record, separators=(",", ":")) + "\n").encode() for record in records
        )
    files["opentour-slack.pyz"] = archive(modules, prefix=b"#!/usr/bin/env python3\n")
    files["PROVENANCE.json"] = _json(
        {
            "kit_version": VERSION,
            "source_repository": "kevon-dot/opentour-ai",
            "source_commit": revision,
            "python_version": "3.12.16",
            "artifact_kind": "release_candidate",
            "fixture_kind": "generated",
            "training_profile": "cpu_text_v1",
            "optional_local_open_weight_profile": "smollm2_135m_lora_v1",
            "managed_repair_routes": ["executable_simple_repair", "permitted_open_weight_adapter"],
        }
    )
    with tempfile.TemporaryDirectory() as temporary:
        work = Path(temporary)
        (work / "opentour-slack.pyz").write_bytes(files["opentour-slack.pyz"])
        executable = [sys.executable, "-I", "-S", "-B", "opentour-slack.pyz"]
        subprocess.run(
            [*executable, "evaluate", "--report-dir", "supplied", "-"],
            input=files["generated/readout-input.json"],
            cwd=work,
            env={},
            check=True,
        )
        for name in ("aggregate.json", "brief.md"):
            files[f"generated/expected-supplied/{name}"] = (work / "supplied" / name).read_bytes()
        command = [
            *executable,
            "data",
            "--db",
            "state.sqlite3",
            "--org",
            "generated-demo",
        ]
        files["generated/dataset.json"] = _json(
            json.loads(
                subprocess.check_output(
                    command + ["generated-fixture", "--reviewer", "generated-reviewer"],
                    cwd=work,
                    env={},
                )
            )
        )
        files["generated/evaluation-only.json"] = _json(
            json.loads(
                subprocess.check_output(
                    command + ["generated-evaluation-fixture"], cwd=work, env={}
                )
            )
        )
        result = json.loads(subprocess.check_output(command + ["rehearse"], cwd=work, env={}))
        # Each installation creates its own opaque job ID. Only reproducible proof is distributed.
        files["generated/expected-training.json"] = _json(
            {
                key: result[key]
                for key in (
                    "dataset_id",
                    "dataset_revision",
                    "profile",
                    "epochs",
                    "status",
                    "cpu_limit_seconds",
                    "result",
                )
            }
        )
    files["SHA256SUMS"] = "".join(
        f"{hashlib.sha256(value).hexdigest()}  {name}\n" for name, value in sorted(files.items())
    ).encode()
    payload: bytes = archive(files, executable="opentour-slack.pyz")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="new ZIP output path")
    args = parser.parse_args()
    if sys.version_info[:3] != PYTHON:
        parser.exit(2, "Build with Python 3.12.16.\n")
    try:
        revision = _git("rev-parse", "--verify", "HEAD^{commit}").decode().strip()
        payload = build(revision)
        with tempfile.TemporaryDirectory(dir=args.output.parent) as work:
            candidate = Path(work) / "candidate.zip"
            candidate.write_bytes(payload)
            os.link(candidate, args.output)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        parser.exit(2, f"Unable to build kit: {exc}\n")
    print(f"{hashlib.sha256(payload).hexdigest()}  {args.output.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
