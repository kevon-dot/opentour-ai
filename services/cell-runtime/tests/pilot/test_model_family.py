"""Profile pinning, no-substitution and separate per-family qualification records."""

from __future__ import annotations

import json
import pickle
from dataclasses import fields, replace
from pathlib import Path

import pytest
import toy_families as toy
from opentour_cell.pilot import custom_trainer_connector as ctc
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
    # OPE-566 owner ruling: a witness string is never a customer witness. Until the OPE-566
    # PilotVerifier exists, CUSTOMER_WITNESSED is refused whatever string is supplied.
    with pytest.raises(fam.FamilyError, match="^CUSTOMER_WITNESS_UNSTRUCTURED$"):
        claim(EvidenceTier.CUSTOMER_WITNESSED, witness_ref="generated-witness", **real)
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


# ---------------------------------------------------------------------------------------------
# OPE-507 qualification hardening: evidence coverage, identity normalization, field shapes.
# ---------------------------------------------------------------------------------------------

_REAL = {
    "fixture_kind": fam.FixtureKind.INSTALLED_MODEL,
    "family_id": "structural-pinned-profile",
    "external_gates": (),
    "revision": "a" * 40,
}
_QUALIFIED_TIERS = (
    EvidenceTier.SOFTWARE_QUALIFIED,
    EvidenceTier.PROVIDER_WITNESSED,
    EvidenceTier.CUSTOMER_WITNESSED,
)
_WITNESSED_TIERS = (EvidenceTier.PROVIDER_WITNESSED, EvidenceTier.CUSTOMER_WITNESSED)


def _claim(
    tmp_path: Path, tier: EvidenceTier, role: str = "reference", **changes: object
) -> fam.QualificationRecord:
    family = toy.Family(tmp_path, "generated-toy-linear", role=role)
    results = {check: True for check in fam.required_checks(family.profile)}
    base = fam.build_record(family.profile, fam.FixtureKind.GENERATED, results)
    fields: dict[str, object] = {**_REAL, "tier": tier}
    if tier in _WITNESSED_TIERS:
        fields["witness_ref"] = "witness:generated-0001"
    fields.update(changes)
    return replace(base, **fields)  # type: ignore[arg-type]


@pytest.mark.parametrize("tier", _QUALIFIED_TIERS)
def test_a_qualified_tier_with_no_recorded_checks_is_refused(
    tmp_path: Path, tier: EvidenceTier
) -> None:
    with pytest.raises(fam.FamilyError, match="EVIDENCE_TIER_UNSUPPORTED"):
        _claim(tmp_path, tier, results=())


@pytest.mark.parametrize("tier", _QUALIFIED_TIERS)
def test_a_qualified_tier_covering_only_some_required_checks_is_refused(
    tmp_path: Path, tier: EvidenceTier
) -> None:
    # The full result set is taken from the highest constructible tier: CUSTOMER_WITNESSED itself
    # is refused (OPE-566 ruling), and the results do not depend on the tier.
    full = _claim(tmp_path, EvidenceTier.SOFTWARE_QUALIFIED).results
    partial = full[:-1]
    assert partial and len(partial) == len(full) - 1
    for results in (
        partial,
        full + (full[0],),  # duplicated check name
        partial + (("nope", True),),  # unknown check standing in for a required one
        partial + ((full[-1][0], "yes"),),  # non-bool outcome
    ):
        with pytest.raises(fam.FamilyError, match="EVIDENCE_TIER_UNSUPPORTED"):
            _claim(tmp_path, tier, results=results)  # type: ignore[arg-type]


def test_a_candidate_role_record_is_not_forced_to_carry_the_reference_check(
    tmp_path: Path,
) -> None:
    record = _claim(tmp_path, EvidenceTier.SOFTWARE_QUALIFIED, role=fam.Role.CANDIDATE.value)
    assert fam.Check.REFERENCE_REPRODUCED.value not in dict(record.results)
    assert record.qualified


def test_qualified_is_false_without_coverage_even_if_a_record_was_forced(tmp_path: Path) -> None:
    record = _claim(tmp_path, EvidenceTier.SOFTWARE_QUALIFIED)
    assert record.qualified
    # Defence in depth for the vacuous ``all(())``: bypass __post_init__ the way only a
    # deliberate caller can; the properties must still not report qualified.
    object.__setattr__(record, "results", ())
    assert not record.passed
    assert not record.qualified


GENERATED_VARIANTS = [
    "Generated-x",
    " generated-x",
    "generated_x",
    "GENERATED-X",
    "generated-x ",
    "\tgenerated-x",
    "generated x",
    "Generated_X",
    "generated​-x",  # zero-width space inside the prefix
    "​generated-x",
    "ｇｅｎｅｒａｔｅｄ-x",  # fullwidth letters fold under NFKC
]


@pytest.mark.parametrize("variant", GENERATED_VARIANTS)
@pytest.mark.parametrize("tier", _QUALIFIED_TIERS)
def test_no_spelling_of_a_generated_family_id_reaches_a_qualified_tier(
    tmp_path: Path, variant: str, tier: EvidenceTier
) -> None:
    with pytest.raises(fam.FamilyError, match="EVIDENCE_TIER_UNSUPPORTED"):
        _claim(tmp_path, tier, family_id=variant)


@pytest.mark.parametrize("variant", GENERATED_VARIANTS)
def test_profile_is_fixture_and_build_record_agree_for_every_spelling(
    tmp_path: Path, variant: str
) -> None:
    family = toy.Family(tmp_path, "generated-toy-linear")
    profile = replace(family.profile, family_id=variant)
    assert profile.is_fixture
    results = {check: True for check in fam.required_checks(profile)}
    record = fam.build_record(profile, fam.FixtureKind.INSTALLED_MODEL, results)
    assert record.tier is EvidenceTier.FIXTURE and not record.qualified
    assert "generated_family_is_never_a_qualified_model" in record.external_gates


@pytest.mark.parametrize(
    "family_id",
    ["generatedx", "degenerated-x", "regenerated-x", "my-generated-x", "structural-pinned-profile"],
)
def test_a_legitimately_distinct_family_id_is_not_collapsed_into_generated(family_id: str) -> None:
    # The narrow ``generated`` prefix rule still keeps these ids distinct from a generated family.
    assert not fam.is_generated_family_id(family_id)


@pytest.mark.parametrize(
    "family_id", ["generatedx", "degenerated-x", "regenerated-x", "my-generated-x"]
)
def test_an_id_merely_containing_generated_is_a_fixture_family(
    tmp_path: Path, family_id: str
) -> None:
    # OPE-566 owner ruling (fixture-name downgrade, as in #218): a name CONTAINING a fixture token
    # is FIXTURE. These ids qualified before; the rule can only lower a tier.
    family = toy.Family(tmp_path, "generated-toy-linear")
    assert replace(family.profile, family_id=family_id).is_fixture
    with pytest.raises(fam.FamilyError, match="EVIDENCE_TIER_UNSUPPORTED"):
        _claim(tmp_path, EvidenceTier.SOFTWARE_QUALIFIED, family_id=family_id)


@pytest.mark.parametrize("tier", _WITNESSED_TIERS)
@pytest.mark.parametrize("bad", [" ", "", None, 5, "has space", "a" * 300, b"witness-1", "x\ny"])
def test_a_malformed_witness_ref_is_refused_at_both_witnessed_tiers(
    tmp_path: Path, tier: EvidenceTier, bad: object
) -> None:
    with pytest.raises(fam.FamilyError, match="EVIDENCE_TIER_UNSUPPORTED"):
        _claim(tmp_path, tier, witness_ref=bad)


@pytest.mark.parametrize("good", ["generated-witness", "witness:generated-0001", "ref/2026#7"])
def test_a_well_formed_witness_ref_is_refused_at_the_provider_tier(
    tmp_path: Path, good: str
) -> None:
    # Owner-delegate decision recorded on OPE-507 (follows the #218 fail-closed precedent): a
    # caller-supplied string never stands in for provider attestation, so PROVIDER_WITNESSED is
    # refused until a provider-attestation verifier exists. The shape check still runs first.
    with pytest.raises(fam.FamilyError, match="^PROVIDER_WITNESS_UNSTRUCTURED$"):
        _claim(tmp_path, EvidenceTier.PROVIDER_WITNESSED, witness_ref=good)


@pytest.mark.parametrize("bad", ["junk", "A" * 40, "a" * 39, "a" * 41, " " + "a" * 39, "g" * 40, 5])
@pytest.mark.parametrize("tier", _QUALIFIED_TIERS)
def test_a_non_40_hex_revision_is_refused_on_the_record(
    tmp_path: Path, bad: object, tier: EvidenceTier
) -> None:
    with pytest.raises(fam.FamilyError, match="EVIDENCE_TIER_UNSUPPORTED"):
        _claim(tmp_path, tier, revision=bad)


def test_a_valid_40_hex_revision_is_accepted_on_the_record(tmp_path: Path) -> None:
    revision = "0123456789abcdef" * 2 + "01234567"
    assert _claim(tmp_path, EvidenceTier.SOFTWARE_QUALIFIED, revision=revision).qualified


# ---------------------------------------------------------------------------------------------
# OPE-507 witness follow-up (OPE-566 owner ruling, mirroring OPE-509 #218): CUSTOMER_WITNESSED
# needs an ``opentour.pilot.customer-witness.v1`` record checked only by the OPE-566
# ``PilotVerifier``. It does not exist, so a model-family record reaches SOFTWARE_QUALIFIED at
# most, a witness string is refused, identities must be exact ``str`` and fixture names downgrade.
# PROVIDER_WITNESSED is refused the same way (owner-delegate decision recorded on OPE-507).
# Fixture kind: generated.
# ---------------------------------------------------------------------------------------------


class _PlainStrSubclass(str):
    pass


class _CasefoldLiarStr(str):
    def casefold(self) -> str:
        return "structural-pinned-profile"


WELL_FORMED_WITNESS_STRINGS = [
    "generated-witness",
    "witness:generated-0001",
    "ref/2026#7",
    "witness:acme-0001",
    "opentour.pilot.customer-witness.v1",
    "customer_owner",
    "a",
]


@pytest.mark.parametrize("witness", WELL_FORMED_WITNESS_STRINGS)
@pytest.mark.parametrize("role", [fam.Role.REFERENCE.value, fam.Role.CANDIDATE.value])
def test_a_witness_string_never_reaches_customer_witnessed(
    tmp_path: Path, witness: str, role: str
) -> None:
    with pytest.raises(fam.FamilyError, match="^CUSTOMER_WITNESS_UNSTRUCTURED$"):
        _claim(tmp_path, EvidenceTier.CUSTOMER_WITNESSED, role=role, witness_ref=witness)
    # The same record one tier lower is accepted: the cap is SOFTWARE_QUALIFIED, not FIXTURE.
    capped = _claim(tmp_path, EvidenceTier.SOFTWARE_QUALIFIED, role=role, witness_ref=witness)
    assert capped.tier is EvidenceTier.SOFTWARE_QUALIFIED and capped.qualified


@pytest.mark.parametrize("witness", WELL_FORMED_WITNESS_STRINGS)
@pytest.mark.parametrize("role", [fam.Role.REFERENCE.value, fam.Role.CANDIDATE.value])
def test_a_witness_string_never_reaches_provider_witnessed(
    tmp_path: Path, witness: str, role: str
) -> None:
    with pytest.raises(fam.FamilyError, match="^PROVIDER_WITNESS_UNSTRUCTURED$"):
        _claim(tmp_path, EvidenceTier.PROVIDER_WITNESSED, role=role, witness_ref=witness)
    capped = _claim(tmp_path, EvidenceTier.SOFTWARE_QUALIFIED, role=role, witness_ref=witness)
    assert capped.tier is EvidenceTier.SOFTWARE_QUALIFIED and capped.qualified


_WITNESS_REFUSALS = {
    EvidenceTier.PROVIDER_WITNESSED: "PROVIDER_WITNESS_UNSTRUCTURED",
    EvidenceTier.CUSTOMER_WITNESSED: "CUSTOMER_WITNESS_UNSTRUCTURED",
}


def test_software_qualified_is_the_highest_reachable_tier(tmp_path: Path) -> None:
    # Every tier above SOFTWARE_QUALIFIED has its own refusal, and no tier is left unmapped.
    order = list(EvidenceTier)
    above = order[order.index(EvidenceTier.SOFTWARE_QUALIFIED) + 1 :]
    assert set(above) == set(_WITNESS_REFUSALS)
    assert _claim(tmp_path, EvidenceTier.SOFTWARE_QUALIFIED).qualified
    for tier in above:
        with pytest.raises(fam.FamilyError, match=f"^{_WITNESS_REFUSALS[tier]}$"):
            _claim(tmp_path, tier)


@pytest.mark.parametrize("tier", _WITNESSED_TIERS)
def test_a_witnessed_claim_failing_consistency_keeps_the_generic_code(
    tmp_path: Path, tier: EvidenceTier
) -> None:
    # Consistency checks run first at both witnessed tiers, exactly as in #218.
    for changes in (
        {"family_id": "generated-x"},
        {"revision": "junk"},
        {"fixture_kind": fam.FixtureKind.GENERATED},
        {"external_gates": ("profile_pin_missing:revision",)},
        {"results": ()},
        {"witness_ref": ""},
        {"witness_ref": "has space"},
    ):
        with pytest.raises(fam.FamilyError, match="^EVIDENCE_TIER_UNSUPPORTED$"):
            _claim(tmp_path, tier, **changes)


def test_a_structured_customer_witness_record_is_still_refused_without_the_verifier(
    tmp_path: Path,
) -> None:
    # Even the #218 structured record cannot be smuggled in: no verifier exists for model families.
    record = ctc.CustomerWitness(
        engagement="generated-engagement",
        trainer_id="structural-pinned-profile",
        job_id="generated-job-1",
        artifact_revision="a" * 40,
        observed_outcome="completed",
        witness_role=ctc.WITNESS_ROLE_CUSTOMER_OWNER,
        purpose=ctc.CUSTOMER_WITNESS_PURPOSE,
        attestation="generated-attestation",
    )
    with pytest.raises(fam.FamilyError, match="EVIDENCE_TIER_UNSUPPORTED"):
        _claim(tmp_path, EvidenceTier.CUSTOMER_WITNESSED, witness_ref=record)


def test_build_record_never_exceeds_software_qualified(tmp_path: Path) -> None:
    family = toy.Family(tmp_path, "generated-toy-linear")
    results = {check: True for check in fam.required_checks(family.profile)}
    pinned = replace(family.profile, family_id="structural-pinned-profile")
    record = fam.build_record(pinned, fam.FixtureKind.INSTALLED_MODEL, results)
    assert record.tier is EvidenceTier.SOFTWARE_QUALIFIED
    assert "customer_witness_not_recorded" in record.external_gates
    with pytest.raises(fam.FamilyError, match="^CUSTOMER_WITNESS_UNSTRUCTURED$"):
        replace(record, tier=EvidenceTier.CUSTOMER_WITNESSED, witness_ref="witness:acme-1")
    with pytest.raises(fam.FamilyError, match="^PROVIDER_WITNESS_UNSTRUCTURED$"):
        replace(record, tier=EvidenceTier.PROVIDER_WITNESSED, witness_ref="witness:acme-1")


@pytest.mark.parametrize(
    "family_id",
    [
        _PlainStrSubclass("structural-pinned-profile"),
        _CasefoldLiarStr("generated-x"),
        _CasefoldLiarStr("structural-pinned-profile"),
    ],
    ids=["plain-subclass", "casefold-liar-generated", "casefold-liar-real"],
)
@pytest.mark.parametrize("tier", _QUALIFIED_TIERS)
def test_a_str_subclass_family_id_is_refused(
    tmp_path: Path, family_id: str, tier: EvidenceTier
) -> None:
    family = toy.Family(tmp_path, "generated-toy-linear")
    assert replace(family.profile, family_id=family_id).is_fixture
    assert fam.is_fixture_family_id(family_id)
    with pytest.raises(fam.FamilyError, match="EVIDENCE_TIER_UNSUPPORTED"):
        _claim(tmp_path, tier, family_id=family_id)


@pytest.mark.parametrize("tier", _QUALIFIED_TIERS)
def test_a_str_subclass_revision_is_refused(tmp_path: Path, tier: EvidenceTier) -> None:
    with pytest.raises(fam.FamilyError, match="EVIDENCE_TIER_UNSUPPORTED"):
        _claim(tmp_path, tier, revision=_PlainStrSubclass("a" * 40))


def test_a_str_subclass_witness_ref_is_refused(tmp_path: Path) -> None:
    with pytest.raises(fam.FamilyError, match="EVIDENCE_TIER_UNSUPPORTED"):
        _claim(tmp_path, EvidenceTier.PROVIDER_WITNESSED, witness_ref=_PlainStrSubclass("ref-1"))
    # Even an empty subclass instance is not an exact empty ``str`` at the unwitnessed tier.
    with pytest.raises(fam.FamilyError, match="EVIDENCE_TIER_UNSUPPORTED"):
        _claim(tmp_path, EvidenceTier.SOFTWARE_QUALIFIED, witness_ref=_PlainStrSubclass(""))


# Edge whitespace and control characters that ``str.strip`` would silently remove (or that sit
# inside the id). Only the pre-NFKC invisible check catches them at the edges (review F1).
_EDGE_CONTROLS = [
    "\t",
    "\n",
    "\v",
    "\f",
    "\r",
    "\x1c",
    "\x1d",
    "\x1e",
    "\x1f",
    "\x85",
    " ",
    " ",
]

FIXTURE_NAME_VARIANTS = [
    "GeneratedFamily",
    "regenerated-x",
    "simulated-family",
    "Simulated:family",
    "acme-reference-linear",
    "ReferenceFamily",
    "fixture-family",
    "fixtures-family",
    "sentinel-family",
    "acmesentinelml",
    "family.invalid",
    "family.INVALID.",
    "family.invalid:443",
    "https://family.invalid/x",
    "a.invalid.example",
    "ｓｅｎｔｉｎｅｌ-family",  # fullwidth "sentinel"
    "ﬁxture-family",  # fi ligature folds to "fixture"
    "gеnerated-x",  # Cyrillic homoglyph
    "génerated-x",  # precomposed accent: non-ASCII identity
    "﻿structural-pinned-profile",  # byte-order mark on an otherwise real id
    "structural​-pinned-profile",  # zero-width space on an otherwise real id
    "structural-pinned-profile\x00",  # control character
    "  ",
    "",
    *(f"{ch}structural-pinned-profile" for ch in _EDGE_CONTROLS),
    *(f"structural-pinned-profile{ch}" for ch in _EDGE_CONTROLS),
    *(f"structural{ch}-pinned-profile" for ch in _EDGE_CONTROLS),
]
# Clean printable ASCII only after NFKC, with no fixture token: fullwidth "acme" (review F2).
NFKC_CLEAN_ID = "ａｃｍｅ-linear"


@pytest.mark.parametrize("family_id", FIXTURE_NAME_VARIANTS)
def test_a_fixture_name_is_downgraded_to_fixture(tmp_path: Path, family_id: str) -> None:
    family = toy.Family(tmp_path, "generated-toy-linear")
    profile = replace(family.profile, family_id=family_id)
    assert profile.is_fixture and fam.is_fixture_family_id(family_id)
    results = {check: True for check in fam.required_checks(profile)}
    record = fam.build_record(profile, fam.FixtureKind.INSTALLED_MODEL, results)
    assert record.tier is EvidenceTier.FIXTURE and not record.qualified
    assert "generated_family_is_never_a_qualified_model" in record.external_gates
    for tier in _QUALIFIED_TIERS:
        with pytest.raises(fam.FamilyError, match="EVIDENCE_TIER_UNSUPPORTED"):
            _claim(tmp_path, tier, family_id=family_id)


@pytest.mark.parametrize(
    "family_id",
    [
        "structural-pinned-profile",
        "smollm2_135m_lora_v1",
        "pythia_160m_neox_candidate",
        "acme-linear-v2",
        "degen-x",
        "family.invalidation.example",
        "invalid-runs-family",
    ],
)
def test_a_name_without_a_fixture_marker_keeps_software_qualified(
    tmp_path: Path, family_id: str
) -> None:
    family = toy.Family(tmp_path, "generated-toy-linear")
    assert not replace(family.profile, family_id=family_id).is_fixture
    assert not fam.is_fixture_family_id(family_id)
    assert _claim(tmp_path, EvidenceTier.SOFTWARE_QUALIFIED, family_id=family_id).qualified


@pytest.mark.parametrize(
    "name",
    [
        *FIXTURE_NAME_VARIANTS,
        *GENERATED_VARIANTS,
        NFKC_CLEAN_ID,
        "structural-pinned-profile",
        "smollm2_135m_lora_v1",
        "acme-linear-v2",
        "family.invalidation.example",
        None,
        5,
        b"structural-pinned-profile",
        _PlainStrSubclass("structural-pinned-profile"),
    ],
)
def test_the_family_downgrade_matches_the_ope_509_trainer_downgrade(name: object) -> None:
    # One rule across lanes: the model-family downgrade agrees with #218's trainer downgrade.
    assert fam.is_fixture_family_id(name) is ctc.is_fixture_trainer(name)


def test_the_real_profiles_are_not_downgraded_by_the_name_rule() -> None:
    for name in ("smollm2_135m_lora_v1.family.json", "pythia_160m_neox_candidate.family.json"):
        assert not fam.load_profile(PROFILES / name).is_fixture


def test_an_id_that_is_clean_only_after_nfkc_follows_the_trainer_rule(tmp_path: Path) -> None:
    # Review F2: the NFKC step decides this id. #218 keeps it (no fixture token once folded), so
    # the family rule must too; dropping NFKC would turn it into a fixture and break parity.
    assert ctc.is_fixture_trainer(NFKC_CLEAN_ID) is False
    assert fam.is_fixture_family_id(NFKC_CLEAN_ID) is False
    family = toy.Family(tmp_path, "generated-toy-linear")
    assert not replace(family.profile, family_id=NFKC_CLEAN_ID).is_fixture
    assert _claim(tmp_path, EvidenceTier.SOFTWARE_QUALIFIED, family_id=NFKC_CLEAN_ID).qualified


@pytest.mark.parametrize("tier", _WITNESSED_TIERS)
def test_a_record_that_bypassed_construction_is_never_qualified_at_a_witnessed_tier(
    tmp_path: Path, tier: EvidenceTier
) -> None:
    # Review F3: ``object.__new__``, ``object.__setattr__`` and unpickling all skip
    # ``__post_init__``. ``qualified`` must still never report a witnessed tier as qualified.
    valid = _claim(tmp_path, EvidenceTier.SOFTWARE_QUALIFIED, witness_ref="witness:acme-1")
    assert valid.qualified
    forged = object.__new__(fam.QualificationRecord)
    for field in fields(valid):
        object.__setattr__(forged, field.name, getattr(valid, field.name))
    object.__setattr__(forged, "tier", tier)
    assert forged.passed and forged.tier is tier
    assert not forged.qualified
    revived = pickle.loads(pickle.dumps(forged))
    assert revived.passed and revived.tier is tier
    assert not revived.qualified
    tampered = _claim(tmp_path, EvidenceTier.SOFTWARE_QUALIFIED, witness_ref="witness:acme-1")
    object.__setattr__(tampered, "tier", tier)
    assert tampered.passed and not tampered.qualified
