"""Generated independent application-level observations with content disabled by default."""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from opentour_cell.pilot import capture_streams
from opentour_cell.pilot.capture_streams import (
    CapturedAttempt,
    CaptureError,
    CaptureGrant,
    CaptureStreams,
    EventRef,
)
from opentour_cell.pilot.custody import Closeout
from opentour_cell.pilot.training import LocalTrainingStore
from test_pilot_custody import START

REQUEST = ("application_request", "request-journal")
RESPONSE = ("provider_response", "response-journal")
COMMIT = ("application_commit", "commit-journal")
SENTINEL = b"GENERATED_PRIVATE_RESPONSE_SENTINEL"


class GrantStore:
    def __init__(self, store):
        self.base = CaptureGrant(
            "app",
            store.organization,
            store.engagement_id,
            "invoice-workflow",
            "capture-grant-v1",
            "policy-v7",
            200,
            "off",
            (REQUEST, RESPONSE, COMMIT),
            bytes(range(32)),
        )

    def resolve(self, actor, workflow, action, boundary):
        if (workflow, action, actor) == (self.base.workflow, "record", "app"):
            return self.base
        if (workflow, action, actor) == (self.base.workflow, "inspect", "owner"):
            return replace(self.base, actor="owner")
        if (workflow, action, actor) == (self.base.workflow, "inspect", "response-inspector"):
            return replace(self.base, actor=actor, sources=(RESPONSE,))
        return None


def row(source, event, when=100):
    return EventRef(source[1], event, 1, when)


def journal(path: Path, tenant="org-a"):
    path.mkdir(mode=0o700)
    store = LocalTrainingStore(path / "training.sqlite3", tenant)
    authority = GrantStore(store)
    return CaptureStreams(store, authority, lambda: 110), authority


def test_metadata_capture_waits_for_opt_in_and_stores_no_plaintext(tmp_path):
    capture, authority = journal(tmp_path / "one")
    workflow = authority.base.workflow
    attempt = CapturedAttempt("operation-a", "attempt-a", "case-a", "family-a", "decision-a", 1)
    with pytest.raises(CaptureError, match="decision_capture_disabled"):
        capture.observed_chunk("app", workflow, attempt, row(RESPONSE, "first"), 0, SENTINEL)
    with capture.store._db() as db:
        assert (
            db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='decision_capture_streams'"
            ).fetchone()
            is None
        )
    authority.base = replace(authority.base, mode="metadata_only")
    capture.started("app", workflow, attempt, row(REQUEST, "start"))
    capture.observed_chunk("app", workflow, attempt, row(RESPONSE, "first"), 0, SENTINEL)
    assert capture.inspect("owner", workflow, attempt.operation_ref)["attempts"][0]["state"] == (
        "incomplete"
    )
    for path in capture.store.path.parent.iterdir():
        assert SENTINEL not in path.read_bytes()
    assert SENTINEL.decode() not in json.dumps(capture.inspect("owner", workflow, "operation-a"))


def test_terminal_gap_survives_restart_and_app_retries_commit_once(tmp_path):
    capture, authority = journal(tmp_path / "one")
    authority.base = replace(authority.base, mode="metadata_only")
    workflow = authority.base.workflow
    first = CapturedAttempt("operation-a", "a1", "case-a", "family-a", "decision-a", 1)
    second = replace(first, attempt_ref="a2")
    capture.started("app", workflow, first, row(REQUEST, "start-1"))
    capture.observed_chunk("app", workflow, first, row(RESPONSE, "partial"), 0, b"sup")
    capture.terminal("app", workflow, first, row(RESPONSE, "timeout"), "timeout", None)
    capture.started("app", workflow, second, row(REQUEST, "start-2"), parent_attempt="a1")
    capture.observed_chunk("app", workflow, second, row(RESPONSE, "chunk-2"), 2, SENTINEL)
    capture.terminal("app", workflow, second, row(RESPONSE, "completed"), "finished", 3)
    capture.observed_chunk("app", workflow, second, row(RESPONSE, "chunk-0"), 0, b"sup")
    capture.observed_chunk("app", workflow, second, row(RESPONSE, "chunk-2"), 2, SENTINEL)
    incomplete = capture.inspect("owner", workflow, first.operation_ref)
    assert incomplete["committed_decisions"] == 0
    a1, a2 = incomplete["attempts"]
    assert (a1["state"], a1["terminal_state"]) == ("incomplete", "timeout")
    assert (a2["state"], a2["terminal_state"], a2["missing_indices"]) == (
        "incomplete",
        "finished",
        [1],
    )
    reopened = CaptureStreams(
        LocalTrainingStore(capture.store.path, "org-a", existing_only=True),
        authority,
        lambda: 110,
    )
    assert reopened.inspect("owner", workflow, first.operation_ref) == incomplete
    reopened.observed_chunk("app", workflow, second, row(RESPONSE, "chunk-1"), 1, b"plies")
    for _ in range(2):
        reopened.committed("app", workflow, second, row(COMMIT, "commit"), "invoice-committed")
    complete = reopened.inspect("owner", workflow, first.operation_ref)
    assert complete["committed_decisions"] == 1 and complete["disputed_decisions"] == 0
    a2 = complete["attempts"][1]
    assert (a2["state"], a2["missing_indices"], a2["exact_replay"]) == (
        "finished",
        [],
        "unavailable",
    )
    scoped = reopened.inspect("response-inspector", workflow, first.operation_ref)
    assert scoped["committed_decisions"] is None and scoped["disputed_decisions"] is None
    assert len(scoped["attempts"]) == 2
    assert SENTINEL not in reopened.store.path.read_bytes()


def test_conflicting_chunk_and_competing_app_commit_remain_distinct(tmp_path):
    capture, authority = journal(tmp_path / "one")
    authority.base = replace(authority.base, mode="metadata_only")
    workflow = authority.base.workflow
    a1 = CapturedAttempt("operation-a", "a1", "case-a", "family-a", "decision-a", 1)
    a2 = replace(a1, attempt_ref="a2")
    capture.started("app", workflow, a2, row(REQUEST, "start"))
    capture.terminal("app", workflow, a2, row(RESPONSE, "finish"), "finished", 1)
    capture.observed_chunk("app", workflow, a2, row(RESPONSE, "chunk"), 0, b"data-a")
    capture.observed_chunk("app", workflow, a2, row(RESPONSE, "chunk"), 0, b"data-b")
    capture.committed("app", workflow, a2, row(COMMIT, "commit-two"), "commit-b")
    one = capture.inspect("owner", workflow, a1.operation_ref)
    assert one["attempts"][0]["state"] == "disputed"
    assert one["committed_decisions"] == 1
    capture.committed("app", workflow, a1, row(COMMIT, "competing"), "commit-a")
    conflicting = capture.inspect("owner", workflow, a1.operation_ref)
    assert conflicting["committed_decisions"] == 0 and conflicting["disputed_decisions"] == 1


@pytest.mark.parametrize("change", ["consent", "source", "expiry"])
def test_current_record_grant_is_rechecked_after_chunk_projection(tmp_path, monkeypatch, change):
    capture, authority = journal(tmp_path / "one")
    current = replace(authority.base, mode="metadata_only")
    authority.base = current
    attempt = CapturedAttempt("operation-a", "a1", "case-a", "family-a", "decision-a", 1)
    capture.started("app", current.workflow, attempt, row(REQUEST, "start"))
    original = capture_streams.hmac.digest
    changes = {
        "consent": replace(current, mode="off"),
        "source": replace(
            current, sources=(REQUEST, ("provider_response", "replacement-journal"), COMMIT)
        ),
        "expiry": replace(current, expires_at=110),
    }

    def project_then_change(*args, **kwargs):
        fingerprint = original(*args, **kwargs)
        authority.base = changes[change]
        return fingerprint

    monkeypatch.setattr(capture_streams.hmac, "digest", project_then_change)
    with pytest.raises(CaptureError):
        capture.observed_chunk("app", current.workflow, attempt, row(RESPONSE, "late"), 0, SENTINEL)
    authority.base = current
    attempt_view = capture.inspect("owner", current.workflow, attempt.operation_ref)["attempts"][0]
    assert attempt_view["observed_chunks"] == 0
    with capture.store._db() as db:
        assert db.execute("SELECT kind FROM decision_capture_streams").fetchall()[0][0] == "start"
        assert db.execute("SELECT count(*) FROM decision_capture_streams").fetchone()[0] == 1


def test_competing_business_revision_is_disputed_across_operations(tmp_path):
    capture, authority = journal(tmp_path / "one")
    authority.base = replace(authority.base, mode="metadata_only")
    workflow = authority.base.workflow
    first = CapturedAttempt("operation-a", "a1", "case-a", "family-a", "decision-a", 1)
    second = replace(first, operation_ref="operation-b", attempt_ref="a2")
    successor = replace(first, operation_ref="operation-c", decision_revision=2)
    another_decision = replace(first, operation_ref="operation-d", decision_ref="decision-b")
    redundant_delivery = replace(first, operation_ref="operation-e")
    for attempt in (first, successor, another_decision):
        ref = attempt.operation_ref
        capture.committed("app", workflow, attempt, row(COMMIT, ref), "commit-" + ref)
    capture.committed(
        "app", workflow, redundant_delivery, row(COMMIT, "redelivery"), "commit-operation-a"
    )
    for attempt in (first, redundant_delivery, another_decision):
        view = capture.inspect("owner", workflow, attempt.operation_ref)
        assert (view["committed_decisions"], view["disputed_decisions"]) == (1, 0)
    capture.committed("app", workflow, second, row(COMMIT, "operation-b"), "competing-commit")
    for attempt in (first, second):
        view = capture.inspect("owner", workflow, attempt.operation_ref)
        assert (view["committed_decisions"], view["disputed_decisions"]) == (0, 1)
        assert [item["attempt_ref"] for item in view["attempts"]] == [attempt.attempt_ref]
    for attempt in (successor, another_decision):
        view = capture.inspect("owner", workflow, attempt.operation_ref)
        assert (view["committed_decisions"], view["disputed_decisions"]) == (1, 0)
    scoped = capture.inspect("response-inspector", workflow, first.operation_ref)
    assert scoped["committed_decisions"] is None and scoped["disputed_decisions"] is None


def test_actor_source_policy_and_withdrawal_scope_normal_reads(tmp_path):
    capture, authority = journal(tmp_path / "one")
    neighbor, other = journal(tmp_path / "two", "org-b")
    authority.base = replace(authority.base, mode="metadata_only")
    other.base = replace(other.base, mode="metadata_only")
    workflow = authority.base.workflow
    attempt = CapturedAttempt("operation-a", "a1", "case-a", "family-a", "decision-a", 1)
    with pytest.raises(CaptureError):
        capture.committed("app", workflow, attempt, row(RESPONSE, "forged"), "commit")
    capture.started("app", workflow, attempt, row(REQUEST, "start"))
    capture.committed("app", workflow, attempt, row(COMMIT, "genuine"), "commit")
    with pytest.raises(CaptureError, match="capture_event_scope_changed"):
        capture.committed(
            "app",
            workflow,
            replace(attempt, operation_ref="foreign-operation"),
            row(COMMIT, "genuine"),
            "commit",
        )
    assert neighbor.inspect("owner", workflow, attempt.operation_ref)["committed_decisions"] == 0
    with pytest.raises(CaptureError):
        capture.inspect("foreign-owner", workflow, attempt.operation_ref)
    prior = authority.base
    authority.base = replace(prior, policy_revision="policy-v8")
    assert capture.inspect("owner", workflow, attempt.operation_ref)["committed_decisions"] == 0
    authority.base = prior
    Closeout(capture.store.path.parent, capture.store.organization).withdraw(**START)
    with pytest.raises(CaptureError):
        capture.inspect("owner", workflow, attempt.operation_ref)
