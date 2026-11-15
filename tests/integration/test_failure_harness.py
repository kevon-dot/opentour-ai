"""Actual local database/persistence plus separately classified simulated provider checks."""

from pathlib import Path

import pytest

from tools.fixtures.postgres_checks import tenant_checks, transaction_checks
from tools.fixtures.postgres_lifecycle import abnormal_shutdown_check
from tools.fixtures.registry import FAULTS, MODES, load_registry
from tools.fixtures.scenarios import run_fixture

REGISTRY = Path(__file__).resolve().parents[2] / "tools/fixtures/registry.json"


def test_abnormal_postgres_shutdown_retains_live_cluster(record_harness_observation):
    record_harness_observation(abnormal_shutdown_check())


@pytest.mark.parametrize("mode", MODES)
def test_actual_postgres_runtime_role_and_transactions(
    mode, disposable_postgres, record_harness_observation
):
    record_harness_observation(tenant_checks(disposable_postgres, mode))
    record_harness_observation(transaction_checks(disposable_postgres, mode))


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("fault", FAULTS)
def test_named_failure_fixture(mode, fault, tmp_path, record_harness_observation):
    fixture = next(
        f
        for f in load_registry(REGISTRY)
        if f.scope.mode == mode and f.provider_behavior_script == (fault,)
    )
    observed = run_fixture(fixture, tmp_path)
    assert observed["observed_effects"] == list(fixture.expected_effects)
    assert observed["observed_ledger_entries"] == list(fixture.expected_ledger_entries)
    record_harness_observation(observed)
