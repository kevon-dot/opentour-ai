"""Durable enrollment metadata, with an authoritative environment transaction port."""

from __future__ import annotations

import re
from base64 import urlsafe_b64encode
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from hashlib import sha256 as sha256_bytes
from typing import Any, Literal, Protocol
from uuid import uuid4

import psycopg
from opentour_journal.crypto import canonical, sha256
from psycopg.pq import TransactionStatus

from ..auth.config import identifier
from ..auth.grant_repository import Connection, ConnectionFactory
from .enrollment import (
    QUALIFIED_STS_HOSTS,
    InstalledCell,
    IssuedChallenge,
    VerifiedEnrollmentIdentity,
    enrollment_proof_sha256,
    validate_enrollment_proof,
)

Mode = Literal["initial", "rotation", "recovery"]
MAX_EPOCH = 9_007_199_254_740_991
MAX_REVISION = 9_223_372_036_854_775_807


class EnrollmentStoreError(ValueError):
    """A redacted durable enrollment decision; no provider or SQL bytes."""


@dataclass(frozen=True)
class EnrollmentScope:
    """Already authenticated by runtime composition, never the request body's scope."""

    organization_id: str
    environment_id: str
    actor_id: str


@dataclass(frozen=True)
class EnrollmentAuthority:
    installation: InstalledCell
    qualified_installation_sha256: str
    environment_revision: int


class EnrollmentAuthorityStore(Protocol):
    def lock_current(
        self, conn: Connection, scope: EnrollmentScope, mode: Mode, *, issuing: bool
    ) -> EnrollmentAuthority | None:
        """Authorize the operation and lock its current qualified environment in this transaction.

        The owner checks current environment.manage or proof admission, plus mode-specific
        current runner/explicit recovery authority. It uses the supplied connection and
        performs no provider I/O. An unavailable or unauthorized source returns None.
        """
        ...

    def advance_revision(
        self, conn: Connection, scope: EnrollmentScope, *, expected: int, proposed: int
    ) -> bool:
        """CAS the locked authoritative environment revision in the same transaction."""
        ...


@dataclass(frozen=True)
class EnrollmentReceipt:
    runner_id: str
    cell_id: str
    environment_id: str
    identity_epoch: int
    bound_role_id: str
    runner_jwk_thumbprint: str
    observed_at: datetime
    state: Literal["enrolled"] = "enrolled"


class PostgresEnrollmentStore:
    def __init__(
        self,
        connect: ConnectionFactory,
        authority: EnrollmentAuthorityStore,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._connect, self._owner, self._clock = connect, authority, clock

    @contextmanager
    def _transaction(self, scope: EnrollmentScope) -> Iterator[Connection]:
        try:
            with self._connect() as conn:
                if conn.info.transaction_status != TransactionStatus.IDLE:
                    raise EnrollmentStoreError("ENROLLMENT_TRANSACTION_REQUIRED")
                with conn.transaction():
                    role = conn.execute(
                        "SELECT r.rolsuper, r.rolbypassrls, EXISTS (SELECT 1 FROM pg_class c "
                        "JOIN pg_namespace n ON n.oid=c.relnamespace "
                        "WHERE n.nspname='opentour_enrollment' AND "
                        "pg_has_role(current_user,c.relowner,'MEMBER')) AS owns "
                        "FROM pg_roles r WHERE r.rolname=current_user"
                    ).fetchone()
                    if role is None or role["rolsuper"] or role["rolbypassrls"] or role["owns"]:
                        raise EnrollmentStoreError("ENROLLMENT_DATABASE_ROLE_INVALID")
                    conn.execute(
                        "SELECT set_config('opentour.organization_id', %s, true)",
                        (identifier(scope.organization_id),),
                    )
                    identifier(scope.environment_id)
                    identifier(scope.actor_id)
                    yield conn
        except psycopg.Error:
            raise EnrollmentStoreError("ENROLLMENT_SQL_UNCONFIRMED") from None

    def _now(self) -> datetime:
        now = self._clock()
        if now.utcoffset() is None:
            raise EnrollmentStoreError("ENROLLMENT_CLOCK_UNAVAILABLE")
        return now.astimezone(UTC)

    def _authority(
        self, conn: Connection, scope: EnrollmentScope, mode: Mode, *, issuing: bool
    ) -> EnrollmentAuthority:
        current = self._owner.lock_current(conn, scope, mode, issuing=issuing)
        if (
            current is None
            or current.installation.organization_id != scope.organization_id
            or current.installation.environment_id != scope.environment_id
            or current.installation.region not in QUALIFIED_STS_HOSTS
            or type(current.environment_revision) is not int
            or not 1 <= current.environment_revision <= MAX_REVISION
        ):
            raise EnrollmentStoreError("ENROLLMENT_NOT_FOUND")
        _digest(current.qualified_installation_sha256)
        return current

    def record_challenge(
        self,
        scope: EnrollmentScope,
        challenge: IssuedChallenge,
        *,
        mode: Mode,
        expected_environment_revision: int,
    ) -> None:
        """Record an independently approved issuance/reissue. The nonce itself never enters SQL."""
        if (
            mode not in {"initial", "rotation", "recovery"}
            or type(expected_environment_revision) is not int
            or not 1 <= expected_environment_revision <= MAX_REVISION
        ):
            raise EnrollmentStoreError("ENROLLMENT_CHALLENGE_INVALID")
        identifier(challenge.id)
        _digest(challenge.nonce_sha256)
        with self._transaction(scope) as conn:
            current = self._authority(conn, scope, mode, issuing=True)
            now = self._now()
            if current.environment_revision != expected_environment_revision:
                raise EnrollmentStoreError("STALE_REVISION")
            if (
                current.installation != challenge.installed
                or challenge.expires_at.utcoffset() is None
                or challenge.expires_at <= now
            ):
                raise EnrollmentStoreError("ENROLLMENT_CHALLENGE_INVALID")
            existing = self._read_challenge(conn, scope, challenge.id)
            if existing is not None:
                if (
                    existing["mode"] == mode
                    and existing["environment_revision"] == expected_environment_revision
                    and existing["installation_sha256"] == current.qualified_installation_sha256
                    and existing["installed_scope_sha256"]
                    == _installation_digest(challenge.installed)
                    and existing["nonce_sha256"] == challenge.nonce_sha256
                    and existing["expires_at"] == challenge.expires_at
                ):
                    return
                raise EnrollmentStoreError("IDEMPOTENCY_CONFLICT")
            previous = self._runner(conn, scope)
            epoch = _assert_transition(mode, current.installation, previous)
            conn.execute(
                "UPDATE opentour_enrollment.challenges SET closed_at=%s "
                "WHERE organization_id=%s AND environment_id=%s AND closed_at IS NULL",
                (now, scope.organization_id, scope.environment_id),
            )
            conn.execute(
                "INSERT INTO opentour_enrollment.challenges (organization_id, environment_id, "
                "challenge_id, mode, environment_revision, identity_epoch, installation_sha256, "
                "installed_scope_sha256, nonce_sha256, issued_at, expires_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    scope.organization_id,
                    scope.environment_id,
                    challenge.id,
                    mode,
                    expected_environment_revision,
                    epoch,
                    current.qualified_installation_sha256,
                    _installation_digest(challenge.installed),
                    challenge.nonce_sha256,
                    now,
                    challenge.expires_at,
                ),
            )

    def prepare_proof(
        self, scope: EnrollmentScope, challenge_id: str, operation_id: str, body: bytes
    ) -> IssuedChallenge | EnrollmentReceipt:
        """Claim a locally verified proof before provider I/O, or return the exact replay."""
        digest = enrollment_proof_sha256(body)
        identifier(operation_id)
        with self._transaction(scope) as conn:
            current, challenge = self._lock_challenge(conn, scope, challenge_id)
            receipt = self._replay(conn, scope, challenge_id, operation_id, digest)
            if receipt is not None:
                return receipt
            now = self._now()
            self._check_pending(current, challenge, now)
            pending = IssuedChallenge(
                challenge_id,
                current.installation,
                challenge["nonce_sha256"],
                challenge["expires_at"],
            )
            validate_enrollment_proof(
                body, installed=current.installation, challenge=pending, now=now
            )
            if challenge["prepared_at"] is not None:
                self._assert_prepared(challenge, scope, operation_id, digest)
                return pending
            other = conn.execute(
                "SELECT 1 FROM opentour_enrollment.challenges WHERE organization_id=%s "
                "AND environment_id=%s AND prepared_operation_id=%s",
                (scope.organization_id, scope.environment_id, operation_id),
            ).fetchone()
            if other is not None:
                raise EnrollmentStoreError("IDEMPOTENCY_CONFLICT")
            claim = conn.execute(
                "UPDATE opentour_enrollment.challenges SET prepared_operation_id=%s, "
                "prepared_actor_id=%s, prepared_proof_sha256=%s, prepared_at=%s "
                "WHERE organization_id=%s AND environment_id=%s AND challenge_id=%s "
                "AND closed_at IS NULL AND prepared_at IS NULL",
                (
                    operation_id,
                    scope.actor_id,
                    digest,
                    now,
                    scope.organization_id,
                    scope.environment_id,
                    challenge_id,
                ),
            )
            if claim.rowcount != 1:
                raise EnrollmentStoreError("STALE_REVISION")
            return pending

    @staticmethod
    def _assert_prepared(
        challenge: Mapping[str, Any], scope: EnrollmentScope, operation_id: str, digest: str
    ) -> None:
        if challenge["prepared_at"] is None:
            raise EnrollmentStoreError("ENROLLMENT_PROOF_NOT_PREPARED")
        if (
            challenge["prepared_operation_id"] != operation_id
            or challenge["prepared_actor_id"] != scope.actor_id
            or challenge["prepared_proof_sha256"] != digest
        ):
            raise EnrollmentStoreError("IDEMPOTENCY_CONFLICT")

    def consume(
        self, scope: EnrollmentScope, operation_id: str, verified: VerifiedEnrollmentIdentity
    ) -> EnrollmentReceipt:
        """CAS the verified identity, challenge, receipt and event in one SQL transaction."""
        identifier(operation_id)
        if (
            type(verified) is not VerifiedEnrollmentIdentity
            or not verified.matches_verifier_result()
        ):
            raise EnrollmentStoreError("ENROLLMENT_PROOF_INVALID")
        _digest(verified.proof_sha256)
        with self._transaction(scope) as conn:
            current, challenge = self._lock_challenge(conn, scope, verified.challenge_id)
            receipt = self._replay(
                conn, scope, verified.challenge_id, operation_id, verified.proof_sha256
            )
            if receipt is not None:
                return receipt
            now = self._now()
            self._check_pending(current, challenge, now)
            self._assert_prepared(challenge, scope, operation_id, verified.proof_sha256)
            if (
                current.installation != verified.installed
                or verified.challenge_nonce_sha256 != challenge["nonce_sha256"]
                or verified.verified_at.utcoffset() is None
                or verified.expires_at.utcoffset() is None
                or not challenge["prepared_at"] <= verified.verified_at <= now < verified.expires_at
                or verified.expires_at > challenge["expires_at"]
                or current.installation.expected_iam_role_id not in (None, verified.iam_role_id)
                or re.fullmatch(r"[A-Za-z0-9_]{16,128}", verified.iam_role_id) is None
                or not all(
                    re.fullmatch(r"[A-Za-z0-9_-]{43}", item)
                    for item in (
                        verified.public_key_x,
                        verified.public_key_y,
                        verified.runner_jwk_thumbprint,
                    )
                )
                or _thumbprint(verified.public_key_x, verified.public_key_y)
                != verified.runner_jwk_thumbprint
            ):
                raise EnrollmentStoreError("ENROLLMENT_PROOF_INVALID")
            previous = self._runner(conn, scope)
            epoch = _assert_transition(challenge["mode"], current.installation, previous)
            revision = current.environment_revision
            if epoch != challenge["identity_epoch"]:
                raise EnrollmentStoreError("STALE_REVISION")
            if (
                previous is not None
                and previous["key_thumbprint"] == verified.runner_jwk_thumbprint
            ):
                raise EnrollmentStoreError("ENROLLMENT_TRANSITION_DENIED")
            if epoch >= MAX_EPOCH or revision >= MAX_REVISION:
                raise EnrollmentStoreError("REVISION_EXHAUSTED")
            if not self._owner.advance_revision(
                conn, scope, expected=revision, proposed=revision + 1
            ):
                raise EnrollmentStoreError("STALE_REVISION")
            accepted_at = self._now()
            if not now <= accepted_at < verified.expires_at:
                raise EnrollmentStoreError("ENROLLMENT_CHALLENGE_UNAVAILABLE")
            receipt = EnrollmentReceipt(
                previous["runner_id"] if previous else uuid4().hex,
                verified.installed.cell_id,
                scope.environment_id,
                epoch + 1,
                verified.iam_role_id,
                verified.runner_jwk_thumbprint,
                accepted_at,
            )
            self._commit(conn, scope, operation_id, verified, receipt, revision + 1)
            if not accepted_at <= self._now() < verified.expires_at:
                raise EnrollmentStoreError("ENROLLMENT_CHALLENGE_UNAVAILABLE")
            return receipt

    @staticmethod
    def _check_pending(
        current: EnrollmentAuthority, challenge: Mapping[str, Any], now: datetime
    ) -> None:
        if (
            challenge["closed_at"] is not None
            or not challenge["issued_at"] <= now < challenge["expires_at"]
        ):
            raise EnrollmentStoreError("ENROLLMENT_CHALLENGE_UNAVAILABLE")
        if (
            current.environment_revision != challenge["environment_revision"]
            or current.qualified_installation_sha256 != challenge["installation_sha256"]
            or _installation_digest(current.installation) != challenge["installed_scope_sha256"]
        ):
            raise EnrollmentStoreError("STALE_REVISION")

    def _lock_challenge(
        self,
        conn: Connection,
        scope: EnrollmentScope,
        challenge_id: str,
    ) -> tuple[EnrollmentAuthority, dict[str, Any]]:
        identifier(challenge_id)
        before = self._read_challenge(conn, scope, challenge_id)
        if before is None:
            raise EnrollmentStoreError("ENROLLMENT_NOT_FOUND")
        current = self._authority(conn, scope, before["mode"], issuing=False)
        locked = self._read_challenge(conn, scope, challenge_id, lock=True)
        if locked is None or before["mode"] != locked["mode"]:
            raise EnrollmentStoreError("ENROLLMENT_NOT_FOUND")
        return current, locked

    @staticmethod
    def _read_challenge(
        conn: Connection,
        scope: EnrollmentScope,
        challenge_id: str,
        *,
        lock: bool = False,
    ) -> dict[str, Any] | None:
        return conn.execute(
            "SELECT * FROM opentour_enrollment.challenges WHERE organization_id=%s "
            "AND environment_id=%s AND challenge_id=%s" + (" FOR UPDATE" if lock else ""),
            (scope.organization_id, scope.environment_id, challenge_id),
        ).fetchone()

    @staticmethod
    def _runner(conn: Connection, scope: EnrollmentScope) -> dict[str, Any] | None:
        return conn.execute(
            "SELECT * FROM opentour_enrollment.runners WHERE organization_id=%s "
            "AND environment_id=%s FOR UPDATE",
            (scope.organization_id, scope.environment_id),
        ).fetchone()

    @staticmethod
    def _replay(
        conn: Connection,
        scope: EnrollmentScope,
        challenge_id: str,
        operation_id: str,
        proof_sha256: str,
    ) -> EnrollmentReceipt | None:
        rows = conn.execute(
            "SELECT * FROM opentour_enrollment.receipts WHERE organization_id=%s "
            "AND environment_id=%s AND (challenge_id=%s OR operation_id=%s)",
            (scope.organization_id, scope.environment_id, challenge_id, operation_id),
        ).fetchall()
        if not rows:
            return None
        if len(rows) != 1 or any(row["challenge_id"] != challenge_id for row in rows):
            raise EnrollmentStoreError("IDEMPOTENCY_CONFLICT")
        row = rows[0]
        if (
            row["operation_id"] != operation_id
            or row["proof_sha256"] != proof_sha256
            or row["actor_id"] != scope.actor_id
        ):
            raise EnrollmentStoreError("CHALLENGE_CONSUMED")
        return EnrollmentReceipt(
            row["runner_id"],
            row["cell_id"],
            row["environment_id"],
            row["identity_epoch"],
            row["bound_role_id"],
            row["key_thumbprint"],
            row["observed_at"],
        )

    @staticmethod
    def _commit(
        conn: Connection,
        scope: EnrollmentScope,
        operation_id: str,
        verified: VerifiedEnrollmentIdentity,
        receipt: EnrollmentReceipt,
        revision: int,
    ) -> None:
        identity = conn.execute(
            "INSERT INTO opentour_enrollment.runners VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (organization_id,environment_id) DO UPDATE SET "
            "identity_epoch=EXCLUDED.identity_epoch, bound_role_id=EXCLUDED.bound_role_id, "
            "key_x=EXCLUDED.key_x, key_y=EXCLUDED.key_y, key_thumbprint=EXCLUDED.key_thumbprint "
            "WHERE opentour_enrollment.runners.identity_epoch=EXCLUDED.identity_epoch-1 "
            "AND opentour_enrollment.runners.runner_id=EXCLUDED.runner_id "
            "AND opentour_enrollment.runners.cell_id=EXCLUDED.cell_id",
            (
                scope.organization_id,
                scope.environment_id,
                receipt.runner_id,
                receipt.cell_id,
                receipt.identity_epoch,
                receipt.bound_role_id,
                verified.public_key_x,
                verified.public_key_y,
                receipt.runner_jwk_thumbprint,
            ),
        )
        closed = conn.execute(
            "UPDATE opentour_enrollment.challenges SET closed_at=%s WHERE organization_id=%s "
            "AND environment_id=%s AND challenge_id=%s AND closed_at IS NULL",
            (
                receipt.observed_at,
                scope.organization_id,
                scope.environment_id,
                verified.challenge_id,
            ),
        )
        if identity.rowcount != 1 or closed.rowcount != 1:
            raise EnrollmentStoreError("STALE_REVISION")
        key = (scope.organization_id, scope.environment_id, verified.challenge_id)
        conn.execute(
            "INSERT INTO opentour_enrollment.receipts VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                *key,
                operation_id,
                scope.actor_id,
                verified.proof_sha256,
                receipt.runner_id,
                receipt.cell_id,
                receipt.identity_epoch,
                receipt.bound_role_id,
                receipt.runner_jwk_thumbprint,
                receipt.observed_at,
            ),
        )
        conn.execute(
            "INSERT INTO opentour_enrollment.outbox VALUES (%s,%s,%s,%s,%s)",
            (*key, revision, receipt.observed_at),
        )


def _digest(value: str) -> None:
    if re.fullmatch(r"[a-f0-9]{64}", value) is None:
        raise EnrollmentStoreError("ENROLLMENT_DIGEST_INVALID")


def _installation_digest(installed: InstalledCell) -> str:
    return sha256(canonical(asdict(installed)))


def _thumbprint(x: str, y: str) -> str:
    encoded = sha256_bytes(canonical({"kty": "EC", "crv": "P-256", "x": x, "y": y})).digest()
    return urlsafe_b64encode(encoded).rstrip(b"=").decode("ascii")


def _assert_transition(
    mode: Mode, installed: InstalledCell, previous: Mapping[str, Any] | None
) -> int:
    if mode == "initial" and previous is None and installed.expected_iam_role_id is None:
        return 0
    if (
        mode in {"rotation", "recovery"}
        and previous is not None
        and previous["cell_id"] == installed.cell_id
        and installed.expected_iam_role_id is not None
        and (mode == "recovery" or installed.expected_iam_role_id == previous["bound_role_id"])
    ):
        return int(previous["identity_epoch"])
    raise EnrollmentStoreError("ENROLLMENT_TRANSITION_DENIED")
