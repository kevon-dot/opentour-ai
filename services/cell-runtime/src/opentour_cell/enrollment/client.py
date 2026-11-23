"""Build one v1 enrollment proof from a fresh, installation-bound challenge."""

from __future__ import annotations

import base64
import hashlib
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Protocol
from urllib.parse import parse_qsl, urlsplit

_PROTOCOL = "opentour-enrollment-v1"
# The current native v1 challenge/transcript schemas admit only Virginia.
_REGION = "us-east-1"
_STS_HOST = "sts.us-east-1.amazonaws.com"
_INSTALLATION_FIELDS = {
    "organization_id",
    "environment_id",
    "cell_id",
    "aws_account_id",
    "iam_role_arn",
    "expected_iam_role_id",
    "region",
    "deployment_mode",
    "installer_sha256",
}
_CHALLENGE_FIELDS = _INSTALLATION_FIELDS | {
    "challenge_id",
    "nonce",
    "secret_unavailable",
    "expires_at",
    "proof_schema_version",
}
_STS_QUERY_FIELDS = {
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


class EnrollmentProofUnavailable(ValueError):
    """A fresh proof could not be produced through the qualified local adapters."""


class RunnerSigningKey(Protocol):
    """An independently selected P-256 signing key in the runner's AWS account."""

    def public_jwk(self) -> Mapping[str, str]:
        """Return only the validated EC/P-256 public JWK from this key."""
        ...

    def sign_sha256(self, digest: bytes) -> str:
        """Sign the digest with this key and convert the DER result to ES256 base64url R||S."""
        ...


class CallerIdentityPresigner(Protocol):
    """Presign through the approved runner workload credentials, without sending a request."""

    def presign_get_caller_identity(
        self, *, endpoint: str, region: str, binding_header: str, expires_in: int
    ) -> str:
        """Produce the regional SigV4 GET with the supplied signed enrollment header."""
        ...


def _encoded(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _canonical_base64url(value: object, size: int) -> bool:
    if (
        not isinstance(value, str)
        or len(value) != (size * 8 + 5) // 6
        or re.fullmatch(r"[A-Za-z0-9_-]+", value) is None
    ):
        return False
    decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    return len(decoded) == size and _encoded(decoded) == value


def _text(value: object, limit: int) -> bool:
    return isinstance(value, str) and 0 < len(value) <= limit


def _prepare_transcript(
    challenge: Mapping[str, object], approved: Mapping[str, str | None]
) -> tuple[dict[str, object], datetime]:
    if (
        challenge.keys() != _CHALLENGE_FIELDS
        or approved.keys() != _INSTALLATION_FIELDS
        or any(challenge[field] != value for field, value in approved.items())
        or approved["region"] != _REGION
        or approved["deployment_mode"] not in ("opentour_managed", "customer_controlled")
        or challenge["proof_schema_version"] != _PROTOCOL
        or challenge["secret_unavailable"] is not False
        or not _canonical_base64url(challenge["nonce"], 32)
        or not _text(challenge["challenge_id"], 256)
        or any(
            not _text(approved[field], 256)
            for field in ("organization_id", "environment_id", "cell_id")
        )
    ):
        raise ValueError
    account = approved["aws_account_id"]
    role = approved["iam_role_arn"]
    role_id = approved["expected_iam_role_id"]
    installer = approved["installer_sha256"]
    timestamp = challenge["expires_at"]
    if (
        not isinstance(account, str)
        or re.fullmatch(r"[0-9]{12}", account) is None
        or not isinstance(role, str)
        or len(role) > 1024
        or not role.startswith(f"arn:aws:iam::{account}:role/")
        or not role.rsplit("/", 1)[-1]
        or (role_id is not None and not _text(role_id, 1024))
        or not isinstance(installer, str)
        or re.fullmatch(r"[a-f0-9]{64}", installer) is None
        or not isinstance(timestamp, str)
        or re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z", timestamp
        )
        is None
    ):
        raise ValueError
    transcript: dict[str, object] = {
        **approved,
        "protocol": _PROTOCOL,
        "challenge_id": challenge["challenge_id"],
        "nonce": challenge["nonce"],
    }
    return transcript, datetime.fromisoformat(timestamp)


def _expiry_seconds(now: datetime, deadline: datetime) -> int:
    if now.utcoffset() is None:
        raise ValueError
    seconds = min(60, int((deadline - now).total_seconds()))
    if seconds < 1:
        raise ValueError
    return seconds


def _check_signed_url(url: str, budget: int, *, now: datetime, deadline: datetime) -> None:
    if (
        not isinstance(url, str)
        or len(url) > 12_288
        or re.search(r"[^\x21-\x7e]|#|%(?![0-9a-fA-F]{2})", url)
    ):
        raise ValueError
    parts = urlsplit(url)
    if (parts.scheme, parts.netloc, parts.path) != ("https", _STS_HOST, "/"):
        raise ValueError
    pairs = parse_qsl(
        parts.query, keep_blank_values=True, strict_parsing=True, errors="strict", max_num_fields=9
    )
    query = dict(pairs)
    if (
        len(query) != len(pairs)
        or query.keys() != _STS_QUERY_FIELDS
        or query["Action"] != "GetCallerIdentity"
        or query["Version"] != "2011-06-15"
        or query["X-Amz-Algorithm"] != "AWS4-HMAC-SHA256"
        or query["X-Amz-SignedHeaders"] != "host;x-opentour-enrollment"
        or re.fullmatch(r"[!-~]+", query["X-Amz-Security-Token"]) is None
        or re.fullmatch(r"[a-f0-9]{64}", query["X-Amz-Signature"]) is None
        or re.fullmatch(r"[1-9][0-9]?", query["X-Amz-Expires"]) is None
    ):
        raise ValueError
    date = query["X-Amz-Date"]
    if (
        re.fullmatch(r"[0-9]{8}T[0-9]{6}Z", date) is None
        or re.fullmatch(
            rf"[A-Z0-9]+/{date[:8]}/{_REGION}/sts/aws4_request", query["X-Amz-Credential"]
        )
        is None
    ):
        raise ValueError
    issued_at = datetime.strptime(date, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    seconds = int(query["X-Amz-Expires"])
    if (
        now.utcoffset() is None
        or seconds > budget
        or not issued_at <= now < issued_at + timedelta(seconds=seconds) <= deadline
    ):
        raise ValueError


def build_enrollment_proof(
    challenge: Mapping[str, object],
    *,
    approved_installation: Mapping[str, str | None],
    canonicalize: Callable[[object], bytes],
    signer: RunnerSigningKey,
    presigner: CallerIdentityPresigner,
    clock: Callable[[], datetime],
) -> bytes:
    """Assemble a proof with the qualified RFC8785 codec and signer/presigner.

    The caller supplies the authenticated challenge and independent installer binding.
    It retains the proof privately for submission or exact replay while valid.
    """
    try:
        transcript, deadline = _prepare_transcript(dict(challenge), dict(approved_installation))
        _expiry_seconds(clock(), deadline)
        jwk = dict(signer.public_jwk())
        if (
            jwk.keys() != {"kty", "crv", "x", "y"}
            or jwk["kty"] != "EC"
            or jwk["crv"] != "P-256"
            or not all(_canonical_base64url(jwk[field], 32) for field in ("x", "y"))
        ):
            raise ValueError
        transcript["runner_jwk_thumbprint"] = _encoded(hashlib.sha256(canonicalize(jwk)).digest())
        digest = hashlib.sha256(canonicalize(transcript)).digest()
        _expiry_seconds(clock(), deadline)
        signature = signer.sign_sha256(digest)
        if not _canonical_base64url(signature, 64):
            raise ValueError
        budget = _expiry_seconds(clock(), deadline)
        binding = _encoded(digest)
        url = presigner.presign_get_caller_identity(
            endpoint=f"https://{_STS_HOST}/",
            region=_REGION,
            binding_header=binding,
            expires_in=budget,
        )
        _check_signed_url(url, budget, now=clock(), deadline=deadline)
        body = canonicalize(
            {
                "transcript": transcript,
                "public_jwk": jwk,
                "signed_sts_request": {"method": "GET", "url": url, "binding_header": binding},
                "runner_signature": signature,
            }
        )
        if not isinstance(body, bytes) or not 0 < len(body) <= 16 * 1024:
            raise ValueError
        return body
    except Exception:
        raise EnrollmentProofUnavailable("ENROLLMENT_PROOF_UNAVAILABLE") from None
