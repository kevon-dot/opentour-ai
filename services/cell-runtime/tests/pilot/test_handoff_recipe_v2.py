"""Generated static mapping checks; no provider import, connection or request executes."""

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

RECIPE = Path(__file__).resolve().parents[2] / "integrations/zapier/handoff-recipe-v2.json"
RELEASE = "release-" + "a" * 48


def recipe() -> dict:
    return json.loads(RECIPE.read_text())


def test_mapping_keeps_connection_authority_out_of_dynamic_inputs() -> None:
    value = recipe()
    connection, action = value["connection"], value["action"]
    assert connection["app"] == action["app"] == "API by Zapier"
    assert connection["credential_location"] == "dedicated_app_connection_only"
    assert connection["authentication_type"] == "Static Headers (API key)"
    assert set(connection["static_headers"]) == {"Authorization", "X-OpenTour-Audience"}
    assert connection["wildcard_domains"] is connection["shared_with_intake"] is False
    assert connection["test_url"] is None
    assert connection["domain_filter"] == "{exact_owner_bound_handoff_hostname}"
    fields = action["builder_fields"]
    assert fields["Method"] == "POST"
    assert fields["URL"] == "{owner_bound_https_handoff_origin}/v1/handoff/dispatch"
    assert fields["Query String Parameters"] == {}
    assert fields["Headers"] == {"Content-Type": "application/json"}
    assert action["dynamic_input_fields"] == value["trigger"]["fields"] == ["release_ref"]
    assert action["caller_supplied_origin_required"] is False
    assert value["trigger"]["app"] == "Webhooks by Zapier"
    assert value["trigger"]["event"] == "Catch Hook"
    assert value["trigger"]["source"] == "committed_worker_release_outbox"
    assert value["trigger"]["alias"] == "release_outbox"
    assert action["alias"] == "dispatch_handoff" and action["event"] == "API Request"
    body = json.loads(fields["Body"].replace("{{release_outbox.release_ref}}", RELEASE))
    assert body == {"release_ref": RELEASE}
    Draft202012Validator.check_schema(value["request_schema"])
    Draft202012Validator(value["request_schema"]).validate(body)


@pytest.mark.parametrize(
    "body",
    [
        {},
        [],
        {"release_ref": 111},
        {"release_ref": RELEASE + "\n"},
        {"release_ref": RELEASE.upper()},
        {"release_ref": {"reference": RELEASE}},
        {"release_ref": RELEASE, "actor": "dispatcher"},
        {"release_ref": RELEASE, "tenant": "generated-other"},
        {"release_ref": RELEASE, "destination": "generated-other"},
        {"release_ref": RELEASE, "artifact": "generated-private-artifact"},
        {"release_ref": RELEASE, "approval": "generated-approval"},
        {"release_ref": RELEASE, "activate": True},
        {"release_ref": RELEASE, "url": "https://other.invalid"},
    ],
)
def test_wire_schema_rejects_missing_malformed_and_authority_fields(body: object) -> None:
    assert not Draft202012Validator(recipe()["request_schema"]).is_valid(body)


def test_mapping_preserves_pending_delivery_and_disabled_proof_level() -> None:
    value = recipe()
    assert value["revision"] == "opentour.approved-handoff.v2"
    assert value["artifact_kind"] == "manual_builder_field_map"
    for key in (
        "enabled_by_default",
        "production_activation",
        "provider_import_qualified",
        "installed_provider_witness",
    ):
        assert value[key] is False
    assert value["evidence_level"] == "generated_local_contract"
    assert value["response"]["schema_version"] == "opentour.pilot.handoff-dispatch.v1"
    assert set(value["response"]["fields"]) == {
        "schema_version",
        "release_ref",
        "state",
        "receiver_receipt_ref",
        "owner_action",
    }
    assert value["response"]["states"] == ["queued", "delivery_unconfirmed", "delivered"]
    assert value["response"]["denial"] == {"error": "private_handoff_unavailable"}
    assert value["recovery"]["zap_receiver_operations"] == []
    assert value["recovery"]["retry_identity"] == "same_release_ref"
    assert (
        value["recovery"]["unknown_outcome"]
        == "worker_reconciles_independent_receiver_state_or_holds"
    )
    assert value["recovery"]["delivery_requires"] == "worker_verified_independent_receiver_receipt"
