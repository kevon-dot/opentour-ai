"""Generated jobs exercise the shared local register with both signed routes."""

from __future__ import annotations

import hmac
import json
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from opentour_cell.pilot import cohort_lease
from opentour_cell.pilot.cohort_lease import (
    AUTHORITY_VERSION,
    COMMAND_VERSION,
    CohortLeaseRegister,
    LeaseAuthority,
)
from opentour_cell.pilot.operating_point import canonical, digest
from opentour_cell.pilot.qualification import (
    EVENT_VERSION,
    Authority,
    Json,
    QualificationRegister,
)

A = "generated-tenant-a"
B = "generated-tenant-b"
C = "generated-tenant-c"
ROLES = {"acquire": "operator", "begin": "operator", "reconcile": "reconciler", "close": "closer"}
QUALIFICATION_KEY = bytes(reversed(range(32)))
MODULE = "opentour_cell.pilot.cohort_lease"


@pytest.fixture
def path(tmp_path: Path) -> Path:
    directory = tmp_path / "private-cohort"
    directory.mkdir(mode=0o700)
    return directory / "register.sqlite3"


def key(org: str, route: str, role: str) -> bytes:
    return bytes.fromhex(digest(["generated-test-key", org, route, role]))


def sign(command: Json, secret: bytes | None = None) -> Json:
    if secret is None:
        secret = key(command["organization_ref"], command["route"], ROLES[command["action"]])
    payload = f"{COMMAND_VERSION}\n{canonical(command)}".encode("ascii")
    return {"command": command, "signature": hmac.digest(secret, payload, "sha256").hex()}


def configuration(org: str, route: str, actions: tuple[str, ...] = ("acquire", "begin")) -> Json:
    role = ROLES[actions[0]]
    return {
        "schema_version": AUTHORITY_VERSION,
        "organization_ref": org,
        "signer_ref": f"generated-{route}-{role}",
        "route": route,
        "expires_at": int(time.time()) + 300,
        "verification_key_hex": key(org, route, role).hex(),
        "authorized_actions": list(actions),
    }


def qualification(
    path: Path,
    org: str,
    *,
    intent: str = "starter",
    revision: int = 1,
    source_revision: int = 1,
    event_id: str = "generated-charter",
    route: str = "manual",
) -> tuple[str, Json]:
    mapping = "generated-crm-qualification-map" if route == "crm" else None
    charter = {
        "owner_ref": "generated-owner",
        "custodian_ref": "generated-custodian",
        "evaluation_purpose_ref": "generated-evaluation",
        "local_location_ref": "generated-private-location",
        "intended_decision_ref": "generated-decision",
        "policy_ref": "generated-policy",
        "policy_revision": "generated-policy-v1",
        "policy_grant_ref": "generated-policy-grant",
        "verifier_role_ref": "generated-verifier-role",
        "verifier_plan_owner_ref": "generated-verifier-plan-owner",
        "verifier_next_check": (datetime.now(UTC).date() + timedelta(days=7)).isoformat(),
    }
    value = {
        "schema_version": EVENT_VERSION,
        "organization_ref": org,
        "event_id": event_id,
        "engagement_revision": revision,
        "source_revision": source_revision,
        "source_ref": "generated-source",
        "signer_ref": "generated-qualification-operator",
        "route": route,
        "mapping_ref": mapping,
        "intent": intent,
        "charter": charter if intent == "starter" else None,
    }
    sig = hmac.digest(
        QUALIFICATION_KEY,
        f"{EVENT_VERSION}\n{canonical(value)}".encode("ascii"),
        "sha256",
    ).hex()
    result = QualificationRegister(path).submit(
        {"event": value, "signature": sig},
        Authority(
            org,
            "generated-qualification-operator",
            route,
            mapping,
            int(time.time()) + 300,
            QUALIFICATION_KEY,
        ),
    )
    return digest(value), result


def command(org: str, route: str = "manual", **changes: object) -> Json:
    role = ROLES[str(changes.get("action", "acquire"))]
    return {
        "schema_version": COMMAND_VERSION,
        "event_id": "generated-acquire",
        "action": "acquire",
        "organization_ref": org,
        "engagement_revision": 1,
        "generation": 0,
        "slot": 1,
        "signer_ref": f"generated-{route}-{role}",
        "route": route,
        "evidence_digest": digest("generated-owner-acceptance"),
        **changes,
    }


def acquire(org: str, charter: str, route: str = "manual", **changes: object) -> Json:
    return command(
        org,
        route,
        **{"qualification_digest": charter, "expires_at": int(time.time()) + 120, **changes},
    )


def run(path: Path, cmd: Json, policy: Json | None = None) -> Json:
    authority = LeaseAuthority.parse(
        policy
        if policy is not None
        else configuration(cmd["organization_ref"], cmd["route"], (cmd["action"],))
    )
    return CohortLeaseRegister(QualificationRegister(path)).submit(sign(cmd), authority)


def checkpoint(receipt: Json) -> Json:
    return {
        "reconciliation_revision": receipt["reconciliation_revision"],
        "reconciliation_digest": receipt["reconciliation_digest"],
    }


def lease_rows(path: Path) -> list[tuple[object, ...]]:
    with closing(sqlite3.connect(path)) as db:
        return db.execute(
            """SELECT organization_ref, engagement_revision, generation, terminal_kind
               FROM cohort_lease ORDER BY generation"""
        ).fetchall()


def test_manual_and_automated_race_replays_winner_without_takeover(path: Path) -> None:
    charter_a, _ = qualification(path, A)
    charter_b, _ = qualification(path, B)
    submissions = (
        acquire(A, charter_a),
        acquire(B, charter_b, "automated"),
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.map(lambda payload: run(path, payload), submissions)
    assert {a["disposition"], b["disposition"]} == {"acquired", "queued"}
    winner, loser = (0, 1) if a["disposition"] == "acquired" else (1, 0)
    receipt = (a, b)[winner]
    assert receipt["generation"] == 1
    assert run(path, submissions[winner]) == receipt
    assert run(path, submissions[loser]) == (a, b)[loser]
    assert lease_rows(path) == [(submissions[winner]["organization_ref"], 1, 1, None)]
    with closing(sqlite3.connect(path)) as db:
        assert db.execute(
            """SELECT count(*) FROM cohort_waiter AS w
               WHERE NOT EXISTS (
                   SELECT 1 FROM cohort_lease AS l
                   WHERE l.organization_ref = w.organization_ref
                     AND l.engagement_revision = w.engagement_revision
               )"""
        ).fetchone() == (1,)
        assert db.execute(
            "SELECT count(*) FROM cohort_lease WHERE terminal_kind IS NULL"
        ).fetchone() == (1,)
        assert db.execute("SELECT count(*) FROM cohort_command").fetchone() == (2,)


def test_unknown_effect_blocks_transfer_until_signed_reconciliation(path: Path) -> None:
    charter_a, _ = qualification(path, A, route="crm")
    charter_b, _ = qualification(path, B)
    lease_a = run(path, acquire(A, charter_a))
    assert run(path, acquire(B, charter_b, "automated"))["disposition"] == "queued"
    begin = command(
        A,
        action="begin",
        event_id="generated-first-connect",
        generation=1,
        lease_ref=lease_a["lease_ref"],
        evidence_digest=digest("generated-source-access"),
    )
    assert run(path, begin)["disposition"] == "begin_authorized"
    assert run(path, begin)["reason"] == "effect_authority_consumed"
    assert run(path, {**begin, "event_id": "generated-effect-duplicate"})["reason"] == (
        "effect_authority_consumed"
    )
    closing_a = command(
        A,
        action="close",
        event_id="generated-close",
        generation=1,
        lease_ref=lease_a["lease_ref"],
        terminal_kind="withdrawal",
        **checkpoint(lease_a),
    )
    assert run(path, closing_a)["reason"] == "live_effect_reconciliation_required"
    retry_b = acquire(B, charter_b, "automated", generation=1, event_id="generated-waiting")
    assert run(path, retry_b)["disposition"] == "queued"

    _, terminal = qualification(
        path,
        A,
        intent="lost",
        source_revision=2,
        event_id="generated-lost",
        route="crm",
    )
    assert terminal["disposition"] == "lost"
    _, replay_old_crm = qualification(path, A, route="crm")
    assert replay_old_crm["reason"] == "stale_qualification_event"
    after_withdrawal = {
        **begin,
        "event_id": "generated-after-withdrawal",
        "evidence_digest": digest("generated-other-source-access"),
    }
    assert run(path, after_withdrawal)["reason"] == "qualification_scope_changed"
    reconcile = command(
        A,
        action="reconcile",
        event_id="generated-reconcile",
        generation=1,
        lease_ref=lease_a["lease_ref"],
        effect_event_id=begin["event_id"],
        evidence_digest=digest("generated-independent-readback"),
        outcome="observed_applied",
    )
    reconciled = run(path, reconcile)
    assert reconciled["disposition"] == "reconciled"
    assert reconciled["reconciliation_revision"] == 2
    assert run(path, closing_a)["reason"] == "terminal_reconciliation_changed"
    assert run(path, {**closing_a, "reconciliation_revision": 2})["reason"] == (
        "terminal_reconciliation_changed"
    )
    assert (
        run(
            path,
            {
                **closing_a,
                "event_id": "generated-fresh-close",
                **checkpoint(reconciled),
            },
        )["disposition"]
        == "closed"
    )
    with closing(sqlite3.connect(path)) as db:
        assert db.execute(
            "SELECT outcome, reconciled_by, reconciliation_digest FROM cohort_effect"
        ).fetchone() == (
            "observed_applied",
            "generated-manual-reconciler",
            digest("generated-independent-readback"),
        )
        assert db.execute(
            "SELECT terminal_reconciliation_digest FROM cohort_lease WHERE generation = 1"
        ).fetchone() == (reconciled["reconciliation_digest"],)
        assert db.execute(
            "SELECT count(DISTINCT key_fingerprint) FROM cohort_command WHERE organization_ref = ?",
            (A,),
        ).fetchone() == (3,)
    assert run(path, retry_b)["disposition"] == "queued"
    new_b = {**retry_b, "event_id": "generated-fresh-acquire-b"}
    assert run(path, new_b)["generation"] == 2
    assert run(path, begin)["reason"] == "effect_authority_consumed"
    assert run(path, after_withdrawal)["reason"] == "lease_scope_stale_or_foreign"
    assert lease_rows(path) == [(A, 1, 1, "withdrawal"), (B, 1, 2, None)]


def test_fresh_epoch_does_not_consume_extra_organization_or_reuse_lease(path: Path) -> None:
    charter, _ = qualification(path, A)
    lease = run(path, acquire(A, charter))
    qualification(path, A, intent="lost", event_id="generated-lost", source_revision=2)
    new_charter, _ = qualification(path, A, event_id="generated-return", revision=2)
    second_epoch = acquire(
        A,
        new_charter,
        generation=1,
        engagement_revision=2,
        event_id="generated-epoch2",
    )
    assert run(path, second_epoch)["disposition"] == "queued"
    close = command(
        A,
        action="close",
        event_id="generated-closeout",
        generation=1,
        lease_ref=lease["lease_ref"],
        terminal_kind="closeout",
        **checkpoint(lease),
    )
    assert run(path, close)["disposition"] == "closed"
    assert run(path, {**second_epoch, "event_id": "generated-epoch2-fresh"})["generation"] == 2
    rows = lease_rows(path)
    assert rows == [(A, 1, 1, "closeout"), (A, 2, 2, None)]
    assert {row[0] for row in rows} == {A}


def test_expired_holder_stays_open_without_usable_lease(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    charter_a, _ = qualification(path, A)
    charter_b, _ = qualification(path, B)
    now = int(time.time())
    lease = run(path, acquire(A, charter_a, expires_at=now + 10))
    monkeypatch.setattr(cohort_lease, "time", SimpleNamespace(time=lambda: now + 11))
    begin = command(
        A,
        action="begin",
        event_id="generated-expired-work",
        generation=1,
        lease_ref=lease["lease_ref"],
    )
    assert run(path, begin)["reason"] == "lease_requires_reconciliation"
    waiting = run(path, acquire(B, charter_b, "automated"))
    assert waiting["reason"] == "lease_requires_reconciliation"
    assert lease_rows(path) == [(A, 1, 1, None)]


def test_ready_queue_is_ordered_and_stale_generation_does_not_transfer(path: Path) -> None:
    charter_a, _ = qualification(path, A)
    charter_b, _ = qualification(path, B)
    charter_c, _ = qualification(path, C)
    lease = run(path, acquire(A, charter_a))
    assert run(path, acquire(B, charter_b))["disposition"] == "queued"
    assert run(path, acquire(C, charter_c, "automated"))["disposition"] == "queued"
    close = command(
        A,
        action="close",
        event_id="generated-terminal",
        generation=1,
        lease_ref=lease["lease_ref"],
        terminal_kind="closeout",
        **checkpoint(lease),
    )
    assert run(path, close)["disposition"] == "closed"
    stale = acquire(B, charter_b, event_id="generated-old-generation")
    assert run(path, stale)["reason"] == "lease_generation_changed"
    fresh_c = acquire(C, charter_c, "automated", generation=1, event_id="generated-fresh-c")
    assert run(path, fresh_c)["reason"] == "earlier_qualified_candidate"
    fresh_b = acquire(B, charter_b, generation=1, event_id="generated-fresh-b")
    assert run(path, fresh_b)["generation"] == 2


def test_cli_holds_foreign_authority_and_reopens_same_register(path: Path) -> None:
    charter_a, _ = qualification(path, A)
    cmd = acquire(A, charter_a)
    policy = path.parent / "authority.json"
    record = path.parent / "submission.json"
    for private_path, value in ((policy, configuration(B, "manual")), (record, sign(cmd))):
        private_path.touch(mode=0o600)
        private_path.write_text(canonical(value), encoding="ascii")
    args = [
        sys.executable,
        "-m",
        MODULE,
        "--register",
        str(path),
        "--authority",
        str(policy),
        "--submission",
        str(record),
    ]
    denied = subprocess.run(args, capture_output=True, text=True, check=False)
    assert denied.returncode == 1
    assert json.loads(denied.stdout)["reason"] == "lease_authority_unverified"
    policy.write_text(canonical(configuration(A, "manual")), encoding="ascii")
    first = subprocess.run(args, capture_output=True, text=True, check=True)
    revived = subprocess.run(args, capture_output=True, text=True, check=True)
    assert revived.stdout == first.stdout
    assert lease_rows(path) == [(A, 1, 1, None)]


def test_expired_waiter_cannot_hold_front_of_queue(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    qa, _ = qualification(path, A)
    qb, _ = qualification(path, B)
    qc, _ = qualification(path, C)
    now = int(time.time())
    active = run(path, acquire(A, qa))
    assert run(path, acquire(B, qb, expires_at=now + 5))["disposition"] == "queued"
    assert run(path, acquire(C, qc))["disposition"] == "queued"
    close = command(
        A,
        action="close",
        event_id="generated-close",
        generation=1,
        lease_ref=active["lease_ref"],
        terminal_kind="closeout",
        **checkpoint(active),
    )
    assert run(path, close)["disposition"] == "closed"
    monkeypatch.setattr(cohort_lease, "time", SimpleNamespace(time=lambda: now + 10))
    retry = acquire(C, qc, event_id="generated-acquire-next", generation=1)
    assert run(path, retry)["disposition"] == "acquired"
    assert lease_rows(path) == [(A, 1, 1, "closeout"), (C, 1, 2, None)]


def test_lease_reference_alone_grants_no_cross_org_or_cross_role_action(path: Path) -> None:
    qa, _ = qualification(path, A)
    lease = run(path, acquire(A, qa))
    foreign = command(
        B,
        action="begin",
        event_id="generated-foreign-effect",
        generation=1,
        lease_ref=lease["lease_ref"],
    )
    assert run(path, foreign)["reason"] == "lease_scope_stale_or_foreign"
    wrong_slot = {**foreign, "organization_ref": A, "slot": 2}
    assert run(path, wrong_slot)["reason"] == "lease_scope_stale_or_foreign"
    scoped = {**wrong_slot, "slot": 1}
    assert run(path, scoped, configuration(A, "manual", ("acquire",)))["reason"] == (
        "lease_authority_unverified"
    )
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT count(*) FROM cohort_effect").fetchone() == (0,)


def test_terminal_authority_cannot_reuse_operator_identity_or_key(path: Path) -> None:
    charter, _ = qualification(path, A)
    lease = run(path, acquire(A, charter))
    close = command(
        A,
        action="close",
        event_id="generated-terminal",
        generation=1,
        lease_ref=lease["lease_ref"],
        terminal_kind="closeout",
        **checkpoint(lease),
    )
    access = configuration(A, "manual")
    terminal = configuration(A, "manual", ("close",))
    register = CohortLeaseRegister(QualificationRegister(path))
    borrowed_identity = {**close, "signer_ref": access["signer_ref"]}
    policy = LeaseAuthority.parse({**terminal, "signer_ref": access["signer_ref"]})
    assert register.submit(sign(borrowed_identity), policy)["reason"] == (
        "lease_authorities_not_independent"
    )
    reused_key = bytes.fromhex(access["verification_key_hex"])
    policy = LeaseAuthority.parse({**terminal, "verification_key_hex": reused_key.hex()})
    assert register.submit(sign(close, reused_key), policy)["reason"] == (
        "lease_authorities_not_independent"
    )
    assert run(path, close)["disposition"] == "closed"


def test_cohort_workplace_normal_entry_claims_before_provider_and_denies_replay(
    path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from collections.abc import Callable, Iterator
    from contextlib import AbstractContextManager, contextmanager

    from opentour_cell.pilot import workplace_context as workplace
    from test_workplace_context import WorkplaceFixture, put

    fixture = WorkplaceFixture.small(tmp_path / "generated-workplace")
    org = fixture.grant["organization"]
    charter, _ = qualification(path, org)
    lease = run(path, acquire(org, charter))
    fixture.cfg["schema_version"] = workplace.COHORT_CONNECTION
    binding: Json = {
        "register": str(path),
        "authority_file": "lease-authority.json",
        "discover_submission_file": "discover-lease.json",
        "acquire_submission_file": "acquire-lease.json",
    }
    fixture.cfg["cohort_lease"] = binding
    put(fixture.connection, fixture.cfg)
    policy = fixture.directory / "lease-authority.json"
    envelope = fixture.directory / "discover-lease.json"
    begin = command(
        org,
        action="begin",
        event_id="generated-connect-workplace",
        generation=1,
        lease_ref=lease["lease_ref"],
        evidence_digest=digest(
            [
                "opentour.pilot.workplace-lease-effect.v1",
                org,
                fixture.grant["engagement"],
                digest(fixture.cfg),
                "discover",
            ]
        ),
    )
    put(policy, configuration(org, "manual", ("begin",)))
    put(envelope, sign(begin))
    attempts: list[str] = []

    @contextmanager
    def attempted_provider(
        cfg: Json,
        root: Path,
        dispatch: Callable[[], AbstractContextManager[None]],
    ) -> Iterator[object]:
        with dispatch():
            attempts.append("connection")
            # This is the provider transport seam. Assert the committed claim already exists.
            with closing(sqlite3.connect(path)) as db:
                assert db.execute(
                    "SELECT event_id FROM cohort_effect WHERE reconciliation_digest IS NULL"
                ).fetchone() == (begin["event_id"],)
        yield object()

    def catalog(*_: object) -> tuple[Json, Json]:
        attempts.append("catalog")
        return {}, {"counts": {"trace_rows": 0, "correction_rows": 0}}

    monkeypatch.setattr(workplace, "_session", attempted_provider)
    monkeypatch.setattr(workplace, "_catalog", catalog)
    result = workplace.discover_workplace(fixture.store, fixture.connection)
    assert result["counts"] == {"trace_rows": 0, "correction_rows": 0}
    assert attempts == ["connection", "catalog"]
    with pytest.raises(workplace.WorkplaceError, match="workplace_lease_not_authorized"):
        workplace.discover_workplace(fixture.store, fixture.connection)
    assert attempts == ["connection", "catalog"]

    missing = path.parent / "missing-authoritative-register.sqlite3"
    binding["register"] = str(missing)
    put(fixture.connection, fixture.cfg)
    put(
        envelope,
        sign(
            {
                **begin,
                "evidence_digest": digest(
                    [
                        "opentour.pilot.workplace-lease-effect.v1",
                        org,
                        fixture.grant["engagement"],
                        digest(fixture.cfg),
                        "discover",
                    ]
                ),
            }
        ),
    )
    with pytest.raises(workplace.WorkplaceError, match="workplace_source_unavailable"):
        workplace.discover_workplace(fixture.store, fixture.connection)
    assert not missing.exists()
    assert attempts == ["connection", "catalog"]


def test_lease_is_rechecked_after_provider_opens_before_catalog(
    path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from collections.abc import Callable, Iterator
    from contextlib import AbstractContextManager, contextmanager

    from opentour_cell.pilot import workplace_context as workplace
    from test_workplace_context import WorkplaceFixture, put

    fixture = WorkplaceFixture.small(tmp_path / "generated-workplace")
    org = fixture.grant["organization"]
    charter, _ = qualification(path, org)
    lease = run(path, acquire(org, charter))
    fixture.cfg["schema_version"] = workplace.COHORT_CONNECTION
    fixture.cfg["cohort_lease"] = {
        "register": str(path),
        "authority_file": "lease-authority.json",
        "discover_submission_file": "discover-lease.json",
        "acquire_submission_file": "acquire-lease.json",
    }
    put(fixture.connection, fixture.cfg)
    put(fixture.directory / "lease-authority.json", configuration(org, "manual", ("begin",)))
    put(
        fixture.directory / "discover-lease.json",
        sign(
            command(
                org,
                action="begin",
                event_id="generated-connect-workplace",
                generation=1,
                lease_ref=lease["lease_ref"],
                evidence_digest=digest(
                    [
                        "opentour.pilot.workplace-lease-effect.v1",
                        org,
                        fixture.grant["engagement"],
                        digest(fixture.cfg),
                        "discover",
                    ]
                ),
            )
        ),
    )
    calls: list[str] = []

    @contextmanager
    def revoked_transport(
        cfg: Json,
        root: Path,
        dispatch: Callable[[], AbstractContextManager[None]],
    ) -> Iterator[object]:
        with dispatch():
            calls.append("open")
        _, terminal = qualification(
            path,
            org,
            intent="lost",
            source_revision=2,
            event_id="generated-source-lost",
        )
        assert terminal["disposition"] == "lost"
        yield object()

    def catalog(*_: object) -> tuple[Json, Json]:
        calls.append("catalog")
        return {}, {}

    monkeypatch.setattr(workplace, "_session", revoked_transport)
    monkeypatch.setattr(workplace, "_catalog", catalog)
    with pytest.raises(workplace.WorkplaceError, match="workplace_lease_no_longer_current"):
        workplace.discover_workplace(fixture.store, fixture.connection)
    assert calls == ["open"]


def _native_cohort_fixture(path: Path, tmp_path: Path, operation: str):
    from opentour_cell.pilot import workplace_context as workplace
    from test_workplace_context import WorkplaceFixture, put

    fixture = WorkplaceFixture.small(tmp_path / "generated-workplace")
    org, engagement = fixture.grant["organization"], fixture.grant["engagement"]
    charter, _ = qualification(path, org)
    lease = run(path, acquire(org, charter))
    fixture.cfg["schema_version"] = workplace.COHORT_CONNECTION
    fixture.cfg["cohort_lease"] = {
        "register": str(path),
        "authority_file": "lease-authority.json",
        "discover_submission_file": "discover-lease.json",
        "acquire_submission_file": "acquire-lease.json",
    }
    put(fixture.connection, fixture.cfg)
    put(fixture.directory / "lease-authority.json", configuration(org, "manual", ("begin",)))
    put(
        fixture.directory / f"{operation}-lease.json",
        sign(
            command(
                org,
                action="begin",
                event_id="generated-connect-workplace",
                generation=1,
                lease_ref=lease["lease_ref"],
                evidence_digest=digest(
                    [
                        "opentour.pilot.workplace-lease-effect.v1",
                        org,
                        engagement,
                        digest(fixture.cfg),
                        operation,
                    ]
                ),
            )
        ),
    )
    return fixture


@pytest.mark.parametrize(
    ("operation", "stop_page"), [("discover", 1), ("acquire", 1), ("acquire", 2)]
)
@pytest.mark.parametrize(
    "reason", ["workplace_lease_no_longer_current", "engagement_withdrawn"]
)
def test_native_reader_stops_catalog_continuation_after_authority_loss(
    path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    stop_page: int,
    reason: str,
) -> None:
    from opentour_cell.pilot import workplace_context as workplace

    fixture = _native_cohort_fixture(path, tmp_path, operation)
    org = fixture.grant["organization"]
    original_receive = workplace._Stdio.receive
    paginated_replies = 0

    def withdraw_between_pages(client: workplace._Stdio, limit: int) -> Json:
        nonlocal paginated_replies
        response = original_receive(client, limit)
        if "nextCursor" in response.get("result", {}):
            paginated_replies += 1
            if paginated_replies == stop_page:
                if reason == "engagement_withdrawn":
                    assert fixture.store.withdraw()["status"] == "withdrawn"
                else:
                    _, terminal = qualification(
                        path,
                        org,
                        intent="lost",
                        source_revision=2,
                        event_id="generated-source-lost",
                    )
                    assert terminal["disposition"] == "lost"
        return response

    monkeypatch.setattr(workplace._Stdio, "receive", withdraw_between_pages)
    use = workplace.discover_workplace if operation == "discover" else workplace.acquire_workplace
    with pytest.raises(workplace.WorkplaceError, match=reason):
        use(fixture.store, fixture.connection)
    received = fixture.received()
    requests = [item for item in received if item["method"] == "resources/list"]
    assert len(requests) == (1 if stop_page == 1 else 3)
    assert requests[-1]["params"] == {}
    assert sum(item["method"] == "resources/read" for item in received) == (
        0 if stop_page == 1 else 2
    )
    assert not list(fixture.directory.glob("workplace-snapshots/*"))


def test_native_reader_dispatch_and_snapshot_fence_register_writers(
    path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from opentour_cell.pilot import workplace_context as workplace

    fixture = _native_cohort_fixture(path, tmp_path, "acquire")
    orig_write, orig_rename = workplace.os.write, workplace.os.rename
    observed: list[str] = []

    def check_locked(effect: str) -> None:
        with closing(sqlite3.connect(path, timeout=0)) as other:
            with pytest.raises(sqlite3.OperationalError, match="database is locked"):
                other.execute("BEGIN IMMEDIATE")
        observed.append(effect)

    def checked_write(fd: int, data: bytes) -> int:
        if b'"method":"resources/read"' in data:
            check_locked("reader")
        return orig_write(fd, data)

    def checked_rename(source: Path, destination: Path) -> None:
        if Path(source).name.startswith(".fetch-"):
            check_locked("snapshot")
        orig_rename(source, destination)

    monkeypatch.setattr(workplace.os, "write", checked_write)
    with monkeypatch.context() as nested:
        nested.setattr(workplace.os, "rename", checked_rename)
        published = workplace.acquire_workplace(fixture.store, fixture.connection)
    assert observed == ["reader", "reader", "snapshot"]
    assert Path(published["grant_path"]).is_file()
    _, terminal = qualification(
        path,
        fixture.grant["organization"],
        intent="lost",
        source_revision=2,
        event_id="generated-source-lost",
    )
    assert terminal["disposition"] == "lost"
