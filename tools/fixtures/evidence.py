"""C03 observation boundary; OPE-126 owns durable empirical aggregation."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from referencing import Registry

from tools import schema_validate
from tools.fixtures.ports import FixtureRefusal
from tools.fixtures.registry import ISSUER


def validate_observations(document: Any) -> None:
    schema = schema_validate.read_json(Path(__file__).with_name("observation.schema.json"))
    Draft202012Validator.check_schema(schema)
    registry: Registry[Any] = Registry(retrieve=schema_validate.deny_retrieval)  # type: ignore[call-arg]
    if not Draft202012Validator(schema, registry=registry).is_valid(document):
        raise FixtureRefusal("INVALID_LOCAL_OBSERVATIONS")


def route_empirical(receipt: dict[str, Any], sink: Callable[[dict[str, Any]], None]) -> None:
    """Reject fixture evidence before an independently owned real sink is called.

    This filter does not authenticate real receipts. A real sink must additionally
    validate its provider authority and evidence. No such sink ships in C03.
    """
    if (
        receipt.get("kind") not in {"provider_integration", "customer_evidence"}
        or str(receipt.get("issuer", "")).startswith("simulated:")
        or str(receipt.get("resource_namespace", "")).startswith("simulated:")
        or receipt.get("product_acceptance") == "NOT_RUN"
    ):
        raise FixtureRefusal("SIMULATED_EVIDENCE_FORBIDDEN")
    sink(receipt)


def observation(
    fixture_id: str,
    version: str,
    seed: int,
    mode: str,
    kind: str,
    effects: list[str],
    ledger_entries: list[str],
) -> dict[str, Any]:
    return {
        "fixture_id": fixture_id,
        "fixture_version": version,
        "seed": seed,
        "mode": mode,
        "kind": kind,
        "issuer": ISSUER,
        "resource_namespace": ISSUER,
        "observed_effects": effects,
        "observed_ledger_entries": ledger_entries,
        "product_acceptance": "NOT_RUN",
        "limitations": [
            "Generated data and scripted provider observations; no production keys or signatures",
            "Local persistence is not DynamoDB/IAM/provider qualification",
        ],
    }
