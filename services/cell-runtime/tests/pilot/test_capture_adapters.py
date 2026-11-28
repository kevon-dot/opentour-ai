"""Generated gateway, journal and historical-export capture; no provider or customer evidence.

Acceptance labels: WA1-WA3 are the ticket's worked acceptance items 1-3, MC is the
missing-chunk and denominator control, CU is custody and failure behavior.
"""

import ast
import hashlib
import json
import socket
from dataclasses import replace
from pathlib import Path

import pytest
from capture_fixtures import (
    CREDENTIAL_SENTINEL,
    GATEWAY,
    HISTORY,
    JOURNAL,
    NOW,
    REQUEST_PROJECTION,
    RESTRICTED,
    STREAM,
    WORKFLOW,
    LiveCase,
    b64,
    chunk,
    commit,
    final_body,
    fixture_w_gateway,
    fixture_w_journal,
    identity,
    jsonl,
    ledger,
    live_decision,
    live_outcome,
    reopen,
    request,
    terminal,
)
from opentour_cell.pilot import capture_adapters
from opentour_cell.pilot.capture_adapters import (
    GATEWAY_SCHEMA,
    HISTORICAL_SCHEMA,
    JOURNAL_SCHEMA,
    AsOfArchive,
    CaptureSourceAdapter,
    HistoricalImport,
    PinnedExport,
    SourcePage,
    application_outcomes,
    capture_coverage,
    dry_run_capture,
    dry_run_historical,
    historical_outcomes,
    provider_outcomes,
    sampling_manifest,
    score_boundary,
)
from opentour_cell.pilot.capture_streams import CaptureError

GATEWAY_ACTOR = "gateway-collector"
JOURNAL_ACTOR = "journal-collector"
HISTORY_ACTOR = "history-collector"
SRC = Path(capture_adapters.__file__).parent


def page(records, cursor, source=GATEWAY, collector="collector-1", next_cursor=None):
    data = records if isinstance(records, bytes) else jsonl(records)
    return SourcePage(source, collector, cursor, next_cursor, 100, 200, data)


def scan(capture, *extra) -> list[str]:
    """Every persisted byte in the store directory plus any supplied outbound values."""
    blobs = [path.read_bytes() for path in capture.store.path.parent.iterdir() if path.is_file()]
    blobs += [json.dumps(value, sort_keys=True, default=str).encode() for value in extra]
    found = []
    for value in RESTRICTED:
        raw = value.encode()
        forms = (raw, raw.hex().encode(), hashlib.sha256(raw).hexdigest().encode())
        if any(form in blob for blob in blobs for form in forms):
            found.append(value)
    return found


def worked(tmp_path: Path, journal_payload="payload-supplies"):
    capture, authority = ledger(tmp_path / "owner")
    adapter = CaptureSourceAdapter(capture, lambda: NOW)
    adapter.ingest_gateway(GATEWAY_ACTOR, WORKFLOW, page(fixture_w_gateway(), "gateway-1"))
    journal = page(fixture_w_journal(journal_payload), "journal-1", JOURNAL)
    adapter.ingest_journal(JOURNAL_ACTOR, WORKFLOW, journal)
    return capture, authority, adapter


def test_wa1_two_attempts_one_commit_through_restart_and_redelivery(tmp_path, monkeypatch):
    attempts = []
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: attempts.append(a))
    monkeypatch.setattr(socket.socket, "connect", lambda *a, **k: attempts.append(a))
    capture, _ = ledger(tmp_path / "owner")
    adapter = CaptureSourceAdapter(capture, lambda: NOW)
    first = adapter.ingest_gateway(GATEWAY_ACTOR, WORKFLOW, page(fixture_w_gateway(), "gw-1"))
    receipts = first["receipts"]
    assert (
        receipts["application_request"]["admitted"],
        receipts["provider_response"]["admitted"],
    ) == (
        2,
        7,
    )
    assert receipts["application_request"]["dropped_fields"] == 6
    assert receipts["provider_response"]["dropped_fields"] == 5
    journal = page(fixture_w_journal(), "journal-1", JOURNAL)

    def crash(*args, **kwargs):
        raise CaptureError("decision_capture_unavailable")

    monkeypatch.setattr(capture, "acknowledge", crash)
    with pytest.raises(CaptureError, match="decision_capture_unavailable"):
        adapter.ingest_journal(JOURNAL_ACTOR, WORKFLOW, journal)
    restarted = reopen(capture)
    after = CaptureSourceAdapter(restarted, lambda: NOW)
    assert restarted.last_receipt(JOURNAL_ACTOR, WORKFLOW, "application_commit", JOURNAL) is None
    second = after.ingest_journal(JOURNAL_ACTOR, WORKFLOW, journal)
    assert second["receipts"]["application_commit"]["admitted"] == 1
    replay = after.ingest_gateway(GATEWAY_ACTOR, WORKFLOW, page(fixture_w_gateway(), "gw-1"))
    assert replay["status"] == "acknowledged"
    view = restarted.inspect("owner", WORKFLOW, "op-d1")
    a1, a2 = view["attempts"]
    assert (a1["state"], a1["terminal_state"], a1["received_indices"]) == (
        "incomplete",
        "timeout",
        [0],
    )
    assert (a1["committed"], a1["exact_stream"]) == (False, False)
    assert (a2["state"], a2["received_indices"], a2["completed_by"]) == (
        "finished",
        [0, 1, 2],
        "chunks",
    )
    assert a2["committed"] is True and a2["exact_replay"] == "unavailable"
    assert a2["response"]["parsed_output_ref"] == "SUPPLIES"
    assert a2["response"]["served_model_ref"] is None
    assert (view["committed_decisions"], view["disputed_decisions"]) == (1, 0)
    assert attempts == []
    assert scan(restarted, view, first, second) == []


@pytest.mark.parametrize("reverse", [False, True])
def test_wa1_conflicts_are_disputed_without_an_arrival_order_winner(tmp_path, reverse):
    capture, _ = ledger(tmp_path / "owner")
    adapter = CaptureSourceAdapter(capture, lambda: NOW)
    records = fixture_w_gateway() + [chunk("a2", "chunk-a2-1b", 114, 1, b"PLIERS")]
    commits = [
        commit("a2", "commit-a2", 120, "payload-supplies"),
        commit("a1", "commit-a1", 121, "payload-review", ref="commit-other"),
    ]
    if reverse:
        records, commits = records[::-1], commits[::-1]
    adapter.ingest_gateway(GATEWAY_ACTOR, WORKFLOW, page(records, "gateway-1"))
    adapter.ingest_journal(JOURNAL_ACTOR, WORKFLOW, page(commits, "journal-1", JOURNAL))
    view = capture.inspect("owner", WORKFLOW, "op-d1")
    states = {item["attempt_ref"]: item["state"] for item in view["attempts"]}
    assert states == {"a1": "incomplete", "a2": "disputed"}
    assert (view["committed_decisions"], view["disputed_decisions"]) == (0, 1)


def test_cu_allowlist_drops_credentials_and_unrelated_fields_before_persistence(tmp_path, caplog):
    caplog.set_level("DEBUG", logger="opentour_cell")
    capture, _, adapter = worked(tmp_path)
    receipts = capture.receipts("owner", WORKFLOW)
    dropped = {
        name: [p["dropped_fields"] for p in item["pages"]] for name, item in receipts.items()
    }
    assert dropped == {
        "application_commit": [1],
        "application_request": [6],
        "historical_export": [],
        "provider_response": [5],
    }
    logs = [(r.getMessage(), getattr(r, "capture", None)) for r in caplog.records]
    assert any(message == "capture page acknowledged" for message, _ in logs)
    assert scan(capture, receipts, logs) == []
    assert all(CREDENTIAL_SENTINEL not in record.getMessage() for record in caplog.records)


def test_cu_unusable_records_are_dead_lettered_by_position_and_reason_only(tmp_path, monkeypatch):
    capture, _ = ledger(tmp_path / "owner")
    adapter = CaptureSourceAdapter(capture, lambda: NOW)
    good = chunk("a1", "chunk-ok", 101, 0, STREAM[0])
    no_operation = {key: value for key, value in chunk("a1", "c-x", 101, 1, b"x").items()}
    no_operation.pop("operation_id")
    bad = [
        b"not json " + RESTRICTED[0].encode(),
        b'{"schema":"a","schema":"' + RESTRICTED[1].encode() + b'"}',
        json.dumps(
            {"schema": GATEWAY_SCHEMA, "record": "transcript", "text": RESTRICTED[1]}
        ).encode(),
        json.dumps(
            {"schema": GATEWAY_SCHEMA, "record": "tool_summary", "text": RESTRICTED[1]}
        ).encode(),
        json.dumps(no_operation).encode(),
        json.dumps(chunk("a1", "c-future", NOW + 1, 1, b"x")).encode(),
        json.dumps(
            {**chunk("a1", "c-b64", 101, 1, b"x"), "data_b64": "%" + RESTRICTED[1]}
        ).encode(),
        json.dumps(request("a1", "r-1", 100)).encode(),
        b"x" * 64,
        json.dumps(good).encode(),
    ]
    monkeypatch.setattr(capture_adapters, "MAX_RECORD_BYTES", 2048)
    bad[8] = json.dumps({"schema": GATEWAY_SCHEMA, "pad": "y" * 4096}).encode()
    result = adapter.ingest_gateway(
        GATEWAY_ACTOR, WORKFLOW, page(b"\n".join(bad), "gw-bad"), boundaries=("provider_response",)
    )
    assert result["receipts"]["provider_response"]["admitted"] == 1
    reasons = capture.receipts("owner", WORKFLOW)["provider_response"]["quarantined"]
    assert reasons == {
        "boundary_mismatch": 1,
        "duplicate_key": 1,
        "future_observation": 1,
        "invalid_field": 1,
        "malformed_record": 1,
        "oversized_record": 1,
        "unattributable": 1,
        "unsupported_record": 2,
    }
    with capture.store._db() as db:
        rows = [tuple(row) for row in db.execute("SELECT * FROM decision_capture_quarantine")]
    assert sorted(row[-2] for row in rows) == list(range(9))
    assert scan(capture, rows) == []
    view = capture.inspect("owner", WORKFLOW, "op-d1")
    assert view["attempts"][0]["received_indices"] == [0]


def test_mc_gap_closes_only_from_the_identical_authorized_chunk(tmp_path):
    capture, _ = ledger(tmp_path / "owner")
    adapter = CaptureSourceAdapter(capture, lambda: NOW)
    head = [
        request("a1", "r-1", 100),
        chunk("a1", "c-0", 101, 0, STREAM[0]),
        chunk("a1", "c-2", 103, 2, STREAM[2]),
        terminal("a1", "t-1", 104, "finished", 3, provider_receipt="receipt-1"),
    ]
    adapter.ingest_gateway(GATEWAY_ACTOR, WORKFLOW, page(head, "gw-1"))
    attempt = capture.inspect("owner", WORKFLOW, "op-d1")["attempts"][0]
    assert (attempt["received_indices"], attempt["terminal_seen"], attempt["exact_stream"]) == (
        [0, 2],
        True,
        False,
    )
    assert (attempt["missing_indices"], attempt["state"]) == ([1], "incomplete")
    guesses = [
        {**chunk("a1", "transcript", 105, 1, STREAM[1]), "record": "transcript"},
        {**chunk("a1", "summary", 105, 1, STREAM[1]), "record": "tool_summary"},
    ]
    adapter.ingest_gateway(GATEWAY_ACTOR, WORKFLOW, page(guesses, "gw-guess"))
    adapter.ingest_gateway(
        GATEWAY_ACTOR,
        WORKFLOW,
        page([chunk("a1", "c-1", 105, 1, STREAM[1])], "gw-wrong-boundary"),
        boundaries=("application_request",),
    )
    with pytest.raises(CaptureError):
        adapter.ingest_gateway(
            GATEWAY_ACTOR,
            WORKFLOW,
            page([chunk("a1", "c-1", 105, 1, STREAM[1])], "later", source="transcript-export"),
        )
    with pytest.raises(CaptureError):
        adapter.ingest_journal(
            JOURNAL_ACTOR, WORKFLOW, page([chunk("a1", "c-1", 105, 1, STREAM[1])], "j", GATEWAY)
        )
    assert capture.inspect("owner", WORKFLOW, "op-d1")["attempts"][0]["exact_stream"] is False
    adapter.ingest_gateway(
        GATEWAY_ACTOR, WORKFLOW, page([chunk("a1", "c-1", 105, 1, STREAM[1])], "gw-2")
    )
    attempt = capture.inspect("owner", WORKFLOW, "op-d1")["attempts"][0]
    assert (attempt["received_indices"], attempt["exact_stream"], attempt["state"]) == (
        [0, 1, 2],
        True,
        "finished",
    )


def test_mc_authenticated_final_body_completes_only_when_consistent(tmp_path):
    capture, _ = ledger(tmp_path / "owner")
    adapter = CaptureSourceAdapter(capture, lambda: NOW)

    def stream(operation, *extra):
        ids = {"operation": operation}
        return [
            request("a1", operation + "-r", 100, **ids),
            chunk("a1", operation + "-c0", 101, 0, STREAM[0], **ids),
            chunk("a1", operation + "-c2", 103, 2, STREAM[2], **ids),
            *extra,
        ]

    def finished(operation, state="finished", expected=3):
        return terminal("a1", operation + "-t", 104, state, expected, operation=operation)

    records = [
        *stream(
            "op-body",
            finished("op-body"),
            final_body("a1", "op-body-f", 106, STREAM, operation="op-body"),
        ),
        *stream(
            "op-wrong",
            finished("op-wrong"),
            final_body(
                "a1", "op-wrong-f", 106, [b"X" + STREAM[0], *STREAM[1:]], operation="op-wrong"
            ),
        ),
        *stream(
            "op-timeout",
            finished("op-timeout", "timeout", None),
            final_body("a1", "op-timeout-f", 106, STREAM, operation="op-timeout"),
        ),
        *stream("op-open", final_body("a1", "op-open-f", 106, STREAM, operation="op-open")),
    ]
    adapter.ingest_gateway(GATEWAY_ACTOR, WORKFLOW, page(records, "gw-bodies"))
    views = {
        operation: capture.inspect("owner", WORKFLOW, operation)["attempts"][0]
        for operation in ("op-body", "op-wrong", "op-timeout", "op-open")
    }
    body = views["op-body"]
    assert (body["state"], body["exact_stream"], body["completed_by"]) == (
        "finished",
        True,
        "final_body",
    )
    assert (body["received_indices"], body["missing_indices"]) == ([0, 2], [1])
    assert views["op-wrong"]["state"] == views["op-timeout"]["state"] == "disputed"
    assert (views["op-open"]["state"], views["op-open"]["exact_stream"]) == ("incomplete", False)
    late = chunk("a1", "op-body-c1", 107, 1, b"PLIERS", operation="op-body")
    adapter.ingest_gateway(GATEWAY_ACTOR, WORKFLOW, page([late], "gw-late"))
    assert capture.inspect("owner", WORKFLOW, "op-body")["attempts"][0]["state"] == "disputed"


def test_cu_changed_page_under_an_acknowledged_cursor_is_refused_without_writes(tmp_path):
    capture, _ = ledger(tmp_path / "owner")
    adapter = CaptureSourceAdapter(capture, lambda: NOW)
    adapter.ingest_gateway(GATEWAY_ACTOR, WORKFLOW, page(fixture_w_gateway()[:3], "gw-1"))
    with capture.store._db() as db:
        before = db.execute("SELECT count(*) FROM decision_capture_streams").fetchone()[0]
    with pytest.raises(CaptureError, match="capture_cursor_conflict"):
        adapter.ingest_gateway(GATEWAY_ACTOR, WORKFLOW, page(fixture_w_gateway()[:4], "gw-1"))
    with capture.store._db() as db:
        assert db.execute("SELECT count(*) FROM decision_capture_streams").fetchone()[0] == before


def test_cu_disabled_or_revoked_grant_writes_nothing_and_resumes_idempotently(tmp_path):
    capture, authority = ledger(tmp_path / "owner", mode="off")
    adapter = CaptureSourceAdapter(capture, lambda: NOW)
    with pytest.raises(CaptureError, match="decision_capture_disabled"):
        adapter.ingest_gateway(GATEWAY_ACTOR, WORKFLOW, page(fixture_w_gateway(), "gw-1"))
    with capture.store._db() as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master")}
    assert not any(name.startswith("decision_capture") for name in tables)
    enabled = replace(authority.base, mode="metadata_only")
    authority.base = enabled
    original = authority.resolve

    def revoke_after(*args):
        if authority.calls > 12:
            authority.base = replace(enabled, mode="off")
        return original(*args)

    authority.resolve = revoke_after
    with pytest.raises(CaptureError, match="decision_capture_disabled"):
        adapter.ingest_gateway(GATEWAY_ACTOR, WORKFLOW, page(fixture_w_gateway(), "gw-1"))
    authority.resolve = original
    authority.base = enabled
    assert capture.last_receipt(GATEWAY_ACTOR, WORKFLOW, "provider_response", GATEWAY) is None
    adapter.ingest_gateway(GATEWAY_ACTOR, WORKFLOW, page(fixture_w_gateway(), "gw-1"))
    view = capture.inspect("owner", WORKFLOW, "op-d1")
    assert [a["state"] for a in view["attempts"]] == ["incomplete", "finished"]


def test_wa3_two_tenants_with_reused_ids_and_foreign_viewers_stay_isolated(tmp_path):
    capture, _, _ = worked(tmp_path / "a")
    other, other_authority = ledger(tmp_path / "b", tenant="org-b")
    CaptureSourceAdapter(other, lambda: NOW).ingest_gateway(
        GATEWAY_ACTOR, WORKFLOW, page(fixture_w_gateway()[:3], "gw-1")
    )
    mine = capture.inspect("owner", WORKFLOW, "op-d1")
    theirs = other.inspect("owner", WORKFLOW, "op-d1")
    assert (mine["committed_decisions"], len(mine["attempts"])) == (1, 2)
    assert (theirs["committed_decisions"], len(theirs["attempts"])) == (0, 1)
    for actor in ("foreign-owner", GATEWAY_ACTOR):
        with pytest.raises(CaptureError):
            capture.inspect(actor, WORKFLOW, "op-d1")
    other_authority.base = replace(other_authority.base, tenant="org-a")
    with pytest.raises(CaptureError):
        other.inspect("owner", WORKFLOW, "op-d1")
    scoped = capture.inspect("response-inspector", WORKFLOW, "op-d1")
    assert all(
        a["committed"] is None and a["request_projection"] is None for a in scoped["attempts"]
    )
    assert "request" not in scoped["attempts"][0]


def test_dry_run_reports_the_same_bytes_without_a_store(tmp_path):
    before = set(tmp_path.iterdir())
    report = dry_run_capture(
        jsonl(fixture_w_gateway()),
        schema=GATEWAY_SCHEMA,
        boundaries=("application_request", "provider_response"),
        clock=lambda: NOW,
    )
    assert (report["writes"], report["records"], report["dropped_fields"]) == (0, 9, 11)
    assert report["by_kind"] == {"chunk": 5, "request": 2, "terminal": 2}
    a1, a2 = report["attempts"]
    assert (a1["received_indices"], a1["terminal_state"]) == ([0], "timeout")
    assert (a2["received_indices"], a2["missing_indices"], a2["conflicting"]) == (
        [0, 1, 2],
        [],
        False,
    )
    journal = dry_run_capture(
        jsonl(fixture_w_journal()), schema=JOURNAL_SCHEMA, boundaries=("application_commit",)
    )
    assert (journal["admitted"], journal["dropped_fields"]) == (1, 1)
    assert set(tmp_path.iterdir()) == before
    assert not any(value in json.dumps(report) for value in RESTRICTED)


def historical(record_id, decision, operation, decided_at, retention="content", **components):
    return {
        "schema": HISTORICAL_SCHEMA,
        "record": "decision",
        "record_id": record_id,
        "recorded_at": decided_at + 100,
        "operation_id": operation,
        "case_id": "case-" + decision.lower(),
        "family_id": "family-d1",
        "decision_id": decision,
        "decision_revision": 1,
        "decided_at": decided_at,
        "retention": retention,
        "components": components,
    }


D1_COMPONENTS = {
    "committed_action": {"value": "SUPPLIES"},
    "policy": {"value": "P7"},
    "template": {"value": "P7"},
    "attachment": {"value": "PO-v1"},
    "requested_model": {"value": "invoice-alias"},
    "served_model": {"value": "served-deployment-v3"},
    "parsed_output": {"value": "SUPPLIES"},
    "request_projection": {"content_b64": b64(REQUEST_PROJECTION)},
    "response_output": {"content_b64": b64(b"".join(STREAM))},
}


def d2_record():
    record = historical(
        "row-2",
        "D2",
        "op-d2",
        200,
        "metadata_only",
        committed_action={"value": "FREIGHT"},
        requested_model={"value": "invoice-alias"},
        served_model={"unavailable": "unresolved_alias"},
        request_projection={"unavailable": "retention"},
        response_output={"unavailable": "retention"},
        raw_prompt={"value": "dropped-component"},
    )
    return record | {"requester_email": RESTRICTED[3]}


def pinned(records, source=HISTORY):
    data = jsonl(records)
    return PinnedExport(source, "history-collector-1", hashlib.sha256(data).hexdigest(), data)


def test_wa3_historical_import_supports_only_observed_agreement(tmp_path):
    capture, _ = ledger(tmp_path / "owner")
    importer = HistoricalImport(capture, lambda: NOW)
    export = pinned([historical("row-1", "D1", "op-d1", 120, **D1_COMPONENTS), d2_record()])
    result = importer.ingest(HISTORY_ACTOR, WORKFLOW, export)
    assert (result["receipt"]["admitted"], result["receipt"]["dropped_fields"]) == (2, 2)
    assert importer.ingest(HISTORY_ACTOR, WORKFLOW, export)["status"] == "acknowledged"
    view = capture.inspect_imports("owner", WORKFLOW)
    d1, d2 = view["decisions"]
    assert d2["fields"]["served_model"] == {
        "fidelity": "unavailable",
        "ref": None,
        "reason": "unresolved_alias",
        "source_ref": None,
    }
    assert d2["fields"]["policy"]["reason"] == "not_in_export"
    assert d2["contents"]["request_projection"] == {
        "form": "unavailable",
        "fingerprint": "absent",
        "reason": "retention",
    }
    assert d2["eligibility"] == {
        "observed_agreement": "eligible",
        "exact_model_replay": "unavailable",
        "exact_model_replay_missing": [
            "policy",
            "request_projection",
            "response_output",
            "served_model",
            "template",
        ],
        "exact_target_training": "unavailable",
    }
    assert d1["eligibility"]["exact_model_replay"] == "requires_projection_verification"
    assert d1["contents"]["request_projection"]["fingerprint"] == "recorded"
    assert d1["eligibility"]["exact_target_training"] == "unavailable"
    assert (view["observed_decisions"], view["eligible_for_replay"], view["exact_replay"]) == (
        2,
        1,
        "unavailable",
    )
    live = LiveCase(tmp_path / "live")
    live.ingest(live_decision("D2", "FREIGHT"))
    live.ingest(live_outcome("D2", "FREIGHT"), outcome=True)
    snapshot = live.snapshot()
    assert (snapshot.decisions, snapshot.verified, snapshot.pending) == (1, 1, 0)
    manifest_bytes = json.dumps(
        {
            "schema": "opentour.capture.sampling-manifest.v1",
            "source_ref": "app-operation-registry",
            "window_start": 0,
            "window_end": 500,
            "eligible_operations": ["op-d1", "op-d2", "op-d3", "op-d4", "op-d5"],
        }
    ).encode()
    manifest = sampling_manifest(manifest_bytes, hashlib.sha256(manifest_bytes).hexdigest())
    coverage = capture_coverage({"op-d1", "op-d2"}, manifest, trace_source_ref=HISTORY)
    outcomes = historical_outcomes(view, snapshot.verified_roots, "invoice-app")
    score = score_boundary("application_commit", outcomes, coverage=coverage)
    assert (score["agreement"], score["pending"], score["decisions"]) == ([1, 1], 1, 2)
    assert (score["capture_coverage"], score["agreement_scope"]) == ([2, 5], "captured_subset")
    assert scan(capture, view, result) == []
    history_only = capture.inspect_imports("history-inspector", WORKFLOW)
    assert history_only["observed_decisions"] == 2
    response_only = capture.inspect_imports("response-inspector", WORKFLOW)
    assert response_only["decisions"] is None


def test_wa2_rerun_with_current_policy_cannot_replace_what_d1_used(tmp_path):
    capture, _ = ledger(tmp_path / "owner")
    importer = HistoricalImport(capture, lambda: NOW)
    importer.ingest(
        HISTORY_ACTOR, WORKFLOW, pinned([historical("row-1", "D1", "op-d1", 120, **D1_COMPONENTS)])
    )
    with capture.store._db() as db:
        original = db.execute("SELECT * FROM decision_capture_imports").fetchall()
    rerun = {**D1_COMPONENTS, "policy": {"value": "P8"}, "attachment": {"value": "PO-v2"}}
    importer.ingest(
        HISTORY_ACTOR, WORKFLOW, pinned([historical("rerun-1", "D1", "op-d1", 120, **rerun)])
    )
    view = capture.inspect_imports("owner", WORKFLOW)
    (d1,) = view["decisions"]
    assert (d1["state"], d1["fields"], d1["exports"]) == ("disputed", None, 2)
    assert (view["disputed_decisions"], view["observed_decisions"]) == (1, 0)
    assert "P8" not in json.dumps(view)
    with capture.store._db() as db:
        rows = [tuple(row) for row in db.execute("SELECT * FROM decision_capture_imports")]
    assert len(original) == 1 and tuple(original[0]) in rows and len(rows) == 2


def test_wa2_as_of_archive_reconstructs_only_what_was_in_force(tmp_path):
    capture, _ = ledger(tmp_path / "owner")
    entries = [
        ("policy", "P7", 0, 800),
        ("policy", "P8", 800, None),
        ("template", "T7", 0, None),
        ("attachment", "PO-v1", 0, 800),
        ("attachment", "PO-v2", 800, None),
        ("template", "T-overlap", 850, None),
    ]
    archive_bytes = jsonl(
        [
            {
                "schema": "opentour.capture.as-of-archive.v1",
                "component": component,
                "ref": ref,
                "effective_from": start,
                "effective_until": end,
            }
            for component, ref, start, end in entries
        ]
    )
    archive = AsOfArchive(hashlib.sha256(archive_bytes).hexdigest(), archive_bytes)
    old = historical(
        "row-3",
        "D3",
        "op-d3",
        500,
        "metadata_only",
        committed_action={"value": "SUPPLIES"},
        template={"unavailable": "retention"},
        attachment={"unavailable": "redaction"},
    )
    new = historical(
        "row-4", "D4", "op-d4", 900, "metadata_only", committed_action={"value": "REVIEW"}
    )
    HistoricalImport(capture, lambda: NOW).ingest(
        HISTORY_ACTOR, WORKFLOW, pinned([old, new]), archive=archive
    )
    d3, d4 = capture.inspect_imports("owner", WORKFLOW)["decisions"]
    archive_ref = "sha256:" + archive.sha256
    assert d3["fields"]["policy"] == {
        "fidelity": "reconstructed",
        "ref": "P7",
        "reason": None,
        "source_ref": archive_ref,
    }
    assert d3["fields"]["template"]["ref"] == "T7"
    assert d3["fields"]["attachment"]["reason"] == "redaction"
    assert d4["fields"]["policy"]["ref"] == "P8"
    assert d4["fields"]["template"]["reason"] == "ambiguous_as_of_source"
    assert d3["eligibility"]["exact_model_replay"] == "unavailable"
    assert {"policy", "template"} <= set(d3["eligibility"]["exact_model_replay_missing"])
    with pytest.raises(CaptureError, match="archive_invalid"):
        HistoricalImport(capture, lambda: NOW).ingest(
            HISTORY_ACTOR,
            WORKFLOW,
            pinned([old]),
            archive=AsOfArchive("0" * 64, archive_bytes),
        )


def test_cu_export_pin_and_retention_are_enforced_before_any_write(tmp_path):
    capture, _ = ledger(tmp_path / "owner")
    importer = HistoricalImport(capture, lambda: NOW)
    data = jsonl([historical("row-1", "D1", "op-d1", 120, **D1_COMPONENTS)])
    with pytest.raises(CaptureError, match="export_pin_mismatch"):
        importer.ingest(HISTORY_ACTOR, WORKFLOW, PinnedExport(HISTORY, "c", "0" * 64, data))
    with capture.store._db() as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master")}
    assert not any(name.startswith("decision_capture") for name in tables)
    leaked = historical("row-5", "D5", "op-d5", 120, "metadata_only", **D1_COMPONENTS)
    result = importer.ingest(HISTORY_ACTOR, WORKFLOW, pinned([leaked]))
    assert (result["receipt"]["admitted"], result["receipt"]["quarantined"]) == (0, 1)
    assert capture.receipts("owner", WORKFLOW)["historical_export"]["quarantined"] == {
        "invalid_field": 1
    }
    with pytest.raises(CaptureError):
        importer.ingest(GATEWAY_ACTOR, WORKFLOW, pinned([leaked]))
    with pytest.raises(CaptureError):
        importer.ingest(HISTORY_ACTOR, WORKFLOW, pinned([leaked], source=GATEWAY))


def test_historical_dry_run_reports_fidelity_without_writing():
    data = jsonl([historical("row-1", "D1", "op-d1", 120, **D1_COMPONENTS), d2_record()])
    report = dry_run_historical(data, hashlib.sha256(data).hexdigest(), clock=lambda: NOW)
    assert (report["writes"], report["admitted"], report["dropped_fields"]) == (0, 2, 2)
    assert report["fidelity"]["served_model"] == {"observed": 1, "unavailable": 1}
    assert report["fidelity"]["request_projection"] == {"content": 1, "unavailable": 1}
    assert report["window"] == [120, 200]
    assert not any(value in json.dumps(report) for value in RESTRICTED)
    with pytest.raises(CaptureError, match="export_pin_mismatch"):
        dry_run_historical(data, "0" * 64)


def manifest(operations, source="app-operation-registry", **changes):
    data = json.dumps(
        {
            "schema": "opentour.capture.sampling-manifest.v1",
            "source_ref": source,
            "window_start": 0,
            "window_end": 500,
            "eligible_operations": operations,
            **changes,
        }
    ).encode()
    return sampling_manifest(data, hashlib.sha256(data).hexdigest())


def test_mc_capture_coverage_needs_an_independent_source_wide_denominator():
    five = manifest(["op-1", "op-2", "op-3", "op-4", "op-5"])
    known = capture_coverage(["op-1", "op-2"], five, trace_source_ref=GATEWAY)
    assert (known["capture_coverage"], known["agreement_scope"]) == ([2, 5], "captured_subset")
    full = capture_coverage(["op-1", "op-2"], manifest(["op-1", "op-2"]), trace_source_ref=GATEWAY)
    assert full["agreement_scope"] == "declared_population"
    for coverage, reason in (
        (capture_coverage(["op-1"], None, trace_source_ref=GATEWAY), "no_independent_denominator"),
        (
            capture_coverage(["op-1"], manifest(["op-1"], GATEWAY), trace_source_ref=GATEWAY),
            "denominator_not_independent",
        ),
        (capture_coverage(["op-9"], five, trace_source_ref=GATEWAY), "traced_outside_denominator"),
    ):
        assert (coverage["capture_coverage"], coverage["reason"]) == (None, reason)
        assert coverage["agreement_scope"] == "captured_subset"
    with pytest.raises(CaptureError, match="manifest_invalid"):
        manifest(["op-1", "op-1"])
    with pytest.raises(CaptureError, match="manifest_invalid"):
        manifest(["op-1"], extra=True)
    with pytest.raises(CaptureError, match="manifest_pin_mismatch"):
        sampling_manifest(b"{}", "0" * 64)


def test_mc_postprocessor_scores_each_boundary_in_its_own_run(tmp_path):
    capture, _, _ = worked(tmp_path, journal_payload="payload-review")
    live = LiveCase(tmp_path / "live")
    live.ingest(live_decision("D1", "REVIEW"))
    live.ingest(live_outcome("D1", "REVIEW"), outcome=True)
    roots = live.snapshot().verified_roots
    views = [capture.inspect("owner", WORKFLOW, "op-d1")]
    coverage = capture_coverage(["op-d1"], None, trace_source_ref=GATEWAY)
    provider = score_boundary(
        "provider_response", provider_outcomes(views, roots, "invoice-app"), coverage=coverage
    )
    application = score_boundary(
        "application_commit", application_outcomes(views, roots, "invoice-app"), coverage=coverage
    )
    assert (provider["agreement"], provider["decisions"]) == ([0, 1], 1)
    assert (application["agreement"], application["decisions"]) == ([1, 1], 1)
    assert provider["capture_coverage"] is None and provider["agreement_scope"] == "captured_subset"
    mixed = provider_outcomes(views, roots, "invoice-app") + application_outcomes(
        views, roots, "invoice-app"
    )
    with pytest.raises(CaptureError):
        score_boundary("provider_response", mixed, coverage=coverage)
    with pytest.raises(CaptureError):
        score_boundary(
            "provider_response",
            provider_outcomes(views, roots, "invoice-app") * 2,
            coverage=coverage,
        )
    scoped = [capture.inspect("response-inspector", WORKFLOW, "op-d1")]
    hidden = score_boundary(
        "application_commit", application_outcomes(scoped, roots, "invoice-app"), coverage=coverage
    )
    assert (hidden["status"], hidden["unavailable"]) == ("unavailable", {"commit_not_permitted": 1})
    other = score_boundary(
        "application_commit",
        application_outcomes(views, roots, "other-namespace"),
        coverage=coverage,
    )
    assert (other["status"], other["pending"]) == ("unavailable", 1)


def test_mc_absent_app_commit_leaves_the_application_metric_unavailable(tmp_path):
    capture, _ = ledger(tmp_path / "owner")
    adapter = CaptureSourceAdapter(capture, lambda: NOW)
    adapter.ingest_gateway(GATEWAY_ACTOR, WORKFLOW, page(fixture_w_gateway(), "gw-1"))
    views = [capture.inspect("owner", WORKFLOW, "op-d1")]
    assert views[0]["committed_decisions"] == 0
    assert views[0]["attempts"][1]["exact_stream"] is True
    coverage = capture_coverage(["op-d1"], None, trace_source_ref=GATEWAY)
    application = score_boundary(
        "application_commit", application_outcomes(views, (), "invoice-app"), coverage=coverage
    )
    assert (application["status"], application["agreement"]) == ("unavailable", None)
    assert application["unavailable"] == {"no_application_commit": 1}
    provider = score_boundary(
        "provider_response", provider_outcomes(views, (), "invoice-app"), coverage=coverage
    )
    assert (provider["status"], provider["pending"]) == ("unavailable", 1)


def test_mc_separate_d1_d2_commits_with_one_correction_keep_live_reducer_totals(tmp_path):
    capture, _, adapter = worked(tmp_path)
    d2 = [
        request("b1", "request-b1", 130, operation="op-d2", case="case-d2", decision="D2"),
        terminal(
            "b1",
            "terminal-b1",
            131,
            "finished",
            0,
            operation="op-d2",
            case="case-d2",
            decision="D2",
        ),
    ]
    adapter.ingest_gateway(GATEWAY_ACTOR, WORKFLOW, page(d2, "gw-d2"))
    adapter.ingest_journal(
        JOURNAL_ACTOR,
        WORKFLOW,
        page(
            [
                commit(
                    "b1",
                    "commit-d2",
                    132,
                    "payload-d2",
                    "commit-d2",
                    operation="op-d2",
                    case="case-d2",
                    decision="D2",
                )
            ],
            "journal-d2",
            JOURNAL,
        ),
    )
    live = LiveCase(tmp_path / "live")
    live.ingest(live_decision("D1", "SUPPLIES"), live_decision("D2", "FREIGHT"))
    live.ingest(live_outcome("D1", "FREIGHT"), outcome=True)
    snapshot = live.snapshot()
    assert (snapshot.decisions, snapshot.verified, snapshot.pending) == (2, 1, 1)
    views = [capture.inspect("owner", WORKFLOW, op) for op in ("op-d1", "op-d2")]
    assert [view["committed_decisions"] for view in views] == [1, 1]
    score = score_boundary(
        "application_commit",
        application_outcomes(views, snapshot.verified_roots, "invoice-app"),
        coverage=capture_coverage(["op-d1", "op-d2"], None, trace_source_ref=GATEWAY),
    )
    assert (score["agreement"], score["pending"], score["decisions"]) == ([0, 1], 1, 2)


def test_adapters_import_no_network_provider_or_training_code():
    allowed_product = {
        "opentour_cell.pilot.capture_streams",
        "opentour_cell.pilot.intake_mcp",
        "opentour_cell.pilot.live_evidence",
    }
    tree = ast.parse((SRC / "capture_adapters.py").read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    product = {name for name in imported if name.startswith("opentour_cell")}
    assert product == allowed_product
    forbidden = {"socket", "ssl", "http", "urllib", "requests", "subprocess", "asyncio"}
    assert not {name.split(".")[0] for name in imported} & forbidden
    assert identity("a1")["attempt_id"] == "a1"


def test_wa3_expired_grant_and_withdrawn_custody_return_no_restricted_fields(tmp_path):
    from opentour_cell.pilot.custody import Closeout
    from test_pilot_custody import START

    capture, authority, adapter = worked(tmp_path)
    importer = HistoricalImport(capture, lambda: NOW)
    importer.ingest(HISTORY_ACTOR, WORKFLOW, pinned([d2_record()]))
    current = authority.base
    authority.base = replace(current, expires_at=NOW)
    reads = (
        lambda: capture.inspect("owner", WORKFLOW, "op-d1"),
        lambda: capture.inspect_imports("owner", WORKFLOW),
        lambda: capture.receipts("owner", WORKFLOW),
        lambda: adapter.ingest_gateway(GATEWAY_ACTOR, WORKFLOW, page(fixture_w_gateway(), "new")),
    )
    for read in reads:
        with pytest.raises(CaptureError):
            read()
    authority.base = current
    assert capture.inspect("owner", WORKFLOW, "op-d1")["committed_decisions"] == 1
    Closeout(capture.store.path.parent, capture.store.organization).withdraw(**START)
    for read in reads:
        with pytest.raises(CaptureError):
            read()
    with pytest.raises(CaptureError):
        importer.ingest(HISTORY_ACTOR, WORKFLOW, pinned([d2_record(), d2_record()]))
