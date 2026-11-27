"""Generated actual named-verifier history, with current independent inspection."""

from dataclasses import asdict, replace
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from opentour_cell.pilot.intake_mcp import IntakeError, IntakePrincipal
from opentour_cell.pilot.live_evidence import LiveEvidence
from opentour_cell.pilot.live_outcome_history import HistoryScope, HistoryWindow, compare_snapshots
from test_live_settings import sign
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
        if kwargs.get("as_of_revision") is not None:
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
