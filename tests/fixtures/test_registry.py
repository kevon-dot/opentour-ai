"""Registry contracts and deterministic generated replay; no provider acceptance."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from tools.fixtures.registry import FAULTS, MODES, Clock, digest, load_registry
from tools.fixtures.scenarios import run_fixture

REGISTRY = Path(__file__).resolve().parents[2] / "tools/fixtures/registry.json"


def test_registry_covers_every_named_fault_in_both_modes():
    fixtures = load_registry(REGISTRY)
    assert len(fixtures) == 2 * len(FAULTS)
    assert {(f.scope.mode, f.provider_behavior_script[0]) for f in fixtures} == {
        (mode, fault) for mode in MODES for fault in FAULTS
    }


@pytest.mark.parametrize(
    "fixture", load_registry(REGISTRY), ids=lambda f: f.fixture_id + f.scope.mode
)
def test_replay_has_identical_logical_events_and_expected_outcomes(fixture, tmp_path):
    observations = []
    for suffix in ("first", "replayed"):
        directory = tmp_path / suffix
        directory.mkdir()
        observed = run_fixture(fixture, directory)
        assert observed["observed_effects"] == list(fixture.expected_effects)
        assert observed["observed_ledger_entries"] == list(fixture.expected_ledger_entries)
        assert observed["product_acceptance"] == "NOT_RUN"
        observations.append(observed)
    assert digest(observations[0]) == digest(observations[1])


@pytest.mark.parametrize(
    "mutation",
    [
        lambda x: x.update(undeclared=True),
        lambda x: x.update(seed=True),
        lambda x: x.update(seed=-1),
        lambda x: x.update(seed=9007199254740992),
        lambda x: x.update(mode="unknown"),
        lambda x: x.update(version="2"),
        lambda x: x.update(clock="2026-01-01T00:00:00+01:00"),
        lambda x: x.update(organization_id="real-customer"),
        lambda x: x.update(provider_behavior_script=["run_arbitrary_shell"]),
    ],
)
def test_registry_rejects_malformed_or_undeclared_configuration(tmp_path, mutation):
    fixture = json.loads(REGISTRY.read_text())[0]
    mutation(fixture)
    path = tmp_path / "registry.json"
    path.write_text(json.dumps([fixture]))
    with pytest.raises(ValueError):
        load_registry(path)


def test_duplicate_version_rejected_and_seeded_order_reproducible(tmp_path):
    fixture = json.loads(REGISTRY.read_text())[0]
    path = tmp_path / "registry.json"
    path.write_text(json.dumps([fixture, fixture]))
    with pytest.raises(ValueError, match="Duplicate"):
        load_registry(path)
    parsed = load_registry(REGISTRY)[0]
    values = [str(x) for x in range(20)]
    assert parsed.order(values) == parsed.order(values)
    assert parsed.order(values) != replace(parsed, seed=125).order(values)


def test_clock_is_injected_and_expiry_boundary_exact():
    clock = Clock.from_wire("2026-01-01T00:00:00.000Z")
    clock.advance(120)
    assert clock.now() == "2026-01-01T00:02:00.000Z"
    with pytest.raises(ValueError):
        clock.advance(-1)
