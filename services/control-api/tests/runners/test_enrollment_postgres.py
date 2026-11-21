"""Real PostgreSQL enrollment CAS with generated authority and offline STS evidence."""

from __future__ import annotations

import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from opentour_control.auth.repository import connection_factory
from opentour_control.runners.enrollment import (
    EnrollmentVerificationUnavailable,
    InstalledCell,
    IssuedChallenge,
    enrollment_proof_sha256,
    verify_enrollment_identity,
)
from opentour_control.runners.enrollment_store import (
    EnrollmentAuthority,
    EnrollmentReceipt,
    EnrollmentScope,
    EnrollmentStoreError,
    PostgresEnrollmentStore,
)
from psycopg.types.json import Jsonb
from test_enrollment import (
    AWS_ROLE_ID,
    AWS_STS_SESSION,
    NONCE_BYTES,
    NOW,
    FixtureSTSExchange,
    aws_sts_xml,
    fixture_json,
    proof_for,
)

from tools.fixtures.postgres import DisposablePostgres

TARGET = "services/control-api/tests/runners/test_enrollment_postgres.py"
ROOT = Path(__file__).resolve().parents[4]
INSTALL_DIGEST = "b" * 64
RECOVERY_ROLE = "AROARECREATEDROLE"


@pytest.fixture(scope="module")
def local_pg():
    if os.environ.get("OPENTOUR_POSTGRES_TARGET") != TARGET:
        raise RuntimeError("Disposable PostgreSQL requires its explicitly supervised test target")
    fixture = DisposablePostgres()
    with fixture:
        fixture.sql(
            (ROOT / "services/control-api/migrations/0005_runner_enrollment.sql").read_text(),
            role="c03_owner",
        )
        fixture.sql(
            "CREATE ROLE fixture_enrollment LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEROLE;"
            "GRANT USAGE ON SCHEMA opentour_enrollment TO fixture_enrollment;"
            "GRANT SELECT,INSERT,UPDATE ON opentour_enrollment.challenges,"
            "opentour_enrollment.runners TO fixture_enrollment;"
            "GRANT SELECT,INSERT ON opentour_enrollment.receipts,"
            "opentour_enrollment.outbox TO fixture_enrollment;"
            "CREATE TABLE generated_environment (organization_id text, environment_id text, "
            "revision bigint NOT NULL, installation jsonb NOT NULL, install_digest text NOT NULL, "
            "allows_recovery boolean NOT NULL DEFAULT false, "
            "PRIMARY KEY(organization_id,environment_id));"
            "GRANT SELECT,UPDATE ON generated_environment TO fixture_enrollment;",
            role="c03_owner",
        )
        yield fixture
    assert fixture.cleanup_complete


class FixtureEnvironmentAuthority:
    """Generated external owner row, locked and advanced on the store's own connection."""

    def lock_current(self, conn, scope, mode, *, issuing):
        row = conn.execute(
            "SELECT * FROM generated_environment WHERE organization_id=%s "
            "AND environment_id=%s FOR UPDATE",
            (scope.organization_id, scope.environment_id),
        ).fetchone()
        if row is None or (mode == "recovery" and not row["allows_recovery"]):
            return None
        return EnrollmentAuthority(
            InstalledCell(**row["installation"]), row["install_digest"], row["revision"]
        )

    def advance_revision(self, conn, scope, *, expected, proposed):
        return (
            conn.execute(
                "UPDATE generated_environment SET revision=%s WHERE organization_id=%s "
                "AND environment_id=%s AND revision=%s",
                (proposed, scope.organization_id, scope.environment_id, expected),
            ).rowcount
            == 1
        )


@pytest.fixture
def scenario(local_pg):
    owner = connection_factory(f"host={local_pg.socket} dbname=postgres user=c03_owner")
    connect = connection_factory(f"host={local_pg.socket} dbname=postgres user=fixture_enrollment")
    scope = EnrollmentScope(uuid4().hex, uuid4().hex, "fixture-actor")
    installed = InstalledCell(
        scope.organization_id,
        scope.environment_id,
        "fixture-cell",
        "000000000001",
        "arn:aws:iam::000000000001:role/cell/fixture-role",
        "us-east-1",
        "opentour_managed",
        "a" * 64,
        None,
    )
    time = SimpleNamespace(now=NOW)
    authority = FixtureEnvironmentAuthority()
    store = PostgresEnrollmentStore(connect, authority, clock=lambda: time.now)
    with owner() as conn:
        conn.execute(
            "INSERT INTO generated_environment VALUES (%s,%s,1,%s,%s,false)",
            (scope.organization_id, scope.environment_id, Jsonb(asdict(installed)), INSTALL_DIGEST),
        )
    return SimpleNamespace(
        owner=owner,
        connect=connect,
        authority=authority,
        store=store,
        scope=scope,
        installed=installed,
        time=time,
        proof_bodies={},
    )


def issue(s, *, installed=None, mode="initial", revision=1, challenge_id=None):
    challenge = IssuedChallenge(
        challenge_id or uuid4().hex,
        installed or s.installed,
        hashlib.sha256(NONCE_BYTES).hexdigest(),
        NOW + timedelta(seconds=75),
    )
    s.store.record_challenge(s.scope, challenge, mode=mode, expected_environment_revision=revision)
    return challenge


def verify(s, challenge, operation_id, *, key_seed=7, role_id=AWS_ROLE_ID, prepare=True):
    raw = fixture_json(proof_for(challenge.installed, challenge, key_seed=key_seed))
    s.proof_bodies[operation_id] = raw
    pending = (
        s.store.prepare_proof(s.scope, challenge.id, operation_id, raw) if prepare else challenge
    )
    assert pending == challenge
    exchange = FixtureSTSExchange(aws_sts_xml(user_id=f"{role_id}:{AWS_STS_SESSION}"))
    return verify_enrollment_identity(
        raw,
        installed=challenge.installed,
        challenge=pending,
        exchange=exchange,
        clock=lambda: s.time.now,
    )


def facts(s):
    key = (s.scope.organization_id, s.scope.environment_id)
    with s.owner() as conn:
        counts = []
        for table in ("runners", "receipts", "outbox"):
            # Fixed generated fixture table names only.
            row = conn.execute(
                f"SELECT count(*) AS n FROM opentour_enrollment.{table} "
                "WHERE organization_id=%s AND environment_id=%s",
                key,
            ).fetchone()
            counts.append(row["n"])
        row = conn.execute(
            "SELECT revision FROM generated_environment WHERE organization_id=%s "
            "AND environment_id=%s",
            key,
        ).fetchone()
    return tuple(counts), row["revision"]


def owner_update(s, *, installed=None, recovery=False, install_digest=INSTALL_DIGEST):
    with s.owner() as conn:
        conn.execute(
            "UPDATE generated_environment SET installation=%s, allows_recovery=%s, "
            "install_digest=%s WHERE organization_id=%s AND environment_id=%s",
            (
                Jsonb(asdict(installed or s.installed)),
                recovery,
                install_digest,
                s.scope.organization_id,
                s.scope.environment_id,
            ),
        )


def test_consumption_persists_one_receipt_and_event_and_replays_after_restart(scenario):
    s = scenario
    challenge = issue(s)
    proof = verify(s, challenge, "initial-op")
    receipt = s.store.consume(s.scope, "initial-op", proof)
    assert receipt.identity_epoch == 1 and receipt.bound_role_id == AWS_ROLE_ID
    assert receipt.runner_jwk_thumbprint == proof.runner_jwk_thumbprint
    assert enrollment_proof_sha256(s.proof_bodies["initial-op"]) == proof.proof_sha256
    assert facts(s) == ((1, 1, 1), 2)
    s.time.now = NOW + timedelta(minutes=10)
    restarted = PostgresEnrollmentStore(s.connect, s.authority, clock=lambda: s.time.now)
    assert (
        restarted.prepare_proof(s.scope, challenge.id, "initial-op", s.proof_bodies["initial-op"])
        == receipt
    )
    assert restarted.consume(s.scope, "initial-op", proof) == receipt
    assert facts(s) == ((1, 1, 1), 2)
    with pytest.raises(EnrollmentStoreError, match="^CHALLENGE_CONSUMED$"):
        restarted.prepare_proof(
            s.scope,
            challenge.id,
            "initial-op",
            fixture_json(proof_for(challenge.installed, challenge, key_seed=9)),
        )
    with pytest.raises(EnrollmentStoreError, match="^CHALLENGE_CONSUMED$"):
        restarted.prepare_proof(
            replace(s.scope, actor_id="other-actor"),
            challenge.id,
            "initial-op",
            s.proof_bodies["initial-op"],
        )


def test_concurrent_distinct_proofs_consume_one_challenge_and_one_epoch(scenario):
    s = scenario
    challenge = issue(s)
    proofs = [verify(s, challenge, "op-0"), verify(s, challenge, "op-1", key_seed=8, prepare=False)]

    def consume(index):
        try:
            return s.store.consume(s.scope, f"op-{index}", proofs[index])
        except EnrollmentStoreError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(consume, (0, 1)))
    assert len([value for value in outcomes if isinstance(value, EnrollmentReceipt)]) == 1
    assert any(value in ("CHALLENGE_CONSUMED", "IDEMPOTENCY_CONFLICT") for value in outcomes)
    assert facts(s) == ((1, 1, 1), 2)


def test_database_failure_rolls_back_identity_challenge_receipt_and_revision(scenario):
    s = scenario
    challenge = issue(s)
    proof = verify(s, challenge, "rollback-op")
    with s.owner() as conn:
        conn.execute("REVOKE INSERT ON opentour_enrollment.outbox FROM fixture_enrollment")
    try:
        with pytest.raises(EnrollmentStoreError, match="^ENROLLMENT_SQL_UNCONFIRMED$"):
            s.store.consume(s.scope, "rollback-op", proof)
        assert facts(s) == ((0, 0, 0), 1)
        assert (
            s.store.prepare_proof(
                s.scope, challenge.id, "rollback-op", s.proof_bodies["rollback-op"]
            )
            == challenge
        )
    finally:
        with s.owner() as conn:
            conn.execute("GRANT INSERT ON opentour_enrollment.outbox TO fixture_enrollment")
    assert s.store.consume(s.scope, "rollback-op", proof).identity_epoch == 1


def test_scope_manifest_nonce_and_expiry_are_checked_before_consumption(scenario):
    s = scenario
    challenge = issue(s)
    proof = verify(s, challenge, "proof-op")
    with pytest.raises(EnrollmentStoreError, match="^ENROLLMENT_NOT_FOUND$"):
        s.store.consume(replace(s.scope, organization_id="foreign-org"), "proof-op", proof)
    for altered in (
        replace(proof, challenge_nonce_sha256="c" * 64),
        replace(proof, runner_jwk_thumbprint="a" * 43),
        replace(proof, iam_role_id=RECOVERY_ROLE),
    ):
        with pytest.raises(EnrollmentStoreError, match="^ENROLLMENT_PROOF_INVALID$"):
            s.store.consume(s.scope, "proof-op", altered)
    owner_update(s, install_digest="c" * 64)
    with pytest.raises(EnrollmentStoreError, match="^STALE_REVISION$"):
        s.store.consume(s.scope, "proof-op", proof)
    owner_update(s)
    s.time.now = proof.expires_at
    with pytest.raises(EnrollmentStoreError, match="^ENROLLMENT_PROOF_INVALID$"):
        s.store.consume(s.scope, "proof-op", proof)
    assert facts(s) == ((0, 0, 0), 1)


def test_reissue_invalidates_old_challenge_and_rotation_recovery_do_not_create_second_runner(
    scenario,
):
    s = scenario
    first = issue(s)
    first_proof = verify(s, first, "reissued-op")
    second = issue(s)
    with pytest.raises(EnrollmentStoreError, match="^ENROLLMENT_CHALLENGE_UNAVAILABLE$"):
        s.store.consume(s.scope, "reissued-op", first_proof)
    enrolled = s.store.consume(s.scope, "init-op", verify(s, second, "init-op"))
    approved = replace(s.installed, expected_iam_role_id=AWS_ROLE_ID)
    owner_update(s, installed=approved)
    rotation = issue(s, installed=approved, mode="rotation", revision=2)
    rotated = s.store.consume(
        s.scope, "rotation-op", verify(s, rotation, "rotation-op", key_seed=8)
    )
    assert (rotated.runner_id, rotated.identity_epoch) == (enrolled.runner_id, 2)
    recreated = replace(s.installed, expected_iam_role_id=RECOVERY_ROLE)
    owner_update(s, installed=recreated)
    with pytest.raises(EnrollmentStoreError, match="^ENROLLMENT_TRANSITION_DENIED$"):
        issue(s, installed=recreated, mode="rotation", revision=3)
    with pytest.raises(EnrollmentStoreError, match="^ENROLLMENT_NOT_FOUND$"):
        issue(s, installed=recreated, mode="recovery", revision=3)
    owner_update(s, installed=recreated, recovery=True)
    recovery = issue(s, installed=recreated, mode="recovery", revision=3)
    recovered = s.store.consume(
        s.scope,
        "recovery-op",
        verify(s, recovery, "recovery-op", key_seed=9, role_id=RECOVERY_ROLE),
    )
    assert (recovered.runner_id, recovered.identity_epoch, recovered.bound_role_id) == (
        enrolled.runner_id,
        3,
        RECOVERY_ROLE,
    )
    assert facts(s) == ((1, 3, 3), 4)


def test_stale_authority_and_unqualified_region_cannot_record_challenge(scenario):
    s = scenario
    with pytest.raises(EnrollmentStoreError, match="^STALE_REVISION$"):
        issue(s, revision=2)
    ohio = replace(s.installed, region="us-east-2")
    owner_update(s, installed=ohio)
    with pytest.raises(EnrollmentStoreError, match="^ENROLLMENT_NOT_FOUND$"):
        issue(s, installed=ohio)
    assert facts(s) == ((0, 0, 0), 1)


def test_expiry_during_authority_cas_rolls_back_and_never_issues_identity(scenario, monkeypatch):
    s = scenario
    challenge = issue(s)
    proof = verify(s, challenge, "expiring-op")
    original = s.authority.advance_revision

    def advance(conn, scope, *, expected, proposed):
        accepted = original(conn, scope, expected=expected, proposed=proposed)
        s.time.now = proof.expires_at
        return accepted

    monkeypatch.setattr(s.authority, "advance_revision", advance)
    with pytest.raises(EnrollmentStoreError, match="^ENROLLMENT_CHALLENGE_UNAVAILABLE$"):
        s.store.consume(s.scope, "expiring-op", proof)
    assert facts(s) == ((0, 0, 0), 1)


def test_unavailable_exchange_can_only_retry_exact_claimed_proof(scenario):
    s = scenario
    challenge = issue(s)
    op = "lost-reply"
    raw = fixture_json(proof_for(challenge.installed, challenge))
    exchange = FixtureSTSExchange(failure=TimeoutError())

    with pytest.raises(EnrollmentVerificationUnavailable):
        s.store.accept_proof(s.scope, challenge.id, op, raw, exchange=exchange)
    assert len(exchange.calls) == 1
    assert facts(s) == ((0, 0, 0), 1)

    restarted = PostgresEnrollmentStore(s.connect, s.authority, clock=lambda: s.time.now)
    retry = FixtureSTSExchange(aws_sts_xml())
    with pytest.raises(EnrollmentStoreError, match="^ENROLLMENT_NOT_FOUND$"):
        restarted.accept_proof(
            replace(s.scope, organization_id="foreign-org"),
            challenge.id,
            op,
            raw,
            exchange=retry,
        )
    changed = fixture_json(proof_for(challenge.installed, challenge, key_seed=8))
    with pytest.raises(EnrollmentStoreError, match="^IDEMPOTENCY_CONFLICT$"):
        restarted.accept_proof(s.scope, challenge.id, op, changed, exchange=retry)
    assert retry.calls == []
    assert facts(s) == ((0, 0, 0), 1)

    receipt = restarted.accept_proof(s.scope, challenge.id, op, raw, exchange=retry)
    assert receipt.identity_epoch == 1 and receipt.bound_role_id == AWS_ROLE_ID
    assert len(retry.calls) == 1
    assert facts(s) == ((1, 1, 1), 2)


def test_accept_proof_replays_committed_receipt_after_lost_ack_without_sts(scenario):
    s = scenario
    challenge = issue(s)
    op = "lost-acknowledgment"
    raw = fixture_json(proof_for(challenge.installed, challenge))
    exchange = FixtureSTSExchange(aws_sts_xml())
    committed = s.store.accept_proof(s.scope, challenge.id, op, raw, exchange=exchange)
    assert committed.identity_epoch == 1 and committed.bound_role_id == AWS_ROLE_ID
    assert len(exchange.calls) == 1
    assert facts(s) == ((1, 1, 1), 2)

    s.time.now = NOW + timedelta(minutes=10)
    restarted = PostgresEnrollmentStore(s.connect, s.authority, clock=lambda: s.time.now)
    replay = FixtureSTSExchange(failure=AssertionError("receipt replay must bypass STS"))
    assert restarted.accept_proof(s.scope, challenge.id, op, raw, exchange=replay) == committed
    assert replay.calls == []
    assert facts(s) == ((1, 1, 1), 2)


def test_accept_proof_rechecks_qualified_installation_after_sts(scenario):
    s = scenario
    challenge = issue(s)
    raw = fixture_json(proof_for(challenge.installed, challenge))

    class ChangedInstallationExchange(FixtureSTSExchange):
        def get_caller_identity(self, validated, *, max_response_bytes):
            response = super().get_caller_identity(validated, max_response_bytes=max_response_bytes)
            owner_update(s, install_digest="c" * 64)
            return response

    exchange = ChangedInstallationExchange(aws_sts_xml())
    with pytest.raises(EnrollmentStoreError, match="^STALE_REVISION$"):
        s.store.accept_proof(s.scope, challenge.id, "changed-install", raw, exchange=exchange)
    assert len(exchange.calls) == 1
    assert facts(s) == ((0, 0, 0), 1)


def test_exact_concurrent_replay_uses_one_durable_result(scenario):
    s = scenario
    challenge = issue(s)
    proof = verify(s, challenge, "same-op")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: s.store.consume(s.scope, "same-op", proof), (0, 1)))
    assert results[0] == results[1]
    assert facts(s) == ((1, 1, 1), 2)
