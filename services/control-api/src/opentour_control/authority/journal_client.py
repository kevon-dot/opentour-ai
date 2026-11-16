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
from typing import Literal, Protocol

from opentour_journal.crypto import JournalVerifier, canonical
from opentour_journal.models import (
    MAX_WIRE_BYTES,
    AppendIntent,
    AppendOutcome,
    AppendResponse,
    ExactEntryRequest,
    ExactEntryResponse,
    ExactOperationRequest,
    ExactOperationResponse,
    JournalError,
    LiveHeadEnvelope,
    LiveHeadRequest,
    Phase,
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


class JournalClient:
    def __init__(
        self,
        transport: AuthenticatedJournalTransport,
        verifier: JournalVerifier,
        checkpoints: CheckpointAuthority,
    ) -> None:
        self.transport, self.verifier = transport, verifier
        self.frontier_verifier = FrontierVerifier(verifier, checkpoints)

    def _post(self, path: str, body: bytes, statuses: tuple[int, ...]) -> bytes:
        response = self.transport.post(path, body)
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
                "RECOVERY_HOLD",
                "JOURNAL_AUTHORITY_CONFLICT",
            }
            try:
                error = strict_json(response.body)
                code = error.get("code") if isinstance(error, dict) else None
                if (
                    isinstance(code, str)
                    and code in codes
                    and response.status in {403, 404, 409, 422, 503}
                ):
                    raise JournalError(code, response.status)
            except JournalError as error:
                if error.code != "SCHEMA_INVALID":
                    raise
            raise JournalError("JOURNAL_UNAVAILABLE", 503)
        return response.body

    def append_intent(self, body: AppendIntent) -> AppendResponse:
        return self._append("intents", body)

    def append_outcome(self, body: AppendOutcome) -> AppendResponse:
        return self._append("outcomes", body)

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
