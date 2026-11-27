"""Actual generated application observations selected by the private worker configuration."""

import hmac
import json
import sqlite3
import subprocess
import sys
import time

import pytest
from opentour_cell.pilot import live_reader, local_decision_context
from opentour_cell.pilot.operating_point import canonical, digest
from opentour_cell.pilot.readiness_selection import open_readiness
from test_intake_mcp import _write_private
from test_live_authority import KEY
from test_live_evidence import DECISION, OUTCOME, decision, outcome
from test_live_readiness import Case
from test_live_worker_readiness import configured


def sign(export, grant):
    _write_private(
        export.directory / "decision-context-grant.json",
        {
            "grant": grant,
            "seal_sha256": hmac.digest(KEY, canonical(grant).encode(), "sha256").hex(),
        },
    )


def prepared(tmp_path, slack_actor=None, extra_decision_resource=False):
    export, selection = configured(tmp_path / "owner")
    scope = export.case.body
    if slack_actor:
        scope["actors"][slack_actor] = ["inspect", "custodian"]
        export.case.write()
    if extra_decision_resource:
        original = next(g for g in scope["grants"] if g["resource"] == DECISION.resource)
        scope["grants"].append(
            dict(original, resource="other-decisions", source_revision="other-revision")
        )
        export.case.write()
    policy = dict(
        Case(tmp_path / "generated-template").policy,
        organization="org-a",
        workflow_ref=export.case.workflow,
        engagement=scope["engagement_epoch"],
        dataset_ref=scope["dataset_ref"],
        dataset_revision=scope["dataset_revision"],
    )
    _write_private(export.directory / "repair-policy.json", policy)
    selection.update(policy_file="repair-policy.json", context_file="decision-context-grant.json")
    _write_private(export.directory / "readiness-selection.json", selection)
    recorded = decision(
        input_ref="observed-input-1", input_fidelity="observed", action_ref="supplies"
    )
    body = {
        "schema_version": local_decision_context.BODY,
        "records": [
            {
                **{
                    key: recorded[key]
                    for key in (
                        "decision_id",
                        "decision_revision",
                        "case_id",
                        "family_id",
                        "input_ref",
                        "action_ref",
                        "policy_ref",
                        "input_fidelity",
                    )
                },
                "receipt_ref": "application-observation-1",
                "features": {"description": "carrier"},
                "incumbent": {"status": "automatic", "label": "supplies"},
            }
        ],
    }
    _write_private(export.directory / "decision-context-body.json", body)
    grant = dict(
        schema_version=local_decision_context.GRANT,
        grant_ref="context-access-v1",
        authority_revision=digest(scope),
        tenant="org-a",
        workflow_ref=export.case.workflow,
        engagement_epoch=scope["engagement_epoch"],
        worker_ref="worker",
        resource=DECISION.resource,
        grant_revision=DECISION.revision,
        source_revision=DECISION.source_revision,
        deciding_namespace=DECISION.deciding_namespace,
        allowed_fields=["description"],
        expires_at=int(time.time()) + 900,
        body={"file": "decision-context-body.json", "sha256": digest(body)},
    )
    sign(export, grant)
    export.install(DECISION, [[export.row(recorded, 11)]])
    decision_run = export.submit(DECISION)
    assert live_reader.main([str(export.selection), decision_run]) == 0
    export.install(OUTCOME, [[export.row(outcome(verified_action_ref="freight"), 21)]])
    outcome_run = export.submit(OUTCOME)
    return export, grant, body, decision_run, outcome_run


def invoke(export, run, command=None):
    result = subprocess.run(
        [
            *(command or [sys.executable, "-m", "opentour_cell.pilot.live_reader"]),
            str(export.selection),
            run,
        ],
        text=True,
        capture_output=True,
        timeout=30,
    )
    return result.returncode, result.stdout.strip(), result.stderr


def current(export):
    components = open_readiness(export.directory, "org-a", export.case.workflow)
    return components, components.readiness.read(export.case.workflow, actor=export.case.owner)


def test_private_config_worker_materializes_observed_one_root_no_residual_then_restarts(tmp_path):
    export, _, _, _, run = prepared(tmp_path)
    benchmark = export.directory / "workflow-benchmarks.sqlite3"
    before = benchmark.read_bytes()
    assert invoke(export, run) == (0, "materialized", "")
    components, receipt = current(export)
    assert receipt["reason_code"] == "too_early_to_train"
    assert (
        receipt["verified_root_count"],
        receipt["residual_group_count"],
        receipt["minimum_residual_groups"],
    ) == (1, 0, 3)
    assert invoke(export, run) == (0, "already_materialized", "")
    assert current(export)[1] == receipt
    assert benchmark.read_bytes() == before
    with sqlite3.connect(export.directory / "training.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM jobs").fetchone() == (0,)
    with sqlite3.connect(export.directory / "live-repair-assessments.sqlite3") as db:
        (raw,) = db.execute("SELECT body FROM assessments").fetchone()
    assessment = json.loads(raw)
    assert assessment["context_receipts"] == ["application-observation-1"]
    assert len(assessment["roots"]) == len(assessment["families"]) == 1
    assert "carrier" not in json.dumps(receipt) and "description" not in json.dumps(receipt)
    assert "features" not in assessment
    assert components.assessments is not None


@pytest.mark.parametrize(
    "fault", ["foreign", "expired", "unsigned", "withdrawn", "fields", "source"]
)
def test_context_permission_denies_before_raw_read(tmp_path, monkeypatch, fault):
    export, grant, _, _, run = prepared(tmp_path)
    if fault == "foreign":
        grant["tenant"] = "other-org"
    elif fault == "expired":
        grant["expires_at"] = 1
    elif fault == "fields":
        grant["allowed_fields"] = ["different-field"]
    elif fault == "source":
        grant["source_revision"] = "other-source"
    sign(export, grant)
    if fault == "unsigned":
        envelope = json.loads((export.directory / "decision-context-grant.json").read_bytes())
        envelope["seal_sha256"] = "0" * 64
        _write_private(export.directory / "decision-context-grant.json", envelope)
    if fault == "withdrawn":
        _write_private(export.directory / "decision-context-withdrawn-context-access-v1.json", {})
    read = local_decision_context._read_private

    def observed(path, *args):
        assert path.name != "decision-context-body.json", "denied raw input read"
        return read(path, *args)

    monkeypatch.setattr(local_decision_context, "_read_private", observed)
    assert live_reader.main([str(export.selection), run]) == 1
    assert not (export.directory / "live-repair-assessments.sqlite3").exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("input_fidelity", "reconstructed"),
        ("input_ref", "other-input"),
        ("action_ref", "freight"),
        ("policy_ref", "other-policy"),
        ("decision_revision", 2),
    ],
)
def test_observed_receipt_must_bind_exact_committed_decision(tmp_path, field, value):
    export, grant, body, _, run = prepared(tmp_path)
    body["records"][0][field] = value
    _write_private(export.directory / "decision-context-body.json", body)
    grant["body"]["sha256"] = digest(body)
    sign(export, grant)
    code, _, _ = invoke(export, run)
    _, receipt = current(export)
    assert code == 1 or receipt["reason_code"] == "decision_time_input_required"
    assert receipt is None or receipt["reason_code"] != "too_early_to_train"


def test_cached_assessment_checks_current_grant_without_raw_reads(tmp_path, monkeypatch):
    export, grant, _, _, run = prepared(tmp_path)
    assert invoke(export, run)[0] == 0
    components, receipt = current(export)
    raw = export.directory / "decision-context-body.json"
    raw.unlink()
    assert current(export)[1] == receipt
    _write_private(export.directory / "decision-context-withdrawn-context-access-v1.json", {})
    assert current(export)[1] is None
    assert components.readiness.read(export.case.workflow, actor=export.case.owner) is None
    assert invoke(export, run)[0] == 1
    assert grant["grant_ref"] == "context-access-v1"


def test_context_rotation_during_source_read_cannot_publish(tmp_path, monkeypatch):
    export, grant, _, _, run = prepared(tmp_path)
    read = local_decision_context._read_private

    def rotated(path, *args):
        value = read(path, *args)
        if path.name == "decision-context-body.json":
            grant["expires_at"] = 1
            sign(export, grant)
        return value

    monkeypatch.setattr(local_decision_context, "_read_private", rotated)
    assert live_reader.main([str(export.selection), run]) == 1
    assert not (export.directory / "live-repair-assessments.sqlite3").exists()


@pytest.mark.parametrize("stage", ["writer_open", "assessment_insert"])
def test_context_withdrawal_inside_assessment_transaction_rolls_back(tmp_path, monkeypatch, stage):
    export, _, _, _, run = prepared(tmp_path)
    path = export.directory / "live-repair-assessments.sqlite3"
    connect = sqlite3.connect

    def withdraw():
        _write_private(export.directory / "decision-context-withdrawn-context-access-v1.json", {})

    class WithdrawingConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            result = super().execute(sql, *args, **kwargs)
            if stage == "assessment_insert" and sql.startswith("INSERT OR IGNORE INTO assessments"):
                withdraw()
            return result

    def opened(database, *args, **kwargs):
        if database == path:
            kwargs["factory"] = WithdrawingConnection
        db = connect(database, *args, **kwargs)
        if database == path and stage == "writer_open":
            withdraw()
        return db

    monkeypatch.setattr(sqlite3, "connect", opened)
    assert live_reader.main([str(export.selection), run]) == 1
    assert (export.directory / "decision-context-withdrawn-context-access-v1.json").exists()
    with connect(path) as db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='assessments'").fetchone():
            assert db.execute("SELECT count(*) FROM assessments").fetchone() == (0,)


@pytest.mark.parametrize("matching_origin", [True, False])
def test_context_permission_requires_actual_decision_resource_before_raw_read(
    tmp_path, monkeypatch, matching_origin
):
    export, grant, _, _, run = prepared(tmp_path, extra_decision_resource=True)
    if not matching_origin:
        grant.update(resource="other-decisions", source_revision="other-revision")
    sign(export, grant)
    reads = []
    read = local_decision_context._read_private

    def observed(path, *args):
        if path.name == "decision-context-body.json":
            reads.append(path.name)
        return read(path, *args)

    monkeypatch.setattr(local_decision_context, "_read_private", observed)
    code = live_reader.main([str(export.selection), run])
    _, receipt = current(export)
    if matching_origin:
        assert code == 0 and reads
        assert receipt["reason_code"] == "too_early_to_train"
        assert (receipt["verified_root_count"], receipt["residual_group_count"]) == (1, 0)
    else:
        assert code == 1 and not reads and receipt is None


@pytest.mark.parametrize("state", ["current", "superseded", "conflicting"])
def test_context_origin_requires_current_nonconflicting_delivery(tmp_path, monkeypatch, state):
    export, _, _, _, run = prepared(tmp_path, extra_decision_resource=True)
    live = export.case.live
    recorded = decision(
        input_ref="observed-input-1", input_fidelity="observed", action_ref="supplies"
    )

    def add(resource, source, events):
        binding = live.save_binding(export.case.custodian, export.case.workflow, resource)
        live.ingest_page(
            export.case.actor,
            binding,
            page_ref="current-delivery",
            source_revision=source,
            events=events,
            source_positions=tuple(range(18, 18 + len(events))),
            event_epochs=(export.case.body["engagement_epoch"],) * len(events),
            checkpoint=17 + len(events),
        )

    add("other-decisions", "other-revision", ({**recorded, "event_id": "other-delivery"},))
    newest = {**recorded, "event_revision": 2}
    if state != "current":
        newest["decision_id"] = "D2"
    events = (newest,)
    if state == "conflicting":
        events += ({**newest, "decision_id": "D3"},)
    add(DECISION.resource, DECISION.source_revision, events)
    reads = []
    read = local_decision_context._read_private

    def observed(path, *args):
        if path.name == "decision-context-body.json":
            reads.append(path.name)
        return read(path, *args)

    monkeypatch.setattr(local_decision_context, "_read_private", observed)
    code = live_reader.main([str(export.selection), run])
    _, receipt = current(export)
    if state == "current":
        assert code == 0 and reads and receipt["reason_code"] == "too_early_to_train"
    else:
        assert code == 1 and not reads and receipt is None


def context_home_witness(tmp_path, monkeypatch, command=None):
    from opentour_cell.pilot.training import LocalTrainingStore
    from test_app_home import publish
    from test_reviewer_workflow import restart
    from test_slack_adapter import ACTOR, Harness
    from test_starter_setup import effects

    export, _, _, _, run = prepared(tmp_path, slack_actor=ACTOR)
    h = Harness(tmp_path / "slack", organization="org-a")
    h.path = export.directory / "slack.json"
    h.config.update(directory=str(export.directory), live_workflow_ref=export.case.workflow)
    h.config["users"][ACTOR]["datasets"] = [export.case.body["dataset_ref"]]
    h.store = LocalTrainingStore(export.directory / "training.sqlite3", "org-a", existing_only=True)
    h.save()
    restart(h)
    benchmark = export.directory / "workflow-benchmarks.sqlite3"
    before = benchmark.read_bytes()
    assert invoke(export, run, command) == (0, "materialized", "")
    components, receipt = current(export)
    home = json.dumps(publish(h, monkeypatch))
    assert "Too early to train" in home and "0 residual groups" in home
    assert receipt["verified_root_count"] == 1 and receipt["minimum_residual_groups"] == 3
    assert invoke(export, run, command) == (0, "already_materialized", "")
    restart(h)
    assert current(export)[1] == receipt and "Too early to train" in json.dumps(
        publish(h, monkeypatch)
    )
    assert effects(h) == (0,) * 7 and benchmark.read_bytes() == before
    with sqlite3.connect(export.directory / "learning-mandates.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM reservations").fetchone() == (0,)
    # Reopening real truth invalidates the prior verdict before the next refresh.
    with sqlite3.connect(export.case.live.path) as db:
        (binding,) = db.execute(
            "SELECT ref FROM live_bindings WHERE kind='independent_outcome'"
        ).fetchone()
    export.case.live.ingest_page(
        export.case.actor,
        binding,
        page_ref="reopen-page",
        source_revision=OUTCOME.source_revision,
        events=(
            outcome(
                event_id="reopened-v2",
                outcome_revision=2,
                finality="reopened",
                verified_action_ref=None,
            ),
        ),
        source_positions=(22,),
        event_epochs=(export.case.body["engagement_epoch"],),
        checkpoint=22,
    )
    assert components.readiness.read(export.case.workflow, actor=export.case.owner) is None
    assert invoke(export, run, command) == (0, "already_materialized", "")
    restarted = current(export)[1]
    assert restarted["verified_root_count"] == 0 and restarted["pending_count"] == 1
    assert "Awaiting outcome" in json.dumps(publish(h, monkeypatch))
    assert benchmark.read_bytes() == before and effects(h) == (0,) * 7
    return receipt, restarted


def test_normal_worker_and_home_use_configured_observations_across_reopen(tmp_path, monkeypatch):
    context_home_witness(tmp_path, monkeypatch)
