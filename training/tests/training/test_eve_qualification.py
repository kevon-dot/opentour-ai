"""Generated sentinel descriptors only. These tests prove software behaviour of the qualification
record and admission gate; they establish no GPU, provider, Pi School or customer evidence.
The only "witness" below is a simulated verifier double and is never a real attestation."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path

import pytest
from opentour_training.training.eve_qualification import (
    REQUIRED_CAPABILITIES,
    AdmissionRefused,
    BackendDescriptor,
    CheckpointDescriptor,
    QualificationInputError,
    QualificationRegistry,
    QualificationRequest,
    ResourceEstimate,
    Witness,
    admit_fit,
    digest,
    qualify,
    serialization_dry_run,
    verify_record,
)

TRUSTED = frozenset({"simulated:verifier"})


def dg(label: str) -> str:
    return "sha256:" + hashlib.sha256(f"generated-{label}".encode()).hexdigest()


class SimulatedVerifier:
    """Test double. A real verifier would be an external provider or customer attestation."""

    def __init__(self, tier: str = "installed-provider", verifier_id: str = "simulated:verifier"):
        self.tier, self.verifier_id = tier, verifier_id

    def verify(self, checkpoint_digest: str, backend_digest: str) -> Witness | None:
        return Witness(self.verifier_id, "simulated:witness-ref", self.tier, 2_000_000_000)


def checkpoint(**changes: object) -> CheckpointDescriptor:
    base = CheckpointDescriptor(
        model_ref="generated-model/eve-sentinel",
        immutable_revision=hashlib.sha1(b"generated-revision").hexdigest(),
        architecture="generated-causal-lm",
        tokenizer_digest=dg("tokenizer"),
        chat_template_digest=dg("template"),
        input_schema_digest=dg("input"),
        output_schema_digest=dg("output"),
        formatter_id="generated-completion-only-v1",
        weight_file_digests=(dg("w1"), dg("w2")),
    )
    return dataclasses.replace(base, **changes)  # type: ignore[arg-type]


def backend(**changes: object) -> BackendDescriptor:
    base = BackendDescriptor(
        backend_id="generated-trainer",
        version="1.0.0",
        route="customer_trainer",
        dependency_revisions={"generated-lib": "1.2.3"},
        capabilities=frozenset(REQUIRED_CAPABILITIES),
        hard_cost_cap=True,
        host_ref="simulated:host-a.invalid",
    )
    return dataclasses.replace(base, **changes)  # type: ignore[arg-type]


FULL_AUTHORITY = {
    "model_rights": "simulated:ref-model",
    "data_rights": "simulated:ref-data",
    "execution_environment": "simulated:ref-env",
    "egress": "simulated:ref-egress",
    "payer": "simulated:ref-payer",
    "cost_cap": "simulated:ref-cap",
    "recipient": "simulated:ref-recipient",
    "retention": "simulated:ref-retention",
    "training_decision_owner": "simulated:ref-owner-a",
    "release_decision_owner": "simulated:ref-owner-b",
}
ROWS = ({"prompt": "generated question one", "completion": "generated answer one"},)


def request(**changes: object) -> QualificationRequest:
    base = QualificationRequest(
        checkpoint=checkpoint(),
        backend=backend(),
        scope="customer",
        authority=FULL_AUTHORITY,
        estimate=ResourceEstimate(80, 3600, 1000, "SIM"),
        development_rows=ROWS,
    )
    return dataclasses.replace(base, **changes)  # type: ignore[arg-type]


def registry(tmp_path: Path) -> QualificationRegistry:
    return QualificationRegistry(tmp_path / "journal.jsonl")


def qualified(tmp_path: Path, **changes: object) -> tuple[QualificationRegistry, str]:
    reg = registry(tmp_path)
    record = qualify(request(**changes), SimulatedVerifier())
    assert record["body"]["state"] == "qualified"
    reg.register(record)
    return reg, record["record_digest"]


def admit(reg: QualificationRegistry, d: str, **changes: object) -> dict[str, object]:
    args: dict[str, object] = {
        "checkpoint": checkpoint(),
        "backend": backend(),
        "scope": "customer",
        "hard_cap_mandate": True,
        "trusted_verifiers": TRUSTED,
        "verifier": SimulatedVerifier(),
        "clock": lambda: 100,
        "allow_simulated_verifiers": True,  # software tests only; production default is False
    }
    args.update(changes)
    return admit_fit(reg, d, **args)  # type: ignore[arg-type]


def refusal(reg: QualificationRegistry, d: str, **changes: object) -> str:
    with pytest.raises(AdmissionRefused) as caught:
        admit(reg, d, **changes)
    return caught.value.code


# A1: record pins exact identity and reproduces on identical inputs.
def test_a1_record_reproducible_and_pins_identity() -> None:
    first, second = qualify(request()), qualify(request())
    assert first == second
    body = first["body"]
    assert body["checkpoint_digest"] == checkpoint().checkpoint_digest
    assert body["backend_digest"] == backend().backend_digest
    assert first["record_digest"] == digest(body)
    assert body["fit_authorized"] is False and body["training_submissions"] == 0
    assert body["dry_run"]["submitted"] is False
    assert body["checkpoint"]["immutable_revision"] == checkpoint().immutable_revision


# A2: without a real witness the record is never qualified, and unwitnessed is not admitted.
def test_a2_unwitnessed_record_is_not_qualified_and_not_admitted(tmp_path: Path) -> None:
    record = qualify(request())
    body = record["body"]
    assert body["state"] == "software_checked_unwitnessed"
    assert body["evidence_tier"] == "generated" and body["witness"] is None
    assert "real_gpu_provider_witness_unsatisfied" in body["external_gates"]
    reg = registry(tmp_path)
    reg.register(record)
    assert refusal(reg, record["record_digest"]) == "witness_missing"


def test_a2_generated_tier_witness_is_not_a_witness() -> None:
    record = qualify(request(), SimulatedVerifier(tier="generated"))
    assert record["body"]["state"] == "software_checked_unwitnessed"


# A3: positive admission, and unqualified pairs are refused.
def test_a3_qualified_pair_admitted_without_granting_authority(tmp_path: Path) -> None:
    reg, d = qualified(tmp_path)
    result = admit(reg, d)
    assert result["fit_authorized"] is False
    assert result["admitted_for"] == "separately_authorized_fit"


def test_a3_unknown_record_refused(tmp_path: Path) -> None:
    assert refusal(registry(tmp_path), dg("nothing")) == "pair_unqualified"


def test_a3_held_record_refused(tmp_path: Path) -> None:
    reg = registry(tmp_path)
    record = qualify(request(backend=backend(capabilities=frozenset())), SimulatedVerifier())
    assert record["body"]["state"] == "benchmark_only_hold"
    reg.register(record)
    assert refusal(reg, record["record_digest"], backend=backend(capabilities=frozenset())) == (
        "pair_unqualified"
    )


def test_a3_different_pair_refused(tmp_path: Path) -> None:
    reg, d = qualified(tmp_path)
    other = checkpoint(model_ref="generated-model/other")
    assert refusal(reg, d, checkpoint=other) == "pair_unqualified"


# A4: any checkpoint drift is refused.
@pytest.mark.parametrize(
    "change",
    [
        {"immutable_revision": hashlib.sha1(b"generated-other").hexdigest()},
        {"tokenizer_digest": dg("tokenizer-2")},
        {"chat_template_digest": dg("template-2")},
        {"input_schema_digest": dg("input-2")},
        {"output_schema_digest": dg("output-2")},
        {"formatter_id": "generated-other-formatter"},
        {"architecture": "generated-other-arch"},
        {"weight_file_digests": (dg("w1"), dg("w3"))},
    ],
)
def test_a4_checkpoint_drift_refused(tmp_path: Path, change: dict[str, object]) -> None:
    reg, d = qualified(tmp_path)
    assert refusal(reg, d, checkpoint=checkpoint(**change)) == "checkpoint_digest_drift"


def test_a4_mutable_revision_rejected_at_qualification() -> None:
    with pytest.raises(QualificationInputError, match="checkpoint_revision_not_immutable"):
        qualify(request(checkpoint=checkpoint(immutable_revision="main")))


# A5: backend drift, unavailable backend, missing capability.
def test_a5_backend_version_drift_refused(tmp_path: Path) -> None:
    reg, d = qualified(tmp_path)
    assert refusal(reg, d, backend=backend(version="1.0.1")) == "backend_version_drift"


def test_a5_dependency_drift_refused(tmp_path: Path) -> None:
    reg, d = qualified(tmp_path)
    drifted = backend(dependency_revisions={"generated-lib": "9.9.9"})
    assert refusal(reg, d, backend=drifted) == "backend_digest_drift"


def test_a5_unavailable_backend_refused(tmp_path: Path) -> None:
    reg, d = qualified(tmp_path)
    assert refusal(reg, d, backend=backend(available=False)) == "backend_unavailable"


@pytest.mark.parametrize("missing", REQUIRED_CAPABILITIES)
def test_a5_missing_capability_refused_at_admission(tmp_path: Path, missing: str) -> None:
    reg, d = qualified(tmp_path)
    caps = frozenset(REQUIRED_CAPABILITIES) - {missing}
    assert refusal(reg, d, backend=backend(capabilities=caps)) == "capability_missing"


@pytest.mark.parametrize("missing", REQUIRED_CAPABILITIES)
def test_a5_missing_capability_holds_qualification(missing: str) -> None:
    caps = frozenset(REQUIRED_CAPABILITIES) - {missing}
    body = qualify(request(backend=backend(capabilities=caps)), SimulatedVerifier())["body"]
    assert body["state"] == "benchmark_only_hold"
    assert body["capabilities"]["missing"] == [missing]
    assert "capability_missing" in body["holds"] and body["witness"] is None


# A6: no enforceable hard cap cannot run under a hard-cap mandate.
def test_a6_no_hard_cap_refused_under_mandate(tmp_path: Path) -> None:
    uncapped = backend(hard_cost_cap=False)
    reg = registry(tmp_path)
    record = qualify(request(backend=uncapped), SimulatedVerifier())
    # Qualification holds: no enforceable cap, so no witness is even requested.
    assert record["body"]["state"] == "benchmark_only_hold"
    reg.register(record)
    code = refusal(reg, record["record_digest"], backend=uncapped)
    assert code == "pair_unqualified"


def test_a6_cap_dropped_after_qualification_refused(tmp_path: Path) -> None:
    reg, d = qualified(tmp_path)
    assert refusal(reg, d, backend=backend(hard_cost_cap=False)) == "enforceable_cap_unavailable"


# A7: missing authority, rights or cost control => hold, zero submissions, named gates.
def test_a7_missing_authority_names_fields_and_holds() -> None:
    authority = {**FULL_AUTHORITY, "payer": None, "egress": ""}
    body = qualify(request(authority=authority), SimulatedVerifier())["body"]
    assert body["state"] == "benchmark_only_hold"
    assert body["missing_authority"] == ["egress", "payer"]
    assert "authority_missing:payer" in body["external_gates"]
    assert body["training_submissions"] == 0 and body["fit_authorized"] is False


def test_a7_unknown_estimate_is_hold_not_zero() -> None:
    estimate = ResourceEstimate(None, 10, None, None)
    body = qualify(request(estimate=estimate), SimulatedVerifier())["body"]
    assert "resource_estimate_unknown" in body["holds"]
    assert body["resource_estimate"]["cost_minor_units"] is None


def test_a7_inference_key_alone_holds() -> None:
    body = qualify(request(inference_api_key_only=True), SimulatedVerifier())["body"]
    assert "inference_key_alone_supports_scoring_only" in body["holds"]


def test_a7_unknown_authority_field_rejected() -> None:
    with pytest.raises(QualificationInputError, match="authority_field_unknown"):
        qualify(request(authority={"surprise": "simulated:x"}))


# A8: supersession, restart replay, idempotence and tamper detection.
def test_a8_superseded_qualification_blocks_promotion(tmp_path: Path) -> None:
    reg, old = qualified(tmp_path)
    newer = qualify(request(backend=backend(capabilities=frozenset())), SimulatedVerifier())
    assert reg.register(newer)["result"] == "superseded"
    assert refusal(reg, old) == "qualification_superseded"


def test_a8_register_is_idempotent_and_replays_after_restart(tmp_path: Path) -> None:
    reg, d = qualified(tmp_path)
    record = reg.find(d)
    assert record is not None
    assert reg.register(record)["result"] == "unchanged"
    assert len((tmp_path / "journal.jsonl").read_text().splitlines()) == 1
    restarted = QualificationRegistry(tmp_path / "journal.jsonl")
    assert admit(restarted, d)["fit_authorized"] is False


def test_a8_supersession_survives_restart(tmp_path: Path) -> None:
    reg, old = qualified(tmp_path)
    reg.register(qualify(request(estimate=ResourceEstimate(None, None, None, None))))
    restarted = QualificationRegistry(tmp_path / "journal.jsonl")
    assert refusal(restarted, old) == "qualification_superseded"


def test_a8_tampered_journal_refused_on_replay(tmp_path: Path) -> None:
    reg, _ = qualified(tmp_path)
    path = tmp_path / "journal.jsonl"
    entry = json.loads(path.read_text())
    entry["record"]["body"]["state"] = "qualified"
    entry["record"]["body"]["checkpoint_digest"] = dg("forged")
    path.write_text(json.dumps(entry) + "\n")
    with pytest.raises(AdmissionRefused) as caught:
        QualificationRegistry(path)
    assert caught.value.code == "record_digest_mismatch"
    assert reg is not None


def test_a8_forged_state_without_digest_update_refused() -> None:
    record = qualify(request())
    record["body"]["state"] = "qualified"
    from opentour_training.training.eve_qualification import verify_record

    with pytest.raises(AdmissionRefused):
        verify_record(record)


# A9: witness trust, scope and mixture-of-experts.
def test_a9_untrusted_verifier_refused(tmp_path: Path) -> None:
    reg, d = qualified(tmp_path)
    assert refusal(reg, d, trusted_verifiers=frozenset()) == "witness_verifier_untrusted"


def test_a9_public_rehearsal_cannot_admit_customer_fit(tmp_path: Path) -> None:
    reg, d = qualified(tmp_path, scope="public_rehearsal")
    assert refusal(reg, d, scope="customer") == "scope_mismatch"
    assert admit(reg, d, scope="public_rehearsal")["fit_authorized"] is False


def test_a9_moe_checkpoint_needs_own_qualification() -> None:
    body = qualify(request(checkpoint=checkpoint(mixture_of_experts=True)), SimulatedVerifier())[
        "body"
    ]
    assert body["state"] == "benchmark_only_hold"
    assert "mixture_of_experts_needs_own_qualification" in body["holds"]


# A10: dry run uses permitted rows only and submits nothing.
@pytest.mark.parametrize("key", ["expected_answer", "reference_answer", "answer_key", "label"])
def test_a10_protected_field_in_dry_run_rows_holds(key: str) -> None:
    rows = ({"prompt": "generated q", "completion": "generated a", key: "generated-secret"},)
    body = qualify(request(development_rows=rows), SimulatedVerifier())["body"]
    assert body["state"] == "benchmark_only_hold"
    assert "dry_run_protected_field_present" in body["holds"] and body["dry_run"] is None


def test_a10_empty_rows_and_bad_shape_hold() -> None:
    assert "dry_run_rows_missing" in qualify(request(development_rows=()))["body"]["holds"]
    bad = ({"prompt": "generated q"},)
    assert "dry_run_row_shape_invalid" in qualify(request(development_rows=bad))["body"]["holds"]


def test_a10_dry_run_is_deterministic_and_completion_only() -> None:
    first = serialization_dry_run(checkpoint(), ROWS)
    assert first == serialization_dry_run(checkpoint(), ROWS)
    assert first["loss_scope"] == "completion_only" and first["training_submissions"] == 0
    other = serialization_dry_run(checkpoint(formatter_id="generated-other"), ROWS)
    assert other["serialization_digest"] != first["serialization_digest"]


# A11: the default verifier can never produce a qualified record.
def test_a11_default_verifier_never_qualifies() -> None:
    assert qualify(request())["body"]["state"] != "qualified"


def test_a11_unavailable_backend_holds_qualification() -> None:
    body = qualify(request(backend=backend(available=False)), SimulatedVerifier())["body"]
    assert body["state"] == "benchmark_only_hold" and "backend_unavailable" in body["holds"]


# Review additions: forged records, journal integrity, cross-process freshness, simulated trust.
def _forged(**body_changes: object) -> dict[str, object]:
    body = json.loads(json.dumps(qualify(request())["body"]))
    body.update(body_changes)
    return {"body": body, "record_digest": digest(body)}


@pytest.mark.parametrize(
    "changes",
    [
        {"state": "qualified", "witness": {"verifier_id": "x", "witness_ref": "r"}},
        {
            "state": "qualified",
            "evidence_tier": "generated",
            "witness": {"verifier_id": "x", "witness_ref": "r"},
        },
        {
            "state": "qualified",
            "evidence_tier": "observed",
            "holds": ["capability_missing"],
            "witness": {"verifier_id": "x", "witness_ref": "r"},
        },
        {"checkpoint_digest": dg("forged")},
        {"backend_digest": dg("forged")},
        {"pair_key": dg("forged")},
        {"training_submissions": 1},
        {"witness": {"verifier_id": "x", "witness_ref": "r"}},
    ],
)
def test_review_self_consistent_forged_record_refused(
    tmp_path: Path, changes: dict[str, object]
) -> None:
    forged = _forged(**changes)
    with pytest.raises(AdmissionRefused):
        verify_record(forged)
    with pytest.raises(AdmissionRefused):
        registry(tmp_path).register(forged)


def test_review_simulated_verifier_refused_by_default(tmp_path: Path) -> None:
    reg, d = qualified(tmp_path)
    assert refusal(reg, d, allow_simulated_verifiers=False) == "witness_verifier_simulated"


def _two_entry_journal(tmp_path: Path) -> tuple[Path, str]:
    reg, old = qualified(tmp_path)
    reg.register(qualify(request(estimate=ResourceEstimate(None, None, None, None))))
    return tmp_path / "journal.jsonl", old


def test_review_deleted_superseding_entry_cannot_reinstate_old_record(tmp_path: Path) -> None:
    path, _ = _two_entry_journal(tmp_path)
    # Removing the first entry leaves seq starting at 2: detected.
    path.write_text(path.read_text().splitlines()[1] + "\n")
    with pytest.raises(AdmissionRefused) as caught:
        QualificationRegistry(path)
    assert caught.value.code == "journal_corrupt"


def test_review_reordered_journal_refused(tmp_path: Path) -> None:
    path, _ = _two_entry_journal(tmp_path)
    first, second = path.read_text().splitlines()
    path.write_text(second + "\n" + first + "\n")
    with pytest.raises(AdmissionRefused) as caught:
        QualificationRegistry(path)
    assert caught.value.code == "journal_corrupt"


def test_review_torn_trailing_line_refused_fail_closed(tmp_path: Path) -> None:
    path, _ = _two_entry_journal(tmp_path)
    path.write_bytes(path.read_bytes()[:-15])
    with pytest.raises(AdmissionRefused) as caught:
        QualificationRegistry(path)
    assert caught.value.code == "journal_corrupt"


def test_review_second_instance_sees_supersession_without_restart(tmp_path: Path) -> None:
    writer, old = qualified(tmp_path)
    reader = QualificationRegistry(tmp_path / "journal.jsonl")
    assert admit(reader, old)["fit_authorized"] is False
    writer.register(qualify(request(estimate=ResourceEstimate(None, None, None, None))))
    assert refusal(reader, old) == "qualification_superseded"


def test_review_stale_writer_appends_with_correct_supersedes_link(tmp_path: Path) -> None:
    first, old = qualified(tmp_path)
    stale = QualificationRegistry(tmp_path / "journal.jsonl")
    first.register(qualify(request(estimate=ResourceEstimate(None, None, None, None))))
    stale.register(qualify(request(estimate=ResourceEstimate(1, None, None, None))))
    # Replay verifies seq/supersedes; a stale-view append would have broken the chain.
    assert len(QualificationRegistry(tmp_path / "journal.jsonl")._entries) == 3


@pytest.mark.parametrize("route", ["customer_trainer", "local_open_weight", "customer_managed"])
def test_shared_connector_route_values(route: str) -> None:
    record = qualify(request(backend=backend(route=route)))
    assert verify_record(record)["route"] == route


def test_readiness_requires_an_unexpired_current_witness(tmp_path: Path) -> None:
    reg, d = qualified(tmp_path)
    assert refusal(reg, d, clock=lambda: 2_000_000_000) == "qualification_expired"
    assert refusal(reg, d, verifier=None) == "witness_verifier_unavailable"
    assert refusal(reg, d, verifier=SimulatedVerifier(verifier_id="foreign")) == (
        "witness_not_current"
    )


def test_digest_consistent_forged_witness_ref_is_not_admitted(tmp_path: Path) -> None:
    record = qualify(request(), SimulatedVerifier())
    record["body"]["witness"]["witness_ref"] = "simulated:forged"
    forged = digest(record["body"])
    record["record_digest"] = forged
    reg = registry(tmp_path)
    reg.register(record)
    assert refusal(reg, forged) == "witness_not_current"
