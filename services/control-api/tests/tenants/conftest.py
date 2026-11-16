"""Generated 171 ports plus the actual qualified 142 verifier fixture.

Memory receipts test handler composition, not durable SQL or real WorkOS behavior.
The unresolved admission-count policy is deliberately not implemented here.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentour_control.tenants.create import OrganizationService, create_organization_router
from opentour_control.tenants.models import (
    CreateReceipt,
    CreateRequest,
    Creator,
    Operation,
    OperationView,
    PublishedTerms,
    SetupError,
    correlation,
)
from opentour_control.tenants.provider import MembershipObservation, OrganizationObservation

# Reuse the owning RSA/token/session fixture, without registering a non-root plugin
# or introducing a second identity verifier. This path is fixed repository source.
_path = Path(__file__).parents[1] / "auth/conftest.py"
_spec = importlib.util.spec_from_file_location("generated_171_identity_fixtures", _path)
assert _spec is not None and _spec.loader is not None
_identity_fixtures = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_identity_fixtures)
identity = _identity_fixtures.identity
signing_key = _identity_fixtures.signing_key


@pytest.fixture
def operation(identity) -> Operation:
    return Operation(
        operation_id="operation_generated",
        organization_id="organization_generated",
        creator_issuer=identity.issuer,
        creator_subject="user_a",
        display_name="Generated buyer",
        terms_revision="terms_generated",
        correlation=correlation("organization_generated", "operation_generated"),
        provider_environment="env_generated",
        state="identity_provisioning",
        revision=1,
        stage="organization",
        stage_outcome="not_dispatched",
        provider_org_id=None,
        provider_membership_id=None,
        reason_code=None,
        created_at=identity.now,
        updated_at=identity.now,
    )


class ReceiptPort:
    """Recorded-result stub; database invariants are exercised by the owning PG suite."""

    def __init__(self, operation: Operation) -> None:
        self.operation = operation
        self.published: PublishedTerms | None = PublishedTerms(
            "terms_generated",
            "https://terms.example.invalid/generated",
            operation.created_at,
        )
        self.recorded: tuple[str, str, CreateReceipt] | None = None
        self.admissions: list[tuple[Creator, CreateRequest, str, datetime]] = []
        self.changes: list[tuple[str, str, str, str]] = []
        self.failure: Exception | None = None

    def terms(self, now: datetime) -> PublishedTerms:
        if self.failure:
            raise self.failure
        if self.published is None:
            raise SetupError("TERMS_UNAVAILABLE", 503)
        return self.published

    def replay(self, creator: Creator, key_hash: str, body_hash: str) -> CreateReceipt | None:
        if self.recorded and self.recorded[0] == key_hash:
            if self.recorded[1] != body_hash:
                raise SetupError("IDEMPOTENCY_CONFLICT", 409)
            return self.recorded[2]
        return None

    def create(
        self,
        creator: Creator,
        body: CreateRequest,
        key_hash: str,
        request_id: str,
        provider_environment: str,
        now: datetime,
    ) -> CreateReceipt:
        if self.failure:
            raise self.failure
        self.admissions.append((creator, body, provider_environment, now))
        receipt = CreateReceipt(
            organization_id=self.operation.organization_id,
            operation_id=self.operation.operation_id,
            request_id=request_id,
        )
        self.recorded = (key_hash, body.digest(), receipt)
        return receipt

    def get(self, creator: Creator, operation_id: str) -> Operation:
        if self.failure:
            raise self.failure
        if operation_id != self.operation.operation_id:
            raise SetupError("RESOURCE_NOT_FOUND", 404)
        return self.operation

    def change(
        self, creator, operation_id, action, expected_revision, key_hash, request_id, now
    ) -> OperationView:
        if self.failure:
            raise self.failure
        self.changes.append((operation_id, action, expected_revision, key_hash))
        return replace(
            self.operation,
            revision=2,
            state="cancelled" if action == "cancel" else self.operation.state,
        ).view(request_id)


class VerifiedEmailPort:
    environment = "env_generated"

    def __init__(self) -> None:
        self.verified = True
        self.checked: list[str] = []
        self.during_check: Callable[[], None] | None = None

    def email_verified(self, subject: str) -> bool:
        self.checked.append(subject)
        if self.during_check:
            self.during_check()
        return self.verified

    def find_organization(self, operation: Operation) -> OrganizationObservation | None:
        raise AssertionError("Request admission must not reconcile provider mutations")

    def create_organization(self, operation: Operation) -> OrganizationObservation:
        raise AssertionError("Request admission must not perform provider mutations")

    def find_membership(self, operation: Operation) -> MembershipObservation | None:
        raise AssertionError("Request admission must not reconcile provider memberships")

    def create_membership(self, operation: Operation) -> MembershipObservation:
        raise AssertionError("Request admission must not perform provider membership mutations")


@pytest.fixture
def setup(identity, operation):
    repository, provider = ReceiptPort(operation), VerifiedEmailPort()
    service = OrganizationService(
        identity.service, repository, provider, clock=lambda: identity.now
    )
    app = FastAPI()
    app.include_router(create_organization_router(service))
    with TestClient(app) as client:
        client.cookies.set("opentour-session", identity.handle)
        client.headers.update(
            {"Authorization": "Bearer " + identity.token(), "Idempotency-Key": "generated-key"}
        )
        yield SimpleNamespace(
            identity=identity,
            repository=repository,
            provider=provider,
            service=service,
            client=client,
            operation=operation,
        )
