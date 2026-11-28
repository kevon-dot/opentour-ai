"""Generated actual named-verifier history, with current independent inspection."""

from dataclasses import asdict, replace
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from opentour_cell.pilot.intake_mcp import IntakeError, IntakePrincipal
from opentour_cell.pilot.live_evidence import LiveEvidence
from opentour_cell.pilot.live_outcome_history import HistoryScope, HistoryWindow, compare_snapshots
from test_home_readiness import weekly_scope
from test_live_settings import sign
from test_reviewer_workflow import buttons, screen, text
from test_slack_adapter import ACTOR, OTHER
from test_starter_setup import effects
from test_verifier_form import open_form, submission

pytest_plugins = ("test_verifier_form",)


def setup_query(ready):
    actor = IntakePrincipal(ready.h.store.organization, ACTOR, "slack")
    grant = ready.grant
    scope = HistoryScope(
        actor.tenant,
        ready.workflow,
        grant["engagement_epoch"],
        grant["dataset_ref"],
        grant["dataset_revision"],
    )

    def query(old, *, who=actor, selected=scope, window=None, proof=None):
        if window is None:
            day = datetime.fromtimestamp(
                old.last_ingested_at, ZoneInfo("America/Los_Angeles")
            ).date()
            window = HistoryWindow(day, day, "America/Los_Angeles")
        return compare_snapshots(
            ready.directory,
            who,
            selected,
            window,
            historical_sequence=old.sequence,
            historical_evidence_revision=proof or old.evidence_revision,
        )

    return actor, scope, query


def test_actual_verification_reopen_replay_and_reverify(named):
    ready = named
    actor, scope, query = setup_query(ready)

    def verify(state):
        view = open_form(ready)
        return ready.h.form(view, submission(ready, view, state=state), OTHER)

    first_submission = verify("final")
    old = ready.worker.source.live.snapshot(actor, ready.workflow)
    assert old.verified == 1
    verify("reopened")
    reopened = query(old)
    assert (reopened.historical.verified, reopened.historical.pending) == (1, 0)
    assert (reopened.current.verified, reopened.current.pending) == (0, 1)
    ready.h.send("interactive", first_submission)
    assert query(old) == reopened
    verify("final")
    final = query(old)
    assert final.scope == scope and final.historical == reopened.historical
    assert (final.current.verified, final.current.pending) == (1, 0)
    assert final.current.sequence > reopened.current.sequence > old.sequence
    assert (
        len(
            {
                old.evidence_revision,
                reopened.current.evidence_revision,
                final.current.evidence_revision,
            }
        )
        == 3
    )
    assert final.historical.last_ingestion_in_window
    earlier = final.window.first_day - timedelta(days=1)
    other = query(old, window=HistoryWindow(earlier, earlier, final.window.timezone))
    assert not other.historical.last_ingestion_in_window
    assert other.detail_status == "outcome_event_detail_unavailable"
    assert set(asdict(final.current)) == {
        "sequence",
        "evidence_revision",
        "decisions",
        "verified",
        "pending",
        "last_ingested_at",
        "last_ingestion_in_window",
    }
    protected = {
        f: f.read_bytes() for f in ready.directory.iterdir() if f.name.endswith(".sqlite3")
    }
    assert query(old) == final and effects(ready.h) == (0,) * 7
    assert all(f.read_bytes() == b for f, b in protected.items())


@pytest.mark.parametrize("home", [True, False])
def test_weekly_private_control_compares_sealed_historical_to_reopened_current(named, home):
    h = named.h

    def verify(state):
        target = open_form(named)
        h.form(target, submission(named, target, state=state), OTHER)

    verify("final")
    view, _ = weekly_scope(named, home=home)
    retained = buttons(screen(h, view))["ot_week_history"]
    verify("reopened")
    h.action(view, "ot_week_history", retained)
    assert screen(h, view)["callback_id"] == "ot_verification_history"
    assert "Previously opened · 1/1 decisions verified · 0 pending" in text(h, view)
    assert "Current · 0/1 decisions verified · 1 pending" in text(h, view)
    assert "effective times and reasons for a change are unavailable" in text(h, view)
    h.action(view, "ot_week_scopes", buttons(screen(h, view))["ot_week_scopes"])
    assert screen(h, view)["callback_id"] == "ot_regression_picker"

    view, _ = weekly_scope(named, home=home)
    retained = buttons(screen(h, view))["ot_week_history"]
    h.config["users"][ACTOR]["aggregate"] = False
    h.save()
    h.action(view, "ot_week_history", retained)
    assert "Previously opened ·" not in text(h, view)
    assert effects(h) == (0,) * 7


def test_scope_or_window_mismatch_and_revocation_hold(named, monkeypatch):
    ready = named
    actor, scope, query = setup_query(ready)
    old = ready.worker.source.live.snapshot(actor, ready.workflow)
    for change in (
        {"engagement_epoch": "other-epoch"},
        {"dataset_revision": "other-revision"},
        {"workflow_ref": "other-workflow"},
        {"tenant": "foreign"},
    ):
        with pytest.raises((IntakeError, OSError)):
            query(old, selected=replace(scope, **change))
    with pytest.raises(IntakeError):
        query(old, who=IntakePrincipal(actor.tenant, OTHER, "slack"))
    with pytest.raises(IntakeError):
        query(old, proof="wrong-receipt")
    with pytest.raises(IntakeError):
        query(old, window=HistoryWindow(date(2026, 1, 2), date(2026, 1, 1), "UTC"))
    with pytest.raises(IntakeError):
        query(old, window=HistoryWindow(date(2026, 1, 1), date(2026, 1, 1), "No/Such_Place"))
    original = LiveEvidence.snapshot

    def revoke(self, *args, **kwargs):
        value = original(self, *args, **kwargs)
        if kwargs.get("as_of_revision") is None:
            ready.grant["actors"][ACTOR] = ["custodian"]
            sign(ready.h, ready.grant)
        return value

    monkeypatch.setattr(LiveEvidence, "snapshot", revoke)
    with pytest.raises(IntakeError):
        query(old)


def test_dispute_or_missing_independent_authority_withholds_pair(named):
    from test_live_evidence import decision

    ready = named
    actor, _, query = setup_query(ready)
    view = open_form(ready)
    ready.h.form(view, submission(ready, view), OTHER)
    old = ready.worker.source.live.snapshot(actor, ready.workflow)
    ready.ingest(decision(action_ref="another-action"))
    assert ready.worker.source.live.snapshot(actor, ready.workflow).disputed == 1
    with pytest.raises(IntakeError):
        query(old)
    ready.grant["grants"] = ready.grant["grants"][:1]
    ready.grant["slack_verifiers"] = {}
    sign(ready.h, ready.grant)
    with pytest.raises(IntakeError):
        query(old)


@pytest.mark.parametrize("damage", ["legacy", "counts", "sequence", "workflow", "tenant"])
def test_archived_projection_requires_original_attestation(named, damage):
    import sqlite3

    view = open_form(named)
    named.h.form(view, submission(named, view), OTHER)
    actor, scope, query = setup_query(named)
    live = named.worker.source.live
    old = live.snapshot(actor, named.workflow)
    assert query(old).historical.sequence == old.sequence
    where = (scope.tenant, scope.workflow_ref, scope.engagement_epoch, old.sequence)
    with sqlite3.connect(named.directory / "live-evidence.sqlite3") as db:
        import json

        row = db.execute(
            "SELECT body FROM live_snapshots WHERE tenant=? AND workflow=? "
            "AND epoch=? AND sequence=?",
            where,
        ).fetchone()
        saved = json.loads(row[0])
        if damage == "legacy":
            del saved["archive_authentication"]
        elif damage == "counts":
            saved["snapshot"].update(decisions=85, verified=0, pending=85)
        elif damage == "sequence":
            saved["snapshot"]["sequence"] += 1
        else:
            saved["snapshot"]["workflow_ref" if damage == "workflow" else "tenant"] = "foreign"
        db.execute(
            "UPDATE live_snapshots SET body=? WHERE tenant=? AND workflow=? "
            "AND epoch=? AND sequence=?",
            (json.dumps(saved), *where),
        )
    with pytest.raises(IntakeError):
        query(old)
    if damage == "legacy":
        assert live.snapshot(actor, named.workflow, as_of_revision=old.sequence) == old


def test_current_key_rotation_does_not_reseal_old_receipt(named, monkeypatch):
    import test_live_settings
    from opentour_cell.pilot.live_authority import _read_private
    from test_live_settings import _write_private

    view = open_form(named)
    named.h.form(view, submission(named, view), OTHER)
    actor, _, query = setup_query(named)
    old = named.worker.source.live.snapshot(actor, named.workflow)
    query(old)
    key_path = named.directory / "live-authority-key.json"
    key = bytes.fromhex(_read_private(key_path, 4096)["key"])
    replacement = bytes(value ^ 255 for value in key)
    _write_private(key_path, {"key": replacement.hex()})
    monkeypatch.setattr(test_live_settings, "KEY", replacement)
    sign(named.h, named.grant)
    assert named.worker.source.live.snapshot(actor, named.workflow) == old
    with pytest.raises(IntakeError):
        query(old)


def test_failed_archive_attestation_rolls_back_ingestion(named, monkeypatch):
    from opentour_cell.pilot.live_authority import PrivateLiveAuthority
    from test_live_evidence import decision

    actor, _, _ = setup_query(named)
    live = named.worker.source.live
    before = live.snapshot(actor, named.workflow)

    def unavailable(self, scope, body):
        raise IntakeError("live_evidence_unavailable")

    monkeypatch.setattr(PrivateLiveAuthority, "_seal_snapshot_archive", unavailable)
    with pytest.raises(IntakeError):
        named.ingest(decision(decision_id="D2", event_id="E2"))
    assert live.snapshot(actor, named.workflow) == before


@pytest.mark.parametrize("fault", ["withdraw", "rotate"])
def test_authority_change_while_signing_rolls_back_page(named, monkeypatch, fault):
    import sqlite3

    import test_live_settings
    from opentour_cell.pilot.live_authority import PrivateLiveAuthority
    from test_live_evidence import decision
    from test_live_settings import _write_private

    path = named.directory / "live-evidence.sqlite3"

    def persisted():
        with sqlite3.connect(path) as db:
            return tuple(db.iterdump())

    before = persisted()
    original = PrivateLiveAuthority._archive_mac

    def change(key, revision, body):
        value = original(key, revision, body)
        if fault == "rotate":
            new_key = bytes(v ^ 255 for v in key)
            _write_private(named.directory / "live-authority-key.json", {"key": new_key.hex()})
            monkeypatch.setattr(test_live_settings, "KEY", new_key)
        else:
            del named.grant["actors"][named.worker.worker.actor]
        sign(named.h, named.grant)
        return value

    monkeypatch.setattr(PrivateLiveAuthority, "_archive_mac", staticmethod(change))
    with pytest.raises(IntakeError):
        named.ingest(decision(decision_id="D2", event_id="E2"))
    assert persisted() == before


@pytest.mark.parametrize("broken", ["[]", "null", "{", '{"snapshot": [], "grants": null}'])
def test_malformed_archive_has_bounded_hold(named, broken):
    import sqlite3

    actor, scope, query = setup_query(named)
    old = named.worker.source.live.snapshot(actor, named.workflow)
    with sqlite3.connect(named.directory / "live-evidence.sqlite3") as db:
        db.execute(
            "UPDATE live_snapshots SET body=? WHERE tenant=? AND workflow=? "
            "AND epoch=? AND sequence=?",
            (broken, scope.tenant, scope.workflow_ref, scope.engagement_epoch, old.sequence),
        )
    with pytest.raises(IntakeError, match="outcome_history_unavailable"):
        query(old)


@pytest.mark.parametrize("boundary", ["archive_mac", "current_snapshot"])
def test_verification_key_current_through_disclosure(named, monkeypatch, boundary):
    import test_live_settings
    from opentour_cell.pilot.live_authority import PrivateLiveAuthority
    from test_live_settings import _write_private

    view = open_form(named)
    named.h.form(view, submission(named, view), OTHER)
    actor, _, query = setup_query(named)
    old = named.worker.source.live.snapshot(actor, named.workflow)
    assert query(old).historical.sequence == old.sequence

    def rotate():
        successor = bytes(v ^ 255 for v in test_live_settings.KEY)
        _write_private(named.directory / "live-authority-key.json", {"key": successor.hex()})
        monkeypatch.setattr(test_live_settings, "KEY", successor)
        sign(named.h, named.grant)

    if boundary == "archive_mac":
        mac = PrivateLiveAuthority._archive_mac

        def changed_mac(key, revision, body):
            value = mac(key, revision, body)
            rotate()
            return value

        monkeypatch.setattr(PrivateLiveAuthority, "_archive_mac", staticmethod(changed_mac))
    else:
        snapshot = LiveEvidence.snapshot

        def changed_snapshot(*args, **kwargs):
            value = snapshot(*args, **kwargs)
            rotate()
            return value

        monkeypatch.setattr(LiveEvidence, "snapshot", changed_snapshot)
    with pytest.raises(IntakeError):
        query(old)
