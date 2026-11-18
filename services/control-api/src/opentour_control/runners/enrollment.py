"""Validate a submitted enrollment proof locally before any STS exchange."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from urllib.parse import parse_qsl, urlsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from opentour_journal.crypto import canonical, raw_to_der
from opentour_journal.models import JournalError

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
) -> None:
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


def validate_enrollment_proof(
    body: bytes, *, installed: InstalledCell, challenge: IssuedChallenge, now: datetime
) -> None:
    """Check local proof conformance. Success grants no identity or challenge state change."""
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
            "runner_jwk_thumbprint": _encoded(hashlib.sha256(canonical(jwk)).digest()),
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
        _validate_sts_request(
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
    except (InvalidSignature, JournalError, OverflowError, RecursionError, ValueError):
        raise _deny() from None
