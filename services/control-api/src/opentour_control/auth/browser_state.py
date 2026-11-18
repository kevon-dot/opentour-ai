"""Durable, server-only browser identity ports. No request transport or new authority."""

from __future__ import annotations

import hmac
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Annotated, Literal, Self

import psycopg
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from .config import MAX_COUNTER, AuthError, FernetSealingPort, counter, digest, timestamp, utc
from .repository import PostgresIdentityRepository, Session
from .session import SessionService, VerifiedIdentityContext
from .workos_provider import VerifiedToken

_Id = Annotated[str, StringConstraints(min_length=1, max_length=256)]
_Token = Annotated[str, StringConstraints(min_length=1, max_length=16_384)]
_FlowPart = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{43}$")]
_BrowserDigest = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
_Handle = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{32,128}$")]
_Generation = Annotated[int, Field(ge=0, le=MAX_COUNTER)]
_FLOW_LIFETIME = timedelta(seconds=600)


class _PrivateModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)


class BrowserCredentials(_PrivateModel):
    access_token: _Token = Field(repr=False)
    refresh_token: _Token = Field(repr=False)
    user_id: _Id


class BrowserSelectionTarget(_PrivateModel):
    organization_id: _Id
    identity_organization_id: _Id
    identity_issuer: _Id
    membership_id: _Id
    authority_revision: _Id


class BrowserSelectionIntent(_PrivateModel):
    organization_id: _Id
    expected_generation: _Generation
    idempotency_key: Annotated[str, StringConstraints(pattern=r"^[!-~]{1,256}$")] = Field(
        repr=False
    )


class BrowserAuthTransaction(_PrivateModel):
    state: _FlowPart = Field(repr=False)
    nonce: _FlowPart = Field(repr=False)
    browser_digest: _BrowserDigest = Field(repr=False)
    verifier: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{43,128}$")] = Field(
        repr=False
    )
    expires_at: AwareDatetime
    return_path: Annotated[str, StringConstraints(min_length=1, max_length=4096)]
    kind: Literal["signin", "switch"]
    session_handle: _Handle | None = Field(repr=False)
    subject: _Id | None
    session_id: _Id | None
    generation: _Generation | None
    selection_target: BrowserSelectionTarget | None
    selection_intent: BrowserSelectionIntent | None = None

    @model_validator(mode="after")
    def validate_binding(self) -> Self:
        if (
            not self.return_path.startswith("/")
            or self.return_path.startswith("//")
            or re.search(r"[\\%\x00-\x20]", self.return_path) is not None
        ):
            raise ValueError("The browser return path must be same-origin")
        coordinates = (
            self.session_handle,
            self.subject,
            self.session_id,
            self.generation,
            self.selection_target,
            self.selection_intent,
        )
        if self.kind == "signin" and any(value is not None for value in coordinates):
            raise ValueError("Ordinary sign-in cannot carry selection coordinates")
        if self.kind == "switch" and any(value is None for value in coordinates):
            raise ValueError("Selection continuation must carry all bound coordinates")
        if self.selection_target is not None and self.selection_intent is not None:
            if (
                self.selection_intent.organization_id != self.selection_target.organization_id
                or self.selection_intent.expected_generation != self.generation
            ):
                raise ValueError("Selection intent must match the target and session generation")
        return self


class _StoredCredentials(_PrivateModel):
    credentials: BrowserCredentials = Field(repr=False)
    authentication_time: AwareDatetime
    verified_at: AwareDatetime


@dataclass(frozen=True)
class BrowserSession:
    """Private server state. An active local row still requires fresh token verification."""

    handle: str = field(repr=False)
    csrf_token: str = field(repr=False)
    session_id: str
    generation: int
    ended: bool
    reconciliation_required: bool
    identity: VerifiedIdentityContext | None
    credentials: BrowserCredentials | None = field(repr=False)
    credential_digest: str | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class BrowserSessionEnd:
    session_id: str
    generation: int


def _binding(handle: str) -> str:
    if not isinstance(handle, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", handle):
        raise AuthError("SESSION_INVALID")
    return digest(handle)


def _flow_coordinates(state_and_nonce: str, browser_digest: str) -> tuple[str, str]:
    if (
        not isinstance(state_and_nonce, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{43}\.[A-Za-z0-9_-]{43}", state_and_nonce)
        or not isinstance(browser_digest, str)
        or not re.fullmatch(r"[a-f0-9]{64}", browser_digest)
    ):
        raise AuthError("CALLBACK_INVALID")
    state_digest = digest(state_and_nonce)
    return state_digest, f"browser-flow:v1:{state_digest}:{browser_digest}"


def _credential_scope(session: Session) -> str:
    coordinates = json.dumps(
        [session.issuer, session.subject, session.session_id], separators=(",", ":")
    )
    return f"browser-credentials:v1:{digest(coordinates)}"


class BrowserAuthState:
    """Typed server operations on the same Python repository used by SessionService."""

    def __init__(
        self, sessions: SessionService, sealer: FernetSealingPort, csrf_key: bytes
    ) -> None:
        if not isinstance(sessions.repository, PostgresIdentityRepository):
            raise ValueError("The durable identity repository is required")
        if not isinstance(csrf_key, bytes) or len(csrf_key) < 32:
            raise ValueError("A dedicated browser CSRF key is required")
        if sealer.max_lifetime_seconds < max(600, sessions.config.credential_max_lifetime_seconds):
            raise ValueError("The sealing policy must cover both configured lifetimes")
        self.sessions = sessions
        self.repository = sessions.repository
        self.sealer = sealer
        self.csrf_key = csrf_key

    def create_flow(self, flow: BrowserAuthTransaction) -> None:
        now = utc(self.sessions.clock())
        state_and_nonce = f"{flow.state}.{flow.nonce}"
        state_digest, scope = _flow_coordinates(state_and_nonce, flow.browser_digest)
        expires_at = utc(flow.expires_at)
        if not now < expires_at <= now + _FLOW_LIFETIME:
            raise AuthError("CALLBACK_INVALID")
        self._check_flow_issuer(flow)
        sealed = self.sealer.seal(flow.model_dump_json(), scope, now, expires_at)
        try:
            self.repository.create_auth_transaction(
                state_digest, flow.browser_digest, sealed, now, expires_at
            )
        except psycopg.Error:
            raise AuthError("IDENTITY_STATE_UNAVAILABLE", 503, True) from None

    def consume_flow(self, state_and_nonce: str, browser_digest: str) -> BrowserAuthTransaction:
        state_digest, scope = _flow_coordinates(state_and_nonce, browser_digest)
        started = utc(self.sessions.clock())
        try:
            sealed = self.repository.consume_auth_transaction(state_digest, browser_digest, started)
        except psycopg.Error:
            raise AuthError("IDENTITY_STATE_UNAVAILABLE", 503, True) from None
        now = self._time_since(started)
        try:
            flow = BrowserAuthTransaction.model_validate_json(self.sealer.open(sealed, scope, now))
        except (AuthError, ValidationError):
            raise AuthError("CALLBACK_INVALID") from None
        if (
            f"{flow.state}.{flow.nonce}" != state_and_nonce
            or flow.browser_digest != browser_digest
            or utc(flow.expires_at) <= now
        ):
            raise AuthError("CALLBACK_INVALID")
        self._check_flow_issuer(flow)
        return flow

    def admit(self, credentials: BrowserCredentials) -> BrowserSession:
        def store(proposed: Session, binding: str, verified: VerifiedToken) -> Session:
            self._match_credentials(proposed, credentials, verified)
            stored = _StoredCredentials(
                credentials=credentials,
                authentication_time=verified.authentication_time,
                verified_at=proposed.created_at,
            )
            sealed = self._seal_credentials(proposed, stored, proposed.created_at)
            return self.repository.create_session(
                proposed, binding, sealed_payload=sealed,
                credential_check=lambda: self.sessions._admission_time(
                    verified, proposed.created_at
                ),
            )

        verified, session, handle, checked_at = self.sessions._admit_session(
            credentials.access_token, store
        )
        return self._view(
            handle,
            session,
            _StoredCredentials(
                credentials=credentials,
                authentication_time=verified.authentication_time,
                verified_at=checked_at,
            ),
        )

    def current(self, handle: str) -> BrowserSession:
        started = utc(self.sessions.clock())
        try:
            session = self._load(handle, started, allow_ended=True)
            if session.ended_at is not None:
                self._check_lifetime(session, started)
                return self._view(handle, session, None)
            sealed = self.repository.read_credentials(session, started)
        except psycopg.Error:
            raise AuthError("IDENTITY_STATE_UNAVAILABLE", 503, True) from None
        now = self._check_lifetime(session, started)
        return self._view(
            handle, session, self._open_credentials(sealed, session, now), digest(sealed)
        )

    def selection_evidence(
        self, access_token: str, handle: str
    ) -> tuple[VerifiedIdentityContext, VerifiedToken, str]:
        verified, session, started = self.sessions._authenticate(access_token, handle)
        try:
            sealed = self.repository.read_credentials(session, started)
        except psycopg.Error:
            raise AuthError("SESSION_CREDENTIALS_UNAVAILABLE", 503, True) from None
        now = self.sessions._admission_time(verified, self._check_lifetime(session, started))
        stored = self._open_credentials(sealed, session, now)
        if not hmac.compare_digest(stored.credentials.access_token.encode(), access_token.encode()):
            raise AuthError("CONTEXT_RECONCILIATION_REQUIRED", 409)
        return self.sessions._context(verified, session, now), verified, digest(sealed)

    def _open_credentials(self, sealed: str, session: Session, now: datetime) -> _StoredCredentials:
        plaintext = self.sealer.open(sealed, _credential_scope(session), now)
        try:
            stored = _StoredCredentials.model_validate_json(plaintext)
        except ValidationError:
            raise AuthError("SESSION_CREDENTIALS_UNAVAILABLE", 503, True) from None
        if (
            stored.credentials.user_id != session.subject
            or not stored.authentication_time <= stored.verified_at <= now
        ):
            raise AuthError("SESSION_CREDENTIALS_UNAVAILABLE", 503, True)
        return stored

    def replace_credentials(
        self, handle: str, generation: int, credentials: BrowserCredentials
    ) -> str:
        started = utc(self.sessions.clock())
        try:
            session = self._load_expected(handle, generation, started)
            verified = self.sessions.verifier.verify(credentials.access_token, started)
            self._match_credentials(session, credentials, verified)
            checked_at = self.sessions._admission_time(verified, started)
            if checked_at >= session.expires_at:
                raise AuthError("SESSION_INVALID")
            stored = _StoredCredentials(
                credentials=credentials,
                authentication_time=verified.authentication_time,
                verified_at=checked_at,
            )
            sealed = self._seal_credentials(session, stored, checked_at)
            self.repository.replace_credentials(session, sealed, checked_at)
        except psycopg.Error:
            raise AuthError("SESSION_CREDENTIALS_UNAVAILABLE", 503, True) from None
        return digest(sealed)

    def end(self, handle: str, generation: int) -> BrowserSessionEnd:
        started = utc(self.sessions.clock())
        try:
            session = self._load_expected(handle, generation, started, allow_ended=True)
            now = self._check_lifetime(session, started)
            ended = self.repository.end_session(session, now)
        except psycopg.Error:
            raise AuthError("SESSION_END_UNCONFIRMED", 503, True) from None
        return BrowserSessionEnd(ended.session_id, ended.generation)

    def require_reconciliation(self, handle: str, generation: int) -> None:
        started = utc(self.sessions.clock())
        try:
            session = self._load_expected(handle, generation, started)
            self._check_lifetime(session, started)
            self.repository.require_reconciliation(session)
        except psycopg.Error:
            raise AuthError("IDENTITY_STATE_UNAVAILABLE", 503, True) from None

    def _load(self, handle: str, now: datetime, *, allow_ended: bool = False) -> Session:
        session = self.repository.load_session(_binding(handle), now, allow_ended=allow_ended)
        if session.issuer != self.sessions.config.issuer:
            raise AuthError("SESSION_INVALID")
        return session

    def _load_expected(
        self, handle: str, generation: int, now: datetime, *, allow_ended: bool = False
    ) -> Session:
        counter(generation)
        session = self._load(handle, now, allow_ended=allow_ended)
        if session.ended_at is None and session.generation != generation:
            raise AuthError("STALE_SESSION_CONTEXT", 409)
        return session

    def _time_since(self, started: datetime) -> datetime:
        now = utc(self.sessions.clock())
        if now < started:
            raise AuthError("IDENTITY_CLOCK_UNAVAILABLE", 503)
        return now

    def _check_lifetime(self, session: Session, started: datetime) -> datetime:
        now = self._time_since(started)
        if now >= session.expires_at:
            raise AuthError("SESSION_INVALID")
        return now

    def _match_credentials(
        self, session: Session, credentials: BrowserCredentials, token: VerifiedToken
    ) -> None:
        if (
            token.issuer != self.sessions.config.issuer
            or token.issuer != session.issuer
            or token.subject != session.subject
            or token.subject != credentials.user_id
            or token.session_id != session.session_id
        ):
            raise AuthError("SESSION_INVALID")

    def _check_flow_issuer(self, flow: BrowserAuthTransaction) -> None:
        if (
            flow.selection_target is not None
            and flow.selection_target.identity_issuer != self.sessions.config.issuer
        ):
            raise AuthError("CALLBACK_INVALID")

    def _seal_credentials(self, session: Session, stored: _StoredCredentials, now: datetime) -> str:
        return self.sealer.seal(
            stored.model_dump_json(), _credential_scope(session), now, session.expires_at
        )

    def _view(
        self, handle: str, session: Session, stored: _StoredCredentials | None,
        credential_digest: str | None = None,
    ) -> BrowserSession:
        csrf = hmac.digest(self.csrf_key, f"browser-csrf:v1:{handle}".encode(), "sha256").hex()
        identity = None
        credentials = None
        if stored is not None:
            credentials = stored.credentials
            identity = VerifiedIdentityContext(
                {"kind": "human", "subject": session.subject, "issuer": session.issuer},
                session.session_id,
                counter(session.generation),
                timestamp(stored.verified_at),
                timestamp(stored.authentication_time),
            )
        return BrowserSession(
            handle,
            csrf,
            session.session_id,
            session.generation,
            session.ended_at is not None,
            session.reconciliation_required,
            identity,
            credentials,
            credential_digest,
        )
