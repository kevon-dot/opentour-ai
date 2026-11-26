"""Generated rewards clarification evidence through the packaged source and worker.

The source export records one question and its answer before the routing decision.
This witnesses the resulting comparator, not a live Slack conversation or installed Zap.
"""

import hashlib
import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from zipfile import ZipFile

import pytest
from opentour_cell.pilot.mcp import PROTOCOL_VERSION
from opentour_cell.pilot.operating_point import canonical, digest
from opentour_cell.pilot.training import LocalTrainingStore

Json = dict[str, Any]
RUNTIME = Path(__file__).resolve().parents[2]


def write(path: Path, value: str) -> None:
    path.write_text(value, encoding="utf-8")
    path.chmod(0o600)


@pytest.fixture(scope="module")
def rewards_command(tmp_path_factory: pytest.TempPathFactory) -> list[str | Path]:
    root = tmp_path_factory.mktemp("rewards-discovery-kit")
    python = [sys.executable, "-I", "-S", "-B"]
    archive = root / "kit.zip"
    subprocess.run(
        [*python, RUNTIME.parents[1] / "tools/build_slack_pilot_kit.py", archive],
        check=True,
        capture_output=True,
        timeout=30,
    )
    with ZipFile(archive) as kit:
        executable = kit.extract("opentour-slack.pyz", root)
    return [*python, executable]


@pytest.mark.parametrize("answered", [True, False], ids=["both-branches", "missing-answer"])
def test_packaged_rewards_clarification_before_model(
    tmp_path: Path, rewards_command: list[str | Path], answered: bool
) -> None:
    fixture = json.loads(
        (Path(__file__).parent / "fixtures/rewards-discovery-v1.json").read_bytes()
    )
    assert fixture["schema_version"] == "opentour.generated.rewards-discovery.v1"
    assert fixture["evidence_kind"] == "generated_owner_local"
    expected = fixture["expected"]
    outcome = expected["answered" if answered else "unanswered"]
    owner = tmp_path / "owner"
    owner.mkdir(mode=0o700)
    grant = json.loads((RUNTIME / "examples/pilot-source-grant.template.json").read_bytes())
    store = LocalTrainingStore(owner / "training.sqlite3", "generated-rewards")
    grant.update(
        organization=store.organization,
        engagement=store.engagement_id,
        dataset_id="generated-rewards-discovery",
        workflow="rewards-helpdesk",
        field="route",
        policy_version="rewards-policy-1",
        source_schema_version="rewards-source-1",
        trace_source="generated-helpdesk-export",
        correction_source="generated-verifier-export",
        adjudicators=["generated-rewards-verifier"],
        permitted_features=[
            "clarification_answer",
            "clarification_question",
            "request",
            "thread_ref",
        ],
        training_attestation="att-2222222222222222",
    )
    scope = {
        k: grant[k]
        for k in ("organization", "workflow", "field", "policy_version", "source_schema_version")
    }
    traces, corrections = [], []
    for index, (answer, reference, decision_at) in enumerate(fixture["cases"]):
        event_id = f"reward-{index:02}"
        features = {
            "request": fixture["request"],
            "thread_ref": f"generated-thread-{index:02}",
            "clarification_question": fixture["clarification"]["question"],
        }
        if answered or event_id != fixture["unanswered_case"]:
            features["clarification_answer"] = answer
        traces.append(
            dict(
                scope,
                event_id=event_id,
                source=grant["trace_source"],
                customer_id="generated-member",
                group_id=f"generated-thread-{index:02}",
                decision_at=decision_at,
                features_available_at=decision_at
                + (1 if event_id == fixture["late_answer"] else -1),
                features=features,
                incumbent={"status": "automatic", "label": "points-guide"},
            )
        )
        corrections.append(
            dict(
                scope,
                event_id=event_id,
                outcome_id=f"verified-route-{index:02}",
                source=grant["correction_source"],
                adjudicator_id=grant["adjudicators"][0],
                facts_as_of=decision_at,
                outcome_at=decision_at + 2,
                verified_at=decision_at + 3,
                reference=reference,
            )
        )
    trace_path, correction_path = owner / "traces.jsonl", owner / "corrections.jsonl"
    for path, rows, key in (
        (trace_path, traces, "trace_sha256"),
        (correction_path, corrections, "correction_sha256"),
    ):
        write(path, "".join(canonical(row) + "\n" for row in rows))
        grant[key] = hashlib.sha256(path.read_bytes()).hexdigest()
    grant_path = owner / "source-grant.json"
    write(grant_path, canonical(grant))

    def run(*args: str | Path) -> Json:
        output = subprocess.run(
            [*rewards_command, *args], env={}, capture_output=True, text=True, timeout=30
        )
        assert output.returncode == 0, output.stderr
        assert not output.stderr
        return json.loads(output.stdout)

    source = ["source", "--store", store.path, "--organization", store.organization]
    inputs = ["--grant", grant_path, "--traces", trace_path, "--corrections", correction_path]
    discovered = run(*source, "discover", *inputs)
    [error_class] = discovered["suspected_error_classes"]
    assert error_class["count"] == error_class["groups"] == expected["recurring_groups"]
    confirmation = owner / "confirmation.json"
    write(
        confirmation,
        canonical(
            {
                "discovery_revision": discovered["discovery_revision"],
                "class_id": error_class["class_id"],
                "owner_id": grant["owner_id"],
                "attestation": "att-3333333333333333",
            }
        ),
    )
    frozen_path = owner / "intake.json"
    frozen = run(
        *source, "freeze", *inputs, "--output", frozen_path, "--confirmation", confirmation
    )
    assert frozen == run(*source, "inspect", "--intake", frozen_path)
    assert frozen["development_count"] == expected["development"]
    assert frozen["protected_count"] == expected["protected"]
    assert frozen["excluded_counts"] == {"future_decision_features": 1}
    data = json.loads(frozen_path.read_bytes())
    assert data["excluded"] == expected["excluded"]
    assert len(traces) == len(data["development"]) + len(data["protected"]) + len(data["excluded"])
    assert data["authority_revision"] == digest(grant)
    assert {r["features"]["request"] for r in data["development"] + data["protected"]} == {
        fixture["request"]
    }
    assert {r["features"]["clarification_question"] for r in data["development"]} == {
        fixture["clarification"]["question"]
    }

    manifest = owner / "plan.json"
    write(
        manifest,
        canonical(
            {
                "schema_version": "opentour.pilot.managed-repair.v1",
                "organization": store.organization,
                "engagement": store.engagement_id,
                "intake_file": frozen_path.name,
                "intake_revision": frozen["intake_revision"],
                "incumbent_revision": digest(["always", "points-guide"]),
                "audience": "generated-rewards-reviewer",
                "terms": {"minimum_coverage_bps": 10000, "error_ceiling_bps": 0},
                "rule": {"feature": "clarification_answer", "min_support": 2, "fallback": "review"},
            }
        ),
    )
    args = [
        "--directory",
        owner,
        "--organization",
        store.organization,
        "--manifest",
        manifest,
        "--authorization",
        owner / "authorization.json",
    ]
    revision = run("workflow-inspect", *args)["plan_revision"]
    run(
        "workflow-authorize",
        *args,
        "--plan-revision",
        revision,
        "--custodian-attestation",
        "att-5555555555555555",
        "--expires-at",
        str(int(time.time()) + 600),
    )

    def call(name: str, arguments: Json) -> Json:
        frames = [
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "generated-rewards-fixture", "version": "1"},
                },
            },
            {"method": "notifications/initialized"},
            {"id": 2, "method": "tools/call", "params": {"name": name, "arguments": arguments}},
        ]
        output = subprocess.run(
            [*rewards_command, "mcp-workflow", *args],
            input="".join(canonical({"jsonrpc": "2.0", **frame}) + "\n" for frame in frames),
            env={},
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        assert not output.stderr
        initial, reply = (json.loads(line) for line in output.stdout.splitlines())
        assert initial["result"]["protocolVersion"] == PROTOCOL_VERSION
        assert reply["id"] == 2 and not reply["result"].get("isError")
        return reply["result"]["structuredContent"]

    queued = call(
        "start_offline_evaluation", {"plan_revision": revision, "idempotency_ref": "rewards-v1"}
    )
    assert queued["state"] == "queued"
    worker = run("workflow-worker", *args, "--once")
    assert worker == call("get_offline_evaluation", {"run_id": queued["run_id"]})
    assert worker["state"] == outcome["state"]
    result_dir = owner / "managed-outputs" / worker["run_id"]
    rule = json.loads((result_dir / "rule.json").read_bytes())
    plan = json.loads((result_dir / "plan.json").read_bytes())
    report = json.loads((result_dir / "comparison.json").read_bytes())
    assert rule["source"] == "development_lookup"
    assert rule["mapping"] == fixture["clarification"]["branches"]
    assert rule["feature"] == "clarification_answer" and rule["fallback"] == "review"
    assert report["outcome"] == outcome["outcome"]
    assert report["decision"]["winner"] == outcome["winner"]
    assert set(report["arms"]) == {"incumbent", "simple"}
    assert (
        report["arms"]["incumbent"]["metrics"]["incorrect_automatic"]
        == expected["incumbent_errors"]
    )
    assert report["arms"]["simple"]["metrics"]["incorrect_automatic"] == 0
    metrics = report["arms"]["simple"]["metrics"]
    assert metrics["automatic"] == metrics["coverage"]["numerator"] == outcome["automatic"]
    assert metrics["coverage"]["denominator"] == expected["protected"]
    assert metrics["review"] == outcome["review"] and metrics["missing"] == 0
    assert metrics["operating_point"]["coverage"] == ("met" if answered else "missed")
    decisions = {row["case_id"]: row["simple"] for row in report["paired_cases"]}
    for row in data["protected"]:
        expected_decision = {"status": "automatic", "label": row["reference"]}
        if not answered and row["case_id"] == fixture["unanswered_case"]:
            expected_decision = {"status": "review"}
        assert decisions[row["case_id"]] == expected_decision
    assert not decisions.keys() & data["excluded"].keys()
    assert worker["result"] == {
        "outcome": outcome["outcome"],
        "case_count": expected["protected"],
        "result_revision": digest(report),
    }
    assert plan["development_decision"]["route"] == "simple"
    assert plan["development_decision"]["residual_count"] == 0
    assert plan["development_decision"]["fit_calls"] == 0
    assert {path.name for path in result_dir.iterdir()} == {
        "comparison.json",
        "plan.json",
        "rule.json",
        "run.json",
    }
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == expected["fit_jobs"]
        assert db.execute("SELECT COUNT(*) FROM fit_checkpoints").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0
