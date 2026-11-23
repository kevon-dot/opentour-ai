import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

ROOT = Path(__file__).resolve().parents[4]
CONTRACTS = ROOT / "packages" / "contracts"
INVITATION = json.loads((CONTRACTS / "platform" / "invitation.schema.json").read_text())
COMMON = json.loads((CONTRACTS / "common" / "common-contract.schema.json").read_text())
REGISTRY = Registry().with_resource(COMMON["$id"], Resource.from_contents(COMMON))


def validator(definition: str) -> Draft202012Validator:
    schema = {**INVITATION, "$ref": f"#/$defs/{definition}"}
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, registry=REGISTRY)


@pytest.fixture
def receipt() -> dict[str, object]:
    return {
        "invitation_id": "invite_generated",
        "membership_id": "member_generated",
        "operation_id": "operation_generated",
        "state": "awaiting_identity",
        "revision": "9007199254740993",
        "request_id": "request_generated",
    }


@pytest.mark.parametrize("token", ["generated", "x" * 512])
def test_accept_request_accepts_opaque_token(token: str) -> None:
    validator("AcceptRequest").validate({"token": token})


@pytest.mark.parametrize(
    "body",
    [
        None,
        [],
        "generated",
        {},
        {"invitation_token": "generated"},
        {"token": "generated", "invitation_id": "invite_generated"},
        {"token": "generated", "organization_id": "other_org"},
        {"token": ""},
        {"token": "x" * 513},
        {"token": 1},
        {"token": "generated\u0000"},
        {"token": "generated\n"},
        {"token": "generated\u007f"},
        {"token": "generated\u0085"},
    ],
)
def test_accept_request_rejects_invalid_body(body: object) -> None:
    assert not validator("AcceptRequest").is_valid(body)


@pytest.mark.parametrize(
    ("state", "operation_id", "receipt_definition"),
    [
        ("awaiting_identity", "operation_generated", "AcceptPendingReceipt"),
        ("active", "operation_generated", "AcceptConfirmedReceipt"),
        ("already_member", None, "AcceptConfirmedReceipt"),
    ],
)
def test_receipt_is_only_valid_for_its_http_status(
    receipt: dict[str, object],
    state: str,
    operation_id: str | None,
    receipt_definition: str,
) -> None:
    receipt.update(state=state, operation_id=operation_id)
    validator(receipt_definition).validate(receipt)
    other = (
        "AcceptConfirmedReceipt"
        if receipt_definition == "AcceptPendingReceipt"
        else "AcceptPendingReceipt"
    )
    assert not validator(other).is_valid(receipt)


@pytest.mark.parametrize(
    "changed_fields",
    [
        {"operation_id": None},
        {"state": "active", "operation_id": None},
        {"state": "already_member", "operation_id": "operation_generated"},
        {"state": "provider_outcome_unknown"},
        {"invitation_id": ""},
        {"membership_id": ""},
        {"operation_id": ""},
        {"request_id": ""},
        {"revision": 1},
        {"revision": "01"},
        {"revision": "9223372036854775808"},
        {"token": "generated"},
        {"workspace_id": "workspace_generated"},
    ],
)
def test_receipt_rejects_invalid_projection(
    receipt: dict[str, object], changed_fields: dict[str, object]
) -> None:
    assert not validator("AcceptReceipt").is_valid({**receipt, **changed_fields})


def test_receipt_requires_all_six_fields(receipt: dict[str, object]) -> None:
    accept_receipt = validator("AcceptReceipt")
    for field in receipt:
        assert not accept_receipt.is_valid(
            {key: value for key, value in receipt.items() if key != field}
        )


@pytest.mark.parametrize("body", [None, [], "active"])
def test_receipt_requires_an_object(body: object) -> None:
    assert not validator("AcceptReceipt").is_valid(body)
