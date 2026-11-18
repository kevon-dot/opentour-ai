"""Validate a submitted enrollment proof locally before any STS exchange."""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import json
import math
import re
import ssl
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol
from urllib.parse import parse_qsl, urlsplit
from xml.etree import ElementTree

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from opentour_journal.crypto import canonical, raw_to_der
from opentour_journal.models import JournalError

MAX_STS_RESPONSE_BYTES = 16 * 1024
_PROOF_ADMITTED = object()
STS_NAMESPACE = "{https://sts.amazonaws.com/doc/2011-06-15/}"
IAM_ROLE_NAME = r"[A-Za-z0-9_+=,.@-]{1,64}"
STS_ROLE_SESSION_NAME = r"[A-Za-z0-9_+=,.@-]{2,64}"
QUALIFIED_STS_HOSTS = {"us-east-1": "sts.us-east-1.amazonaws.com"}
PROOF_FIELDS = {"transcript", "public_jwk", "signed_sts_request", "runner_signature"}
STS_QUERY_FIELDS = {
    "Action",
    "Version",
    "X-Amz-Algorithm",
    "X-Amz-Credential",
    "X-Amz-Date",
    "X-Amz-Expires",
    "X-Amz-Security-Token",
    "X-Amz-Signature",
    "X-Amz-SignedHeaders",
}


class InvalidEnrollmentProof(ValueError):
    """The submitted proof or its independently supplied binding cannot be admitted."""


class EnrollmentVerificationUnavailable(ValueError):
    """Provider evidence cannot establish a current enrollment identity."""


@dataclass(frozen=True)
class STSIdentityRequest:
    """Created only by proof preflight and sent unchanged by the trusted exchange."""

    url: str = field(repr=False)
    binding_header: str = field(repr=False)
    expires_at: datetime
    _preflight: tuple[object, str, str, datetime] | None = field(
        default=None, repr=False, compare=False
    )


@dataclass(frozen=True)
class STSIdentityResponse:
    status: int
    response_url: str = field(repr=False)
    body: bytes = field(repr=False)


class STSIdentityExchange(Protocol):
    def get_caller_identity(
        self, validated: STSIdentityRequest, *, max_response_bytes: int
    ) -> STSIdentityResponse:
        """Send one verified HTTPS GET with x-opentour-enrollment and no extra credentials.

        Verify the exact TLS hostname, send the unmodified URL and binding, bound the
        response while reading, and refuse redirects, proxies and retries. The adapter
        owns timeout and must honor the request expiration as its latest deadline.
        """
        ...


@dataclass(frozen=True)
class InstalledCell:
    """Read from the approved installation record, never from the request body."""

    organization_id: str
    environment_id: str
    cell_id: str
    aws_account_id: str
    iam_role_arn: str
    region: str
    deployment_mode: Literal["opentour_managed", "customer_controlled"]
    installer_sha256: str
    expected_iam_role_id: str | None


@dataclass(frozen=True)
class IssuedChallenge:
    """Metadata for the currently admissible challenge selected by the server."""

    id: str
    installed: InstalledCell
    nonce_sha256: str
    expires_at: datetime


@dataclass(frozen=True)
class VerifiedEnrollmentIdentity:
    """A verified identity candidate. Only an atomic challenge commit can enroll it."""

    challenge_id: str
    installed: InstalledCell
    iam_role_id: str
    runner_jwk_thumbprint: str
    verified_at: datetime
    expires_at: datetime


def _deny() -> InvalidEnrollmentProof:
    return InvalidEnrollmentProof("ENROLLMENT_PROOF_INVALID")


def _object(value: object, fields: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise _deny()
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise _deny()
    return result


def _encoded(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decoded_32(encoded: object) -> bytes:
    if not isinstance(encoded, str) or re.fullmatch(r"[A-Za-z0-9_-]{43}", encoded) is None:
        raise _deny()
    raw = base64.urlsafe_b64decode(encoded + "=")
    if _encoded(raw) != encoded:
        raise _deny()
    return raw


def _validate_sts_request(
    request: object, binding: str, region: str, now: datetime, challenge_expires_at: datetime
) -> STSIdentityRequest:
    request = _object(request, {"method", "url", "binding_header"})
    url = request["url"]
    if (
        request["method"] != "GET"
        or request["binding_header"] != binding
        or not isinstance(url, str)
        or len(url) > 12288
        or re.search(r"[^\x21-\x7e]|#|%(?![0-9A-Fa-f]{2})", url)
    ):
        raise _deny()
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.netloc != QUALIFIED_STS_HOSTS[region] or parts.path != "/":
        raise _deny()
    pairs = parse_qsl(
        parts.query, keep_blank_values=True, strict_parsing=True, errors="strict", max_num_fields=9
    )
    query = dict(pairs)
    if (
        len(query) != len(pairs)
        or set(query) != STS_QUERY_FIELDS
        or query["Action"] != "GetCallerIdentity"
        or query["Version"] != "2011-06-15"
        or query["X-Amz-Algorithm"] != "AWS4-HMAC-SHA256"
        or query["X-Amz-SignedHeaders"] != "host;x-opentour-enrollment"
        or re.fullmatch(r"[a-f0-9]{64}", query["X-Amz-Signature"]) is None
        or re.fullmatch(r"[!-~]+", query["X-Amz-Security-Token"]) is None
        or re.fullmatch(r"[1-9][0-9]?", query["X-Amz-Expires"]) is None
    ):
        raise _deny()
    date = query["X-Amz-Date"]
    if re.fullmatch(r"[0-9]{8}T[0-9]{6}Z", date) is None:
        raise _deny()
    scope = rf"[A-Z0-9]+/{date[:8]}/{region}/sts/aws4_request"
    seconds = int(query["X-Amz-Expires"])
    issued_at = datetime.strptime(date, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    expires_at = issued_at + timedelta(seconds=seconds)
    if (
        re.fullmatch(scope, query["X-Amz-Credential"]) is None
        or seconds > 60
        or not issued_at <= now < expires_at <= challenge_expires_at
    ):
        raise _deny()
    return STSIdentityRequest(url, binding, expires_at)


def validate_enrollment_proof(
    body: bytes, *, installed: InstalledCell, challenge: IssuedChallenge, now: datetime
) -> None:
    """Check local proof conformance. Success grants no identity or challenge state change."""
    _validate_enrollment_proof(body, installed=installed, challenge=challenge, now=now)


def _validate_enrollment_proof(
    body: bytes, *, installed: InstalledCell, challenge: IssuedChallenge, now: datetime
) -> tuple[STSIdentityRequest, str]:
    try:
        if (
            not 0 < len(body) <= 16 * 1024
            or challenge.installed != installed
            or installed.region not in QUALIFIED_STS_HOSTS
            or installed.deployment_mode not in {"opentour_managed", "customer_controlled"}
            or re.fullmatch(r"[0-9]{12}", installed.aws_account_id) is None
            or not installed.iam_role_arn.startswith(
                f"arn:aws:iam::{installed.aws_account_id}:role/"
            )
            or installed.iam_role_arn.endswith("/")
            or re.fullmatch(r"[a-f0-9]{64}", installed.installer_sha256) is None
            or now.utcoffset() is None
            or challenge.expires_at.utcoffset() is None
            or now >= challenge.expires_at
        ):
            raise _deny()
        proof = _object(
            json.loads(body.decode("utf-8"), object_pairs_hook=_unique_object), PROOF_FIELDS
        )
        jwk = _object(proof["public_jwk"], {"kty", "crv", "x", "y"})
        if jwk["kty"] != "EC" or jwk["crv"] != "P-256":
            raise _deny()
        x, y = _decoded_32(jwk["x"]), _decoded_32(jwk["y"])
        public_key = ec.EllipticCurvePublicNumbers(
            int.from_bytes(x, "big"), int.from_bytes(y, "big"), ec.SECP256R1()
        ).public_key()
        thumbprint = _encoded(hashlib.sha256(canonical(jwk)).digest())
        expected: dict[str, str | None] = {
            "protocol": "opentour-enrollment-v1",
            "challenge_id": challenge.id,
            "nonce": None,
            "organization_id": installed.organization_id,
            "environment_id": installed.environment_id,
            "cell_id": installed.cell_id,
            "aws_account_id": installed.aws_account_id,
            "iam_role_arn": installed.iam_role_arn,
            "expected_iam_role_id": installed.expected_iam_role_id,
            "region": installed.region,
            "deployment_mode": installed.deployment_mode,
            "installer_sha256": installed.installer_sha256,
            "runner_jwk_thumbprint": thumbprint,
        }
        supplied = _object(proof["transcript"], set(expected))
        nonce = supplied["nonce"]
        if not isinstance(nonce, str) or not hmac.compare_digest(
            hashlib.sha256(_decoded_32(nonce)).hexdigest(), challenge.nonce_sha256
        ):
            raise _deny()
        expected["nonce"] = nonce
        if supplied != expected:
            raise _deny()
        transcript = canonical(expected)
        request = _validate_sts_request(
            proof["signed_sts_request"],
            _encoded(hashlib.sha256(transcript).digest()),
            installed.region,
            now,
            challenge.expires_at,
        )
        signature = proof["runner_signature"]
        if not isinstance(signature, str):
            raise _deny()
        public_key.verify(raw_to_der(signature), transcript, ec.ECDSA(hashes.SHA256()))
        return replace(
            request,
            _preflight=(_PROOF_ADMITTED, request.url, request.binding_header, request.expires_at),
        ), thumbprint
    except (InvalidSignature, JournalError, OverflowError, RecursionError, ValueError):
        raise _deny() from None


def verify_enrollment_identity(
    body: bytes,
    *,
    installed: InstalledCell,
    challenge: IssuedChallenge,
    exchange: STSIdentityExchange,
    clock: Callable[[], datetime],
) -> VerifiedEnrollmentIdentity:
    """Verify the proof and trusted provider response without consuming the challenge."""
    started_at = clock()
    request, thumbprint = _validate_enrollment_proof(
        body, installed=installed, challenge=challenge, now=started_at
    )
    registered_role = re.fullmatch(
        rf"arn:aws:iam::{installed.aws_account_id}:role/(?:[!-~]{{1,510}}/)?"
        rf"(?P<name>{IAM_ROLE_NAME})",
        installed.iam_role_arn,
    )
    if registered_role is None:
        raise _deny()
    try:
        response = exchange.get_caller_identity(request, max_response_bytes=MAX_STS_RESPONSE_BYTES)
        verified_at = clock()
        if verified_at.utcoffset() is None or not started_at <= verified_at < request.expires_at:
            raise ValueError
        identity = _read_sts_identity(request, response)
    except Exception:
        # A transport exception may contain the credential-bearing URL.
        raise EnrollmentVerificationUnavailable("ENROLLMENT_VERIFICATION_UNAVAILABLE") from None

    role_name = re.escape(registered_role["name"])
    arn = re.fullmatch(
        rf"arn:aws:sts::{installed.aws_account_id}:assumed-role/{role_name}/"
        rf"(?P<session>{STS_ROLE_SESSION_NAME})",
        identity["Arn"],
    )
    role_id, _, session = identity["UserId"].partition(":")
    if (
        identity["Account"] != installed.aws_account_id
        or arn is None
        or session != arn["session"]
        or re.fullmatch(r"[A-Za-z0-9_]{16,128}", role_id) is None
        or installed.expected_iam_role_id not in (None, role_id)
    ):
        raise _deny()
    return VerifiedEnrollmentIdentity(
        challenge.id, installed, role_id, thumbprint, verified_at, request.expires_at
    )


def _read_sts_identity(
    request: STSIdentityRequest, response: STSIdentityResponse
) -> dict[str, str]:
    body = response.body
    if (
        response.status != 200
        or response.response_url != request.url
        or type(body) is not bytes
        or not 0 < len(body) <= MAX_STS_RESPONSE_BYTES
        or b"<!" in body
    ):
        raise ValueError
    root = ElementTree.fromstring(body.decode("utf-8"))
    if root.tag != STS_NAMESPACE + "GetCallerIdentityResponse":
        raise ValueError
    result = _xml_fields(root, {"GetCallerIdentityResult", "ResponseMetadata"})
    metadata = _xml_text(result["ResponseMetadata"], {"RequestId"})
    if re.fullmatch(r"[A-Za-z0-9-]{1,128}", metadata["RequestId"]) is None:
        raise ValueError
    return _xml_text(result["GetCallerIdentityResult"], {"Account", "Arn", "UserId"})


def _xml_fields(element: ElementTree.Element, names: set[str]) -> dict[str, ElementTree.Element]:
    parsed = {child.tag: child for child in element}
    if (
        element.attrib
        or (element.text or "").strip()
        or len(parsed) != len(element)
        or set(parsed) != {STS_NAMESPACE + name for name in names}
        or any((child.tail or "").strip() for child in element)
    ):
        raise ValueError
    return {name: parsed[STS_NAMESPACE + name] for name in names}


def _xml_text(element: ElementTree.Element, names: set[str]) -> dict[str, str]:
    text: dict[str, str] = {}
    for name, value in _xml_fields(element, names).items():
        if value.attrib or len(value) or not value.text:
            raise ValueError
        text[name] = value.text
    return text


class HTTPSSTSIdentityExchange:
    """One direct TLS request per admitted proof, with a bounded local wait and response."""

    def __init__(
        self,
        *,
        region: str,
        trusted_ca_pem: bytes,
        timeout_seconds: float,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        try:
            if (
                region not in QUALIFIED_STS_HOSTS
                or type(timeout_seconds) not in (float, int)
                or not math.isfinite(timeout_seconds)
                or not 0 < timeout_seconds <= 60
            ):
                raise ValueError
            # Only explicitly approved server roots. No environment trust replacement,
            # TLS key log, client certificates, credential resolver or proxy discovery.
            tls = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            tls.minimum_version = ssl.TLSVersion.TLSv1_2
            tls.load_verify_locations(cadata=trusted_ca_pem.decode("ascii"))
            if (
                tls.verify_mode != ssl.CERT_REQUIRED
                or not tls.check_hostname
                or tls.cert_store_stats()["x509_ca"] == 0
            ):
                raise ValueError
        except (OSError, ValueError):
            raise EnrollmentVerificationUnavailable("ENROLLMENT_VERIFICATION_UNAVAILABLE") from None
        self._tls, self._region, self._clock = tls, region, clock
        self._timeout = timeout_seconds
        self._close_lock = threading.Lock()
        self._closed = threading.Event()
        # Native DNS can outlive an I/O timeout. A timed-out worker keeps its slot until
        # disconnect and rechecks the deadline before sending the proof.
        self._slots = threading.BoundedSemaphore(4)
        self._workers = ThreadPoolExecutor(max_workers=4, thread_name_prefix="enrollment-sts")

    def close(self) -> None:
        """Refuse new requests and close workers as their I/O returns."""
        with self._close_lock:
            self._closed.set()
            self._workers.shutdown(wait=False, cancel_futures=True)

    def get_caller_identity(
        self, validated: STSIdentityRequest, *, max_response_bytes: int
    ) -> STSIdentityResponse:
        aborted = threading.Event()
        try:
            if (
                type(validated) is not STSIdentityRequest
                or type(max_response_bytes) is not int
                or not 0 < max_response_bytes <= MAX_STS_RESPONSE_BYTES
                or validated._preflight
                != (_PROOF_ADMITTED, validated.url, validated.binding_header, validated.expires_at)
            ):
                raise ValueError
            now = self._clock()
            _validate_sts_request(
                {"method": "GET", "url": validated.url, "binding_header": validated.binding_header},
                validated.binding_header,
                self._region,
                now,
                validated.expires_at,
            )
            deadline = time.monotonic() + min(
                self._timeout, (validated.expires_at - now).total_seconds()
            )
            with self._close_lock:
                if self._closed.is_set() or not self._slots.acquire(blocking=False):
                    raise ValueError
                try:
                    future = self._workers.submit(
                        self._exchange, validated, max_response_bytes, deadline, aborted
                    )
                except Exception:
                    self._slots.release()
                    raise
                future.add_done_callback(lambda _future: self._slots.release())
            response = future.result(timeout=self._remaining(validated, deadline, aborted))
            self._remaining(validated, deadline, aborted)
            return response
        except Exception:
            aborted.set()
            raise EnrollmentVerificationUnavailable("ENROLLMENT_VERIFICATION_UNAVAILABLE") from None

    def _remaining(
        self, request: STSIdentityRequest, deadline: float, aborted: threading.Event
    ) -> float:
        remaining = min(
            deadline - time.monotonic(), (request.expires_at - self._clock()).total_seconds()
        )
        if aborted.is_set() or self._closed.is_set() or remaining <= 0:
            raise TimeoutError
        return remaining

    def _exchange(
        self,
        request: STSIdentityRequest,
        limit: int,
        deadline: float,
        aborted: threading.Event,
    ) -> STSIdentityResponse:
        parts = urlsplit(request.url)
        connection = http.client.HTTPSConnection(
            QUALIFIED_STS_HOSTS[self._region],
            port=443,
            context=self._tls,
            timeout=self._remaining(request, deadline, aborted),
        )
        try:
            connection.set_debuglevel(0)
            # TLS and SNI validation finish before the credential-bearing query is sent.
            connection.connect()
            if connection.sock is None:
                raise ValueError
            connection.sock.settimeout(self._remaining(request, deadline, aborted))
            connection.request(
                "GET",
                f"{parts.path}?{parts.query}",
                headers={
                    "x-opentour-enrollment": request.binding_header,
                    "Accept-Encoding": "identity",
                    "Connection": "close",
                },
            )
            with connection.getresponse() as response:
                declared = response.getheader("Content-Length")
                transfer = response.getheader("Transfer-Encoding")
                if (
                    response.status != 200
                    or response.getheader("Content-Encoding", "identity") != "identity"
                    or transfer not in (None, "chunked")
                    or (
                        declared is not None
                        and (
                            transfer is not None
                            or re.fullmatch(r"[0-9]{1,8}", declared) is None
                            or int(declared) > limit
                        )
                    )
                ):
                    raise ValueError
                body = bytearray()
                while True:
                    self._remaining(request, deadline, aborted)
                    chunk = response.read1(min(4096, limit - len(body) + 1))
                    if not chunk:
                        break
                    body.extend(chunk)
                    if len(body) > limit:
                        raise ValueError
                if declared is not None and len(body) != int(declared):
                    raise ValueError
                return STSIdentityResponse(response.status, request.url, bytes(body))
        finally:
            connection.close()
