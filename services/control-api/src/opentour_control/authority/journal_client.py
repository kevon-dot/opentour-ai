"""Control-side journal client. No AWS storage SDK, table access or signing keys."""

from __future__ import annotations

import math
import secrets
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol

from opentour_journal.crypto import JournalVerifier, canonical, sha256
from opentour_journal.models import (
    MAX_COUNTER,
    MAX_REVISION,
    MAX_WIRE_BYTES,
    AppendIntent,
    AppendOutcome,
    AppendResponse,
    ExactEntryRequest,
    ExactEntryResponse,
    ExactOperationRequest,
    ExactOperationResponse,
    JournalError,
    JournalTelemetry,
    LiveHeadEnvelope,
    LiveHeadRequest,
    Phase,
    emit_metric,
    strict_json,
)
from opentour_journal.replay import (
    CheckpointAuthority,
    EvidenceWriter,
    FrontierVerifier,
    VerifiedAuthorityFrontier,
)

PATHS = frozenset(
    {
        "/v1/authority-journal/intents",
        "/v1/authority-journal/outcomes",
        "/v1/authority-journal/heads/read",
        "/v1/authority-journal/operations/read",
        "/v1/authority-journal/entries/read",
    }
)


@dataclass(frozen=True)
class TransportResponse:
    status: int
    body: bytes


class AuthenticatedJournalTransport(Protocol):
    """Only the independently configured live audit endpoint; no request URL override."""

    def post(self, path: str, body: bytes) -> TransportResponse: ...


class WorkloadRequestAuthorizer(Protocol):
    """Qualified workload authenticator signs this exact method/URL/body.

    This is a composition port for OPE-145, not authorization from a caller header.
    The implementation must use current service authority scoped to this audit API.
    """

    def headers(self, method: str, url: str, body: bytes) -> dict[str, str]: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: object, fp: object, code: int, msg: str, headers: object, newurl: str
    ) -> None:
        return None


class HTTPSJournalTransport:
    """Bounded HTTPS client with finite socket timeout, no redirect or implicit retry.

    A timeout/late result is unknown. A socket timeout cannot promise cancellation
    of DNS or a request already accepted by the independent service. Deployment
    config supplies approved origin/213 digest and workload identity; callers do not.
    """

    def __init__(
        self,
        origin: str,
        approved_origin: str,
        configuration_digest: str,
        approved_configuration_digest: str,
        authorizer: WorkloadRequestAuthorizer,
        tls: ssl.SSLContext,
        *,
        timeout: float = 10.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        url = urllib.parse.urlsplit(origin)
        if (
            origin != approved_origin
            or url.scheme != "https"
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.path not in {"", "/"}
            or url.query
            or url.fragment
            or configuration_digest != approved_configuration_digest
            or len(configuration_digest) != 64
            or any(c not in "0123456789abcdef" for c in configuration_digest)
            or tls.verify_mode != ssl.CERT_REQUIRED
            or not tls.check_hostname
            or not math.isfinite(timeout)
            or not 0 < timeout <= 30
        ):
            raise JournalError("JOURNAL_CONFIGURATION_INVALID", 503)
        self.origin = origin.rstrip("/")
        self.authorizer, self.timeout, self.monotonic = authorizer, timeout, monotonic
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _NoRedirect(),
            urllib.request.HTTPSHandler(context=tls),
        )

    def post(self, path: str, body: bytes) -> TransportResponse:
        if path not in PATHS or len(body) > MAX_WIRE_BYTES:
            raise JournalError("SCHEMA_INVALID", 422)
        url = self.origin + path
        headers = self.authorizer.headers("POST", url, body)
        allowed = {"authorization", "x-amz-date", "x-amz-security-token", "x-amz-content-sha256"}
        if (
            not headers
            or len(headers) > len(allowed)
            or any(
                k.lower() not in allowed or "\r" in v or "\n" in v or len(v) > 16384
                for k, v in headers.items()
            )
            or len({k.lower() for k in headers}) != len(headers)
            or "authorization" not in {k.lower() for k in headers}
        ):
            raise JournalError("JOURNAL_FORBIDDEN", 403)
        headers.update({"Content-Type": "application/json", "Accept-Encoding": "identity"})
        request = urllib.request.Request(url, data=body, method="POST", headers=headers)
        started = self.monotonic()
        try:
            try:
                response = self.opener.open(request, timeout=self.timeout)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                payload = response.read(MAX_WIRE_BYTES + 1)
                encoding = response.headers.get("Content-Encoding", "identity")
                if len(payload) > MAX_WIRE_BYTES or encoding != "identity":
                    raise JournalError("JOURNAL_UNAVAILABLE", 503)
                ended = self.monotonic()
                if (
                    not math.isfinite(started)
                    or not math.isfinite(ended)
                    or not 0 <= ended - started < self.timeout
                ):
                    raise JournalError("JOURNAL_UNAVAILABLE", 503)
                return TransportResponse(response.status, bytes(payload))
        except (urllib.error.URLError, OSError, TimeoutError):
            raise JournalError("JOURNAL_UNAVAILABLE", 503) from None


@dataclass(frozen=True)
class ExactOperationObservation:
    state: Literal["committed", "absent_after_strong_read"]
    response: AppendResponse | None


@dataclass(frozen=True)
class RetryBinding:
    organization_id: str
    operation_id: str
    phase: Phase
    body_sha256: str


@dataclass(frozen=True)
class RetryClaim:
    binding: RetryBinding
    original_body: bytes
    token: str
    revision: str
    attempt: int
    first_attempt_at: datetime
    not_before: datetime


class DurableRetryPort(Protocol):
    """Required OPE-143 SQL coordinator port, never an in-memory production fallback.

    claim atomically binds immutable original bytes, refuses changed intent/not-due/
    concurrent execution, increments attempt and durably marks outcome unknown BEFORE
    returning an exclusive claim. Every completion/schedule/hold compares its token
    and revision in the same SQL transaction. A lost commit acknowledgement must raise;
    it never authorizes a second claim until the coordinator reconciles its own SQL.
    Runtime recovery invokes the original body again; no method rewrites expected head.
    """

    def claim(self, binding: RetryBinding, original_body: bytes, now: datetime) -> RetryClaim: ...

    def schedule(self, claim: RetryClaim, not_before: datetime, code: str) -> None: ...

    def complete(self, claim: RetryClaim, response: AppendResponse) -> None: ...

    def hold(self, claim: RetryClaim, code: str) -> None: ...


@dataclass(frozen=True)
class AppendAttempt:
    state: Literal["acknowledged", "retry_scheduled"]
    response: AppendResponse | None
    not_before: datetime | None


def _utc(value: datetime) -> bool:
    return (
        isinstance(value, datetime)
        and value.tzinfo is not None
        and value.utcoffset() == timedelta(0)
    )


class JournalClient:
    def __init__(
        self,
        transport: AuthenticatedJournalTransport,
        verifier: JournalVerifier,
        checkpoints: CheckpointAuthority,
        telemetry: JournalTelemetry,
    ) -> None:
        self.transport, self.verifier = transport, verifier
        self.telemetry = telemetry
        self.frontier_verifier = FrontierVerifier(verifier, checkpoints, telemetry)

    def _post(self, path: str, body: bytes, statuses: tuple[int, ...]) -> bytes:
        try:
            response = self.transport.post(path, body)
        except JournalError:
            raise
        except Exception:
            raise JournalError("JOURNAL_UNAVAILABLE", 503) from None
        if len(response.body) > MAX_WIRE_BYTES:
            raise JournalError("JOURNAL_UNAVAILABLE", 503)
        if response.status not in statuses:
            # Decode only a finite safe error code; never propagate body/message fields.
            codes = {
                "JOURNAL_OPERATION_CONFLICT",
                "AUTHORITY_OPERATION_PENDING",
                "JOURNAL_HEAD_CONFLICT",
                "JOURNAL_FORBIDDEN",
                "JOURNAL_NOT_FOUND",
                "SCHEMA_INVALID",
                "REVISION_EXHAUSTED",
                "JOURNAL_RECONCILIATION_REQUIRED",
                "JOURNAL_UNAVAILABLE",
                "JOURNAL_OBSERVABILITY_UNAVAILABLE",
                "JOURNAL_STATE_INVALID",
                "JOURNAL_SEQUENCE_MISSING",
                "RECOVERY_HOLD",
                "JOURNAL_AUTHORITY_CONFLICT",
            }
            try:
                error = strict_json(response.body)
            except JournalError:
                raise JournalError("JOURNAL_UNAVAILABLE", 503) from None
            code = error.get("code") if isinstance(error, dict) else None
            if (
                isinstance(code, str)
                and code in codes
                and response.status in {403, 404, 409, 422, 503}
            ):
                raise JournalError(code, response.status)
            raise JournalError("JOURNAL_UNAVAILABLE", 503)
        return response.body

    def append_intent(self, body: AppendIntent) -> AppendResponse:
        return self._append("intents", body)

    def append_outcome(self, body: AppendOutcome) -> AppendResponse:
        return self._append("outcomes", body)

    def retry_append(
        self,
        body: AppendIntent | AppendOutcome,
        durable: DurableRetryPort,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        jitter: Callable[[], float] = lambda: secrets.randbelow(1 << 53) / ((1 << 53) - 1),
    ) -> AppendAttempt:
        """One bounded attempt, no sleep. Only the durable scheduler admits later attempts.

        Strong OP absence is not proof of abort. Retrying identical append bytes is safe
        only because the independent service atomically deduplicates that exact operation.
        A stale head, changed body, trust failure or unproven SQL outcome is held, never
        rewritten. This API records append acceptance, not granted authority or SQL success.
        """
        original = canonical(body.wire())
        if len(original) > MAX_WIRE_BYTES:
            raise JournalError("SCHEMA_INVALID", 422)
        phase: Phase = "intent" if isinstance(body, AppendIntent) else "outcome"
        binding = RetryBinding(body.organization_id, body.operation_id, phase, sha256(original))
        now = clock()
        if not _utc(now):
            raise JournalError("JOURNAL_TIME_UNAVAILABLE", 503)
        try:
            claim = durable.claim(binding, original, now)
        except JournalError:
            raise
        except Exception:
            raise JournalError("JOURNAL_RETRY_STATE_UNAVAILABLE", 503) from None
        if (
            not isinstance(claim, RetryClaim)
            or claim.binding != binding
            or claim.original_body != original
            or not isinstance(claim.token, str)
            or not 1 <= len(claim.token) <= 256
            or not isinstance(claim.revision, str)
            or not claim.revision.isascii()
            or not claim.revision.isdecimal()
            or claim.revision.startswith("0")
            or not 1 <= len(claim.revision) <= 19
            or int(claim.revision) > MAX_REVISION
            or type(claim.attempt) is not int
            or not 1 <= claim.attempt <= MAX_COUNTER
            or not _utc(claim.first_attempt_at)
            or not _utc(claim.not_before)
            or not claim.first_attempt_at <= claim.not_before <= now
        ):
            raise JournalError("JOURNAL_RETRY_STATE_INVALID", 503)
        try:
            observation = self.read_exact_operation(body.organization_id, body.operation_id, phase)
            if observation.response is not None:
                response = observation.response
                self.verifier.response(response, body)
            else:
                response = self._append("intents" if phase == "intent" else "outcomes", body)
        except JournalError as error:
            if error.code not in {"JOURNAL_UNAVAILABLE", "JOURNAL_OBSERVABILITY_UNAVAILABLE"}:
                try:
                    durable.hold(claim, error.code)
                except Exception:
                    raise JournalError("JOURNAL_RETRY_STATE_UNAVAILABLE", 503) from None
                raise
            next_time = clock()
            sample = jitter()
            if (
                not _utc(next_time)
                or next_time < now
                or type(sample) not in {int, float}
                or not math.isfinite(sample)
                or not 0 <= sample <= 1
            ):
                raise JournalError("JOURNAL_TIME_UNAVAILABLE", 503) from None
            cap = min(30, 2 ** min(claim.attempt - 1, 5))
            not_before = next_time + timedelta(seconds=1 + sample * (cap - 1))
            try:
                durable.schedule(claim, not_before, error.code)
            except Exception:
                raise JournalError("JOURNAL_RETRY_STATE_UNAVAILABLE", 503) from None
            return AppendAttempt("retry_scheduled", None, not_before)
        try:
            durable.complete(claim, response)
        except Exception:
            raise JournalError("JOURNAL_RETRY_STATE_UNAVAILABLE", 503) from None
        return AppendAttempt("acknowledged", response, None)

    def _append(self, suffix: str, body: AppendIntent | AppendOutcome) -> AppendResponse:
        response = AppendResponse.parse(
            self._post(
                f"/v1/authority-journal/{suffix}",
                canonical(body.wire()),
                (200, 201),
            )
        )
        self.verifier.response(response, body)
        return response

    def read_exact_operation(
        self,
        organization_id: str,
        operation_id: str,
        phase: Phase,
    ) -> ExactOperationObservation:
        request = ExactOperationRequest(
            organization_id=organization_id,
            operation_id=operation_id,
            phase=phase,
        )
        response = ExactOperationResponse.parse(
            self._post(
                "/v1/authority-journal/operations/read",
                canonical(request.wire()),
                (200,),
            )
        )
        if (response.organization_id, response.operation_id, response.phase) != (
            organization_id,
            operation_id,
            phase,
        ):
            raise JournalError("JOURNAL_BINDING_INVALID")
        signed = response.signed_response
        if signed is None:
            # This observation never authorizes abort/retry of an unknown transaction.
            return ExactOperationObservation("absent_after_strong_read", None)
        self.verifier.receipt(signed.signed_receipt, organization_id, operation_id, phase)
        for field in AppendResponse.model_fields:
            if field != "signed_receipt" and getattr(signed, field) != getattr(
                signed.signed_receipt.payload, field
            ):
                raise JournalError("JOURNAL_BINDING_INVALID")
        return ExactOperationObservation("committed", signed)

    def read_live_head(self, organization_id: str, nonce: str | None = None) -> LiveHeadEnvelope:
        nonce = nonce if nonce is not None else secrets.token_hex(32)
        request = LiveHeadRequest(organization_id=organization_id, request_nonce=nonce)
        envelope = LiveHeadEnvelope.parse(
            self._post(
                "/v1/authority-journal/heads/read",
                canonical(request.wire()),
                (200,),
            )
        )
        self.verifier.live_head(envelope, organization_id, nonce)
        return envelope

    def read_entry(self, organization_id: str, sequence: int) -> ExactEntryResponse:
        request = ExactEntryRequest(organization_id=organization_id, sequence=sequence)
        response = ExactEntryResponse.parse(
            self._post(
                "/v1/authority-journal/entries/read",
                canonical(request.wire()),
                (200,),
            )
        )
        self.verifier.entry(response.entry, organization_id, sequence)
        return response

    def verify_frontier(
        self, organization_id: str, writer: EvidenceWriter
    ) -> VerifiedAuthorityFrontier:
        try:
            return self.frontier_verifier.verify(organization_id, self, writer)
        except JournalError:
            raise JournalError("RECOVERY_HOLD") from None

    def require_issuance_frontier(
        self, organization_id: str, writer: EvidenceWriter
    ) -> VerifiedAuthorityFrontier:
        """143 issuance precondition only; this does not issue a lease or authorize an effect."""
        try:
            frontier = self.verify_frontier(organization_id, writer)
            frontier.require_resolved()
            return frontier
        except JournalError:
            emit_metric(self.telemetry, "denied_issuance_count", 1)
            raise
