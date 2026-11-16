"""Durable control authority coordination. SQL and audit HTTP are separate commits.

The journal client performs one bounded network attempt. This module owns its
SQL claim/token fence and immutable original bytes; timeout is never an abort.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal, Protocol

from opentour_control.auth.grant_models import ClosedModel, GrantError, server_time
from opentour_control.auth.grant_repository import (
    Connection,
    PostgresGrantRepository,
    RepositoryScope,
)
from opentour_journal.crypto import JournalVerifier, canonical, sha256
from opentour_journal.models import (
    MAX_COUNTER,
    MAX_REVISION,
    AppendIntent,
    AppendOutcome,
    AppendResponse,
    Counter,
    Id,
    ImmutableMetadataRef,
    JournalError,
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
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from .journal_client import RetryBinding, RetryClaim


class IntentSource(ClosedModel):
    """Immutable canonical reservation, authored in the same SQL transaction."""

    schema_version: Literal["opentour.grant-intent-source.v1"]
    organization_id: Id
    operation_id: Id
    grant_id: Id
    request_sha256: Sha256
    proposed_grant_sha256: Sha256
    plan_sha256: Sha256
    actor_ref: ImmutableMetadataRef
    mutation_kind: MutationKind
    proposed_epoch: Counter
    proposed_authority_revision: Id
    expected_head_sequence: Counter
    expected_head_digest: Sha256


class ActorSource(ClosedModel):
    schema_version: Literal["opentour.grant-actor-source.v1"]
    organization_id: Id
    operation_id: Id
    issuer: Id
    subject: Id
    session_id: Id
    session_generation: Counter
    membership_id: Id


class TransactionSource(ClosedModel):
    """Actual SQL result, never a timeout/absence inference or caller boolean."""

    schema_version: Literal["opentour.grant-transaction-source.v1"]
    organization_id: Id
    operation_id: Id
    grant_id: Id
    intent_content_sha256: Sha256
    proposed_grant_sha256: Sha256
    result: Literal["committed", "aborted"]
    resulting_epoch: Counter
    resulting_authority_revision: Id


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
        source = IntentSource.model_validate(
            self._source(
                conn, body.organization_id, body.operation_id, "intent", body.scoped_intent_ref
            )
        )
        actor = ActorSource.model_validate(
            self._source(conn, body.organization_id, body.operation_id, "actor", body.actor_ref)
        )
        expected = IntentSource(
            schema_version="opentour.grant-intent-source.v1",
            organization_id=body.organization_id,
            operation_id=body.operation_id,
            grant_id=operation["grant_id"],
            request_sha256=sha256(canonical(operation["request_body"])),
            proposed_grant_sha256=sha256(canonical(operation["proposed_grant"])),
            plan_sha256=sha256(canonical(operation["plan"])),
            actor_ref=body.actor_ref,
            mutation_kind=body.mutation_kind,
            proposed_epoch=operation["proposed_epoch"],
            proposed_authority_revision=operation["proposed_authority_revision"],
            expected_head_sequence=body.expected_head_sequence,
            expected_head_digest=body.expected_head_digest,
        )
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
                source = TransactionSource.model_validate(
                    self._source(
                        conn,
                        body.organization_id,
                        body.operation_id,
                        "transaction",
                        body.control_transaction_ref,
                    )
                )
                if source != TransactionSource(
                    schema_version="opentour.grant-transaction-source.v1",
                    organization_id=body.organization_id,
                    operation_id=body.operation_id,
                    grant_id=operation["grant_id"],
                    intent_content_sha256=sha256(canonical(intent.wire())),
                    proposed_grant_sha256=sha256(canonical(operation["proposed_grant"])),
                    result=body.result,
                    resulting_epoch=body.resulting_epoch,
                    resulting_authority_revision=body.resulting_authority_revision,
                ):
                    raise JournalError("JOURNAL_RECONCILIATION_REQUIRED", 503)
                grant = conn.execute(
                    "SELECT creation_operation_id,body FROM opentour_authority.grants "
                    "WHERE organization_id=%s AND grant_id=%s",
                    (body.organization_id, operation["grant_id"]),
                ).fetchone()
                if body.result == "committed":
                    if grant is None or (
                        grant["creation_operation_id"] != body.operation_id
                        or sha256(canonical(grant["body"])) != source.proposed_grant_sha256
                        or (body.resulting_epoch, body.resulting_authority_revision)
                        != (intent.proposed_epoch, intent.proposed_authority_revision)
                    ):
                        raise JournalError("JOURNAL_RECONCILIATION_REQUIRED", 503)
                # The immutable SQL-aborted source exists before the independent
                # journal outcome. Public operation state must remain pending
                # until that outcome is verified; absence alone proves nothing.
                elif grant is not None or operation["state"] == "succeeded":
                    raise JournalError("JOURNAL_RECONCILIATION_REQUIRED", 503)
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
