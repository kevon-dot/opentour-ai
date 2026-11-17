"""Verify that evaluation tooling consumes the one registered inference contract."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from tools import schema_validate

URI = "urn:opentour:contracts:learning:statistical-inference:v1"
COMMON_URI = "urn:opentour:common-contract:1.0"
SCHEMA = "packages/contracts/learning/benchmarks/statistical-design.schema.json"
COMMON = "packages/contracts/common/common-contract.schema.json"
FIXTURE = "training/tests/evaluation/fixtures/precision-numeric-fixtures.json"


@pytest.mark.parametrize(
    "path,hash_value,uri",
    [
        (SCHEMA, "76fa494afea46f7bb9f505838d1269320b4aacaaa010b5545731e223affd2cda", URI),
        (COMMON, "2d78b380cf3c924683d11549a1c455e2287ac5d1b30eb4007b24eb2aa48775fe", COMMON_URI),
        (FIXTURE, "43d9e9de30242ba079eff565e1b06827ce1554363cdf44450789e449edf6b01a", None),
    ],
)
def test_contract_bytes_are_exact(path: str, hash_value: str, uri: str | None) -> None:
    data = (schema_validate.ROOT / Path(path)).read_bytes()
    assert hashlib.sha256(data).hexdigest() == hash_value
    if uri is not None:
        assert json.loads(data)["$id"] == uri


def _case_plan() -> dict[str, Any]:
    reference = {
        "artifact_id": "generated-artifact",
        "kind": "synthetic_fixture",
        "revision": "v1",
        "sha256": "a" * 64,
        "manifest_id": "generated-manifest",
        "environment_id": "generated-environment",
        "classification": "control_metadata",
        "purpose_grant_id": "generated-grant",
    }
    return {
        "independent_unit": "case",
        "cluster_method_ref": None,
        "paired_effect_method": "paired_binary_exact_marginals_v1",
        "confidence_level": 0.95,
        "effect_interval_sides": 2,
        "retention_interval_sides": 1,
        "critical_bound_method": "binomial_clopper_pearson_one_sided_v1",
        "multiplicity_policy_ref": reference,
        "stopping_mode": "fixed_final",
        "look_plan": [
            {
                "look_id": "final",
                "cumulative_independent_units": 200,
                "claim_alpha": {"generated-claim": 0.05},
            }
        ],
        "repetitions": 1,
        "nuisance_assumptions_ref": reference,
    }


@pytest.mark.parametrize("family", [False, True])
def test_registry_resolves_inference_plan_and_common_references(family: bool) -> None:
    plan = _case_plan()
    if family:
        plan["independent_unit"] = "family"
        plan["paired_effect_method"] = "bounded_independent_family_hoeffding_v1"
        plan["repetitions"] = 3
        plan["cluster_method_ref"] = plan["multiplicity_policy_ref"]
    registry = schema_validate.schema_registry()
    assert schema_validate.valid_instance(URI, plan, registry)
    plan["nuisance_assumptions_ref"] = {"artifact_id": "generated-artifact"}
    assert not schema_validate.valid_instance(URI, plan, registry)


def test_only_the_canonical_schema_is_registered() -> None:
    manifest = json.loads(
        (schema_validate.ROOT / COMMON).with_name("schema-registry.json").read_bytes()
    )
    matching = [item for item in manifest["schemas"] if item["id"] == URI]
    assert len(matching) == 1
    assert matching[0]["file"] == SCHEMA
