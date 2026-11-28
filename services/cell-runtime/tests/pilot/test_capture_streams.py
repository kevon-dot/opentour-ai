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
    ProjectionSpec,
    RequestMetadata,
    ResponseMetadata,
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
        reopened.committed(
            "app",
            workflow,
            second,
            row(COMMIT, "commit"),
            "invoice-committed",
            decision_payload_ref="opaque-committed-payload",
        )
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


def test_original_attempt_metadata_is_scoped_and_reopens_without_original_bodies(tmp_path):
    capture, authority = journal(tmp_path / "one")
    authority.base = replace(authority.base, mode="metadata_only")
    workflow = authority.base.workflow
    attempt = CapturedAttempt("operation-a", "attempt-a", "case-a", "family-a", "decision-a", 1)
    capture.started(
        "app",
        workflow,
        attempt,
        row(REQUEST, "observed-request", 100),
        metadata=RequestMetadata("requested-alias", "projection-v1", "prompt-v7"),
    )
    capture.observed_chunk("app", workflow, attempt, row(RESPONSE, "chunk"), 0, SENTINEL)
    capture.terminal(
        "app",
        workflow,
        attempt,
        row(RESPONSE, "observed-response", 104),
        "finished",
        1,
        metadata=ResponseMetadata("provider-receipt-a", "served-v3", "parsed-output", "parsing-v1"),
    )
    full = capture.inspect("owner", workflow, attempt.operation_ref)
    observed = full["attempts"][0]
    assert full["committed_decisions"] == 0
    assert observed["request"] == {
        "observed_at": 100,
        "requested_model_ref": "requested-alias",
        "projection_revision": "projection-v1",
        "template_revision": "prompt-v7",
    }
    assert observed["response"] == {
        "observed_at": 104,
        "provider_receipt_ref": "provider-receipt-a",
        "served_model_ref": "served-v3",
        "parsed_output_ref": "parsed-output",
        "parser_revision": "parsing-v1",
    }
    assert observed["exact_replay"] == "unavailable"
    scoped = capture.inspect("response-inspector", workflow, attempt.operation_ref)["attempts"][0]
    assert "request" not in scoped and scoped["response"] == observed["response"]
    reopened = CaptureStreams(
        LocalTrainingStore(capture.store.path, "org-a", existing_only=True),
        authority,
        lambda: 110,
    )
    assert reopened.inspect("owner", workflow, attempt.operation_ref) == full
    assert SENTINEL not in capture.store.path.read_bytes()
    assert SENTINEL.decode() not in json.dumps(full)
    with capture.store._db(write=True) as db:
        db.execute(
            "UPDATE decision_capture_streams SET payload="
            "json_set(payload, '$.response.served_model_ref', 'invented-served-model') "
            "WHERE kind='terminal'"
        )
    with pytest.raises(CaptureError, match="decision_capture_unavailable"):
        reopened.inspect("owner", workflow, attempt.operation_ref)


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
    capture.committed(
        "app",
        workflow,
        a2,
        row(COMMIT, "commit-two"),
        "commit-b",
        decision_payload_ref="opaque-payload-b",
    )
    one = capture.inspect("owner", workflow, a1.operation_ref)
    assert one["attempts"][0]["state"] == "disputed"
    assert one["committed_decisions"] == 1
    capture.committed(
        "app",
        workflow,
        a1,
        row(COMMIT, "competing"),
        "commit-a",
        decision_payload_ref="opaque-payload-a",
    )
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
        capture.committed(
            "app",
            workflow,
            attempt,
            row(COMMIT, ref),
            "commit-" + ref,
            decision_payload_ref="opaque-payload-" + ref,
        )
    capture.committed(
        "app",
        workflow,
        redundant_delivery,
        row(COMMIT, "redelivery"),
        "new-receipt",
        decision_payload_ref="opaque-payload-operation-a",
    )
    for attempt in (first, redundant_delivery, another_decision):
        view = capture.inspect("owner", workflow, attempt.operation_ref)
        assert (view["committed_decisions"], view["disputed_decisions"]) == (1, 0)
    capture.committed(
        "app",
        workflow,
        second,
        row(COMMIT, "operation-b"),
        "competing-commit",
        decision_payload_ref="changed-payload",
    )
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
        capture.committed(
            "app",
            workflow,
            attempt,
            row(RESPONSE, "forged"),
            "commit",
            decision_payload_ref="opaque-payload",
        )
    capture.started("app", workflow, attempt, row(REQUEST, "start"))
    capture.committed(
        "app",
        workflow,
        attempt,
        row(COMMIT, "genuine"),
        "commit",
        decision_payload_ref="opaque-payload",
    )
    with pytest.raises(CaptureError, match="capture_event_scope_changed"):
        capture.committed(
            "app",
            workflow,
            replace(attempt, operation_ref="foreign-operation"),
            row(COMMIT, "genuine"),
            "commit",
            decision_payload_ref="opaque-payload",
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


@pytest.mark.parametrize("disputed_revision", [1, 2])
def test_current_counts_use_newest_revision_and_retain_prior_attempts(tmp_path, disputed_revision):
    capture, authority = journal(tmp_path / "one")
    authority.base = replace(authority.base, mode="metadata_only")
    workflow = authority.base.workflow
    first = CapturedAttempt("operation-a", "attempt-a", "case-a", "family-a", "decision-a", 1)
    newer = replace(first, attempt_ref="attempt-b", decision_revision=2)
    other = replace(first, attempt_ref="attempt-c", decision_ref="decision-b")
    dispute = replace(first, attempt_ref="attempt-disputed", decision_revision=disputed_revision)
    for attempt in (first, newer, other, dispute):
        ref = attempt.attempt_ref
        capture.committed(
            "app",
            workflow,
            attempt,
            row(COMMIT, ref),
            "receipt-" + ref,
            decision_payload_ref="payload-" + ref,
        )
    view = capture.inspect("owner", workflow, first.operation_ref)
    expected = (2, 0) if disputed_revision == 1 else (1, 1)
    assert (view["committed_decisions"], view["disputed_decisions"]) == expected
    assert {item["attempt_ref"] for item in view["attempts"]} == {
        attempt.attempt_ref for attempt in (first, newer, other, dispute)
    }


# Follow-through tests for sealed rows, the missing-chunk control and exact projections.
# Labels: MC is the ticket's missing-chunk and denominator control, CU custody and failure.


def _streams(tmp_path):
    from capture_fixtures import ledger

    return ledger(tmp_path / "owner")


def _gap_stream(capture, operation="op-d1", *, chunks=(0, 2)):
    from capture_fixtures import NOW, REQUEST_PROJECTION, STREAM, WORKFLOW

    attempt = CapturedAttempt(operation, "a1", "case-d1", "family-d1", "D1", 1)

    def event(source, name, when):
        return EventRef(source, name, 1, when)

    capture.started(
        "gateway-collector",
        WORKFLOW,
        attempt,
        event("gateway-log", operation + "-start", 100),
        metadata=RequestMetadata("invoice-alias", "projection-v1", "P7"),
        request_projection=REQUEST_PROJECTION,
    )
    for index in chunks:
        capture.observed_chunk(
            "gateway-collector",
            WORKFLOW,
            attempt,
            event("gateway-log", f"{operation}-chunk-{index}", 101 + index),
            index,
            STREAM[index],
        )
    capture.terminal(
        "gateway-collector",
        WORKFLOW,
        attempt,
        event("gateway-log", operation + "-terminal", 104),
        "finished",
        3,
        metadata=ResponseMetadata("receipt-1", None, "SUPPLIES", "parser-v1"),
    )
    assert NOW > 104
    return attempt


def test_mc_copied_or_edited_rows_cannot_fill_a_gap_or_invent_a_commit(tmp_path):
    from capture_fixtures import WORKFLOW

    capture, _ = _streams(tmp_path)
    _gap_stream(capture, "op-full", chunks=(0, 1, 2))
    _gap_stream(capture, "op-gap")
    assert capture.inspect("owner", WORKFLOW, "op-gap")["attempts"][0]["exact_stream"] is False
    with capture.store._db(write=True) as db:
        db.execute(
            "INSERT INTO decision_capture_streams SELECT tenant,epoch,workflow,grant_revision,"
            "policy_revision,'op-gap',replace(attempt,'op-full','op-gap'),decision_ref,"
            "decision_revision,source_ref,'copied-chunk',event_revision,observed_at,kind,payload "
            "FROM decision_capture_streams WHERE operation_ref='op-full' AND kind='chunk' "
            "AND json_extract(payload,'$.index')=1"
        )
    with pytest.raises(CaptureError, match="decision_capture_unavailable"):
        capture.inspect("owner", WORKFLOW, "op-gap")
    with capture.store._db(write=True) as db:
        db.execute("DELETE FROM decision_capture_streams WHERE event_ref='copied-chunk'")
        db.execute(
            "INSERT INTO decision_capture_streams SELECT tenant,epoch,workflow,grant_revision,"
            "policy_revision,operation_ref,attempt,decision_ref,decision_revision,"
            "'decision-journal','forged-commit',1,observed_at,'commit',"
            "json_object('commit_ref','forged','decision_payload_ref','forged','seal',"
            "json_extract(payload,'$.seal')) "
            "FROM decision_capture_streams WHERE operation_ref='op-gap' AND kind='chunk' LIMIT 1"
        )
    with pytest.raises(CaptureError, match="decision_capture_unavailable"):
        capture.inspect("owner", WORKFLOW, "op-gap")
    with capture.store._db(write=True) as db:
        db.execute("DELETE FROM decision_capture_streams WHERE event_ref='forged-commit'")
    assert capture.inspect("owner", WORKFLOW, "op-gap")["committed_decisions"] == 0


def test_mc_final_body_from_another_source_or_actor_is_refused(tmp_path):
    from capture_fixtures import STREAM, WORKFLOW

    capture, _ = _streams(tmp_path)
    attempt = _gap_stream(capture)
    for actor, source in (
        ("journal-collector", "decision-journal"),
        ("gateway-collector", "transcript-export"),
        ("owner", "gateway-log"),
    ):
        with pytest.raises(CaptureError):
            capture.final_body(
                actor,
                WORKFLOW,
                attempt,
                EventRef(source, "body", 1, 105),
                b"".join(STREAM),
                tuple(len(part) for part in STREAM),
            )
    with pytest.raises(CaptureError):
        capture.final_body(
            "gateway-collector",
            WORKFLOW,
            attempt,
            EventRef("gateway-log", "body", 1, 105),
            b"".join(STREAM),
            (1, 2, 3),
        )
    assert capture.inspect("owner", WORKFLOW, "op-d1")["attempts"][0]["exact_stream"] is False


def test_mc_request_projection_verifies_in_a_fresh_process(tmp_path):
    import subprocess
    import sys

    from capture_fixtures import COMPONENTS, NOW, REQUEST_PROJECTION, WORKFLOW, install_local

    capture, _ = _streams(tmp_path)
    capture.authority = install_local(capture)
    saved = tmp_path / "saved-projection.json"
    saved.write_bytes(REQUEST_PROJECTION)
    script = (
        "import json,sys\n"
        "from pathlib import Path\n"
        "from opentour_cell.pilot.capture_streams import CaptureStreams, ProjectionSpec\n"
        "from opentour_cell.pilot.local_capture_authority import LocalCaptureAuthority\n"
        "from opentour_cell.pilot.training import LocalTrainingStore\n"
        "store = LocalTrainingStore(Path(sys.argv[1]), 'org-a', existing_only=True)\n"
        f"authority = LocalCaptureAuthority(Path(sys.argv[1]).parent, 'org-a', {WORKFLOW!r})\n"
        f"streams = CaptureStreams(store, authority, lambda: {NOW})\n"
        "spec = ProjectionSpec('application_request', 'canonical-json-v1', 'projection-v1',"
        f" {COMPONENTS!r})\n"
        "saved = Path(sys.argv[2]).read_bytes()\n"
        "print(json.dumps(streams.verify_projection('owner', "
        f"{WORKFLOW!r}, 'op-d1', 'a1', spec, saved)))\n"
    )
    _gap_stream(capture, chunks=(0, 1, 2))

    def fresh(path):
        result = subprocess.run(
            [sys.executable, "-c", script, str(capture.store.path), str(path)],
            capture_output=True,
            timeout=120,
            check=False,
        )
        assert result.returncode == 0, result.stderr[-600:]
        return json.loads(result.stdout)

    verdict = fresh(saved)
    assert (verdict["exact_replay"], verdict["reason"]) == ("verified", None)
    assert (verdict["integrity"], verdict["issuer_authenticated"]) == ("locally_keyed", False)
    saved.write_bytes(REQUEST_PROJECTION.replace(b"PO-v1", b"PO-v2"))
    assert fresh(saved)["reason"] == "fingerprint_mismatch"


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (lambda value: {k: v for k, v in value.items() if k != "tools"}, "missing_component"),
        (lambda value: value | {"extra": 1}, "undeclared_component"),
        (lambda value: json.dumps(value, sort_keys=False).encode(), "normalization_collision"),
        (
            lambda value: json.dumps(value, ensure_ascii=True, sort_keys=True).encode(),
            "normalization_collision",
        ),
        (
            lambda value: (
                b'{"tools":[],"tools":[],'
                + json.dumps(value, separators=(",", ":"), sort_keys=True).encode()[1:]
            ),
            "normalization_collision",
        ),
        (lambda value: b"[" + b"]", "unparseable_projection"),
        (lambda value: b"\xff\xfe", "unparseable_projection"),
    ],
)
def test_mc_missing_components_and_normalization_collisions_fail_exact_replay(
    tmp_path, change, reason
):
    from capture_fixtures import COMPONENTS, REQUEST_PROJECTION, WORKFLOW, projection_bytes

    capture, _ = _streams(tmp_path)
    _gap_stream(capture, chunks=(0, 1, 2))
    spec = ProjectionSpec("application_request", "canonical-json-v1", "projection-v1", COMPONENTS)
    value = json.loads(REQUEST_PROJECTION)
    changed = change(value)
    saved = changed if isinstance(changed, bytes) else projection_bytes(changed)
    verdict = capture.verify_projection("owner", WORKFLOW, "op-d1", "a1", spec, saved)
    assert (verdict["exact_replay"], verdict["reason"]) == ("unavailable", reason)
    exact = capture.verify_projection("owner", WORKFLOW, "op-d1", "a1", spec, REQUEST_PROJECTION)
    assert exact["exact_replay"] == "verified"
    other = ProjectionSpec("application_request", "canonical-json-v1", "projection-v2", COMPONENTS)
    assert (
        capture.verify_projection("owner", WORKFLOW, "op-d1", "a1", other, REQUEST_PROJECTION)[
            "reason"
        ]
        == "projection_revision_mismatch"
    )
    with pytest.raises(CaptureError):
        capture.verify_projection("response-inspector", WORKFLOW, "op-d1", "a1", spec, saved)


def test_mc_response_bytes_cannot_fill_an_unobserved_chunk(tmp_path):
    from capture_fixtures import STREAM, WORKFLOW

    capture, _ = _streams(tmp_path)
    attempt = _gap_stream(capture)
    spec = ProjectionSpec("provider_response", "raw-chunks-v1", "projection-v1")
    gap = capture.verify_projection("owner", WORKFLOW, "op-d1", "a1", spec, STREAM)
    assert (gap["exact_replay"], gap["reason"]) == ("unavailable", "incomplete_stream")
    capture.final_body(
        "gateway-collector",
        WORKFLOW,
        attempt,
        EventRef("gateway-log", "final-body", 1, 106),
        b"".join(STREAM),
        tuple(len(part) for part in STREAM),
    )
    view = capture.inspect("response-inspector", WORKFLOW, "op-d1")["attempts"][0]
    assert (view["exact_stream"], view["completed_by"], view["received_indices"]) == (
        True,
        "final_body",
        [0, 2],
    )
    verified = capture.verify_projection(
        "response-inspector", WORKFLOW, "op-d1", "a1", spec, STREAM
    )
    assert verified["exact_replay"] == "verified"
    wrong = (STREAM[0], b"PLIERS", STREAM[2])
    assert capture.verify_projection("owner", WORKFLOW, "op-d1", "a1", spec, wrong)["reason"] == (
        "fingerprint_mismatch"
    )
    assert (
        capture.verify_projection("owner", WORKFLOW, "op-d1", "a1", spec, STREAM[:2])["reason"]
        == "missing_component"
    )


def test_cu_receipts_and_dead_letters_are_scoped_and_conflicts_refused(tmp_path):
    from capture_fixtures import WORKFLOW
    from opentour_cell.pilot.capture_streams import PageReceipt

    capture, _ = _streams(tmp_path)
    receipt = PageReceipt(
        "gateway-log", "collector-1", "projection-v1", "cursor-1", "cursor-2", 1, 1, 0, 100, 200
    )
    page = b"generated page bytes"
    status = capture.page_status(
        "gateway-collector", WORKFLOW, "provider_response", "gateway-log", "cursor-1", page
    )
    assert status == "new"
    capture.quarantined(
        "gateway-collector",
        WORKFLOW,
        "provider_response",
        "gateway-log",
        "cursor-1",
        3,
        "malformed_record",
    )
    with pytest.raises(CaptureError):
        capture.quarantined(
            "gateway-collector",
            WORKFLOW,
            "provider_response",
            "gateway-log",
            "cursor-1",
            4,
            "free text reason",
        )
    stored = capture.acknowledge("gateway-collector", WORKFLOW, "provider_response", receipt, page)
    assert (
        capture.acknowledge("gateway-collector", WORKFLOW, "provider_response", receipt, page)
        == stored
    )
    assert (
        capture.page_status(
            "gateway-collector", WORKFLOW, "provider_response", "gateway-log", "cursor-1", page
        )
        == "acknowledged"
    )
    for changed in (page + b"!", None):
        with pytest.raises(CaptureError, match="capture_cursor_conflict"):
            if changed is None:
                capture.acknowledge(
                    "gateway-collector",
                    WORKFLOW,
                    "provider_response",
                    PageReceipt(**{**receipt.__dict__, "admitted": 2}),
                    page,
                )
            else:
                capture.page_status(
                    "gateway-collector",
                    WORKFLOW,
                    "provider_response",
                    "gateway-log",
                    "cursor-1",
                    changed,
                )
    last = capture.last_receipt("gateway-collector", WORKFLOW, "provider_response", "gateway-log")
    assert last["next_cursor"] == "cursor-2" and len(last["page_ref"]) == 64
    owner = capture.receipts("owner", WORKFLOW)
    assert owner["provider_response"]["quarantined"] == {"malformed_record": 1}
    assert "page_ref" not in owner["provider_response"]["pages"][0]
    assert set(capture.receipts("response-inspector", WORKFLOW)) == {"provider_response"}
    with pytest.raises(CaptureError):
        capture.last_receipt("owner", WORKFLOW, "provider_response", "gateway-log")
