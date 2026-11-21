"""Human session admission and own-session context. Capabilities remain separate."""

from __future__ import annotations

import logging
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Literal, Protocol

import psycopg
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .config import AuthError, IdentityConfig, counter, digest, identifier, timestamp, utc
from .repository import IdentityRepository, Observation, Session
from .workos_provider import MembershipProvider, TokenVerifier, VerifiedToken


class IdentityUse(StrEnum):
    ORGANIZATION_CREATE = "organization_create"
    OWN_ORGANIZATIONS = "own_organizations"
    RECIPIENT_INVITATION = "recipient_invitation"
    OWN_SETUP_OPERATION = "own_setup_operation"
    ACCOUNT_ENTRY = "account_entry"
    SESSION_END = "session_end"
    SESSION_CONTEXT = "session_context"


@dataclass(frozen=True)
class LocalMembership:
    """Current171/172 and143 result, including the explicit provider organization binding.

    organization_id is OpenTour's local ID; identity_organization_id comes from171's
    authenticated provider binding for this configured issuer. They are separate
    namespaces. The authority port must never derive one from the other or from JWT
    claims, provider role names, email domains or caller-supplied context.
    """

    subject: str
    organization_id: str
    identity_organization_id: str
    membership_id: str
    provider_membership_id: str
    active: bool
    authority_revision: str
    revocation_epoch: int

    def provider_binding(self) -> tuple[str, str, str, str, str]:
        return (
            self.subject,
            self.organization_id,
            self.membership_id,
            self.identity_organization_id,
            self.provider_membership_id,
        )


class MembershipAuthority(Protocol):
    """Return the current local membership and independently committed provider mapping.

    A mapping change must be visible to the final current() recheck. The provider
    identity belongs to the configured verified issuer, never a response-chosen issuer.
    """

    def current(self, subject: str, organization_id: str) -> LocalMembership | None: ...


@dataclass(frozen=True)
class VerifiedIdentityContext:
    actor: dict[str, str]
    session_id: str
    session_generation: int
    identity_checked_at: str
    authentication_time: str
    assurance_level: Literal["unknown"] = "unknown"

    def wire(self) -> dict[str, object]:
        return {
            "actor": dict(self.actor),
            "session_id": self.session_id,
            "session_generation": self.session_generation,
            "identity_checked_at": self.identity_checked_at,
            "authentication_time": self.authentication_time,
            "assurance_level": self.assurance_level,
        }


@dataclass(frozen=True)
class AuthContext:
    identity: VerifiedIdentityContext
    organization_id: str
    membership_id: str
    authority_revision: str
    revocation_epoch: int

    def wire(self) -> dict[str, object]:
        return {
            **self.identity.wire(),
            "organization_id": self.organization_id,
            "membership_id": self.membership_id,
            "membership_status": "active",
            "authority_revision": self.authority_revision,
            "revocation_epoch": self.revocation_epoch,
            "delegated_grant_id": None,
        }

    def require_organization(self, organization_id: str) -> None:
        # Call before resource lookup; the resource owner still checks capabilities.
        if organization_id != self.organization_id:
            raise AuthError("RESOURCE_NOT_FOUND", 404)


class SessionService:
    def __init__(
        self,
        config: IdentityConfig,
        verifier: TokenVerifier,
        repository: IdentityRepository,
        authority: MembershipAuthority,
        provider: MembershipProvider,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.config, self.verifier, self.repository = config, verifier, repository
        self.authority, self.provider, self.clock = authority, provider, clock

    def admit(self, token: str) -> tuple[VerifiedIdentityContext, str]:
        """Server-only callback port. Return opaque browser binding only after commit."""
        verified, session, handle, checked_at = self._admit_session(
            token, lambda session, binding, _: self.repository.create_session(session, binding)
        )
        return self._context(verified, session, checked_at), handle

    def _admit_session(
        self,
        token: str,
        store: Callable[[Session, str, VerifiedToken], Session],
    ) -> tuple[VerifiedToken, Session, str, datetime]:
        now = utc(self.clock())
        verified = self.verifier.verify(token, now)
        now = self._admission_time(verified, now)
        expires = now + timedelta(seconds=self.config.credential_max_lifetime_seconds)
        proposed = Session(
            verified.issuer,
            verified.session_id,
            verified.subject,
            0,
            None,
            None,
            False,
            now,
            expires,
            None,
            expires,
        )
        handle = secrets.token_urlsafe(32)
        try:
            store(proposed, digest(handle), verified)
            session = self.repository.load_session(digest(handle), utc(self.clock()))
        except psycopg.Error:
            raise AuthError("SESSION_ADMISSION_UNCONFIRMED", 503, True) from None
        return verified, session, handle, self._admission_time(verified, now)

    def _admission_time(self, token: VerifiedToken, started: datetime) -> datetime:
        now = utc(self.clock())
        if now < started:
            raise AuthError("IDENTITY_CLOCK_UNAVAILABLE", 503)
        if now >= token.expires_at:
            raise AuthError("SESSION_INVALID")
        return now

    def _authenticate(
        self, token: str, binding: str, *, allow_ended: bool = False
    ) -> tuple[VerifiedToken, Session, datetime]:
        now = utc(self.clock())
        verified = self.verifier.verify(token, now)
        now = self._admission_time(verified, now)
        if not isinstance(binding, str) or not 32 <= len(binding) <= 128:
            raise AuthError("SESSION_INVALID")
        try:
            session = self.repository.load_session(digest(binding), now, allow_ended=allow_ended)
        except psycopg.Error:
            raise AuthError("IDENTITY_STATE_UNAVAILABLE", 503, True) from None
        if (verified.issuer, verified.session_id, verified.subject) != (
            session.issuer,
            session.session_id,
            session.subject,
        ):
            raise AuthError("SESSION_INVALID")
        counter(session.generation)
        return verified, session, self._admission_time(verified, now)

    @staticmethod
    def _context(
        token: VerifiedToken, session: Session, checked_at: datetime
    ) -> VerifiedIdentityContext:
        return VerifiedIdentityContext(
            {"kind": "human", "subject": token.subject, "issuer": token.issuer},
            token.session_id,
            counter(session.generation),
            timestamp(checked_at),
            timestamp(token.authentication_time),
        )

    def verified_identity(
        self, token: str, binding: str, use: IdentityUse
    ) -> VerifiedIdentityContext:
        identity, _ = self.verified_identity_with_expiry(token, binding, use)
        return identity

    def verified_identity_with_expiry(
        self, token: str, binding: str, use: IdentityUse
    ) -> tuple[VerifiedIdentityContext, datetime]:
        """Return verified identity and the private bearer deadline for a transactional fence."""
        if not isinstance(use, IdentityUse):
            raise AuthError("CAPABILITY_DENIED", 403)
        verified, session, now = self._authenticate(token, binding)
        return self._context(verified, session, now), verified.expires_at

    @staticmethod
    def _matches_selection(session: Session, member: LocalMembership) -> bool:
        return (
            member.subject == session.subject
            and member.organization_id == session.selected_organization_id
            and member.membership_id == session.selected_membership_id
        )

    def _membership(self, session: Session) -> LocalMembership:
        if session.selected_organization_id is None:
            raise AuthError("CONTEXT_RECONCILIATION_REQUIRED", 409)
        member = self.authority.current(session.subject, session.selected_organization_id)
        if (
            member is None
            or not member.active
            or not self._matches_selection(session, member)
        ):
            raise AuthError("MEMBERSHIP_INACTIVE", 403)
        identifier(member.authority_revision)
        identifier(member.identity_organization_id)
        counter(member.revocation_epoch)
        return member

    def _fresh(
        self, session: Session, member: LocalMembership, now: datetime, sensitive: bool
    ) -> Observation:
        observation = self.repository.observation(session.issuer, member.provider_membership_id)
        if observation is not None:
            self._same_membership(session, member, observation)
        if observation and (observation.locally_denied or observation.status == "removed"):
            raise AuthError("MEMBERSHIP_INACTIVE", 403)
        maximum = (
            self.config.sensitive_freshness_seconds
            if sensitive
            else self.config.metadata_freshness_seconds
        )
        if (
            observation is None
            or now < observation.checked_at
            or (now - observation.checked_at).total_seconds() > maximum
        ):
            try:
                fetched = self.provider.current_membership(member.provider_membership_id, now)
            except AuthError as error:
                if error.code != "PROVIDER_MEMBERSHIP_REMOVED":
                    raise
                fetched = Observation(
                    session.issuer,
                    member.provider_membership_id,
                    member.identity_organization_id,
                    session.subject,
                    "removed",
                    now,
                    now,
                    locally_denied=True,
                )
            self._same_membership(session, member, fetched)
            if fetched.status == "removed" or fetched.locally_denied:
                # A request-time removal may create a sticky denial and revocation intent.
                # Confirm that the independent local mapping still names this target.
                current = self.authority.current(session.subject, member.organization_id)
                if current is None:
                    raise AuthError("MEMBERSHIP_INACTIVE", 403)
                if current.provider_binding() != member.provider_binding():
                    raise AuthError("STALE_SESSION_CONTEXT", 409)
            observation = self.repository.record_observation(fetched)
        self._same_membership(session, member, observation)
        if observation.status == "removed" or observation.locally_denied:
            raise AuthError("MEMBERSHIP_INACTIVE", 403)
        if observation.status != "active":
            raise AuthError("CONTEXT_RECONCILIATION_REQUIRED", 409)
        finished = utc(self.clock())
        if finished < now:
            raise AuthError("IDENTITY_CLOCK_UNAVAILABLE", 503)
        if (
            finished < observation.checked_at
            or (finished - observation.checked_at).total_seconds() > maximum
        ):
            raise AuthError("IDENTITY_FRESHNESS_UNAVAILABLE", 503, True)
        return observation

    @staticmethod
    def _same_membership(
        session: Session, member: LocalMembership, observation: Observation
    ) -> None:
        if (
            observation.issuer,
            observation.subject,
            observation.organization_id,
            observation.membership_id,
        ) != (
            session.issuer,
            session.subject,
            member.identity_organization_id,
            member.provider_membership_id,
        ):
            raise AuthError("IDENTITY_MEMBERSHIP_CONFLICT", 403)

    def authorization_context(
        self, token: str, binding: str, *, sensitive: bool = False
    ) -> AuthContext:
        verified, session, now = self._authenticate(token, binding)
        member = self._membership(session)
        if (
            session.reconciliation_required
            or verified.organization_id is None
            or verified.organization_id != member.identity_organization_id
        ):
            raise AuthError("CONTEXT_RECONCILIATION_REQUIRED", 409)
        self._fresh(session, member, now, sensitive)
        observed = self._after_wait(verified, session, binding, member, sensitive=sensitive)
        assert observed is not None
        # No capability is granted here. Each domain action consumes the current143 resolver.
        return AuthContext(
            self._context(verified, session, observed.checked_at),
            member.organization_id,
            member.membership_id,
            member.authority_revision,
            member.revocation_epoch,
        )

    def _after_wait(
        self,
        token: VerifiedToken,
        session: Session,
        binding: str,
        member: LocalMembership | None = None,
        *,
        sensitive: bool = False,
    ) -> Observation | None:
        """Fence asynchronous provider work against current durable and local authority state.

        This is a point-in-time context, not an authority lease. Domain mutations
        must still consume the current authority revision at their own commit boundary.
        """
        now = utc(self.clock())
        if now >= token.expires_at:
            raise AuthError("SESSION_INVALID")
        current = self.repository.load_session(digest(binding), now)
        if (
            current.issuer,
            current.session_id,
            current.subject,
            current.generation,
            current.selected_organization_id,
            current.selected_membership_id,
            current.reconciliation_required,
        ) != (
            session.issuer,
            session.session_id,
            session.subject,
            session.generation,
            session.selected_organization_id,
            session.selected_membership_id,
            session.reconciliation_required,
        ):
            raise AuthError("STALE_SESSION_CONTEXT", 409)
        if member is not None and self._membership(current) != member:
            raise AuthError("STALE_SESSION_CONTEXT", 409)
        observed = None
        if member is not None:
            observed = self.repository.observation(session.issuer, member.provider_membership_id)
            if observed is None:
                raise AuthError("IDENTITY_FRESHNESS_UNAVAILABLE", 503, True)
            self._same_membership(current, member, observed)
            if observed.locally_denied or observed.status == "removed":
                raise AuthError("MEMBERSHIP_INACTIVE", 403)
            if observed.status != "active":
                raise AuthError("CONTEXT_RECONCILIATION_REQUIRED", 409)
        finished = self._admission_time(token, now)
        if observed is not None:
            maximum = (
                self.config.sensitive_freshness_seconds
                if sensitive
                else self.config.metadata_freshness_seconds
            )
            if (
                finished < observed.checked_at
                or (finished - observed.checked_at).total_seconds() > maximum
            ):
                raise AuthError("IDENTITY_FRESHNESS_UNAVAILABLE", 503, True)
            logging.getLogger(__name__).info(
                "identity_admission_checked",
                extra={
                    "identity_observed_at": timestamp(observed.observed_at),
                    "identity_checked_at": timestamp(observed.checked_at),
                    "identity_freshness_policy_seconds": maximum,
                },
            )
        return observed

    def current_context(self, token: str, binding: str, request_id: str) -> dict[str, object]:
        verified, session, now = self._authenticate(token, binding)
        result: dict[str, object] = {
            "state": "unselected",
            "organization_id": None,
            "membership_id": None,
            "session_generation": session.generation,
            "request_id": identifier(request_id),
        }
        if session.selected_organization_id is None:
            if session.reconciliation_required:
                result["state"] = "reconciliation_required"
            return result
        member: LocalMembership | None = None
        try:
            member = self._membership(session)
            self._fresh(session, member, now, False)
            self._after_wait(verified, session, binding, member)
        except AuthError as error:
            if error.code == "MEMBERSHIP_INACTIVE":
                # Never clear a newer selection or recover an ended session.
                self._after_wait(verified, session, binding)
                current = self.authority.current(session.subject, session.selected_organization_id)
                if current is not None and current.active:
                    mapping_changed = (
                        self._matches_selection(session, current)
                        if member is None
                        else current.provider_binding() != member.provider_binding()
                    )
                    if mapping_changed:
                        raise AuthError("STALE_SESSION_CONTEXT", 409) from None
                self._admission_time(verified, now)
                session = self.repository.clear_selection(session, digest(binding))
                self._admission_time(verified, now)
                result["session_generation"] = session.generation
                return result
            if error.status == 503 or error.code in {
                "CONTEXT_RECONCILIATION_REQUIRED", "IDENTITY_MEMBERSHIP_CONFLICT"
            }:
                self._after_wait(verified, session, binding)
                result["state"] = "reconciliation_required"
                return result
            raise
        if (
            session.reconciliation_required
            or verified.organization_id != member.identity_organization_id
        ):
            result["state"] = "reconciliation_required"
        else:
            result.update(
                state="active",
                organization_id=member.organization_id,
                membership_id=member.membership_id,
            )
        return result

    def end(self, token: str, binding: str) -> dict[str, object]:
        """Internal BFF port: caller verifies Origin/CSRF before invoking it."""
        _, session, now = self._authenticate(token, binding, allow_ended=True)
        try:
            ended = self.repository.end_session(session, now)
        except psycopg.Error:
            raise AuthError("SESSION_END_UNCONFIRMED", 503, True) from None
        return {
            "session_id": ended.session_id,
            "session_generation": ended.generation,
            "local_ended": True,
            "provider_completion": "unconfirmed",
        }


def create_session_router(service: SessionService) -> APIRouter:
    """Composition injects configured durable authority/provider ports; no fallback allow."""
    router = APIRouter()

    @router.get("/v1/session/context")
    def session_context(request: Request) -> JSONResponse:
        request_id = secrets.token_hex(16)
        headers = {"Cache-Control": "no-store", "Vary": "Authorization, Cookie"}
        try:
            authorization = request.headers.get("authorization", "")
            if not authorization.startswith("Bearer "):
                raise AuthError("SESSION_INVALID")
            # This opaque credential locates server state, never forwarded AuthContext.
            binding = request.cookies.get("opentour-session", "")
            value = service.current_context(authorization[7:], binding, request_id)
            return JSONResponse(value, headers=headers)
        except AuthError as error:
            return JSONResponse(error.wire(request_id), status_code=error.status, headers=headers)
        except psycopg.Error:
            unavailable = AuthError("IDENTITY_STATE_UNAVAILABLE", 503, True)
            return JSONResponse(unavailable.wire(request_id), status_code=503, headers=headers)

    return router
