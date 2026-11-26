"""Guardrail tests use generated data and never report simulated fit as qualification."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
import qualify_open_weight as qualification
from opentour_cell.pilot import open_weight as model

Json = dict[str, Any]


@pytest.fixture
def owner(tmp_path: Path) -> Json:
    qualification.prepare(tmp_path)
    return {
        "bundle": tmp_path / "fitted",
        "model_dir": tmp_path / "unavailable",
        "grant": qualification.read(tmp_path / "grant.json"),
        "residual": qualification.read(tmp_path / "residual.json"),
        "training_rows": qualification.read(tmp_path / "training.json"),
        "protected_fingerprints": qualification.read(tmp_path / "protected-keys.json"),
    }


@pytest.mark.parametrize(
    ("key", "value", "reason"),
    [
        ("profile", "other", "unsupported_open_weight_profile"),
        ("compute_authorized", False, "model_compute_data_and_destination_approval_required"),
        ("training_data_authorized", False, "model_compute_data_and_destination_approval_required"),
        ("model_use_authorized", False, "model_compute_data_and_destination_approval_required"),
        ("destination_authorized", False, "model_compute_data_and_destination_approval_required"),
        ("rights_attestation", "", "model_and_data_rights_attestation_required"),
        ("expires_at", 10, "current_open_weight_permission_required"),
        ("max_seconds", 301, "bounded_zero_cost_local_compute_required"),
        ("hourly_cost_cents", 1, "bounded_zero_cost_local_compute_required"),
        ("max_cost_cents", -1, "bounded_zero_cost_local_compute_required"),
        ("payer", "", "named_scope_trainer_payer_and_destination_required"),
        ("features", ["reference"], "decision_time_feature_required"),
        ("labels", ["billing"], "unsupported_feature_or_label_schema"),
    ],
)
def test_scoped_grant_precedes_compute(owner: Json, key: str, value: Any, reason: str) -> None:
    owner["grant"][key] = value
    with pytest.raises(model.OpenWeightError, match=f"^{reason}$"):
        model.fit_open_weight(**owner)
    assert not owner["bundle"].exists()


def test_residual_comparator_must_be_genuine_and_current(owner: Json) -> None:
    for key, value in (
        ("route", "simple"),
        ("compute_authorized", False),
        ("residual_count", 0),
        ("revision", "0" * 64),
    ):
        invalid = copy.deepcopy(owner)
        invalid["residual"][key] = value
        with pytest.raises(
            model.OpenWeightError, match="approved_residual_comparator_branch_required"
        ):
            model.fit_open_weight(**invalid)
    owner["residual"]["reason"] = "altered-with-stale-signed-digest"
    with pytest.raises(model.OpenWeightError, match="unverified_development_decision"):
        model.fit_open_weight(**owner)


@pytest.mark.parametrize("field", ["case_id", "family_id", "features"])
def test_all_protected_membership_keys_block_fit(owner: Json, field: str) -> None:
    row = {
        "case_id": "generated-outside",
        "family_id": "family-outside",
        "features": {"question": "new case"},
    }
    row[field] = owner["training_rows"][0][field]
    owner["protected_fingerprints"] = model.partition_fingerprints([row])
    with pytest.raises(model.OpenWeightError, match="training_protected_partition_overlap"):
        model.fit_open_weight(**owner)


def test_unapproved_cases_labels_and_empty_protected_set_hold(owner: Json) -> None:
    original = copy.deepcopy(owner)
    owner["training_rows"][0]["case_id"] = "different-case"
    with pytest.raises(model.OpenWeightError, match="outside_approved_development"):
        model.fit_open_weight(**owner)
    owner = copy.deepcopy(original)
    owner["training_rows"][0]["label"] = "third-answer"
    with pytest.raises(model.OpenWeightError, match="invalid_training_rows_or_labels"):
        model.fit_open_weight(**owner)
    original["protected_fingerprints"] = []
    with pytest.raises(model.OpenWeightError, match="sealed_protected_partition_required"):
        model.fit_open_weight(**original)


def test_protected_answers_never_enter_numeric_request(
    owner: Json, monkeypatch: pytest.MonkeyPatch
) -> None:
    def stop(
        _bundle: Any, _base: Any, _grant: Any, _python: Any, request: Json, **_checks: Any
    ) -> Json:
        assert set(request) == {"operation", "examples", "targets"}
        encoded = json.dumps(request)
        assert all(text not in encoded for text in qualification.PROTECTED_INPUTS)
        assert not any(key in encoded for key in ("family_id", "case_id", "reference"))
        assert len(request["targets"]) == len(qualification.TRAIN)
        raise model.OpenWeightError("controlled_stop_before_fit")

    monkeypatch.setattr(model, "_run", stop)
    with pytest.raises(model.OpenWeightError, match="controlled_stop_before_fit"):
        model.fit_open_weight(**owner)
    assert not owner["bundle"].exists()
    assert not list(owner["bundle"].parent.glob(".fitted.pending-*"))


def test_missing_model_is_a_setup_hold_and_withdrawal_is_durable(owner: Json) -> None:
    with pytest.raises(model.OpenWeightError, match="staged_model_file_required"):
        model.fit_open_weight(**owner)
    model.withdraw_open_weight(owner["bundle"], grant=owner["grant"])
    model.withdraw_open_weight(owner["bundle"], grant=owner["grant"])
    with pytest.raises(model.OpenWeightError, match="open_weight_authority_withdrawn"):
        model.fit_open_weight(**owner)
    with pytest.raises(model.OpenWeightError, match="open_weight_grant_mismatch"):
        model.withdraw_open_weight(
            owner["bundle"], grant={**owner["grant"], "trainer": "different"}
        )
    assert not owner["bundle"].exists()


def test_live_source_revocation_terminates_supervised_child_without_publishing(
    owner: Json,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """This subprocess exercises the supervisor. The real-model qualification repeats the case."""
    import os
    import sys

    destination = owner["bundle"].parent
    signal = destination / "active-worker"
    probe = destination / "block.py"
    probe.write_text(
        "import os, pathlib, time\n"
        f"pathlib.Path({str(signal)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(20)\n"
    )
    profile = destination / "local-profile.json"
    profile.write_text('{"files":{}}')
    monkeypatch.setattr(model, "_resources", lambda: (probe, profile))

    def source_permission() -> None:
        if signal.is_file():
            raise model.OpenWeightError("source_permission_withdrawn")

    with pytest.raises(model.OpenWeightError, match="^source_permission_withdrawn$"):
        model.fit_open_weight(**owner, python=sys.executable, check_permission=source_permission)
    with pytest.raises(ProcessLookupError):
        os.kill(int(signal.read_text()), 0)
    assert not owner["bundle"].exists()
    assert not list(destination.glob(".fitted.pending-*"))
