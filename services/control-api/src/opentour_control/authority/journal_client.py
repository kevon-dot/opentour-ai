"""Control-side journal client. No AWS storage SDK, table access or signing keys."""

from __future__ import annotations

import asyncio
import math
import secrets
import ssl
import threading
import time
import urllib.parse
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, wait
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol

import httpx2 as httpx
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


class HTTPSJournalTransport:
    """One absolute HTTP deadline, cancelable streaming, no redirect/proxy/retry.

    Four private worker slots bound native DNS/cleanup that may resist cancellation.
    Timed-out work retains its slot until it actually exits; admission never queues
    more work behind it. The caller returns unavailable at the deadline, with no late
    result accepted. A timed-out write may already have reached the remote service.
    Runtime composition must call close() and treat False as incomplete cleanup.
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
        self.tls = tls
        self._capacity = threading.BoundedSemaphore(4)
        self._lock = threading.RLock()
        self._closed = False
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="journal-http")
        self._active: dict[Future[TransportResponse], threading.Event] = {}

    def close(self, timeout: float = 1.0) -> bool:
        """Stop admission, request cancellation and observe cleanup; never pretend success."""
        if not math.isfinite(timeout) or not 0 <= timeout <= 30:
            raise JournalError("JOURNAL_CONFIGURATION_INVALID", 503)
        with self._lock:
            self._closed = True
            active = list(self._active)
            for abort in self._active.values():
                abort.set()
            self._pool.shutdown(wait=False, cancel_futures=True)
        _, pending = wait(active, timeout=timeout)
        if not pending:
            self._pool.shutdown(wait=True)
        return not pending

    def _completed(self, future: Future[TransportResponse]) -> None:
        with self._lock:
            self._active.pop(future, None)
            self._capacity.release()

    async def _http(
        self, url: str, body: bytes, deadline: float, abort: threading.Event
    ) -> TransportResponse:
        remaining = deadline - time.monotonic()
        if abort.is_set() or remaining <= 0:
            raise JournalError("JOURNAL_UNAVAILABLE", 503)
        started = self.monotonic()
        # The145 authorizer is trusted composition, but even a blocked implementation
        # holds a bounded worker slot and cannot dispatch after this request expires.
        headers = self.authorizer.headers("POST", url, body)
        allowed = {"authorization", "x-amz-date", "x-amz-security-token", "x-amz-content-sha256"}
        if (
            not headers
            or len(headers) > len(allowed)
            or any(
                not isinstance(k, str) or not isinstance(v, str)
                or k.lower() not in allowed or "\r" in v or "\n" in v or len(v) > 16384
                for k, v in headers.items()
            )
            or len({k.lower() for k in headers}) != len(headers)
            or "authorization" not in {k.lower() for k in headers}
        ):
            raise JournalError("JOURNAL_FORBIDDEN", 403)
        headers = {**headers, "Content-Type": "application/json", "Accept-Encoding": "identity"}
        remaining = deadline - time.monotonic()
        if abort.is_set() or remaining <= 0:
            raise JournalError("JOURNAL_UNAVAILABLE", 503)
        async with asyncio.timeout(remaining):
            async with httpx.AsyncClient(
                verify=self.tls, timeout=remaining, follow_redirects=False, trust_env=False,
                limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
            ) as client:
                if abort.is_set() or time.monotonic() >= deadline:
                    raise JournalError("JOURNAL_UNAVAILABLE", 503)
                async with client.stream("POST", url, content=body, headers=headers) as response:
                    if response.headers.get("Content-Encoding", "identity") != "identity":
                        raise JournalError("JOURNAL_UNAVAILABLE", 503)
                    payload = bytearray()
                    async for chunk in response.aiter_raw():
                        if abort.is_set() or len(chunk) > MAX_WIRE_BYTES - len(payload):
                            raise JournalError("JOURNAL_UNAVAILABLE", 503)
                        payload.extend(chunk)
                    ended = self.monotonic()
                    if (
                        abort.is_set() or time.monotonic() >= deadline
                        or not math.isfinite(started) or not math.isfinite(ended)
                        or not 0 <= ended - started < self.timeout
                    ):
                        raise JournalError("JOURNAL_UNAVAILABLE", 503)
                    return TransportResponse(response.status_code, bytes(payload))

    def _run(
        self, url: str, body: bytes, deadline: float, abort: threading.Event
    ) -> TransportResponse:
        try:
            return asyncio.run(self._http(url, body, deadline, abort))
        except JournalError:
            raise
        except Exception:
            raise JournalError("JOURNAL_UNAVAILABLE", 503) from None

    def post(self, path: str, body: bytes) -> TransportResponse:
        if path not in PATHS or type(body) is not bytes or len(body) > MAX_WIRE_BYTES:
            raise JournalError("SCHEMA_INVALID", 422)
        deadline = time.monotonic() + self.timeout
        abort = threading.Event()
        with self._lock:
            if self._closed or not self._capacity.acquire(blocking=False):
                raise JournalError("JOURNAL_UNAVAILABLE", 503)
            try:
                future = self._pool.submit(self._run, self.origin + path, body, deadline, abort)
            except Exception:
                self._capacity.release()
                raise JournalError("JOURNAL_UNAVAILABLE", 503) from None
            self._active[future] = abort
            future.add_done_callback(self._completed)
        try:
            return future.result(timeout=max(0.0, deadline - time.monotonic()))
        except FutureTimeout:
            abort.set()
            future.cancel()
            raise JournalError("JOURNAL_UNAVAILABLE", 503) from None
        except JournalError:
            raise
        except Exception:
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
