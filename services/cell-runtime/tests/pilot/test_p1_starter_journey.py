"""Exercise the generated P1 oracle through emitted private owner controls."""

import json
import sqlite3
import subprocess
import sys
import time
from dataclasses import asdict, replace
from pathlib import Path

import pytest
from opentour_cell.pilot import (
    model_connector,
    model_registry,
    starter_case_authoring,
    text_classifier,
)
from opentour_cell.pilot.intake_mcp import IntakePrincipal
from opentour_cell.pilot.operating_point import digest
from opentour_cell.pilot.starter_cases import FrozenPlan
from opentour_cell.pilot.starter_map_confirmation import GRANT, SELECTION, StarterCaseSession
from test_app_home import click, publish
from test_intake_mcp import _write_private
from test_p1_cpu_fixture import write_p1_cpu_fixture
from test_policy_map_review import (
    actions,
    assertion_values,
    branch_values,
    enter_map,
    open_assertions,
    prepare_map,
)
from test_reviewer_workflow import buttons, restart, screen, text
from test_slack_adapter import ACTOR, OTHER, Harness
from test_starter_case_authoring import P1_OWNER_LABELS, P1_POLICY, SevenInvoiceFamilies
from test_starter_case_review_ui import choose, open_cases, select
from test_starter_evaluation_authority import seal
from test_starter_map_confirmation import sign_case
from test_starter_setup import configure_starter, effects, open_starter

pytest_plugins = ("test_starter_setup",)


def _confirm_p1(h, monkeypatch):
    workflow = prepare_map(
        h,
        monkeypatch,
        P1_POLICY,
        field_kinds={"category": "reference", "po": "reference"},
        actions=sorted({label for label in P1_OWNER_LABELS.values() if label}),
    )
    draft = json.loads(next(h.path.parent.glob("source-intake-starter-draft-*.json")).read_bytes())
    assert draft["workflow_ref"] == workflow
    restart(h)
    view = open_starter(h, monkeypatch)
    assert screen(h, view)["callback_id"] == "ot_starter_saved"
    assert workflow in text(h, view) and draft["run_ref"] in text(h, view)
    view = open_assertions(h, monkeypatch, ACTOR)
    h.form(
        view, assertion_values("Invoice arrives", "unresolved", "P1 is silent on mixed invoices.")
    )
    view = enter_map(h, monkeypatch, ACTOR)
    first = [ref for name, ref in actions(screen(h, view)) if name == "ot_map_edit"]
    h.action(view, "ot_map_page", "next")
    second = [ref for name, ref in actions(screen(h, view)) if name == "ot_map_edit"]
    h.action(view, "ot_map_page", "previous")
    assert len(first) == 5 and len(second) == 1
    for index, ref in enumerate(first + second):
        if index == len(first):
            h.action(view, "ot_map_page", "next")
        h.action(view, "ot_map_citation", ref)
        assert screen(h, view)["callback_id"] == "ot_map_clause"
        assert screen(h, view)["blocks"][2]["text"]["text"].strip().encode() in P1_POLICY
        h.action(view, "ot_map_back", "back")
        if index == len(first):
            h.action(view, "ot_map_page", "next")
        assert ("ot_map_edit", ref) in actions(screen(h, view))
        h.action(view, "ot_map_edit", ref)
        h.form(view, branch_values(screen(h, view)))
        assert screen(h, view)["callback_id"] == "ot_map_review"
    maps, principal = h.app._map_service(ACTOR, workflow)
    confirmed = maps.preview(principal)
    assert confirmed.confirmed_ref and confirmed.settings_ref
    assert confirmed.settings_trigger == "Invoice arrives" and confirmed.fallback_action is None
    assert confirmed.expected_action({"category": "mixed_invoice", "po": "po-f"}) == (None, ())
    return draft, maps, confirmed


def _authorize_case_review(h, draft, maps, confirmed):
    directory = h.path.parent
    selection = json.loads((directory / "selected.json").read_bytes())
    grant = maps.authority._read_grant()
    plan = FrozenPlan(
        "baseline-pending",
        "simple-policy-v1",
        "exact-action-v1",
        "review-v1",
        "v1",
        "independent-evaluator",
        "selected-model",
    )
    capability = {
        "schema_version": GRANT,
        "grant_ref": "generated-p1-review",
        "tenant": selection["tenant"],
        "engagement_epoch": selection["engagement_epoch"],
        "workflow_ref": draft["workflow_ref"],
        "run_ref": draft["run_ref"],
        "starter_grant_revision": grant.revision,
        "source_revision": grant.source_revision,
        "settings_revision": grant.setup_policy_revision,
        "source_digest": confirmed.source_digest,
        "map_confirmation_ref": confirmed.confirmed_ref,
        "map_settings_ref": confirmed.settings_ref,
        "owner_ref": selection["owner_id"],
        "owner_actor": ACTOR,
        "custodian_ref": grant.custodian_id,
        "expires_at": int(time.time()) + 600,
        "owner_purposes": ["propose", "review", "adjudicate", "freeze"],
        "worker_purposes": {},
        "frozen_plan": asdict(plan),
        "critical_branches": [
            row.branch_ref
            for row in confirmed.branches
            if row.action in {"SOFTWARE", "FREIGHT", "SUPPLIES", "REVIEW"}
        ],
    }
    sign_case(directory, capability)
    _write_private(
        directory / "starter-case-selection.json",
        {"schema_version": SELECTION, "grant_file": "starter-case-grant.json"},
    )
    return StarterCaseSession(h.app.settings, IntakePrincipal(selection["tenant"], ACTOR, "slack"))


@pytest.fixture
def p1_frozen_from_home(tmp_path, monkeypatch, request):
    h = Harness(tmp_path, seed_generated_dataset=False)
    configure_starter(h)
    if getattr(request, "param", False):
        h.config["recipient"] = ACTOR
        h.save()
    directory = h.path.parent
    with sqlite3.connect(h.store.path) as db:
        assert db.execute("SELECT count(*) FROM datasets").fetchone() == (0,)
    assert not any(
        (directory / file).exists()
        for file in (
            "intake.sqlite3",
            "workflow-benchmarks.sqlite3",
            "starter-cases.sqlite3",
            "starter-evaluations.sqlite3",
            "model-registration.json",
        )
    )
    assert effects(h) == (0,) * 7
    with monkeypatch.context() as fault:
        render = h.app.views.app_home_view

        def without_add_workflow(*args, **kwargs):
            return render(*args, **(kwargs | {"can_add_workflow": False}))

        fault.setattr(h.app.views, "app_home_view", without_add_workflow)
        with pytest.raises(AssertionError, match="ot_app_add_workflow"):
            open_starter(h, monkeypatch)
        assert not (directory / "intake.sqlite3").exists()
    draft, maps, confirmed = _confirm_p1(h, monkeypatch)
    owner = _authorize_case_review(h, draft, maps, confirmed)
    with monkeypatch.context() as fixture:
        fixture.setattr(starter_case_authoring, "guided_case_provider", SevenInvoiceFamilies)

        def forbidden(*_args, **_kwargs):
            pytest.fail("Owner case review ran a model before freeze.")

        fixture.setattr(text_classifier, "predict", forbidden)
        fixture.setattr(text_classifier, "fit", forbidden)
        view = open_cases(h, monkeypatch)
        choose(h, view, "ot_case_generate")
        pack = owner.review()
        rows = pack["cases"]
        assert len(rows) == len({row["family_id"] for row in rows}) == 7
        assert all(row["owner_adjudicated_target"] is None for row in rows)
        choices = pack["authority"]["allowed_actions"]
        for index, row in enumerate(rows):
            name = row["case_id"]
            assert name in text(h, view)
            assert set(dict(row["decision_inputs"])) == {"category", "po"}
            label = P1_OWNER_LABELS[name]
            choose(h, view, "ot_case_edit")
            h.form(
                view,
                {
                    "target": {
                        "target": select(
                            "unresolved" if label is None else str(choices.index(label))
                        )
                    },
                    "reason": {
                        "reason": {
                            "type": "plain_text_input",
                            "value": "P1 is silent on mixed invoices."
                            if label is None
                            else "Checked the cited P1 rule.",
                        }
                    },
                },
            )
            assert owner.review()["cases"][index]["owner_adjudicated_target"] == label
            if index + 1 < len(rows):
                choose(h, view, "ot_case_page_next")
        choose(h, view, "ot_case_splits")
        families = sorted(row["family_id"] for row in rows)
        splits = {
            family: "excluded"
            if family == "family-mixed"
            else "development"
            if family in {"family-tax", "family-utilities"}
            else "reserved"
            for family in families
        }
        h.form(
            view,
            {
                f"family_{i}": {f"family_{i}": select(splits[family])}
                for i, family in enumerate(families)
            },
        )
        assert owner.review()["frozen"] is None
        choose(h, view, "ot_case_commit")
        assert "Frozen, unscored" in text(h, view)
    return h, owner, draft, maps, confirmed, owner.review()


def test_generated_p1_home_freezes_blind_owner_truth(p1_frozen_from_home, monkeypatch):
    h, owner, draft, _maps, _confirmed, pack = p1_frozen_from_home
    frozen = pack["frozen"]
    assert frozen["state"] == "frozen_unscored"
    assert tuple(
        frozen[k] for k in ("proposed", "resolved", "unresolved", "development", "reserved")
    ) == (7, 6, 1, 2, 4)
    expected_families = {
        "family-freight",
        "family-no-po",
        "family-subscription",
        "family-supplies",
    }
    assert {row["family_id"] for row in pack["cases"] if row["split_ref"] == "reserved"} == (
        expected_families
    )
    references = frozen["reserved_family_refs"]
    assert len(set(references)) == len(references) == 4
    assert all(ref.startswith("starter-family-") for ref in references)
    assert not frozen["uncovered_critical"]
    rows = {row["case_id"]: row for row in pack["cases"]}
    assert rows["freight"]["suggestion"] == "SUPPLIES"
    assert rows["freight"]["owner_adjudicated_target"] == "FREIGHT"
    assert rows["mixed"]["owner_adjudicated_target"] is None
    assert rows["mixed"]["split_ref"] == "excluded"
    restart(h)
    view = open_cases(h, monkeypatch)
    choose(h, view, "ot_case_review")
    assert "Frozen, unscored" in text(h, view)
    assert owner.review() == pack
    with sqlite3.connect(h.path.parent / "intake.sqlite3") as db:
        assert db.execute("SELECT workflow_ref,run_ref FROM intake_runs").fetchall() == [
            (draft["workflow_ref"], draft["run_ref"])
        ]
    assert effects(h) == (0,) * 7


def _authorize_p1_evaluation(h, owner, draft, confirmed, pack):
    from opentour_cell.pilot.starter_evaluation_authority import (
        EXECUTION,
        INSPECTION,
        SELECTION,
        _context,
    )
    from opentour_cell.pilot.starter_evaluation_session import RUNTIME
    from opentour_cell.pilot.starter_map_comparator import initialize_journal
    from opentour_cell.pilot.starter_scoring import EvaluationPermission

    directory = h.path.parent
    model_file = directory / "generated-p1-model.json"
    model_revision = write_p1_cpu_fixture(model_file)
    key_file = directory / "generated-p1-receipt-key"
    key_file.touch(mode=0o600, exist_ok=False)
    key_file.write_bytes(b"a" * 32)
    initialize_journal(directory)
    _write_private(
        directory / "starter-evaluation-runtime.json",
        {
            "schema_version": RUNTIME,
            "models": {
                "selected-model": {
                    "artifact_file": model_file.name,
                    "receipt_key_file": key_file.name,
                }
            },
        },
    )
    (authority_path,) = directory.glob("starter-case-authority-*.json")
    record = json.loads(authority_path.read_bytes())
    capability = record["envelope"]["grant"]
    plan = FrozenPlan(**capability["frozen_plan"])
    frozen = pack["frozen"]
    expires = int(time.time()) + 600
    permission = EvaluationPermission(
        owner.actor.tenant,
        draft["workflow_ref"],
        capability["engagement_epoch"],
        draft["run_ref"],
        plan.evaluator_ref,
        "generated-p1-evaluation",
        "generated-p1-execution-grant",
        frozen["reference"],
        frozen["revision"],
        digest(asdict(plan)),
        _context(confirmed, plan).comparator_revision,
        "selected-model",
        model_revision,
        "generated-p1-model-grant",
        ("category", "po"),
        4,
        expires,
    )
    for evaluating in (
        permission,
        replace(
            permission,
            execution_ref="generated-p1-rerun",
            revision="generated-p1-rerun-grant",
        ),
    ):
        execution = {
            "schema_version": EXECUTION,
            "grant_ref": evaluating.revision,
            "tenant": evaluating.tenant,
            "custodian_ref": capability["custodian_ref"],
            "case_authority_ref": authority_path.stem,
            "case_envelope_digest": digest(record["envelope"]),
            "permission": asdict(evaluating),
            "expires_at": expires,
        }
        seal(directory, evaluating.execution_ref + ".json", execution)
        if evaluating == permission:
            seal(
                directory,
                "generated-p1-inspection.json",
                execution
                | {
                    "schema_version": INSPECTION,
                    "grant_ref": "generated-p1-inspection-grant",
                    "viewer_actor": ACTOR,
                    "owner_ref": capability["owner_ref"],
                },
            )
    _write_private(
        directory / "starter-evaluation-selection.json",
        {
            "schema_version": SELECTION,
            "executions": {
                permission.execution_ref: permission.execution_ref + ".json",
                "generated-p1-rerun": "generated-p1-rerun.json",
            },
            "inspections": {permission.execution_ref: "generated-p1-inspection.json"},
        },
    )
    registration = {
        "schema_version": model_registry.VERSION,
        "tenant": permission.tenant,
        "models": [
            {
                "model_ref": permission.model_ref,
                "immutable_revision": model_revision,
                "task": "classification",
                "input_schema": "starter-structured-fields-v1",
                "output_schema": "action-output-v1",
                "preprocessing_revision": text_classifier.PROFILE,
                "route": model_connector.Route.LOCAL.value,
                "account_or_host": "generated-host",
                "region": "local",
                "credential_ref": "generated-local-registration",
            }
        ],
        "grants": [
            {
                "actor": permission.evaluator,
                "model_ref": permission.model_ref,
                "immutable_revision": model_revision,
                "purpose": model_connector.Purpose.BASELINE.value,
                "revision": permission.baseline_grant_revision,
                "expires_at": expires,
                "retention_until": expires + 60,
                "permitted_fields": list(permission.input_fields),
                "fit_bound": None,
                "benchmark_revisions": [],
            }
        ],
    }
    return permission, registration


def _invoke_worker(selector):
    from opentour_cell.pilot import starter_evaluation_worker

    kit = next(
        (
            path
            for path in Path(starter_evaluation_worker.__file__).parents
            if path.suffix == ".pyz"
        ),
        None,
    )
    command = (
        [str(kit), "starter-evaluation-worker"]
        if kit is not None
        else ["-m", "opentour_cell.pilot.starter_evaluation_worker"]
    )
    result = subprocess.run(
        [sys.executable, *command, "--invocation", str(selector)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert not result.stderr
    output = json.loads(result.stdout)
    assert output["schema_version"] == starter_evaluation_worker.RESULT
    assert set(output).isdisjoint({"rows", "metrics", "predictions", "inputs", "targets"})
    return result.returncode, output


def _stored_score(directory, execution):
    journal = directory / "starter-evaluations.sqlite3"
    with sqlite3.connect(journal.as_uri() + "?mode=ro", uri=True) as db:
        row = db.execute("SELECT result FROM evaluations WHERE ref=?", (execution,)).fetchone()
    assert row and row[0]
    return json.loads(row[0])


def score_p1_from_frozen(journey, monkeypatch):
    from opentour_cell.pilot.starter_evaluation_session import StarterEvaluationSession
    from test_starter_evaluation_worker import provision_invocation

    h, owner, draft, _maps, confirmed, pack = journey
    directory = h.path.parent
    permission, registration = _authorize_p1_evaluation(h, owner, draft, confirmed, pack)
    viewer = StarterEvaluationSession(owner.settings, owner.actor)
    selector, *_ = provision_invocation(
        h.path,
        permission,
        invocation_ref="generated-p1-worker",
        grant_file="generated-p1-evaluation.json",
    )
    assert viewer.summary(permission.execution_ref)["state"] == "frozen_unscored"
    code, unscored = _invoke_worker(selector)
    assert code == 2 and unscored["state"] == "frozen_unscored", unscored
    assert not (directory / "starter-evaluations.sqlite3").exists()
    _write_private(directory / "model-registration.json", registration)
    code, completed = _invoke_worker(selector)
    assert code == 0 and completed["state"] == "scored", completed
    assert completed["workflow_ref"] == draft["workflow_ref"]
    assert completed["run_ref"] == draft["run_ref"]
    result = _stored_score(directory, permission.execution_ref)
    assert result["state"] == "scored", result
    assert result["freeze_revision"] == pack["frozen"]["revision"]
    rows = {row["case_id"]: row for row in result["rows"]}
    oracle = {
        "subscription": ("SOFTWARE", "SOFTWARE", "SOFTWARE"),
        "freight": ("FREIGHT", "SUPPLIES", "FREIGHT"),
        "supplies": ("SUPPLIES", "SUPPLIES", "SUPPLIES"),
        "no-po": ("REVIEW", "REVIEW", "REVIEW"),
    }
    assert set(rows) == set(oracle)

    def label(prediction):
        return "REVIEW" if prediction["status"] == "review" else prediction["label"]

    for key, row in rows.items():
        assert row["origin"] == "generated_starter"
        assert (row["expected"]["target"], label(row["model"]), label(row["comparator"])) == oracle[
            key
        ]
    for arm, column in (("model", 1), ("comparator", 2)):
        automatic = [row for row in oracle.values() if row[column] != "REVIEW"]
        expected_metrics = {
            "exact_action": (sum(row[0] == row[column] for row in oracle.values()), 4),
            "coverage": (len(automatic), 4),
            "wrong_automatic": (sum(row[0] != row[column] for row in automatic), len(automatic)),
        }
        for key, (n, d) in expected_metrics.items():
            assert result["metrics"][arm][key] == {"numerator": n, "denominator": d, "value": n / d}
        assert result["metrics"][arm]["missing"] == 0
    assert result["metrics"]["paired"] == {
        "fixed": 1,
        "regressed": 0,
        "denominator": 4,
        "excluded_missing": 0,
    }
    assert result["unresolved"] == result["excluded"] == 1
    assert result["result_digest"] == digest(
        {key: value for key, value in result.items() if key not in {"result_digest", "result_ref"}}
    )
    expected_inputs = {
        "starter-case-" + digest([permission.freeze_revision, row["case_id"]])[:40]: dict(
            row["decision_inputs"]
        )
        for row in pack["cases"]
        if row["split_ref"] == "reserved"
    }
    assert {receipt["request"]["input_digest"] for receipt in result["predictions"].values()} == {
        digest(expected_inputs)
    }
    restart(h)
    assert _invoke_worker(selector) == (code, completed)
    assert _stored_score(directory, permission.execution_ref) == result
    next_permission = replace(
        permission,
        execution_ref="generated-p1-rerun",
        revision="generated-p1-rerun-grant",
    )
    next_selector, *_ = provision_invocation(
        h.path,
        next_permission,
        invocation_ref="generated-p1-worker-rerun",
        grant_file="generated-p1-rerun.json",
    )
    next_code, next_completed = _invoke_worker(next_selector)
    assert next_code == 0 and next_completed["state"] == "scored", next_completed
    rerun = _stored_score(directory, next_permission.execution_ref)
    assert rerun["execution_ref"] != result["execution_ref"]
    assert rerun["freeze_revision"] == result["freeze_revision"]
    assert rerun["metrics"] == result["metrics"]
    assert _invoke_worker(next_selector) == (next_code, next_completed)
    with sqlite3.connect(directory / "starter-evaluations.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM evaluations").fetchone() == (2,)
        assert db.execute("SELECT count(*) FROM predictions").fetchone() == (4,)
    for filename in ("starter-local-predictions.sqlite3", "starter-comparator-receipts.sqlite3"):
        with sqlite3.connect(directory / filename) as db:
            assert db.execute("SELECT count(*) FROM receipts").fetchone() == (2,)

    def forbidden(*_args, **_kwargs):
        pytest.fail("An owner inspection attempted to execute an evaluation.")

    with monkeypatch.context() as view_check:
        view_check.setattr(StarterEvaluationSession, "run", forbidden)
        home = publish(h, monkeypatch)
        assert "ot_app_starter_score" in buttons(home)
        click(h, home, "ot_app_starter_score")
        view = list(h.slack.screens)[-1]
        assert screen(h, view)["callback_id"] == "ot_starter_score"
        document = text(h, view)
        assert "Expert-reviewed starter benchmark · generated examples" in document
        for fragment in ("3/4", "4/4", "1/3", "0/3", "Fixed 1", "Regressed 0"):
            assert fragment in document
        assert "carrier_freight" not in document and "po-b" not in document
        assert "ot_app_starter_score" not in buttons(publish(h, monkeypatch, OTHER))
    assert _stored_score(directory, permission.execution_ref) == result
    assert owner.review() == pack
    assert effects(h) == (0,) * 7
    return permission, registration, result


def test_generated_p1_worker_scores_same_frozen_families_and_private_home(
    p1_frozen_from_home, monkeypatch
):
    score_p1_from_frozen(p1_frozen_from_home, monkeypatch)
