"""Truthful C03 transport and bounded failure behavior without starting a database."""

import json
import sys
import time
from types import SimpleNamespace

import pytest

from tools import schema_validate
from tools.fixtures import local_harness
from tools.fixtures.evidence import validate_observations
from tools.fixtures.ports import FixtureRefusal
from tools.fixtures.postgres import (
    DisposablePostgres,
    PostgresFixtureFailure,
    PrerequisiteMissing,
)


@pytest.mark.parametrize(
    "failure,expected,status,reason",
    [
        (PrerequisiteMissing, 3, "BLOCKED", "MISSING_EXTERNAL_PREREQUISITE"),
        (PostgresFixtureFailure, 1, "FAIL", "RUNNER_ERROR"),
    ],
)
def test_preflight_failure_has_valid_nonpassing_report(
    tmp_path, monkeypatch, failure, expected, status, reason
):
    destination = tmp_path / "report.json"
    for key, value in {
        "OPENTOUR_RESULT_NONCE": "a" * 32,
        "OPENTOUR_RESULT_FILE": str(destination),
        "OPENTOUR_TEST_TARGET": local_harness.TARGET,
        "OPENTOUR_REPOSITORY_ROOT": str(local_harness.ROOT),
    }.items():
        monkeypatch.setenv(key, value)

    def unavailable(_self):
        raise failure("Generated prerequisite failure")

    monkeypatch.setattr(DisposablePostgres, "qualify_binaries", unavailable)
    assert local_harness.main(["harness-smoke", "--result-file", str(destination)]) == expected
    result = json.loads(destination.read_text())
    assert schema_validate.valid_instance(
        "urn:opentour:test-result:1.0", result, schema_validate.schema_registry()
    )
    assert (result["status"], result["reason_code"], result["collected"]) == (status, reason, 0)
    assert result["cleanup_complete"] is True


@pytest.mark.parametrize("phase", ["setup", "call", "teardown"])
def test_failed_pytest_phase_cannot_be_hidden_by_passing_call(phase, monkeypatch):
    monkeypatch.setenv("OPENTOUR_RESULT_NONCE", "a" * 32)
    node = "tests/integration/test_failure_harness.py::generated_case"
    local_harness._items.clear()
    local_harness._items[node] = {
        "id": node,
        "file": local_harness.TARGET,
        "mode": None,
        "status": "skipped",
    }
    for when in ("setup", "call", "teardown"):
        local_harness.pytest_runtest_logreport(
            SimpleNamespace(
                nodeid=node,
                when=when,
                failed=when == phase,
                skipped=False,
                passed=when != phase,
            )
        )
    report = local_harness.envelope(list(local_harness._items.values()))
    assert report["status"] == "FAIL" and report["failed"] == 1
    assert local_harness.envelope([], cleanup=False)["reason_code"] == "CLEANUP_FAILED"
    assert local_harness.envelope([])["reason_code"] == "NO_TESTS"


@pytest.mark.parametrize(
    "program", ["import time; time.sleep(5)", "import os; os.write(1,b'x'*2000000)"]
)
def test_postgres_helper_timeout_and_output_are_bounded(program):
    database = DisposablePostgres()
    started = time.monotonic()
    with pytest.raises(PostgresFixtureFailure):
        database._command([sys.executable, "-c", program], timeout=1)
    assert time.monotonic() - started < 4
    assert database.receipt["helper_aborted"] is True
    with pytest.raises(PostgresFixtureFailure, match="retained"):
        database.close()
    assert not database.cleanup_complete


def test_observation_shape_refuses_arbitrary_fields_or_empirical_upgrade():
    from tools.fixtures.evidence import observation

    record = observation("generated", "1", 124, "customer_controlled", "simulated", [], [])
    document = {
        "issue_key": "OPE-124",
        "fixture_version": "1",
        "product_acceptance": "NOT_RUN",
        "observations": [record],
    }
    validate_observations(document)
    record["kind"] = "provider_integration"
    with pytest.raises(FixtureRefusal, match="INVALID_LOCAL_OBSERVATIONS"):
        validate_observations(document)
    record["kind"] = "simulated"
    record["private_path"] = "generated-adversarial-extra-field"
    with pytest.raises(FixtureRefusal, match="INVALID_LOCAL_OBSERVATIONS"):
        validate_observations(document)
