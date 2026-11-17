"""Durable control authority coordination. SQL and audit HTTP are separate commits.

The journal client performs one bounded network attempt. This module owns its
SQL claim/token fence and immutable original bytes; timeout is never an abort.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal, Protocol, Self

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from opentour_control.auth.config import AuthError, identifier
from opentour_control.auth.grant_models import (
    AccessGrant,
    ClosedModel,
    CreateGrant,
    GrantCreationReceipt,
    GrantError,
    GrantOperationView,
    InitialAuthorityPolicy,
    ResourceScope,
    instant,
    server_time,
    timestamp,
)
from opentour_control.auth.grant_repository import (
    CompletedOrganizationFact,
    Connection,
    PostgresGrantRepository,
    RepositoryScope,
)
from opentour_control.auth.grants import (
    AuthorityState,
    Decision,
    DelegationPlan,
    GrantAncestry,
    GrantResolver,
    PrincipalRecord,
)
from opentour_control.auth.session import AuthContext, SessionService, VerifiedIdentityContext
from opentour_control.tenants.models import SetupError, key_digest
from opentour_journal.crypto import JournalVerifier, canonical, genesis_digest, sha256
from opentour_journal.models import (
    MAX_COUNTER,
    MAX_REVISION,
    AppendIntent,
    AppendOutcome,
    AppendResponse,
    Counter,
    HeadState,
    Id,
    ImmutableMetadataRef,
    JournalError,
    JournalIdentity,
    MutationKind,
    Sha256,
    strict_json,
)
from opentour_journal.ports import (
    ApprovedRegistration,
    AuthorizedScope,
    ReconciledTransaction,
    ResolvedIntent,
    ServicePrincipal,
)
from opentour_journal.replay import EvidenceWriter, VerifiedAuthorityFrontier
from psycopg.types.json import Jsonb
from pydantic import BeforeValidator, Field, ValidationError, model_validator
from starlette.concurrency import run_in_threadpool

from .journal_client import JournalClient, RetryBinding, RetryClaim


class BoundIntentSource(ClosedModel):
    """Immutable canonical reservation, authored in the same SQL transaction."""

    organization_id: Id
    operation_id: Id
    request_sha256: Sha256
    plan_sha256: Sha256
    actor_ref: ImmutableMetadataRef
    mutation_kind: MutationKind
    proposed_epoch: Counter
    proposed_authority_revision: Id
    expected_head_sequence: Counter
    expected_head_digest: Sha256


class IntentSource(BoundIntentSource):
    schema_version: Literal["opentour.grant-intent-source.v1"]
    grant_id: Id
    proposed_grant_sha256: Sha256


class CreatorIntentSource(BoundIntentSource):
    schema_version: Literal["opentour.creator-installation-intent.v1"]
    grant_manifest_sha256: Sha256


class ActorSource(ClosedModel):
    schema_version: Literal["opentour.grant-actor-source.v1"]
    organization_id: Id
    operation_id: Id
    issuer: Id
    subject: Id
    session_id: Id
    session_generation: Counter
    membership_id: Id


class BoundTransactionSource(ClosedModel):
    """Actual SQL result, never a timeout/absence inference or caller boolean."""

    organization_id: Id
    operation_id: Id
    intent_content_sha256: Sha256
    result: Literal["committed", "aborted"]
    resulting_epoch: Counter
    resulting_authority_revision: Id


class TransactionSource(BoundTransactionSource):
    schema_version: Literal["opentour.grant-transaction-source.v1"]
    grant_id: Id
    proposed_grant_sha256: Sha256


class CreatorTransactionSource(BoundTransactionSource):
    schema_version: Literal["opentour.creator-installation-transaction.v1"]
    grant_manifest_sha256: Sha256


def _manifest_grants(value: object) -> object:
    return tuple(value) if isinstance(value, list) else value


class CreatorGrantManifest(ClosedModel):
    grants: Annotated[
        tuple[AccessGrant, ...],
        BeforeValidator(_manifest_grants),
        Field(min_length=1, max_length=50),
    ]

    @model_validator(mode="after")
    def unique_prepared(self) -> Self:
        ids = {g.grant_id for g in self.grants}
        if len(ids) != len(self.grants) or any(g.state != "prepared" for g in self.grants):
            raise ValueError("Creator manifest requires unique prepared grants")
        return self


@dataclass(frozen=True)
class JournalCallerContext:
    principal: ServicePrincipal
    authorization: AuthorizedScope
    sql_scope: RepositoryScope


class JournalCallerAuthority(Protocol):
    """145 composition authenticates current service registration and exact tenant.

    This required port must verify the principal's immutable authentication ref,
    current service/capability registration and server-derived SQL identity. Neither
    an HTTP caller nor a source-record ID may supply its returned context.
    """

    def current(
        self,
        principal: ServicePrincipal,
        organization_id: str,
        capability: Literal["append", "read", "enroll"],
    ) -> JournalCallerContext: ...


class JournalEnrollmentAuthority(Protocol):
    """Trusted approved enrollment source; no policy/default inferred from a role."""

    def current(
        self,
        principal: ServicePrincipal,
        organization_id: str,
    ) -> ApprovedRegistration: ...


class PostgresAuthorityResolver:
    """Production SQL source adapter for217's pure AuthorityResolver interface.

    Required service authentication/enrollment ports are composed server-side.
    No public resolver endpoint, AWS import, signing key or migration role is used.
    Historical SQL outcomes remain immutable after later membership/revocation changes.
    """

    def __init__(
        self,
        repository: PostgresGrantRepository,
        callers: JournalCallerAuthority,
        enrollments: JournalEnrollmentAuthority,
    ) -> None:
        if repository.role != "coordinator":
            raise JournalError("JOURNAL_CONFIGURATION_INVALID", 503)
        self.repository, self.callers, self.enrollments = repository, callers, enrollments

    def _caller(
        self,
        principal: ServicePrincipal,
        organization_id: str,
        capability: Literal["append", "read", "enroll"],
    ) -> JournalCallerContext:
        try:
            context = self.callers.current(principal, organization_id, capability)
            if (
                not isinstance(context, JournalCallerContext)
                or context.principal != principal
                or context.authorization
                != AuthorizedScope(principal.service_id, organization_id, capability)
                or context.sql_scope.organization_id != organization_id
            ):
                raise JournalError("JOURNAL_FORBIDDEN", 403)
            return context
        except Exception:
            raise JournalError("JOURNAL_FORBIDDEN", 403) from None

    def authorize(
        self,
        principal: ServicePrincipal,
        organization_id: str,
        capability: Literal["append", "read", "enroll"],
    ) -> AuthorizedScope:
        return self._caller(principal, organization_id, capability).authorization

    @staticmethod
    def _source(
        conn: Connection,
        organization_id: str,
        operation_id: str,
        kind: str,
        reference: ImmutableMetadataRef,
    ) -> dict[str, Any]:
        row = conn.execute(
            "SELECT operation_id,kind,revision,sha256,body FROM opentour_authority.sources "
            "WHERE organization_id=%s AND record_id=%s",
            (organization_id, reference.record_id),
        ).fetchone()
        if row is None or (row["operation_id"], row["kind"], row["revision"], row["sha256"]) != (
            operation_id,
            kind,
            reference.revision,
            reference.sha256,
        ):
            raise JournalError("JOURNAL_RECONCILIATION_REQUIRED", 503)
        raw = bytes(row["body"])
        value = strict_json(raw)
        if (
            not isinstance(value, dict)
            or canonical(value) != raw
            or sha256(raw) != reference.sha256
        ):
            raise JournalError("JOURNAL_RECONCILIATION_REQUIRED", 503)
        return value

    def _intent(
        self,
        conn: Connection,
        body: AppendIntent,
    ) -> tuple[dict[str, Any], ResolvedIntent]:
        operation = self.repository.retained_operation(
            conn, body.organization_id, body.operation_id
        )
        raw = canonical(body.wire())
        if bytes(operation["intent_body"]) != raw:
            raise JournalError("JOURNAL_OPERATION_CONFLICT", 409)
        source_value = self._source(
            conn, body.organization_id, body.operation_id, "intent", body.scoped_intent_ref
        )
        actor = ActorSource.model_validate(
            self._source(conn, body.organization_id, body.operation_id, "actor", body.actor_ref)
        )
        binding: dict[str, Any] = dict(
            organization_id=body.organization_id,
            operation_id=body.operation_id,
            request_sha256=sha256(canonical(operation["request_body"])),
            plan_sha256=sha256(canonical(operation["plan"])),
            actor_ref=body.actor_ref,
            mutation_kind=body.mutation_kind,
            proposed_epoch=operation["proposed_epoch"],
            proposed_authority_revision=operation["proposed_authority_revision"],
            expected_head_sequence=body.expected_head_sequence,
            expected_head_digest=body.expected_head_digest,
        )
        proposed_digest = sha256(canonical(operation["proposed_grant"]))
        source: BoundIntentSource
        expected: BoundIntentSource
        if operation["operation_kind"] == "creator_installation":
            source = CreatorIntentSource.model_validate(source_value)
            expected = CreatorIntentSource(
                schema_version="opentour.creator-installation-intent.v1",
                grant_manifest_sha256=proposed_digest,
                **binding,
            )
        elif operation["operation_kind"] == "access_grant_create":
            source = IntentSource.model_validate(source_value)
            expected = IntentSource(
                schema_version="opentour.grant-intent-source.v1",
                grant_id=operation["grant_id"],
                proposed_grant_sha256=proposed_digest,
                **binding,
            )
        else:
            raise JournalError("JOURNAL_RECONCILIATION_REQUIRED", 503)
        context = operation["context"]
        if source != expected or sha256(canonical(source.wire())) != body.canonical_intent_digest:
            raise JournalError("JOURNAL_RECONCILIATION_REQUIRED", 503)
        if actor != ActorSource(
            schema_version="opentour.grant-actor-source.v1",
            organization_id=body.organization_id,
            operation_id=body.operation_id,
            issuer=operation["issuer"],
            subject=operation["subject"],
            session_id=context["session_id"],
            session_generation=context["session_generation"],
            membership_id=context["membership_id"],
        ):
            raise JournalError("JOURNAL_RECONCILIATION_REQUIRED", 503)
        return operation, ResolvedIntent(
            body.organization_id,
            body.operation_id,
            source.mutation_kind,
            source.proposed_epoch,
            source.proposed_authority_revision,
            body.canonical_intent_digest,
            body.scoped_intent_ref,
            body.actor_ref,
        )

    def resolve_intent(self, principal: ServicePrincipal, body: AppendIntent) -> ResolvedIntent:
        caller = self._caller(principal, body.organization_id, "append")
        try:
            with self.repository.transaction(caller.sql_scope) as conn:
                _, resolved = self._intent(conn, body)
                return resolved
        except (GrantError, ValidationError, KeyError, TypeError, ValueError):
            raise JournalError("JOURNAL_RECONCILIATION_REQUIRED", 503) from None

    def reconcile_transaction(
        self,
        principal: ServicePrincipal,
        body: AppendOutcome,
        intent: AppendIntent,
    ) -> ReconciledTransaction:
        caller = self._caller(principal, body.organization_id, "append")
        if (body.organization_id, body.operation_id) != (
            intent.organization_id,
            intent.operation_id,
        ):
            raise JournalError("JOURNAL_OPERATION_CONFLICT", 409)
        try:
            with self.repository.transaction(caller.sql_scope) as conn:
                operation, _ = self._intent(conn, intent)
                if operation["outcome_body"] is None:
                    raise JournalError("JOURNAL_RECONCILIATION_REQUIRED", 503)
                if bytes(operation["outcome_body"]) != canonical(body.wire()):
                    raise JournalError("JOURNAL_OPERATION_CONFLICT", 409)
                source = self._transaction_source(conn, body, intent, operation)
                return ReconciledTransaction(
                    body.organization_id,
                    body.operation_id,
                    source.intent_content_sha256,
                    body.control_transaction_ref,
                    source.result,
                    source.resulting_epoch,
                    source.resulting_authority_revision,
                )
        except (GrantError, ValidationError, KeyError, TypeError, ValueError):
            raise JournalError("JOURNAL_RECONCILIATION_REQUIRED", 503) from None

    def _transaction_source(
        self,
        conn: Connection,
        body: AppendOutcome,
        intent: AppendIntent,
        operation: dict[str, Any],
    ) -> BoundTransactionSource:
        value = self._source(
            conn,
            body.organization_id,
            body.operation_id,
            "transaction",
            body.control_transaction_ref,
        )
        binding: dict[str, Any] = dict(
            organization_id=body.organization_id,
            operation_id=body.operation_id,
            intent_content_sha256=sha256(canonical(intent.wire())),
            result=body.result,
            resulting_epoch=body.resulting_epoch,
            resulting_authority_revision=body.resulting_authority_revision,
        )
        digest = sha256(canonical(operation["proposed_grant"]))
        source: BoundTransactionSource
        expected: BoundTransactionSource
        if operation["operation_kind"] == "creator_installation":
            source = CreatorTransactionSource.model_validate(value)
            expected = CreatorTransactionSource(
                schema_version="opentour.creator-installation-transaction.v1",
                grant_manifest_sha256=digest,
                **binding,
            )
            manifest = CreatorGrantManifest.model_validate(operation["proposed_grant"])
            expected_grants = {g.grant_id: g.digest() for g in manifest.grants}
            rows = conn.execute(
                "SELECT grant_id,body FROM opentour_authority.grants "
                "WHERE organization_id=%s AND creation_operation_id=%s LIMIT 51",
                (body.organization_id, body.operation_id),
            ).fetchall()
            actual_grants = {row["grant_id"]: sha256(canonical(row["body"])) for row in rows}
        else:
            source = TransactionSource.model_validate(value)
            expected = TransactionSource(
                schema_version="opentour.grant-transaction-source.v1",
                grant_id=operation["grant_id"],
                proposed_grant_sha256=digest,
                **binding,
            )
            expected_grants = {operation["grant_id"]: digest}
            grant = conn.execute(
                "SELECT creation_operation_id,body FROM opentour_authority.grants "
                "WHERE organization_id=%s AND grant_id=%s",
                (body.organization_id, operation["grant_id"]),
            ).fetchone()
            actual_grants = {}
            if grant is not None:
                if grant["creation_operation_id"] != body.operation_id:
                    raise JournalError("JOURNAL_RECONCILIATION_REQUIRED", 503)
                actual_grants[operation["grant_id"]] = sha256(canonical(grant["body"]))
        if source != expected:
            raise JournalError("JOURNAL_RECONCILIATION_REQUIRED", 503)
        if body.result == "committed":
            if actual_grants != expected_grants or (
                body.resulting_epoch,
                body.resulting_authority_revision,
            ) != (intent.proposed_epoch, intent.proposed_authority_revision):
                raise JournalError("JOURNAL_RECONCILIATION_REQUIRED", 503)
        # A proven abort is preserved in SQL before the independent outcome.
        elif actual_grants or operation["state"] == "succeeded":
            raise JournalError("JOURNAL_RECONCILIATION_REQUIRED", 503)
        return source

    def registration(
        self,
        principal: ServicePrincipal,
        organization_id: str,
    ) -> ApprovedRegistration:
        self._caller(principal, organization_id, "enroll")
        try:
            registration = self.enrollments.current(principal, organization_id)
            if not isinstance(registration, ApprovedRegistration) or (
                registration.organization_id != organization_id
            ):
                raise JournalError("JOURNAL_FORBIDDEN", 403)
            return registration
        except Exception:
            raise JournalError("JOURNAL_FORBIDDEN", 403) from None


def _next_revision(value: int) -> int:
    if type(value) is not int or not 1 <= value < MAX_REVISION:
        raise JournalError("JOURNAL_RETRY_STATE_EXHAUSTED", 409)
    return value + 1


@dataclass(frozen=True)
class RetryState:
    binding: RetryBinding
    state: Literal["unknown", "scheduled", "completed", "held"]
    token: str
    revision: str
    response: AppendResponse | None


class PostgresJournalRetryPort:
    """DurableRetryPort bound to one trusted operation owner/tenant context.

    Unknown/in-flight claims cannot be reacquired. An explicit coordinator
    reconciliation may fence the old SQL token and schedule the SAME bytes; it
    does not assert an old HTTP operation stopped or failed. The next journal
    client first reconciles the exact independently deduplicated operation.
    """

    def __init__(
        self,
        repository: PostgresGrantRepository,
        scope: RepositoryScope,
        verifier: JournalVerifier,
    ) -> None:
        if repository.role != "coordinator":
            raise JournalError("JOURNAL_RETRY_STATE_UNAVAILABLE", 503)
        self.repository, self.scope, self.verifier = repository, scope, verifier

    def _binding(self, binding: RetryBinding, body: bytes) -> AppendIntent | AppendOutcome:
        if binding.phase not in {"intent", "outcome"} or (
            binding.organization_id != self.scope.organization_id
            or sha256(body) != binding.body_sha256
        ):
            raise JournalError("JOURNAL_BINDING_INVALID", 409)
        parsed = (
            AppendIntent.parse(body) if binding.phase == "intent" else AppendOutcome.parse(body)
        )
        if (parsed.organization_id, parsed.operation_id) != (
            binding.organization_id,
            binding.operation_id,
        ) or canonical(parsed.wire()) != body:
            raise JournalError("JOURNAL_BINDING_INVALID", 409)
        return parsed

    @staticmethod
    def _row(
        conn: Connection, binding: RetryBinding, *, lock: bool = False
    ) -> dict[str, Any] | None:
        return conn.execute(
            "SELECT * FROM opentour_authority.retries "
            "WHERE organization_id=%s AND operation_id=%s AND phase=%s"
            + (" FOR UPDATE" if lock else ""),
            (binding.organization_id, binding.operation_id, binding.phase),
        ).fetchone()

    @staticmethod
    def _matches(row: dict[str, Any], claim: RetryClaim) -> bool:
        return (
            row["token"] == claim.token
            and str(row["revision"]) == claim.revision
            and row["state"] == "unknown"
            and row["body_sha256"] == claim.binding.body_sha256
            and bytes(row["original_body"]) == claim.original_body
        )

    def claim(self, binding: RetryBinding, original_body: bytes, now: datetime) -> RetryClaim:
        self._binding(binding, original_body)
        now = server_time(now)
        try:
            with self.repository.transaction(self.scope) as conn:
                operation = self.repository.retained_operation(
                    conn, binding.organization_id, binding.operation_id, lock=True
                )
                expected = (
                    operation["intent_body"]
                    if binding.phase == "intent"
                    else operation["outcome_body"]
                )
                if expected is None or bytes(expected) != original_body:
                    raise JournalError("JOURNAL_BINDING_INVALID", 409)
                row = self._row(conn, binding, lock=True)
                now = self.repository.now(now)
                token = secrets.token_hex(24)
                if row is None:
                    revision, attempt, first = 1, 1, now
                    conn.execute(
                        "INSERT INTO opentour_authority.retries "
                        "(organization_id,operation_id,phase,body_sha256,original_body,token,revision,"
                        "attempt,state,first_attempt_at,not_before) "
                        "VALUES(%s,%s,%s,%s,%s,%s,1,1,'unknown',%s,%s)",
                        (
                            binding.organization_id,
                            binding.operation_id,
                            binding.phase,
                            binding.body_sha256,
                            original_body,
                            token,
                            now,
                            now,
                        ),
                    )
                else:
                    if (
                        bytes(row["original_body"]) != original_body
                        or row["body_sha256"] != binding.body_sha256
                    ):
                        raise JournalError("JOURNAL_BINDING_INVALID", 409)
                    if row["state"] != "scheduled" or now < row["not_before"]:
                        raise JournalError("JOURNAL_RETRY_NOT_ADMITTED", 409)
                    if now < row["first_attempt_at"] or row["attempt"] >= MAX_COUNTER:
                        raise JournalError("JOURNAL_RETRY_STATE_EXHAUSTED", 409)
                    revision = _next_revision(row["revision"])
                    attempt = row["attempt"] + 1
                    first = row["first_attempt_at"]
                    conn.execute(
                        "UPDATE opentour_authority.retries SET token=%s,revision=%s,attempt=%s,"
                        "state='unknown',not_before=%s,reason_code=null "
                        "WHERE organization_id=%s AND operation_id=%s AND phase=%s",
                        (
                            token,
                            revision,
                            attempt,
                            now,
                            binding.organization_id,
                            binding.operation_id,
                            binding.phase,
                        ),
                    )
                result = RetryClaim(
                    binding, original_body, token, str(revision), attempt, first, now
                )
            # Return only after the SQL commit acknowledgment, never from inside its try.
            return result
        except GrantError:
            raise JournalError("JOURNAL_RETRY_STATE_UNAVAILABLE", 503) from None

    def _change(
        self,
        claim: RetryClaim,
        state: Literal["scheduled", "completed", "held"],
        *,
        not_before: datetime | None = None,
        code: str | None = None,
        response: AppendResponse | None = None,
    ) -> None:
        parsed = self._binding(claim.binding, claim.original_body)
        if (
            not isinstance(claim.revision, str)
            or not claim.revision.isascii()
            or not claim.revision.isdecimal()
            or claim.revision.startswith("0")
            or not 1 <= len(claim.revision) <= 19
            or int(claim.revision) >= MAX_REVISION
        ):
            raise JournalError("JOURNAL_RETRY_STATE_INVALID", 503)
        if response is not None:
            self.verifier.response(response, parsed)
        try:
            with self.repository.transaction(self.scope) as conn:
                row = self._row(conn, claim.binding, lock=True)
                if row is None or not self._matches(row, claim):
                    # Exact same completion may be read after a lost commit acknowledgment.
                    if (
                        row is not None
                        and state == "completed"
                        and row["state"] == "completed"
                        and (
                            row["token"] == claim.token
                            and row["revision"] == int(claim.revision) + 1
                            and response is not None
                            and row["response"] == response.wire()
                            and bytes(row["original_body"]) == claim.original_body
                        )
                    ):
                        return
                    raise JournalError("JOURNAL_RETRY_CLAIM_STALE", 409)
                next_time = row["not_before"] if not_before is None else server_time(not_before)
                if state == "scheduled":
                    now = self.repository.now(claim.not_before)
                    if (
                        not claim.not_before + timedelta(seconds=1)
                        <= next_time
                        <= now + timedelta(seconds=30)
                    ):
                        raise JournalError("JOURNAL_TIME_UNAVAILABLE", 503)
                conn.execute(
                    "UPDATE opentour_authority.retries SET state=%s,revision=%s,not_before=%s,"
                    "reason_code=%s,response=%s WHERE organization_id=%s "
                    "AND operation_id=%s AND phase=%s",
                    (
                        state,
                        _next_revision(row["revision"]),
                        next_time,
                        code,
                        Jsonb(response.wire()) if response is not None else None,
                        claim.binding.organization_id,
                        claim.binding.operation_id,
                        claim.binding.phase,
                    ),
                )
        except GrantError:
            raise JournalError("JOURNAL_RETRY_STATE_UNAVAILABLE", 503) from None

    def schedule(self, claim: RetryClaim, not_before: datetime, code: str) -> None:
        if code not in {"JOURNAL_UNAVAILABLE", "JOURNAL_OBSERVABILITY_UNAVAILABLE"}:
            raise JournalError("JOURNAL_RETRY_STATE_INVALID", 503)
        self._change(claim, "scheduled", not_before=not_before, code=code)

    def complete(self, claim: RetryClaim, response: AppendResponse) -> None:
        self._change(claim, "completed", response=response)

    def hold(self, claim: RetryClaim, code: str) -> None:
        # The reason is an internal finite diagnostic, never arbitrary exception text.
        allowed = {
            "JOURNAL_OPERATION_CONFLICT",
            "AUTHORITY_OPERATION_PENDING",
            "JOURNAL_HEAD_CONFLICT",
            "JOURNAL_FORBIDDEN",
            "JOURNAL_NOT_FOUND",
            "SCHEMA_INVALID",
            "REVISION_EXHAUSTED",
            "JOURNAL_RECONCILIATION_REQUIRED",
            "JOURNAL_STATE_INVALID",
            "JOURNAL_SEQUENCE_MISSING",
            "RECOVERY_HOLD",
            "JOURNAL_AUTHORITY_CONFLICT",
            "JOURNAL_BINDING_INVALID",
        }
        safe_code = (
            code if isinstance(code, str) and code in allowed else "JOURNAL_PROTOCOL_REJECTED"
        )
        self._change(claim, "held", code=safe_code)

    def current(self, binding: RetryBinding) -> RetryState | None:
        if binding.organization_id != self.scope.organization_id:
            raise JournalError("JOURNAL_BINDING_INVALID", 409)
        try:
            with self.repository.transaction(self.scope) as conn:
                row = self._row(conn, binding)
                if row is None:
                    return None
                parsed = self._binding(binding, bytes(row["original_body"]))
                response = (
                    AppendResponse.model_validate(row["response"]) if row["response"] else None
                )
                if response is not None:
                    self.verifier.response(response, parsed)
                return RetryState(
                    binding, row["state"], row["token"], str(row["revision"]), response
                )
        except GrantError:
            raise JournalError("JOURNAL_RETRY_STATE_UNAVAILABLE", 503) from None

    def fence_for_reconciliation(self, binding: RetryBinding, token: str, revision: str) -> None:
        """Trusted worker recovery: retire old SQL claim; never declare provider absence.

        The durable SQL row itself is reread under lock, resolving whether that
        claim committed. A newer token/progress wins. No original byte, operation
        ID, expected journal head or source binding can change here.
        """
        if binding.organization_id != self.scope.organization_id:
            raise JournalError("JOURNAL_BINDING_INVALID", 409)
        try:
            with self.repository.transaction(self.scope) as conn:
                row = self._row(conn, binding, lock=True)
                if (
                    row is None
                    or row["token"] != token
                    or str(row["revision"]) != revision
                    or row["state"] != "unknown"
                ):
                    raise JournalError("JOURNAL_RETRY_CLAIM_STALE", 409)
                self._binding(binding, bytes(row["original_body"]))
                now = self.repository.now(row["not_before"])
                conn.execute(
                    "UPDATE opentour_authority.retries SET state='scheduled',token=%s,revision=%s,"
                    "not_before=%s,reason_code='EXACT_OPERATION_RECONCILIATION_REQUIRED' "
                    "WHERE organization_id=%s AND operation_id=%s AND phase=%s",
                    (
                        secrets.token_hex(24),
                        _next_revision(row["revision"]),
                        now + timedelta(seconds=1),
                        binding.organization_id,
                        binding.operation_id,
                        binding.phase,
                    ),
                )
        except GrantError:
            raise JournalError("JOURNAL_RETRY_STATE_UNAVAILABLE", 503) from None


@dataclass(frozen=True)
class GrantResult:
    status: Literal[200, 201, 202]
    operation: GrantOperationView


@dataclass(frozen=True)
class CreatorEnrollment:
    """Current authenticated installer and exact independent enrollment attestation."""

    installer: ServicePrincipal
    registration: ApprovedRegistration
    journal: JournalIdentity
    policy: InitialAuthorityPolicy


class CreatorEnrollmentAuthority(Protocol):
    def current(self, organization_id: str) -> CreatorEnrollment: ...


@dataclass(frozen=True)
class CreatorInstallationResult:
    operation_id: str
    state: Literal["prepared", "reconciliation_required", "succeeded", "aborted"]
    grant_ids: tuple[str, ...]


_DEFINITE_CURRENT_DENIAL = frozenset(
    {
        "CAPABILITY_DENIED",
        "DELEGATION_CEILING",
        "DELEGATION_CHANGED",
        "DUTY_CONFLICT",
        "GRANT_EXPIRED",
        "MEMBERSHIP_INACTIVE",
        "PRINCIPAL_UNAVAILABLE",
        "RESOURCE_NOT_FOUND",
        "SESSION_ENDED",
        "SESSION_INVALID",
        "STALE_SESSION_CONTEXT",
    }
)


class GrantCoordinator:
    """Configured workspace create and operations reader using SQL and verified journal.

    The request-scoped identity callback calls the existing sensitive 142 verifier
    outside SQL transactions. Resource and principal adapters observe canonical
    sources through their owning domain's local admission fence.
    """

    def __init__(
        self,
        repository: PostgresGrantRepository,
        resolver: GrantResolver,
        journal: JournalClient,
        evidence: EvidenceWriter,
        *,
        management_purpose: str,
        creator_enrollment: CreatorEnrollmentAuthority | None = None,
    ) -> None:
        if repository.role != "coordinator" or management_purpose not in (
            resolver.vocabulary.purpose_codes
        ):
            raise GrantError("GRANT_CONFIGURATION_INVALID", 503)
        self.repository, self.resolver, self.journal = repository, resolver, journal
        self.evidence, self.management_purpose = evidence, management_purpose
        self.creator_enrollment = creator_enrollment

    @staticmethod
    def _identity(
        current_identity: Callable[[], AuthContext], original: AuthContext | None = None
    ) -> AuthContext:
        try:
            context = current_identity()
        except AuthError as error:
            raise GrantError(error.code, error.status, retryable=error.retryable) from None
        if not isinstance(context, AuthContext) or context.identity.actor.get("kind") != "human":
            raise GrantError("SESSION_INVALID", 401)
        if original is not None and (
            RepositoryScope.from_context(context) != RepositoryScope.from_context(original)
            or context.identity.session_generation != original.identity.session_generation
            or context.membership_id != original.membership_id
        ):
            raise GrantError("STALE_SESSION_CONTEXT", 409)
        return context

    def _frontier(self, organization_id: str) -> VerifiedAuthorityFrontier | None:
        try:
            return self.journal.verify_frontier(organization_id, self.evidence)
        except JournalError:
            return None

    def install_creator(
        self, current_identity: Callable[[], VerifiedIdentityContext], organization_id: str
    ) -> CreatorInstallationResult:
        """Internal organization-birth method. No selectable installer or public bypass."""
        started = server_time(self.repository.clock())
        scope, original = self._creator_identity(current_identity, organization_id)
        enrollment = self._creator_approval(organization_id)
        frontier = self._frontier(organization_id)
        if frontier is None:
            raise GrantError("AUTHORITY_UNAVAILABLE", 503, retryable=True)

        def current() -> VerifiedIdentityContext:
            # Revalidate the independently configured enrollment after waits.
            if self._creator_approval(organization_id) != enrollment:
                raise GrantError("CREATOR_ENROLLMENT_CHANGED", 409)
            return self._creator_identity(current_identity, organization_id, scope, original)[1]

        operation_id = self._reserve_creator(scope, current(), enrollment, frontier, started)
        try:
            self._advance_creator(scope, current, enrollment, operation_id)
        except (GrantError, JournalError, ValidationError):
            self._mark_reconciliation(scope, operation_id)
        with self.repository.transaction(scope) as conn:
            row = self.repository.retained_operation(conn, organization_id, operation_id)
            if row["operation_kind"] != "creator_installation":
                raise GrantError("AUTHORITY_STATE_INVALID", 503)
            manifest = CreatorGrantManifest.model_validate(row["proposed_grant"])
            return CreatorInstallationResult(
                operation_id,
                row["state"],
                tuple(g.grant_id for g in manifest.grants) if row["state"] == "succeeded" else (),
            )

    @staticmethod
    def _creator_identity(
        current: Callable[[], VerifiedIdentityContext],
        organization_id: str,
        previous_scope: RepositoryScope | None = None,
        previous: VerifiedIdentityContext | None = None,
    ) -> tuple[RepositoryScope, VerifiedIdentityContext]:
        try:
            verified = current()
            if (
                not isinstance(verified, VerifiedIdentityContext)
                or verified.actor.get("kind") != "human"
                or set(verified.actor) != {"kind", "issuer", "subject"}
            ):
                raise GrantError("SESSION_INVALID", 401)
            scope = RepositoryScope.from_identity(identifier(organization_id), verified)
        except AuthError as error:
            raise GrantError(error.code, error.status, retryable=error.retryable) from None
        if previous is not None and (
            scope != previous_scope or previous.session_generation != verified.session_generation
        ):
            raise GrantError("STALE_SESSION_CONTEXT", 409)
        return scope, verified

    def _creator_approval(self, organization_id: str) -> CreatorEnrollment:
        if self.creator_enrollment is None:
            raise GrantError("CREATOR_ENROLLMENT_UNAVAILABLE", 503)
        approval = self.creator_enrollment.current(organization_id)
        if (
            not isinstance(approval, CreatorEnrollment)
            or approval.registration.organization_id != organization_id
            or approval.journal != self.journal.verifier.trust.journal
            or approval.policy.enrolled_organization_id != organization_id
            or approval.registration.enrollment_ref != approval.policy.enrollment_ref
            or approval.installer.authentication_ref != approval.policy.authenticated_installer
        ):
            raise GrantError("CREATOR_ENROLLMENT_INVALID", 503)
        InitialAuthorityPolicy.model_validate(approval.policy.wire())
        return approval

    @staticmethod
    def _birth_binding(
        fact: CompletedOrganizationFact, principal: PrincipalRecord, approval: CreatorEnrollment
    ) -> dict[str, object]:
        return {
            "completed_organization": fact.wire(),
            "creator_principal": principal.principal.wire(),
            "policy": approval.policy.wire(),
            "installer_id": approval.installer.service_id,
            "journal": approval.journal.wire(),
        }

    def _reserve_creator(
        self,
        scope: RepositoryScope,
        identity: VerifiedIdentityContext,
        approval: CreatorEnrollment,
        frontier: VerifiedAuthorityFrontier,
        started: datetime,
    ) -> str:
        org = scope.organization_id
        with self.repository.transaction(scope) as conn:
            fact = self.repository.canonical_completed_organization(conn, scope, identity, started)
            actor = self.resolver.human_principal(
                org, scope.issuer, scope.subject, fact.membership_id
            )
            binding = self._birth_binding(fact, actor, approval)
            key = sha256(canonical(binding))
            now = self.repository.now(started)
            source = conn.execute(
                "SELECT * FROM opentour_authority.organizations "
                "WHERE organization_id=%s FOR UPDATE",
                (org,),
            ).fetchone()
            if source is not None:
                row = conn.execute(
                    "SELECT * FROM opentour_authority.operations WHERE organization_id=%s "
                    "AND operation_kind='creator_installation'",
                    (org,),
                ).fetchone()
                if (
                    row is None
                    or row["key_sha256"] != key
                    or canonical(row["request_body"]) != canonical(binding)
                    or source["enrollment_ref"] != approval.registration.enrollment_ref.wire()
                ):
                    raise GrantError("CREATOR_INSTALLATION_CONFLICT", 409)
                return str(row["operation_id"])
            head, registration = frontier.head, approval.registration
            if (
                head.organization_id != org
                or head.sequence != 0
                or head.revision != "1"
                or head.unresolved_operation_id is not None
                or head.latest_epoch != registration.initial_epoch
                or head.latest_authority_revision != registration.initial_authority_revision
                or head.chain_sha256 != genesis_digest(approval.journal, head)
            ):
                raise GrantError("CREATOR_GENESIS_UNVERIFIED", 503)
            self.repository.canonical_completed_organization(conn, scope, identity, now)
            return self._seed_creator(
                conn,
                scope,
                identity,
                fact,
                approval,
                actor,
                binding,
                frontier,
                key,
                self.repository.now(now),
            )

    def _seed_creator(
        self,
        conn: Connection,
        scope: RepositoryScope,
        identity: VerifiedIdentityContext,
        fact: CompletedOrganizationFact,
        approval: CreatorEnrollment,
        actor: PrincipalRecord,
        binding: dict[str, object],
        frontier: VerifiedAuthorityFrontier,
        key: str,
        now: datetime,
    ) -> str:
        org, head = scope.organization_id, frontier.head
        operation_id, revision = secrets.token_hex(24), secrets.token_hex(24)
        manifest = CreatorGrantManifest(
            grants=approval.policy.birth_grants(
                org, actor.principal.id, operation_id, revision, now
            )
        )
        plan = {"principal_revision": actor.registration_revision}
        actor_source = ActorSource(
            schema_version="opentour.grant-actor-source.v1",
            organization_id=org,
            operation_id=operation_id,
            issuer=scope.issuer,
            subject=scope.subject,
            session_id=scope.session_id,
            session_generation=identity.session_generation,
            membership_id=fact.membership_id,
        )
        conn.execute(
            "INSERT INTO opentour_authority.organizations VALUES"
            "(%s,%s,%s,%s,'reconciliation_required',%s,%s,%s,%s)",
            (
                org,
                head.latest_authority_revision,
                head.latest_epoch,
                operation_id,
                Jsonb(head.wire()),
                Jsonb(frontier.proof_ref.wire()),
                Jsonb(approval.registration.enrollment_ref.wire()),
                now,
            ),
        )
        actor_ref = self._source_record(conn, actor_source, "actor", now)
        source = CreatorIntentSource(
            schema_version="opentour.creator-installation-intent.v1",
            organization_id=org,
            operation_id=operation_id,
            request_sha256=key,
            grant_manifest_sha256=manifest.digest(),
            plan_sha256=sha256(canonical(plan)),
            actor_ref=actor_ref,
            mutation_kind="grant",
            proposed_epoch=head.latest_epoch,
            proposed_authority_revision=revision,
            expected_head_sequence=head.sequence,
            expected_head_digest=head.chain_sha256,
        )
        reference = self._source_record(conn, source, "intent", now)
        intent = AppendIntent(
            organization_id=org,
            operation_id=operation_id,
            expected_head_sequence=head.sequence,
            expected_head_digest=head.chain_sha256,
            mutation_kind="grant",
            proposed_epoch=head.latest_epoch,
            proposed_authority_revision=revision,
            canonical_intent_digest=reference.sha256,
            scoped_intent_ref=reference,
            actor_ref=actor_ref,
            created_at=timestamp(now),
        )
        conn.execute(
            "INSERT INTO opentour_authority.operations "
            "(organization_id,operation_id,operation_kind,issuer,subject,key_sha256,context,"
            "request_body,plan,proposed_grant,proposed_authority_revision,proposed_epoch,"
            "intent_body,state,revision,created_at,updated_at) "
            "VALUES(%s,%s,'creator_installation',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'prepared',1,%s,%s)",
            (
                org,
                operation_id,
                scope.issuer,
                scope.subject,
                key,
                Jsonb(identity.wire() | {"membership_id": fact.membership_id}),
                Jsonb(binding),
                Jsonb(plan),
                Jsonb(manifest.wire()),
                revision,
                head.latest_epoch,
                canonical(intent.wire()),
                now,
                now,
            ),
        )
        self._outbox(conn, org, operation_id, 1, "grant.prepared", now)
        return operation_id

    def _advance_creator(
        self,
        scope: RepositoryScope,
        current: Callable[[], VerifiedIdentityContext],
        approval: CreatorEnrollment,
        operation_id: str,
    ) -> None:
        with self.repository.transaction(scope) as conn:
            row = self.repository.retained_operation(conn, scope.organization_id, operation_id)
        if row["state"] in {"succeeded", "aborted"}:
            return
        intent = AppendIntent.parse(bytes(row["intent_body"]))
        receipt = self._ack(scope, intent)
        if receipt is None:
            self._mark_reconciliation(scope, operation_id)
            return
        if row["outcome_body"] is None:
            outcome = self._apply_creator(scope, current(), approval, intent, receipt)
        else:
            outcome = AppendOutcome.parse(bytes(row["outcome_body"]))
        response = self._ack(scope, outcome)
        frontier = self._frontier(scope.organization_id)
        if (
            response is None
            or frontier is None
            or not self._includes_outcome(frontier, outcome, response)
        ):
            self._mark_reconciliation(scope, operation_id)
            return
        try:
            self._finalize_creator(scope, current(), approval, intent, outcome, response, frontier)
        except GrantError as error:
            if error.code not in _DEFINITE_CURRENT_DENIAL | {
                "CREATOR_ENROLLMENT_CHANGED",
                "CREATOR_INSTALLATION_CONFLICT",
            }:
                raise
            self._retain_denied_commit(scope, outcome, response, frontier)

    def _creator_source(
        self,
        conn: Connection,
        scope: RepositoryScope,
        identity: VerifiedIdentityContext,
        approval: CreatorEnrollment,
        intent: AppendIntent,
        started: datetime,
    ) -> tuple[AuthorityState, dict[str, Any], CreatorGrantManifest, datetime]:
        fact = self.repository.canonical_completed_organization(conn, scope, identity, started)
        org = scope.organization_id
        authority, source = self.repository.authority(conn, org, lock=True)
        row = self.repository.retained_operation(conn, org, intent.operation_id, lock=True)
        manifest = CreatorGrantManifest.model_validate(row["proposed_grant"])
        now = self.repository.now(max(started, row["updated_at"], source["updated_at"]))
        if row["state"] in {"succeeded", "aborted"}:
            return authority, row, manifest, now
        self._require_reservation(authority, source, intent)
        actor = self.resolver.human_principal(org, scope.issuer, scope.subject, fact.membership_id)
        expected = CreatorGrantManifest(
            grants=approval.policy.birth_grants(
                org,
                actor.principal.id,
                intent.operation_id,
                intent.proposed_authority_revision,
                row["created_at"],
            )
        )
        if (
            row["operation_kind"] != "creator_installation"
            or row["context"]["session_id"] != identity.session_id
            or row["context"]["session_generation"] != identity.session_generation
            or row["context"]["membership_id"] != fact.membership_id
            or row["plan"]["principal_revision"] != actor.registration_revision
            or source["enrollment_ref"] != approval.registration.enrollment_ref.wire()
            or canonical(row["request_body"])
            != canonical(self._birth_binding(fact, actor, approval))
            or canonical(manifest.wire()) != canonical(expected.wire())
        ):
            raise GrantError("CREATOR_INSTALLATION_CONFLICT", 409)
        final_fact = self.repository.canonical_completed_organization(conn, scope, identity, now)
        if fact != final_fact:
            raise GrantError("CREATOR_INSTALLATION_CONFLICT", 409)
        return authority, row, manifest, self.repository.now(now)

    def _apply_creator(
        self,
        scope: RepositoryScope,
        identity: VerifiedIdentityContext,
        approval: CreatorEnrollment,
        intent: AppendIntent,
        receipt: AppendResponse,
    ) -> AppendOutcome:
        self.journal.verifier.response(receipt, intent)
        started = server_time(self.repository.clock())
        with self.repository.transaction(scope) as conn:
            authority, row, manifest, now = self._creator_source(
                conn, scope, identity, approval, intent, started
            )
            if row["outcome_body"] is not None:
                return AppendOutcome.parse(bytes(row["outcome_body"]))
            for grant in manifest.grants:
                self._insert_prepared(conn, scope.organization_id, grant, now)
            return self._write_outcome(conn, authority, row, intent, receipt, "committed", now)

    def _finalize_creator(
        self,
        scope: RepositoryScope,
        identity: VerifiedIdentityContext,
        approval: CreatorEnrollment,
        intent: AppendIntent,
        outcome: AppendOutcome,
        response: AppendResponse,
        frontier: VerifiedAuthorityFrontier,
    ) -> None:
        self.journal.verifier.response(response, outcome)
        started = server_time(self.repository.clock())
        with self.repository.transaction(scope) as conn:
            _, row, manifest, now = self._creator_source(
                conn, scope, identity, approval, intent, started
            )
            if row["state"] in {"succeeded", "aborted"}:
                return
            if (
                outcome.result != "committed"
                or row["outcome_body"] is None
                or bytes(row["outcome_body"]) != canonical(outcome.wire())
            ):
                raise GrantError("AUTHORITY_STATE_INVALID", 503)
            for grant in manifest.grants:
                self._activate_prepared(
                    conn,
                    scope.organization_id,
                    intent.operation_id,
                    grant.grant_id,
                    grant.wire(),
                    now,
                )
            self._publish(conn, row, outcome, response, frontier, now)

    def create(
        self,
        current_identity: Callable[[], AuthContext],
        workspace_id: str,
        key: str,
        request_id: str,
        raw: bytes,
    ) -> GrantResult:
        try:
            workspace_id, request_id, key_hash = (
                identifier(workspace_id),
                identifier(request_id),
                key_digest(key),
            )
        except (AuthError, SetupError):
            raise GrantError("SCHEMA_INVALID", 422) from None
        body = CreateGrant.parse(raw)
        started = server_time(self.repository.clock())
        original = self._identity(current_identity)
        frontier = self._frontier(original.organization_id)
        context = self._identity(current_identity, original)
        operation_id, new = self._reserve(
            context, workspace_id, key_hash, body, request_id, started, frontier
        )
        scope = RepositoryScope.from_context(context)
        try:
            self._advance(lambda: self._identity(current_identity, original), scope, operation_id)
        except (GrantError, JournalError, ValidationError):
            # The reservation is known durable. A fresh read determines retained progress.
            self._mark_reconciliation(scope, operation_id)
        view = self._progress(current_identity, operation_id, request_id, original)
        if view.state in {"prepared", "reconciliation_required"}:
            return GrantResult(202, view)
        return GrantResult(201 if new and view.state == "succeeded" else 200, view)

    def progress(
        self, current_identity: Callable[[], AuthContext], operation_id: str, request_id: str
    ) -> GrantOperationView:
        return self._progress(current_identity, operation_id, request_id, None)

    def _progress(
        self,
        current_identity: Callable[[], AuthContext],
        operation_id: str,
        request_id: str,
        original: AuthContext | None,
    ) -> GrantOperationView:
        try:
            identifier(operation_id)
            identifier(request_id)
        except AuthError:
            raise GrantError("SCHEMA_INVALID", 422) from None
        started = server_time(self.repository.clock())
        initial = self._identity(current_identity, original)
        frontier = self._frontier(initial.organization_id)
        context = self._identity(current_identity, initial)
        with self.repository.transaction(context) as conn:
            self.repository.canonical_identity(conn, context, started)
            authority, source = self.repository.authority(conn, context.organization_id, lock=True)
            row = self.repository.retained_operation(conn, context.organization_id, operation_id)
            if row["operation_kind"] != "access_grant_create":
                raise GrantError("RESOURCE_NOT_FOUND", 404)
            grants = self.repository.grants(conn, context.organization_id, lock=True)
            now = self.repository.now(max(started, row["updated_at"], source["updated_at"]))
            decision = self._authorize_metadata(
                conn, context, authority, source, frontier, grants, row["workspace_id"], now
            )
            target_current = False
            if row["state"] == "succeeded":
                target = grants.get(row["grant_id"])
                if target is not None and target.state != "revoked":
                    try:
                        record = self.resolver.principal(context.organization_id, target.principal)
                        self.resolver.scope(
                            context.organization_id, row["workspace_id"], target.resource_scope
                        )
                        target_current = record.state == "active"
                    except GrantError:
                        pass
            observed = max(now, decision.observed_at)
            self.repository.canonical_identity(conn, context, observed)
            now = self.repository.now(observed)
            self.resolver.require_management_current(
                grants, decision.grant_ids, organization_id=context.organization_id, now=now
            )
            return self.repository.view(
                conn,
                row,
                request_id=request_id,
                now=now,
                frontier=frontier,
                target_current=target_current,
            )

    def _authorize_metadata(
        self,
        conn: Connection,
        context: AuthContext,
        authority: AuthorityState,
        source: dict[str, Any],
        frontier: VerifiedAuthorityFrontier | None,
        grants: Mapping[str, AccessGrant],
        workspace_id: str,
        now: datetime,
    ) -> Decision:
        # An unresolved additive mutation confers none of the grants used by this
        # metadata-only read. Unknown or restrictive operations cannot use this path.
        pending = None
        if authority.current_operation_id is not None:
            row = self.repository.retained_operation(
                conn, context.organization_id, authority.current_operation_id
            )
            original = AppendIntent.parse(bytes(row["intent_body"]))
            if original.mutation_kind != "grant" or row["state"] in {"succeeded", "aborted"}:
                raise GrantError("AUTHORITY_UNAVAILABLE", 503, retryable=True)
            pending = row
            authority = replace(authority, state="current", current_operation_id=None)
        elif authority.state == "reconciliation_required":
            held = conn.execute(
                "SELECT 1 FROM opentour_authority.outbox o JOIN opentour_authority.operations p "
                "USING (organization_id,operation_id) WHERE o.organization_id=%s AND "
                "o.kind='grant.restrictive_followup_required' AND p.proposed_authority_revision=%s",
                (authority.organization_id, authority.authority_revision),
            ).fetchone()
            if held is None:
                raise GrantError("AUTHORITY_UNAVAILABLE", 503, retryable=True)
            authority = replace(authority, state="current")
        if frontier is not None:
            self._require_compatible_metadata_head(source, pending, frontier)
        return self.resolver.decide(
            context,
            self._actor(context),
            authority,
            grants,
            action="access.manage",
            resource_kind="workspace",
            resource_id=workspace_id,
            purpose=self.management_purpose,
            data_class="control_metadata",
            now=now,
        )

    @staticmethod
    def _require_compatible_metadata_head(
        source: dict[str, Any],
        pending: dict[str, Any] | None,
        frontier: VerifiedAuthorityFrontier,
    ) -> None:
        durable = HeadState.model_validate(source["journal_head"])
        head = frontier.head
        if head.organization_id != durable.organization_id or frontier.unresolved_kind in {
            "restrict",
            "tombstone",
        }:
            raise GrantError("AUTHORITY_UNAVAILABLE", 503, retryable=True)
        # An observation can predate a concurrent successful local commit. The
        # local SQL deny is consulted even then; it gives no current projection.
        if head.sequence < durable.sequence or head == durable:
            return
        if head.sequence > durable.sequence and pending is not None and frontier.observations:
            latest = frontier.observations[-1]
            raw = canonical(latest.body.wire())
            if isinstance(latest.body, AppendIntent):
                original = pending["intent_body"]
            else:
                original = pending["outcome_body"]
            if (
                original is not None
                and raw == bytes(original)
                and (
                    latest.entry.sequence == head.sequence
                    and latest.entry.chain_sha256 == head.chain_sha256
                )
            ):
                return
        raise GrantError("AUTHORITY_UNAVAILABLE", 503, retryable=True)

    def _actor(self, context: AuthContext) -> PrincipalRecord:
        return self.resolver.human_principal(
            context.organization_id,
            context.identity.actor["issuer"],
            context.identity.actor["subject"],
            context.membership_id,
        )

    def _plan(
        self,
        context: AuthContext,
        authority: AuthorityState,
        grants: Mapping[str, AccessGrant],
        workspace_id: str,
        body: CreateGrant,
        now: datetime,
    ) -> DelegationPlan:
        return self.resolver.delegation(
            context,
            self._actor(context),
            authority,
            grants,
            workspace_id=workspace_id,
            body=body,
            management_purpose=self.management_purpose,
            now=now,
        )

    def _reserve(
        self,
        context: AuthContext,
        workspace_id: str,
        key_hash: str,
        body: CreateGrant,
        request_id: str,
        started: datetime,
        frontier: VerifiedAuthorityFrontier | None,
    ) -> tuple[str, bool]:
        scope = RepositoryScope.from_context(context)
        with self.repository.transaction(context) as conn:
            self.repository.canonical_identity(conn, context, started)
            authority, source = self.repository.authority(conn, scope.organization_id, lock=True)
            previous = self.repository.operation_for_key(conn, context, workspace_id, key_hash)
            grants = self.repository.grants(conn, scope.organization_id, lock=True)
            now = self.repository.now(max(started, source["updated_at"]))
            if previous is not None:
                self._authorize_metadata(
                    conn, context, authority, source, frontier, grants, workspace_id, now
                )
                if canonical(previous["request_body"]) != canonical(body.wire()):
                    raise GrantError("IDEMPOTENCY_CONFLICT", 409)
                return str(previous["operation_id"]), False
            authority.require_current(context)
            if frontier is None or not self.repository.matches_frontier(
                authority, source, frontier
            ):
                raise GrantError("AUTHORITY_UNAVAILABLE", 503, retryable=True)
            plan = self._plan(context, authority, grants, workspace_id, body, now)
            now, management = self._fence_delegation(conn, context, grants, body, plan, now)
            plan = replace(plan, management_grant_ids=management)
            self.repository.retain_request(
                conn, context, workspace_id, key_hash, body, request_id, now
            )
            operation_id = self._write_reservation(
                conn, context, scope, workspace_id, key_hash, body, plan, frontier.head, now
            )
        return operation_id, True

    @staticmethod
    def _source_record(
        conn: Connection,
        source: BoundIntentSource | ActorSource | BoundTransactionSource,
        kind: Literal["intent", "actor", "transaction"],
        now: datetime,
    ) -> ImmutableMetadataRef:
        record = ImmutableMetadataRef(
            record_id=secrets.token_hex(24), revision="1", sha256=source.digest()
        )
        conn.execute(
            "INSERT INTO opentour_authority.sources "
            "(organization_id,operation_id,record_id,kind,revision,sha256,body,created_at) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                source.organization_id,
                source.operation_id,
                record.record_id,
                kind,
                record.revision,
                record.sha256,
                canonical(source.wire()),
                now,
            ),
        )
        return record

    @staticmethod
    def _outbox(
        conn: Connection, org: str, operation_id: str, revision: int, kind: str, now: datetime
    ) -> None:
        conn.execute(
            "INSERT INTO opentour_authority.outbox VALUES(%s,%s,%s,%s,%s)",
            (org, operation_id, revision, kind, now),
        )

    @staticmethod
    def _plan_wire(plan: DelegationPlan) -> dict[str, object]:
        return {
            "parent_grant_ids": list(plan.parent_grant_ids),
            "resource_revisions": [list(r) for r in plan.resource_revisions],
            "principal_revision": plan.principal_revision,
            "management_grant_ids": list(plan.management_grant_ids),
        }

    def _write_reservation(
        self,
        conn: Connection,
        context: AuthContext,
        scope: RepositoryScope,
        workspace_id: str,
        key_hash: str,
        body: CreateGrant,
        plan: DelegationPlan,
        head: HeadState,
        now: datetime,
    ) -> str:
        org = scope.organization_id
        operation_id, grant_id, revision = (secrets.token_hex(24) for _ in range(3))
        proposed = AccessGrant(
            grant_id=grant_id,
            revision="1",
            principal=body.principal,
            capabilities=body.capabilities,
            resource_scope=ResourceScope(organization_id=org, **body.resource_scope.model_dump()),
            purposes=body.purposes,
            data_classes=body.data_classes,
            valid_from=body.valid_from,
            expires_at=body.expires_at,
            reason_code=body.reason_code,
            state="prepared",
            parent_grant_ids=plan.parent_grant_ids,
            template_bindings=(),
            creation_operation_id=operation_id,
            creation_authority_revision=revision,
        )
        plan_body = self._plan_wire(plan)
        actor = ActorSource(
            schema_version="opentour.grant-actor-source.v1",
            organization_id=org,
            operation_id=operation_id,
            issuer=scope.issuer,
            subject=scope.subject,
            session_id=scope.session_id,
            session_generation=context.identity.session_generation,
            membership_id=context.membership_id,
        )
        actor_ref = self._source_record(conn, actor, "actor", now)
        source = IntentSource(
            schema_version="opentour.grant-intent-source.v1",
            organization_id=org,
            operation_id=operation_id,
            grant_id=grant_id,
            request_sha256=body.digest(),
            proposed_grant_sha256=proposed.digest(),
            plan_sha256=sha256(canonical(plan_body)),
            actor_ref=actor_ref,
            mutation_kind="grant",
            proposed_epoch=head.latest_epoch,
            proposed_authority_revision=revision,
            expected_head_sequence=head.sequence,
            expected_head_digest=head.chain_sha256,
        )
        source_ref = self._source_record(conn, source, "intent", now)
        intent = AppendIntent(
            organization_id=org,
            operation_id=operation_id,
            expected_head_sequence=head.sequence,
            expected_head_digest=head.chain_sha256,
            mutation_kind="grant",
            proposed_epoch=head.latest_epoch,
            proposed_authority_revision=revision,
            canonical_intent_digest=source_ref.sha256,
            scoped_intent_ref=source_ref,
            actor_ref=actor_ref,
            created_at=timestamp(now),
        )
        conn.execute(
            "INSERT INTO opentour_authority.operations "
            "(organization_id,operation_id,grant_id,issuer,subject,workspace_id,key_sha256,"
            "context,request_body,plan,proposed_grant,proposed_authority_revision,proposed_epoch,"
            "intent_body,state,revision,created_at,updated_at) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'prepared',1,%s,%s)",
            (
                org,
                operation_id,
                grant_id,
                scope.issuer,
                scope.subject,
                workspace_id,
                key_hash,
                Jsonb(context.identity.wire() | {"membership_id": context.membership_id}),
                Jsonb(body.wire()),
                Jsonb(plan_body),
                Jsonb(proposed.wire()),
                revision,
                head.latest_epoch,
                canonical(intent.wire()),
                now,
                now,
            ),
        )
        conn.execute(
            "UPDATE opentour_authority.organizations SET current_operation_id=%s,"
            "state='reconciliation_required',updated_at=%s WHERE organization_id=%s",
            (operation_id, now, org),
        )
        self._outbox(conn, org, operation_id, 1, "grant.prepared", now)
        return operation_id

    def _ack(
        self, scope: RepositoryScope, body: AppendIntent | AppendOutcome
    ) -> AppendResponse | None:
        durable = PostgresJournalRetryPort(self.repository, scope, self.journal.verifier)
        binding = RetryBinding(
            body.organization_id,
            body.operation_id,
            "intent" if isinstance(body, AppendIntent) else "outcome",
            sha256(canonical(body.wire())),
        )
        retained = durable.current(binding)
        if retained is not None and retained.state == "completed":
            if retained.response is None:
                raise GrantError("AUTHORITY_STATE_INVALID", 503)
            self.journal.verifier.response(retained.response, body)
            return retained.response
        if retained is not None:
            # Only an exact signed observation settles a lost acknowledgment.
            # Absence never proves abort or admits a new operation.
            observation = self.journal.read_exact_operation(
                binding.organization_id, binding.operation_id, binding.phase
            )
            if observation.response is not None:
                self.journal.verifier.response(observation.response, body)
                return observation.response
            elif retained.state == "unknown":
                current = self._frontier(body.organization_id)
                unresolved = body.operation_id if isinstance(body, AppendOutcome) else None
                if current is not None and (
                    current.head.sequence,
                    current.head.chain_sha256,
                    current.head.unresolved_operation_id,
                ) == (body.expected_head_sequence, body.expected_head_digest, unresolved):
                    durable.fence_for_reconciliation(binding, retained.token, retained.revision)
            if retained.state != "scheduled":
                return None
        return self.journal.retry_append(body, durable, clock=self.repository.clock).response

    def _advance(
        self, current_identity: Callable[[], AuthContext], scope: RepositoryScope, operation_id: str
    ) -> None:
        with self.repository.transaction(scope) as conn:
            row = self.repository.retained_operation(conn, scope.organization_id, operation_id)
        if row["state"] in {"succeeded", "aborted"}:
            return
        intent = AppendIntent.parse(bytes(row["intent_body"]))
        intent_response = self._ack(scope, intent)
        if intent_response is None:
            self._mark_reconciliation(scope, operation_id)
            return
        if row["outcome_body"] is None:
            context = current_identity()
            try:
                outcome = self._apply(context, scope, intent, intent_response)
            except GrantError as error:
                if error.code not in _DEFINITE_CURRENT_DENIAL - {"STALE_SESSION_CONTEXT"}:
                    raise
                outcome = self._abort(scope, intent, intent_response)
        else:
            outcome = AppendOutcome.parse(bytes(row["outcome_body"]))
        response = self._ack(scope, outcome)
        if response is None:
            self._mark_reconciliation(scope, operation_id)
            return
        frontier = self._frontier(scope.organization_id)
        if frontier is None or not self._includes_outcome(frontier, outcome, response):
            raise GrantError("RECONCILIATION_REQUIRED", 503, retryable=True)
        try:
            current = current_identity() if outcome.result == "committed" else None
            self._finalize(scope, current, outcome, response, frontier)
        except GrantError as error:
            if outcome.result != "committed" or error.code not in _DEFINITE_CURRENT_DENIAL:
                raise
            self._retain_denied_commit(scope, outcome, response, frontier)

    @staticmethod
    def _includes_outcome(
        frontier: VerifiedAuthorityFrontier, body: AppendOutcome, response: AppendResponse
    ) -> bool:
        head = frontier.head
        return (
            head.organization_id == body.organization_id
            and head.unresolved_operation_id is None
            and head.latest_epoch == body.resulting_epoch
            and head.latest_authority_revision == body.resulting_authority_revision
            and head.sequence == response.sequence
            and head.chain_sha256 == response.chain_sha256
            and head.revision == response.head_revision
        )

    @staticmethod
    def _require_original(
        scope: RepositoryScope, context: AuthContext, row: dict[str, Any]
    ) -> None:
        original = row["context"]
        if (
            RepositoryScope.from_context(context) != scope
            or original["session_id"] != context.identity.session_id
            or original["session_generation"] != context.identity.session_generation
            or original["membership_id"] != context.membership_id
            or (row["issuer"], row["subject"]) != (scope.issuer, scope.subject)
        ):
            raise GrantError("STALE_SESSION_CONTEXT", 409)

    @staticmethod
    def _require_reservation(
        authority: AuthorityState, source: dict[str, Any], intent: AppendIntent
    ) -> None:
        head = HeadState.model_validate(source["journal_head"])
        if (
            authority.current_operation_id != intent.operation_id
            or authority.state != "reconciliation_required"
            or head.organization_id != intent.organization_id
            or head.unresolved_operation_id is not None
            or head.latest_epoch != authority.revocation_epoch
            or head.latest_authority_revision != authority.authority_revision
            or (head.sequence, head.chain_sha256)
            != (intent.expected_head_sequence, intent.expected_head_digest)
        ):
            raise GrantError("RECONCILIATION_REQUIRED", 503, retryable=True)

    def _check_plan(
        self,
        conn: Connection,
        context: AuthContext,
        authority: AuthorityState,
        row: dict[str, Any],
        now: datetime,
    ) -> datetime:
        grants = self.repository.grants(conn, context.organization_id, lock=True)
        body = CreateGrant.model_validate(row["request_body"])
        base = replace(authority, state="current", current_operation_id=None)
        fresh = self._plan(context, base, grants, row["workspace_id"], body, now)
        original_management = tuple(row["plan"]["management_grant_ids"])
        original_binding = replace(fresh, management_grant_ids=original_management)
        if row["plan"] != self._plan_wire(original_binding):
            raise GrantError("DELEGATION_CHANGED", 409)
        return self._fence_delegation(conn, context, grants, body, original_binding, now)[0]

    def _fence_delegation(
        self,
        conn: Connection,
        context: AuthContext,
        grants: Mapping[str, AccessGrant],
        body: CreateGrant,
        plan: DelegationPlan,
        started: datetime,
    ) -> tuple[datetime, tuple[str, ...]]:
        checked = max(started, plan.observed_at)
        self.repository.canonical_identity(conn, context, checked)
        now = self.repository.now(checked)
        management = self.resolver.require_management_current(
            grants, plan.management_grant_ids, organization_id=context.organization_id, now=now
        )
        ancestry = GrantAncestry(grants, now, context.organization_id)
        if any(
            (grant := grants.get(key)) is None or not ancestry.valid(grant)
            for key in plan.parent_grant_ids
        ):
            raise GrantError("DELEGATION_CHANGED", 409)
        if body.expires_at is not None and instant(body.expires_at) <= now:
            raise GrantError("GRANT_EXPIRED", 403)
        return now, management

    def _apply(
        self,
        context: AuthContext,
        scope: RepositoryScope,
        intent: AppendIntent,
        receipt: AppendResponse,
    ) -> AppendOutcome:
        self.journal.verifier.response(receipt, intent)
        started = server_time(self.repository.clock())
        org, operation_id = intent.organization_id, intent.operation_id
        with self.repository.transaction(scope) as conn:
            self.repository.canonical_identity(conn, context, started)
            authority, source = self.repository.authority(conn, org, lock=True)
            row = self.repository.retained_operation(conn, org, operation_id, lock=True)
            self._require_original(scope, context, row)
            if row["outcome_body"] is not None:
                return AppendOutcome.parse(bytes(row["outcome_body"]))
            self._require_reservation(authority, source, intent)
            now = self.repository.now(max(started, row["updated_at"], source["updated_at"]))
            now = self._check_plan(conn, context, authority, row, now)
            grant = AccessGrant.model_validate(row["proposed_grant"])
            if (grant.grant_id, grant.creation_operation_id, grant.creation_authority_revision) != (
                row["grant_id"],
                operation_id,
                intent.proposed_authority_revision,
            ) or grant.state != "prepared":
                raise GrantError("AUTHORITY_STATE_INVALID", 503)
            self._insert_prepared(conn, org, grant, now)
            return self._write_outcome(conn, authority, row, intent, receipt, "committed", now)

    @staticmethod
    def _insert_prepared(conn: Connection, org: str, grant: AccessGrant, now: datetime) -> None:
        conn.execute(
            "INSERT INTO opentour_authority.grants VALUES(%s,%s,%s,%s,'prepared',1,%s)",
            (org, grant.grant_id, grant.creation_operation_id, Jsonb(grant.wire()), now),
        )
        for parent_id in grant.parent_grant_ids:
            conn.execute(
                "INSERT INTO opentour_authority.ancestry VALUES(%s,%s,%s)",
                (org, grant.grant_id, parent_id),
            )
        for template in grant.template_bindings:
            conn.execute(
                "INSERT INTO opentour_authority.template_bindings VALUES(%s,%s,%s,%s,%s)",
                (
                    org,
                    grant.grant_id,
                    template.template_id,
                    int(template.revision),
                    template.sha256,
                ),
            )

    def _abort(
        self, scope: RepositoryScope, intent: AppendIntent, receipt: AppendResponse
    ) -> AppendOutcome:
        self.journal.verifier.response(receipt, intent)
        org, operation_id = intent.organization_id, intent.operation_id
        started = server_time(self.repository.clock())
        with self.repository.transaction(scope) as conn:
            authority, source = self.repository.authority(conn, org, lock=True)
            row = self.repository.retained_operation(conn, org, operation_id, lock=True)
            if row["outcome_body"] is not None:
                return AppendOutcome.parse(bytes(row["outcome_body"]))
            self._require_reservation(authority, source, intent)
            # This is an explicit irreversible SQL abort decision. A completed
            # or in-flight application is fenced by the same locked rows.
            if (
                conn.execute(
                    "SELECT 1 FROM opentour_authority.grants WHERE organization_id=%s "
                    "AND creation_operation_id=%s",
                    (org, operation_id),
                ).fetchone()
                is not None
            ):
                raise GrantError("RECONCILIATION_REQUIRED", 503, retryable=True)
            now = self.repository.now(max(started, row["updated_at"], source["updated_at"]))
            return self._write_outcome(conn, authority, row, intent, receipt, "aborted", now)

    def _write_outcome(
        self,
        conn: Connection,
        authority: AuthorityState,
        row: dict[str, Any],
        intent: AppendIntent,
        receipt: AppendResponse,
        result: Literal["committed", "aborted"],
        now: datetime,
    ) -> AppendOutcome:
        epoch, revision = (
            (intent.proposed_epoch, intent.proposed_authority_revision)
            if result == "committed"
            else (authority.revocation_epoch, authority.authority_revision)
        )
        binding: dict[str, Any] = dict(
            organization_id=intent.organization_id,
            operation_id=intent.operation_id,
            intent_content_sha256=sha256(canonical(intent.wire())),
            result=result,
            resulting_epoch=epoch,
            resulting_authority_revision=revision,
        )
        digest = sha256(canonical(row["proposed_grant"]))
        source: BoundTransactionSource
        if row["operation_kind"] == "creator_installation":
            source = CreatorTransactionSource(
                schema_version="opentour.creator-installation-transaction.v1",
                grant_manifest_sha256=digest,
                **binding,
            )
        else:
            source = TransactionSource(
                schema_version="opentour.grant-transaction-source.v1",
                grant_id=row["grant_id"],
                proposed_grant_sha256=digest,
                **binding,
            )
        ref = self._source_record(conn, source, "transaction", now)
        outcome = AppendOutcome(
            organization_id=intent.organization_id,
            operation_id=intent.operation_id,
            expected_head_sequence=receipt.sequence,
            expected_head_digest=receipt.chain_sha256,
            intent_sequence=receipt.sequence,
            result=result,
            control_transaction_ref=ref,
            resulting_epoch=epoch,
            resulting_authority_revision=revision,
        )
        operation_revision = _next_revision(row["revision"])
        conn.execute(
            "UPDATE opentour_authority.operations SET intent_response=%s,outcome_body=%s,"
            "revision=%s,updated_at=%s WHERE organization_id=%s AND operation_id=%s",
            (
                Jsonb(receipt.wire()),
                canonical(outcome.wire()),
                operation_revision,
                now,
                intent.organization_id,
                intent.operation_id,
            ),
        )
        if result == "committed":
            self._outbox(
                conn,
                intent.organization_id,
                intent.operation_id,
                operation_revision,
                "grant.sql_committed",
                now,
            )
        return outcome

    def _finalize(
        self,
        scope: RepositoryScope,
        context: AuthContext | None,
        outcome: AppendOutcome,
        response: AppendResponse,
        frontier: VerifiedAuthorityFrontier,
    ) -> None:
        self.journal.verifier.response(response, outcome)
        started = server_time(self.repository.clock())
        org, operation_id = outcome.organization_id, outcome.operation_id
        with self.repository.transaction(scope) as conn:
            if context is not None:
                self.repository.canonical_identity(conn, context, started)
            authority, source = self.repository.authority(conn, org, lock=True)
            row = self.repository.retained_operation(conn, org, operation_id, lock=True)
            if row["state"] in {"succeeded", "aborted"}:
                return
            intent = AppendIntent.parse(bytes(row["intent_body"]))
            self._require_reservation(authority, source, intent)
            if row["outcome_body"] is None or bytes(row["outcome_body"]) != canonical(
                outcome.wire()
            ):
                raise GrantError("AUTHORITY_STATE_INVALID", 503)
            now = self.repository.now(max(started, row["updated_at"], source["updated_at"]))
            creation = None
            if outcome.result == "committed":
                if context is None:
                    raise GrantError("SESSION_INVALID", 401)
                self._require_original(scope, context, row)
                now = self._check_plan(conn, context, authority, row, now)
                self._activate_prepared(
                    conn, org, operation_id, row["grant_id"], row["proposed_grant"], now
                )
                creation = GrantCreationReceipt(
                    grant_id=row["grant_id"],
                    revision="1",
                    authority_revision=outcome.resulting_authority_revision,
                    revocation_epoch=outcome.resulting_epoch,
                    committed_at=timestamp(now),
                )
            self._publish(conn, row, outcome, response, frontier, now, creation)

    @staticmethod
    def _activate_prepared(
        conn: Connection,
        org: str,
        operation_id: str,
        grant_id: str,
        grant: dict[str, object],
        now: datetime,
    ) -> None:
        changed = conn.execute(
            "UPDATE opentour_authority.grants SET state='active',updated_at=%s "
            "WHERE organization_id=%s AND grant_id=%s AND creation_operation_id=%s "
            "AND body=%s AND revision=1 AND state='prepared'",
            (now, org, grant_id, operation_id, Jsonb(grant)),
        )
        if changed.rowcount != 1:
            raise GrantError("AUTHORITY_STATE_INVALID", 503)

    def _retain_denied_commit(
        self,
        scope: RepositoryScope,
        outcome: AppendOutcome,
        response: AppendResponse,
        frontier: VerifiedAuthorityFrontier,
    ) -> None:
        self.journal.verifier.response(response, outcome)
        if outcome.result != "committed" or not self._includes_outcome(frontier, outcome, response):
            raise GrantError("RECONCILIATION_REQUIRED", 503, retryable=True)
        org, operation_id = outcome.organization_id, outcome.operation_id
        started = server_time(self.repository.clock())
        with self.repository.transaction(scope) as conn:
            authority, source = self.repository.authority(conn, org, lock=True)
            row = self.repository.retained_operation(conn, org, operation_id, lock=True)
            if row["state"] == "succeeded":
                return
            intent = AppendIntent.parse(bytes(row["intent_body"]))
            self._require_reservation(authority, source, intent)
            if row["outcome_body"] is None or (
                bytes(row["outcome_body"]) != canonical(outcome.wire())
            ):
                raise GrantError("AUTHORITY_STATE_INVALID", 503)
            now = self.repository.now(max(started, row["updated_at"], source["updated_at"]))
            creation = None
            if row["operation_kind"] == "access_grant_create":
                creation = GrantCreationReceipt(
                    grant_id=row["grant_id"],
                    revision="1",
                    authority_revision=outcome.resulting_authority_revision,
                    revocation_epoch=outcome.resulting_epoch,
                    committed_at=timestamp(now),
                )
            # A later identity denial does not undo a signed SQL commit. Keep
            # the grant prepared and require a separately authorized restriction.
            self._publish(conn, row, outcome, response, frontier, now, creation, hold=True)

    def _publish(
        self,
        conn: Connection,
        row: dict[str, Any],
        outcome: AppendOutcome,
        response: AppendResponse,
        frontier: VerifiedAuthorityFrontier,
        now: datetime,
        creation: GrantCreationReceipt | None = None,
        *,
        hold: bool = False,
    ) -> None:
        org, operation_id = outcome.organization_id, outcome.operation_id
        state = "succeeded" if outcome.result == "committed" else "aborted"
        operation_revision = _next_revision(row["revision"])
        conn.execute(
            "UPDATE opentour_authority.operations SET state=%s,revision=%s,outcome_response=%s,"
            "creation_receipt=%s,updated_at=%s WHERE organization_id=%s AND operation_id=%s",
            (
                state,
                operation_revision,
                Jsonb(response.wire()),
                Jsonb(creation.wire()) if creation else None,
                now,
                org,
                operation_id,
            ),
        )
        conn.execute(
            "UPDATE opentour_authority.organizations SET authority_revision=%s,"
            "revocation_epoch=%s,current_operation_id=NULL,state=%s,journal_head=%s,"
            "frontier_proof=%s,updated_at=%s WHERE organization_id=%s",
            (
                outcome.resulting_authority_revision,
                outcome.resulting_epoch,
                "reconciliation_required" if hold else "current",
                Jsonb(frontier.head.wire()),
                Jsonb(frontier.proof_ref.wire()),
                now,
                org,
            ),
        )
        self._outbox(conn, org, operation_id, operation_revision, "grant." + state, now)
        if hold:
            self._outbox(
                conn,
                org,
                operation_id,
                operation_revision,
                "grant.restrictive_followup_required",
                now,
            )

    def _mark_reconciliation(self, scope: RepositoryScope, operation_id: str) -> None:
        try:
            with self.repository.transaction(scope) as conn:
                row = self.repository.retained_operation(
                    conn, scope.organization_id, operation_id, lock=True
                )
                if row["state"] != "prepared":
                    return
                now = self.repository.now(row["updated_at"])
                revision = _next_revision(row["revision"])
                conn.execute(
                    "UPDATE opentour_authority.operations SET state='reconciliation_required',"
                    "revision=%s,updated_at=%s WHERE organization_id=%s AND operation_id=%s",
                    (revision, now, scope.organization_id, operation_id),
                )
                self._outbox(
                    conn,
                    scope.organization_id,
                    operation_id,
                    revision,
                    "grant.reconciliation_required",
                    now,
                )
        except (GrantError, JournalError):
            # Readback will show the retained state. Never synthesize a phase.
            pass


@dataclass(frozen=True)
class GrantHandlers:
    create_router: APIRouter
    operation_progress: Callable[[str, Request], JSONResponse]


def configure_grant_handlers(service: GrantCoordinator, sessions: SessionService) -> GrantHandlers:
    """Mount only workspace creation here. The OPE-202 operations router mounts the reader."""
    router = APIRouter()
    headers = {"Cache-Control": "no-store", "Vary": "Authorization, Cookie"}

    def identity(request: Request) -> Callable[[], AuthContext]:
        authorization = request.headers.get("Authorization", "")
        if not authorization.startswith("Bearer "):
            raise GrantError("SESSION_INVALID", 401)
        token = authorization[7:]
        binding = request.cookies.get("opentour-session", "")
        return lambda: sessions.authorization_context(token, binding, sensitive=True)

    def error_response(error: GrantError, request_id: str) -> JSONResponse:
        return JSONResponse(error.wire(request_id), status_code=error.status, headers=headers)

    @router.post("/v1/workspaces/{workspace_id}/grants")
    async def create(workspace_id: str, request: Request) -> JSONResponse:
        request_id = secrets.token_hex(16)
        try:
            current = identity(request)
            data = bytearray()
            async for chunk in request.stream():
                if len(data) + len(chunk) > 65_536:
                    raise GrantError("SCHEMA_INVALID", 422)
                data.extend(chunk)
            response = await run_in_threadpool(
                service.create,
                current,
                workspace_id,
                request.headers.get("Idempotency-Key", ""),
                request_id,
                bytes(data),
            )
            return JSONResponse(
                response.operation.wire(), status_code=response.status, headers=headers
            )
        except GrantError as error:
            return error_response(error, request_id)
        except (JournalError, ValidationError):
            return error_response(
                GrantError("AUTHORITY_UNAVAILABLE", 503, retryable=True), request_id
            )

    def operation_progress(operation_id: str, request: Request) -> JSONResponse:
        request_id = secrets.token_hex(16)
        try:
            view = service.progress(identity(request), operation_id, request_id)
            return JSONResponse(view.wire(), headers=headers)
        except GrantError as error:
            return error_response(error, request_id)
        except (JournalError, ValidationError):
            return error_response(
                GrantError("AUTHORITY_UNAVAILABLE", 503, retryable=True), request_id
            )

    return GrantHandlers(router, operation_progress)
