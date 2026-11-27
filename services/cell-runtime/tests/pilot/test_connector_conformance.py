"""The shared suite passes for each generated family and flags a connector that breaks a rule."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
import toy_families as toy
from opentour_cell.pilot import connector_conformance as cc
from opentour_cell.pilot import model_connector as mc
from opentour_cell.pilot.model_family import Check, FixtureKind, Role

FAMILIES = [
    ("generated-toy-linear", "reference"),
    ("generated-toy-normmlp", "candidate"),
]


@pytest.mark.parametrize(("architecture", "role"), FAMILIES)
def test_every_shared_obligation_passes_for_each_family(
    tmp_path: Path, architecture: str, role: str
) -> None:
    family = toy.Family(tmp_path, architecture, role=role)
    results = cc.run_conformance(family.harness(), tmp_path / "conformance")
    assert {r.name for r in results} == {
        "duplicate_dispatch_single_execution",
        "lost_acknowledgement_recovers_same_job",
        "restart_replays_receipt_and_candidate",
        "unverifiable_lookup_is_unknown_and_not_retried",
        "cap_exceeded_refused_before_dispatch",
        "revision_mismatch_needs_configuration_and_runs_nothing",
        "cancel_yields_no_candidate",
        "fence_blocks_new_effects",
        "receipt_for_other_intent_rejected",
        "idempotency_key_reuse_with_different_intent_refused",
    }
    assert cc.all_passed(results), [r for r in results if r.outcome is not cc.Outcome.PASSED]


def test_a_connector_that_executes_every_duplicate_is_flagged(tmp_path: Path) -> None:
    """Adverse control: the suite must be able to fail."""

    class Reexecuting(toy.OpenWeightConnector):
        def _key(self, intent: mc.FitIntent) -> str:
            self.counter = getattr(self, "counter", 0) + 1
            return super()._key(
                replace(intent, idempotency_key=f"{intent.idempotency_key}-{self.counter}")
            )

    family = toy.Family(tmp_path, "generated-toy-linear")
    harness = family.harness()

    def make(state: Path) -> mc.Connector:
        honest = family.connector(state)
        return Reexecuting(honest.runner, state, allow_fixture_profiles=True)

    results = cc.run_conformance(replace(harness, make_connector=make), tmp_path / "conformance")
    by_name = {r.name: r for r in results}
    assert by_name["duplicate_dispatch_single_execution"].outcome is cc.Outcome.FAILED
    assert not cc.all_passed(results)


def test_unexercisable_checks_are_not_exercised_and_never_pass(tmp_path: Path) -> None:
    family = toy.Family(tmp_path, "generated-toy-linear")
    harness = replace(family.harness(), make_slow_connector=None)
    results = cc.run_conformance(harness, tmp_path / "conformance")
    cancel = next(r for r in results if r.name == "cancel_yields_no_candidate")
    assert cancel.outcome is cc.Outcome.NOT_EXERCISED
    assert not cc.all_passed(results)
    assert not cc.all_passed(())


@pytest.mark.parametrize(("architecture", "role"), FAMILIES)
def test_each_family_has_its_own_separate_qualification_record(
    tmp_path: Path, architecture: str, role: str
) -> None:
    family = toy.Family(tmp_path, architecture, role=role)
    record = cc.qualify_family(family.scenario(), tmp_path / "qualification")
    assert record.family_id == family.profile.family_id
    assert record.passed, record.results
    expected = {check.value for check in Check} - (
        set() if Role(role) is Role.REFERENCE else {Check.REFERENCE_REPRODUCED.value}
    )
    assert {name for name, _ in record.results} == expected
    # A generated fixture is never promoted to installed-model or customer evidence.
    assert record.fixture_kind is FixtureKind.GENERATED
    assert not record.qualified
    detail = dict(record.details)
    assert detail["scorer"] == "exact-label-match-v1"
    assert float(detail["tuned_score"]) >= 0 and int(detail["parameters_changed"]) > 0


def test_a_failed_qualification_check_is_reported_not_hidden(tmp_path: Path) -> None:
    """Adverse control: a connector that wrongly trains an incompatible model fails the record."""
    family = toy.Family(tmp_path, "generated-toy-normmlp")
    scenario = replace(
        family.scenario(),
        make_incompatible_connector=lambda state: family.connector(state),  # compatible!
    )
    record = cc.qualify_family(scenario, tmp_path / "qualification")
    results = dict(record.results)
    assert results[Check.INCOMPATIBLE_MODEL_ZERO_FIT.value] is False
    assert not record.passed
