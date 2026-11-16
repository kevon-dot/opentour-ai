"""Closed 171 wire values and pending/unknown projections; simulated fixtures only."""

from dataclasses import replace
from datetime import timedelta

import pytest
from opentour_control.tenants.models import (
    MAX_REVISION,
    CreateRequest,
    Creator,
    RevisionRequest,
    SetupError,
    TermsView,
    advance_revision,
    correlation,
    decode_create,
    key_digest,
)
from pydantic import ValidationError


def test_normalized_name_has_stable_body_digest_and_preserves_internal_spaces():
    first = decode_create(
        b'{"display_name":"  Cafe\\u0301  Team  ","accepted_terms_revision":"terms_generated"}'
    )
    second = CreateRequest(display_name="Café  Team", accepted_terms_revision="terms_generated")
    assert first.display_name == "Café  Team" and first.digest() == second.digest()
    assert len(first.digest()) == 64


@pytest.mark.parametrize(
    "raw",
    [
        b"{}",
        b"[]",
        b"null",
        b'{"display_name":"x"}',
        b'{"display_name":"x","display_name":"y","accepted_terms_revision":"t"}',
        b'{"display_name":"x","accepted_terms_revision":"t","organization_id":"other"}',
        b'{"display_name":7,"accepted_terms_revision":"t"}',
        b'{"display_name":"\\nGenerated","accepted_terms_revision":"t"}',
        b'{"display_name":"Generated\\u0000","accepted_terms_revision":"t"}',
        b'{"display_name":"\\ud800","accepted_terms_revision":"t"}',
        b'{"display_name":"Generated","accepted_terms_revision":"\\ud800"}',
        b'{"display_name":"Generated","accepted_terms_revision":1}',
        b"{" + b" " * 16_384 + b"}",
    ],
)
def test_create_refuses_missing_duplicate_coerced_or_unsafe_input(raw):
    with pytest.raises(SetupError, match="SCHEMA_INVALID"):
        decode_create(raw)


@pytest.mark.parametrize("name", ["", " ", "a" * 121])
def test_display_name_bounds_are_checked_after_normalization(name):
    with pytest.raises(ValidationError):
        CreateRequest(display_name=name, accepted_terms_revision="terms_generated")
    assert (
        len(CreateRequest(display_name="a" * 120, accepted_terms_revision="t").display_name) == 120
    )


@pytest.mark.parametrize(
    "value", [1, True, "0", "01", "+1", "1.0", "1e3", " 1", "9223372036854775808"]
)
def test_revision_rejects_coercion_syntax_and_bigint_overflow(value):
    with pytest.raises(ValidationError):
        RevisionRequest(expected_revision=value)


def test_large_revision_keeps_exact_string_and_exhaustion_has_no_next_value():
    assert (
        RevisionRequest(expected_revision="9007199254740993").expected_revision
        == "9007199254740993"
    )
    assert RevisionRequest(expected_revision=str(MAX_REVISION)).expected_revision == str(
        MAX_REVISION
    )
    with pytest.raises(SetupError, match="REVISION_EXHAUSTED") as error:
        advance_revision(MAX_REVISION)
    assert error.value.status == 409 and error.value.current_revision == str(MAX_REVISION)


@pytest.mark.parametrize("key", ["", " ", "x\n", "é", "x" * 257])
def test_idempotency_key_rejects_unsafe_or_unbounded_values(key):
    with pytest.raises(SetupError, match="SCHEMA_INVALID"):
        key_digest(key)


def test_correlation_is_opaque_bounded_and_intent_specific():
    a = correlation("organization_a", "operation_a")
    assert len(a) == 63 and a.startswith("ot_") and "organization" not in a
    assert a == correlation("organization_a", "operation_a")
    assert a != correlation("organization_b", "operation_a")
    assert a != correlation("organization_a", "operation_b")


@pytest.mark.parametrize(
    "outcome,provider_org,expected",
    [
        ("not_dispatched", None, "not_dispatched"),
        ("rejected", None, "known_no_effect"),
        ("observed", "provider_generated", "known_retained"),
        ("unknown", "provider_generated", "outcome_unknown"),
        ("dispatched", None, "outcome_unknown"),
    ],
)
def test_cancellation_preserves_remote_uncertainty_without_claiming_external_delete(
    operation, outcome, provider_org, expected
):
    view = replace(
        operation, state="cancelled", stage_outcome=outcome, provider_org_id=provider_org
    ).view("request_generated")
    assert view.remote_effect_state == expected
    assert not view.retry_available and not view.cancel_available
    assert "provider_org_id" not in view.model_dump() and "workspace_id" not in view.model_dump()


def test_foreign_owner_and_unknown_reason_do_not_expose_current_state(operation):
    with pytest.raises(SetupError, match="RESOURCE_NOT_FOUND") as error:
        operation.belongs_to(
            Creator(operation.creator_issuer, "foreign_user", "session_generated", 0)
        )
    assert error.value.wire("request_generated")["current_revision"] is None
    with pytest.raises(ValidationError):
        replace(operation, reason_code="generated raw private failure").view("request_generated")


@pytest.mark.parametrize(
    "url",
    [
        "http://terms.example.invalid",
        "https://" + "user:pass@terms.example.invalid",  # Constructed sentinel credentials
        "https://terms.example.invalid/#secret",
        "https://terms.example.invalid/\n",
    ],
)
def test_terms_reject_unsafe_urls(url):
    with pytest.raises(ValidationError):
        TermsView(
            revision="terms_generated", document_url=url, effective_at="2026-11-15T12:00:00.000Z"
        )


def test_future_terms_are_not_published_and_timestamp_is_millisecond_utc(setup):
    terms = setup.repository.published
    assert terms is not None
    with pytest.raises(SetupError, match="TERMS_UNAVAILABLE"):
        terms.current_view(terms.effective_at - timedelta(microseconds=1))
    assert terms.current_view(terms.effective_at).effective_at.endswith(".000Z")
