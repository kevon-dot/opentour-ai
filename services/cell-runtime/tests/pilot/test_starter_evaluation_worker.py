"""Generated installed authentication through real process entry and CPU scoring."""

import hashlib
import hmac
import json
import sqlite3
import subprocess
import sys
from dataclasses import asdict, replace

import pytest
from opentour_cell.pilot import starter_evaluation_authority as authority_module
from opentour_cell.pilot import text_classifier as cpu
from opentour_cell.pilot.intake_mcp import IntakeError
from opentour_cell.pilot.operating_point import canonical, digest
from opentour_cell.pilot.starter_evaluation_session import StarterEvaluationSession
from opentour_cell.pilot.starter_evaluation_worker import (
    CURRENT,
    INVOCATION,
    RESULT,
    SELECTOR,
    _Invocation,
    execute,
    main,
)
from opentour_cell.pilot.starter_local_baseline import LocalStarterBaseline
from test_intake_mcp import _write_private
from test_starter_evaluation_authority import files, seal

pytest_plugins = ("test_starter_evaluation_session",)


def provision(
    installed, *, invocation_ref="invocation-a", permission=None, grant_file="execution-a.json"
):
    """Generated custodian provisioning, separate from shipped worker code."""
    directory, _, _, p, _, _, authorized = installed
    p = permission or p
    cfg = authorized[0].path
    return provision_invocation(cfg, p, invocation_ref=invocation_ref, grant_file=grant_file)


def provision_invocation(settings_path, permission, *, invocation_ref, grant_file):
    """Test-only custodian; no caller role is derived from an execution subject."""
    directory, cfg, p = settings_path.parent, settings_path, permission
    key = bytes.fromhex(json.loads((directory / "starter-intake-key.json").read_text())["key"])
    grant = json.loads((directory / grant_file).read_text())
    cap = dict(
        schema_version=INVOCATION,
        invocation_ref=invocation_ref,
        custodian_ref=grant["grant"]["custodian_ref"],
        tenant=p.tenant,
        evaluator=p.evaluator,
        epoch=p.epoch,
        workflow_ref=p.workflow_ref,
        run_ref=p.run_ref,
        freeze_ref=p.freeze_ref,
        freeze_revision=p.freeze_revision,
        execution_ref=p.execution_ref,
        execution_grant_file=grant_file,
        execution_grant_ref=p.revision,
        execution_grant_digest=digest(grant),
        settings_file=cfg.name,
        settings_sha256=hashlib.sha256(cfg.read_bytes()).hexdigest(),
        expires_at=p.expires_at,
    )
    selector = directory / f"{invocation_ref}-selector.json"
    write_invocation(selector, cap, key)
    return selector, cap, key


def write_invocation(selector, cap, key):
    signing_key = hmac.digest(key, INVOCATION.encode(), "sha256")
    envelope = dict(
        invocation=cap,
        seal_sha256=hmac.digest(signing_key, canonical(cap).encode(), "sha256").hex(),
    )
    filename = f"{cap['invocation_ref']}.json"
    _write_private(selector.parent / filename, envelope)
    _write_private(selector, dict(schema_version=SELECTOR, invocation_file=filename))
    _write_private(
        selector.parent / "starter-worker-current.json",
        dict(schema_version=CURRENT, invocations={cap["invocation_ref"]: digest(envelope)}),
    )


def process(selector, *extra):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "opentour_cell.pilot.starter_evaluation_worker",
            "--invocation",
            str(selector),
            *extra,
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.stderr == "", result.stderr
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == RESULT
    assert len(result.stdout) < 2048
    assert (
        not {"rows", "metrics", "predictions", "inputs", "targets", "result_digest"}
        & payload.keys()
    )
    return result.returncode, payload


def test_actual_process_restart_and_separate_authorized_invocation(installed):
    selector, cap, key = provision(installed)
    directory, _, reader, p, _, _, authorized = installed
    first_code, first = process(selector)
    assert first_code == 0 and first["state"] == "scored", first
    assert process(selector) == (first_code, first)
    with sqlite3.connect(directory / "starter-evaluations.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM evaluations").fetchone()[0] == 1
    for filename in ("starter-local-predictions.sqlite3", "starter-comparator-receipts.sqlite3"):
        with sqlite3.connect(directory / filename) as db:
            assert db.execute("SELECT count(*) FROM receipts").fetchone()[0] == 1
    assert (
        reader.summary(p.execution_ref)["metrics"]["comparator"]["exact_action"]["numerator"] == 5
    )
    next_p = replace(p, execution_ref="execution-b", revision="evaluation-b")
    grant = authorized[6] | dict(grant_ref=next_p.revision, permission=asdict(next_p))
    seal(directory, "execution-b.json", grant)
    selection_path = directory / "starter-evaluation-selection.json"
    selection = json.loads(selection_path.read_text())
    selection["executions"][next_p.execution_ref] = "execution-b.json"
    _write_private(selection_path, selection)
    second_selector, _, _ = provision(
        installed, invocation_ref="invocation-b", permission=next_p, grant_file="execution-b.json"
    )
    code, second = process(second_selector)
    assert code == 0 and second["state"] == "scored"
    assert (
        second["freeze_ref"] == first["freeze_ref"]
        and second["execution_ref"] != first["execution_ref"]
    )
    assert process(second_selector) == (code, second)
    assert process(selector)[0] == 2  # Current pointer now authorizes only B.
    with sqlite3.connect(directory / "starter-evaluations.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM evaluations").fetchone()[0] == 2
    for filename in ("starter-local-predictions.sqlite3", "starter-comparator-receipts.sqlite3"):
        with sqlite3.connect(directory / filename) as db:
            assert db.execute("SELECT count(*) FROM receipts").fetchone()[0] == 2
    assert authorized[-2].read_bytes() == authorized[-1]


@pytest.mark.parametrize(
    "field",
    [
        "tenant",
        "evaluator",
        "custodian_ref",
        "epoch",
        "workflow_ref",
        "run_ref",
        "execution_ref",
        "execution_grant_ref",
        "freeze_ref",
        "freeze_revision",
    ],
)
def test_authentic_but_mismatched_invocation_denies_before_policy(installed, monkeypatch, field):
    selector, cap, key = provision(installed)
    cap[field] = "0" * 64 if field == "freeze_revision" else "other-identity"
    write_invocation(selector, cap, key)
    before = files(selector.parent)
    monkeypatch.setattr(
        authority_module, "_read_policy", lambda *a: pytest.fail("Mismatched identity read policy")
    )
    monkeypatch.setattr(cpu, "predict", lambda *a: pytest.fail("Mismatched identity predicted"))
    with pytest.raises(IntakeError):
        execute(selector)
    assert files(selector.parent) == before


@pytest.mark.parametrize(
    "changed",
    [
        "tamper",
        "wrong_domain",
        "pointer",
        "withdrawn",
        "expired",
        "settings",
        "execution_grant",
        "execution_withdrawn",
        "key",
        "symlink",
        "permissions",
        "traversal",
    ],
)
def test_changed_authentication_denies_without_effect(installed, monkeypatch, changed):
    selector, cap, key = provision(installed)
    directory = selector.parent
    path = directory / "invocation-a.json"
    if changed in {"tamper", "wrong_domain"}:
        body = json.loads(path.read_text())
        body["seal_sha256"] = (
            "0" * 64
            if changed == "tamper"
            else hmac.digest(key, canonical(cap).encode(), "sha256").hex()
        )
        _write_private(path, body)
    elif changed == "pointer":
        _write_private(
            directory / "starter-worker-current.json",
            dict(schema_version=CURRENT, invocations={"invocation-a": "0" * 64}),
        )
    elif changed == "withdrawn":
        (directory / "starter-worker-withdrawn-invocation-a.json").touch(mode=0o600)
    elif changed == "expired":
        cap["expires_at"] = 1
        write_invocation(selector, cap, key)
    elif changed == "settings":
        cfg_path = directory / cap["settings_file"]
        body = json.loads(cfg_path.read_text()) | dict(workflow_label="changed")
        _write_private(cfg_path, body)
    elif changed == "execution_grant":
        body = json.loads((directory / "execution-a.json").read_text())["grant"]
        body["permission"]["maximum_cases"] += 1
        seal(directory, "execution-a.json", body)
    elif changed == "execution_withdrawn":
        (directory / "starter-evaluation-withdrawn-evaluation-a.json").touch(mode=0o600)
    elif changed == "key":
        _write_private(directory / "starter-intake-key.json", {"key": "1" * 64})
    elif changed == "symlink":
        path.rename(directory / "original.json")
        path.symlink_to(directory / "original.json")
    elif changed == "permissions":
        path.chmod(0o644)
    else:
        _write_private(selector, dict(schema_version=SELECTOR, invocation_file="../outside.json"))
    before = files(directory)
    monkeypatch.setattr(
        authority_module, "_read_policy", lambda *a: pytest.fail("Invalid invocation read policy")
    )
    monkeypatch.setattr(cpu, "predict", lambda *a: pytest.fail("Invalid invocation predicted"))
    with pytest.raises((ValueError, OSError)):
        execute(selector)
    assert files(directory) == before


def test_instantiated_identity_rechecks_before_session_and_pre_dispatch(installed, monkeypatch):
    selector, _, _ = provision(installed)
    invocation = _Invocation(selector)
    original = StarterEvaluationSession._ports
    calls = []
    monkeypatch.setattr(
        cpu, "predict", lambda *a: calls.append(a) or pytest.fail("Withdrawn invocation predicted")
    )

    def withdraw(self, *args):
        ports = original(self, *args)
        (selector.parent / "starter-worker-withdrawn-invocation-a.json").touch(mode=0o600)
        return ports

    monkeypatch.setattr(StarterEvaluationSession, "_ports", withdraw)
    with pytest.raises(IntakeError):
        execute(selector)
    with pytest.raises(IntakeError):
        invocation.settings()
    assert not calls and not (selector.parent / "starter-evaluations.sqlite3").exists()


def test_cli_rejects_authority_arguments_and_does_not_print_diagnostics(installed, capsys):
    selector, _, _ = provision(installed)
    before = files(selector.parent)
    for args in (
        ["--invocation", str(selector), "--actor", "private-sentinel"],
        ["--invocation", str(selector), "--execution-ref", "execution-b"],
        ["--invocation", "relative.json"],
    ):
        assert main(args) == 2
        out = capsys.readouterr()
        assert not out.err and "private-sentinel" not in out.out
        assert set(json.loads(out.out)) == {"schema_version", "state", "reason_code", "next_action"}
    assert files(selector.parent) == before


def test_missing_model_remains_existing_freeze_without_creating_an_execution(installed):
    selector, _, _ = provision(installed)
    (selector.parent / "model-registration.json").unlink()
    before = files(selector.parent)
    code, result = process(selector)
    assert code == 2 and result["state"] == "frozen_unscored"
    assert files(selector.parent) == before


@pytest.mark.parametrize("grant", ["case", "source", "execution"])
def test_authenticated_process_cannot_override_withdrawn_product_permission(installed, grant):
    selector, _, _ = provision(installed)
    filename = {
        "case": "starter-case-withdrawn-case-permission.json",
        "source": "starter-intake-withdrawn-policy-scope.json",
        "execution": "starter-evaluation-withdrawn-evaluation-a.json",
    }[grant]
    (selector.parent / filename).touch(mode=0o600)
    before = files(selector.parent)
    code, result = process(selector)
    assert code == 2 and result["state"] == "held"
    assert "execution_ref" not in result
    assert files(selector.parent) == before


def test_started_unknown_execution_stays_unknown_through_wrapper(installed, monkeypatch):
    selector, _, _ = provision(installed)
    monkeypatch.setattr(LocalStarterBaseline, "execute", lambda *a: None)
    result = execute(selector)
    assert result["state"] == "held"
    assert result["reason_code"] == "execution_requires_reconciliation"
    (selector.parent / "starter-evaluation-runtime.json").unlink()
    before = files(selector.parent)
    assert execute(selector) == result
    assert files(selector.parent) == before


def test_invocation_withdrawn_at_final_metrics_rolls_back_result(installed, monkeypatch):
    from opentour_cell.pilot import starter_scoring

    selector, _, _ = provision(installed)
    original = starter_scoring._metrics

    def withdraw(rows):
        (selector.parent / "starter-worker-withdrawn-invocation-a.json").touch(mode=0o600)
        return original(rows)

    monkeypatch.setattr(starter_scoring, "_metrics", withdraw)
    with pytest.raises(IntakeError):
        execute(selector)
    with sqlite3.connect(selector.parent / "starter-evaluations.sqlite3") as db:
        assert (
            db.execute("SELECT count(*) FROM evaluations WHERE result IS NOT NULL").fetchone()[0]
            == 0
        )


def test_engagement_withdrawal_holds_process_before_scoring(installed):
    selector, _, _ = provision(installed)
    installed[-1][0].store.withdraw()
    before = files(selector.parent)
    code, result = process(selector)
    assert code == 2 and result["state"] == "held"
    assert files(selector.parent) == before


def test_authentication_does_not_replace_independent_execution_signature(installed, monkeypatch):
    selector, cap, key = provision(installed)
    path = selector.parent / cap["execution_grant_file"]
    envelope = json.loads(path.read_text()) | {"seal_sha256": "0" * 64}
    _write_private(path, envelope)
    cap["execution_grant_digest"] = digest(envelope)
    write_invocation(selector, cap, key)
    before = files(selector.parent)
    monkeypatch.setattr(
        authority_module, "_read_policy", lambda *a: pytest.fail("Unsigned execution read policy")
    )
    with pytest.raises(IntakeError):
        execute(selector)
    assert files(selector.parent) == before


def test_authenticated_invocation_without_baseline_permission_stays_unscored(
    installed, monkeypatch
):
    selector, _, _ = provision(installed)
    registry = installed[4]
    registry["grants"] = []
    _write_private(selector.parent / "model-registration.json", registry)
    before = files(selector.parent)
    monkeypatch.setattr(cpu, "predict", lambda *a: pytest.fail("Missing BASELINE grant predicted"))
    assert execute(selector)["state"] == "frozen_unscored"
    assert files(selector.parent) == before
