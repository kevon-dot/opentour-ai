"""Profile pinning, no-substitution and separate per-family qualification records."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
import toy_families as toy
from opentour_cell.pilot import model_family as fam
from opentour_cell.pilot.connector_ext import EvidenceTier

REPO = Path(__file__).resolve().parents[4]
PROFILES = REPO / "training/profiles/families"


def test_reference_profile_matches_the_qualified_numeric_profile_exactly() -> None:
    profile = fam.load_profile(PROFILES / "smollm2_135m_lora_v1.family.json")
    qualified = json.loads((REPO / "training/profiles/smollm2_135m_lora_v1.json").read_bytes())
    assert profile.pinned
    assert profile.revision == qualified["revision"]
    assert dict(profile.files) == qualified["files"]
    assert profile.license == qualified["license"]
    assert profile.resource.max_rows == qualified["limits"]["max_training_rows"]
    assert profile.resource.max_steps == qualified["adapter"]["steps"]
    assert (
        profile.resource.max_address_space_bytes == qualified["limits"]["max_address_space_bytes"]
    )
    assert profile.resource.incremental_cost_minor_units == qualified["limits"]["hourly_cost_cents"]


def test_second_family_candidate_declares_every_missing_pin_and_cannot_qualify() -> None:
    profile = fam.load_profile(PROFILES / "pythia_160m_neox_candidate.family.json")
    assert not profile.pinned
    assert {"revision", "license", "digest:model.safetensors", "digest:config.json"} <= set(
        profile.pin_gaps
    )
    reference = fam.load_profile(PROFILES / "smollm2_135m_lora_v1.family.json")
    assert profile.architecture != reference.architecture
    assert profile.tuning_method == reference.tuning_method
    assert profile.benchmark_contract == reference.benchmark_contract
    # Even a fully passing installed-model run is FIXTURE tier while any pin is missing.
    results = {check: True for check in fam.required_checks(profile)}
    record = fam.build_record(profile, fam.FixtureKind.INSTALLED_MODEL, results)
    assert record.tier is EvidenceTier.FIXTURE
    assert not record.qualified
    assert "profile_pin_missing:revision" in record.external_gates


def test_generated_results_are_fixture_tier_with_unsatisfied_gates(tmp_path: Path) -> None:
    family = toy.Family(tmp_path, "generated-toy-linear")
    results = {check: True for check in fam.required_checks(family.profile)}
    record = fam.build_record(family.profile, fam.FixtureKind.GENERATED, results)
    assert record.passed
    assert record.tier is EvidenceTier.FIXTURE
    assert not record.qualified
    assert "installed_model_and_gpu_or_cpu_run_not_performed" in record.external_gates
    assert "customer_witness_not_recorded" in record.external_gates
    # A generated family can never be qualified, even if a caller labels the run INSTALLED_MODEL.
    mislabeled = fam.build_record(family.profile, fam.FixtureKind.INSTALLED_MODEL, results)
    assert mislabeled.tier is EvidenceTier.FIXTURE and not mislabeled.qualified
    assert "generated_family_is_never_a_qualified_model" in mislabeled.external_gates
    # Structural path only: a pinned, non-generated profile with an installed-model run.
    pinned = replace(family.profile, family_id="structural-pinned-profile")
    installed = fam.build_record(pinned, fam.FixtureKind.INSTALLED_MODEL, results)
    assert installed.tier is EvidenceTier.SOFTWARE_QUALIFIED and installed.qualified
    # Software qualification still never records customer or provider witness.
    assert "customer_witness_not_recorded" in installed.external_gates


def test_a_record_cannot_claim_a_tier_its_evidence_does_not_support(tmp_path: Path) -> None:
    family = toy.Family(tmp_path, "generated-toy-linear")
    base = fam.build_record(family.profile, fam.FixtureKind.GENERATED, {})

    def claim(tier: EvidenceTier, **changes: object) -> fam.QualificationRecord:
        return replace(base, tier=tier, **changes)  # type: ignore[arg-type]

    for tier in (
        EvidenceTier.SOFTWARE_QUALIFIED,
        EvidenceTier.PROVIDER_WITNESSED,
        EvidenceTier.CUSTOMER_WITNESSED,
    ):
        with pytest.raises(fam.FamilyError, match="EVIDENCE_TIER_UNSUPPORTED"):
            claim(tier)  # generated fixture, generated family
        with pytest.raises(fam.FamilyError, match="EVIDENCE_TIER_UNSUPPORTED"):
            claim(
                tier,
                fixture_kind=fam.FixtureKind.INSTALLED_MODEL,
                family_id="structural-pinned-profile",
                external_gates=("profile_pin_missing:revision",),
            )
    with pytest.raises(fam.FamilyError, match="INVALID_EVIDENCE_TIER"):
        claim("customer_witnessed")  # type: ignore[arg-type]
    real = {
        "fixture_kind": fam.FixtureKind.INSTALLED_MODEL,
        "family_id": "structural-pinned-profile",
        "external_gates": (),
    }
    # A witnessed tier additionally needs a witness reference; a lower tier is not enough.
    with pytest.raises(fam.FamilyError, match="EVIDENCE_TIER_UNSUPPORTED"):
        claim(EvidenceTier.CUSTOMER_WITNESSED, **real)
    assert claim(EvidenceTier.CUSTOMER_WITNESSED, witness_ref="generated-witness", **real)
    assert claim(EvidenceTier.SOFTWARE_QUALIFIED, **real).tier is EvidenceTier.SOFTWARE_QUALIFIED


def test_missing_or_failed_required_check_is_never_a_pass(tmp_path: Path) -> None:
    family = toy.Family(tmp_path, "generated-toy-normmlp")
    results = {check: True for check in fam.required_checks(family.profile)}
    del results[fam.Check.CANCELLATION]
    assert not fam.build_record(family.profile, fam.FixtureKind.GENERATED, results).passed
    results[fam.Check.CANCELLATION] = False
    assert not fam.build_record(family.profile, fam.FixtureKind.GENERATED, results).passed
    with pytest.raises(fam.FamilyError, match="UNKNOWN_QUALIFICATION_CHECK"):
        fam.build_record(family.profile, fam.FixtureKind.GENERATED, {"nope": True})  # type: ignore[dict-item]


def test_reference_role_requires_reproduction_and_candidate_does_not(tmp_path: Path) -> None:
    reference = toy.Family(tmp_path / "a", "generated-toy-linear", role="reference")
    candidate = toy.Family(tmp_path / "b", "generated-toy-normmlp", role="candidate")
    assert fam.Check.REFERENCE_REPRODUCED in fam.required_checks(reference.profile)
    assert fam.Check.REFERENCE_REPRODUCED not in fam.required_checks(candidate.profile)


def test_records_are_per_family_and_must_share_one_benchmark_contract(tmp_path: Path) -> None:
    a = toy.Family(tmp_path / "a", "generated-toy-linear", role="reference")
    b = toy.Family(tmp_path / "b", "generated-toy-normmlp")

    def record(family: toy.Family) -> fam.QualificationRecord:
        results = {check: True for check in fam.required_checks(family.profile)}
        return fam.build_record(family.profile, fam.FixtureKind.GENERATED, results)

    first, second = record(a), record(b)
    assert first.family_identity != second.family_identity
    fam.require_distinct_families_on_one_benchmark(first, second)
    with pytest.raises(fam.FamilyError, match="SECOND_FAMILY_NOT_DISTINCT"):
        fam.require_distinct_families_on_one_benchmark(first, first)
    other_contract = replace(second, benchmark_contract="generated-other-contract")
    with pytest.raises(fam.FamilyError, match="BENCHMARK_CONTRACT_MISMATCH"):
        fam.require_distinct_families_on_one_benchmark(first, other_contract)
    other_scorer = replace(second, scorer="generated-other-scorer")
    with pytest.raises(fam.FamilyError, match="BENCHMARK_CONTRACT_MISMATCH"):
        fam.require_distinct_families_on_one_benchmark(first, other_scorer)


def test_no_automatic_substitution_of_a_reference_model_for_the_customer_target(
    tmp_path: Path,
) -> None:
    reference = toy.Family(tmp_path / "a", "generated-toy-linear", role="reference")
    registry = fam.ProfileRegistry()
    registry.register("customer-target", reference.profile)
    binding = replace(reference.binding, model_ref="customer-target")
    assert registry.resolve(binding) is reference.profile
    for change in (
        {"immutable_revision": "f" * 40},
        {"task": "generated-other-task"},
        {"preprocessing_revision": "generated-other-formatter"},
    ):
        with pytest.raises(fam.FamilyError, match="MODEL_SUBSTITUTION_REFUSED"):
            registry.resolve(replace(binding, **change))
    with pytest.raises(fam.FamilyError, match="MODEL_FAMILY_NOT_REGISTERED"):
        registry.resolve(replace(binding, model_ref="customer-unregistered"))
    with pytest.raises(fam.FamilyError, match="AMBIGUOUS_FAMILY_REGISTRATION"):
        registry.register("customer-target", reference.profile)


def test_approved_customer_model_directory_is_verified_file_by_file(tmp_path: Path) -> None:
    family = toy.Family(tmp_path, "generated-toy-linear")
    assert fam.verify_model_directory(family.profile, family.model_dir) == family.profile.revision
    (family.model_dir / "base.json").write_text("{}")
    with pytest.raises(fam.FamilyError, match="INSTALLED_MODEL_MISMATCH"):
        fam.verify_model_directory(family.profile, family.model_dir)
    (family.model_dir / "base.json").unlink()
    with pytest.raises(fam.FamilyError, match="INSTALLED_MODEL_FILE_MISSING"):
        fam.verify_model_directory(family.profile, family.model_dir)
    link = family.model_dir / "base.json"
    link.symlink_to(family.model_dir / "tokenizer.json")
    with pytest.raises(fam.FamilyError, match="INSTALLED_MODEL_FILE_MISSING"):
        fam.verify_model_directory(family.profile, family.model_dir)
    unpinned = fam.load_profile(PROFILES / "pythia_160m_neox_candidate.family.json")
    with pytest.raises(fam.FamilyError, match="FAMILY_NOT_PINNED"):
        fam.verify_model_directory(unpinned, tmp_path)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(extra="field"),
        lambda d: d.pop("scorer"),
        lambda d: d.update(schema_version="other"),
        lambda d: d["resource"].update(max_steps=0),
        lambda d: d.update(role="other"),
        lambda d: d.update(task=" padded "),
    ],
)
def test_profile_document_is_strict(tmp_path: Path, mutate) -> None:  # type: ignore[no-untyped-def]
    document = json.loads((PROFILES / "smollm2_135m_lora_v1.family.json").read_bytes())
    mutate(document)
    with pytest.raises(fam.FamilyError, match="INVALID_FAMILY_PROFILE"):
        fam.parse_profile(document)


def test_adding_a_compatible_profile_needs_only_a_declarative_document(tmp_path: Path) -> None:
    """The documented onboarding path: copy a profile, pin every field, load, verify, register."""
    family = toy.Family(tmp_path, "generated-toy-normmlp")
    reloaded = fam.load_profile(family.profile_path)
    assert reloaded == family.profile and reloaded.pinned
    assert fam.verify_model_directory(reloaded, family.model_dir) == reloaded.revision
    registry = fam.ProfileRegistry()
    registry.register(family.binding.model_ref, reloaded)
    assert registry.resolve(family.binding) is reloaded
    with pytest.raises(fam.FamilyError, match="INVALID_FAMILY_PROFILE"):
        fam.load_profile(tmp_path / "missing.family.json")
