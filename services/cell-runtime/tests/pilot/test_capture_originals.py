"""Independent original-record verification of a generated capture run.

The qualification runs the real gateway and journal adapters on fixture W, collects every
emitted surface verbatim, and hands a separate evaluator the originals, a manifest and a
separately retained fixture. Each fault must turn the evaluator red and its restoration
green again. Generated evidence only; no provider, installation or customer is involved.
"""

import ast
import base64
import hashlib
import json
import logging
import shutil
import sqlite3
import subprocess
import sys
import traceback
from contextlib import closing
from pathlib import Path

import pytest
from capture_fixtures import (
    GATEWAY,
    JOURNAL,
    KEY,
    NOW,
    REQUEST_SENTINEL,
    RESPONSE_SENTINEL,
    RESTRICTED,
    WORKFLOW,
    commit,
    fixture_w_gateway,
    fixture_w_journal,
    jsonl,
    ledger,
    reopen,
)
from opentour_cell.pilot import capture_originals
from opentour_cell.pilot.capture_adapters import (
    GATEWAY_PROJECTION,
    JOURNAL_PROJECTION,
    CaptureSourceAdapter,
    SourcePage,
)
from opentour_cell.pilot.capture_originals import verify_originals
from opentour_cell.pilot.capture_streams import CaptureError

GATEWAY_CURSOR = "gateway-page-1"
JOURNAL_CURSOR = "journal-page-1"
JSON_COLUMNS = {"payload", "body", "evidence", "attempt", "identity"}
STORE_KEYS = {
    "table",
    "row",
    "tenant",
    "epoch",
    "workflow",
    "grant_revision",
    "policy_revision",
    "operation_ref",
    "attempt",
    "decision_ref",
    "decision_revision",
    "source_ref",
    "event_ref",
    "event_revision",
    "observed_at",
    "kind",
    "payload",
    "boundary",
    "cursor",
    "position",
    "reason",
    "body",
    "attempt_ref",
    "case_ref",
    "family_ref",
    "parent_attempt",
    "request",
    "requested_model_ref",
    "projection_revision",
    "template_revision",
    "opaque_projection_ref",
    "metadata_seal",
    "seal",
    "index",
    "opaque_chunk_ref",
    "state",
    "expected_chunks",
    "response",
    "provider_receipt_ref",
    "served_model_ref",
    "parsed_output_ref",
    "parser_revision",
    "chunk_refs",
    "commit_ref",
    "decision_payload_ref",
    "collector_ref",
    "next_cursor",
    "admitted",
    "quarantined",
    "dropped_fields",
    "window_start",
    "window_end",
    "page_ref",
}
LOG_KEYS = {
    "logger",
    "level",
    "message",
    "capture",
    "source",
    "cursor",
    "reason",
    "position",
    "boundary",
    "source_ref",
    "collector_ref",
    "projection_revision",
    "admitted",
    "quarantined",
    "dropped_fields",
}
EXCEPTION_KEYS = {"type", "args", "text", "traceback", "context", "cause"}
EXPECTED_ATTEMPT = {
    "a1": {
        "state": "incomplete",
        "terminal_state": "timeout",
        "received_indices": [0],
        "missing_indices": [],
        "exact_stream": False,
        "requested_model": "observed",
        "served_model": "unavailable",
        "parsed_output": "unavailable",
        "request_projection": "fingerprinted",
        "exact_replay": "unavailable",
    },
    "a2": {
        "state": "finished",
        "terminal_state": "finished",
        "received_indices": [0, 1, 2],
        "missing_indices": [],
        "exact_stream": True,
        "requested_model": "observed",
        "served_model": "unavailable",
        "parsed_output": "observed",
        "request_projection": "fingerprinted",
        "exact_replay": "unavailable",
    },
}


class Collect(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[dict] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(
            {
                "logger": record.name,
                "level": record.levelname,
                "message": record.getMessage(),
                "capture": getattr(record, "capture", None),
            }
        )


def exception_record(error: BaseException) -> dict:
    """The verbatim exception, including any context Python keeps even when suppressed."""
    return {
        "type": type(error).__name__,
        "args": [repr(arg) for arg in error.args],
        "text": str(error),
        "traceback": "".join(traceback.format_exception(error, chain=True)),
        "context": repr(error.__context__),
        "cause": repr(error.__cause__),
    }


def dump(path: Path) -> list[dict]:
    rows = []
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        tables = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name LIKE 'decision_capture_%' ORDER BY name"
        ).fetchall()
        for (table,) in tables:
            for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid"):
                item = {}
                for key in row.keys():
                    value = row[key]
                    item[key] = json.loads(value) if key in JSON_COLUMNS else value
                rows.append({"table": table, "row": item})
    return rows


def declared_attempt(entry: dict) -> dict:
    response = entry.get("response") or {}
    return {
        "state": entry["state"],
        "terminal_state": entry["terminal_state"],
        "received_indices": entry["received_indices"],
        "missing_indices": entry["missing_indices"],
        "exact_stream": entry["exact_stream"],
        "requested_model": "observed" if entry.get("request") else "unavailable",
        "served_model": "observed" if response.get("served_model_ref") else "unavailable",
        "parsed_output": "observed" if response.get("parsed_output_ref") else "unavailable",
        "request_projection": entry["request_projection"],
        "exact_replay": entry["exact_replay"],
    }


def sums(pages: list[dict]) -> dict:
    return {key: sum(page[key] for page in pages) for key in ("admitted", "quarantined")} | {
        "dropped_fields": sum(page["dropped_fields"] for page in pages)
    }


def write(directory: Path, name: str, data: bytes) -> str:
    (directory / name).write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def qualify(tmp_path: Path):
    """Run fixture W through the real adapters and export every surface verbatim."""
    capture, authority = ledger(tmp_path / "store")
    gateway = jsonl(fixture_w_gateway())
    journal = jsonl(fixture_w_journal())
    handler = Collect()
    logger = logging.getLogger("opentour_cell")
    previous = logger.level
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    errors: list[dict] = []
    try:
        adapter = CaptureSourceAdapter(capture, lambda: NOW)
        gateway_page = SourcePage(GATEWAY, "collector-1", GATEWAY_CURSOR, None, 100, 200, gateway)
        journal_page = SourcePage(JOURNAL, "collector-1", JOURNAL_CURSOR, None, 100, 200, journal)
        adapter.ingest_gateway("gateway-collector", WORKFLOW, gateway_page)
        acknowledge = capture.acknowledge

        def crash(*args, **kwargs):
            raise CaptureError("decision_capture_unavailable")

        capture.acknowledge = crash  # restart before the app commit is acknowledged
        with pytest.raises(CaptureError) as crashed:
            adapter.ingest_journal("journal-collector", WORKFLOW, journal_page)
        errors.append(exception_record(crashed.value))
        capture.acknowledge = acknowledge
        restarted = reopen(capture)
        CaptureSourceAdapter(restarted, lambda: NOW).ingest_journal(
            "journal-collector", WORKFLOW, journal_page
        )
        changed = SourcePage(
            GATEWAY, "collector-1", GATEWAY_CURSOR, None, 100, 200, gateway + b"\n" + gateway
        )
        refusals = [
            lambda: adapter.ingest_gateway("gateway-collector", WORKFLOW, changed),
            lambda: restarted.inspect("foreign-owner", WORKFLOW, "op-d1"),
            lambda: SourcePage(REQUEST_SENTINEL + "!", "c", "c", None, None, None, b""),
            lambda: adapter.ingest_gateway("journal-collector", WORKFLOW, gateway_page),
        ]
        for refusal in refusals:
            with pytest.raises(CaptureError) as refused:
                refusal()
            errors.append(exception_record(refused.value))
        view = restarted.inspect("owner", WORKFLOW, "op-d1")
        receipts = restarted.receipts("owner", WORKFLOW)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)
    rows = dump(restarted.store.path)
    store_dir = restarted.store.path.parent
    raw = b"".join(path.read_bytes() for path in sorted(store_dir.iterdir()) if path.is_file())
    page_refs = {
        row["row"]["source_ref"]: row["row"]["body"]["page_ref"]
        for row in rows
        if row["table"] == "decision_capture_receipts"
    }
    export = tmp_path / "export"
    export.mkdir()
    files = {
        "gateway_log": write(export, "gateway_log", gateway),
        "decision_journal": write(export, "decision_journal", journal),
        "worker_log": write(export, "worker_log", jsonl(handler.records)),
        "exceptions": write(export, "exceptions", jsonl(errors)),
        "dead_letter": write(
            export,
            "dead_letter",
            jsonl([row for row in rows if row["table"] == "decision_capture_quarantine"]),
        ),
        "metadata_store": write(export, "metadata_store", jsonl(rows)),
        "metadata_store_raw": write(export, "metadata_store_raw", raw),
        "inspection": write(export, "inspection", jsonl([view, receipts])),
    }
    scope = {
        "collector": "collector-1",
        "run": "qualification-w-1",
        "tenant": "org-a",
        "engagement": capture.store.engagement_id,
        "workflow": WORKFLOW,
        "grant_revision": "capture-grant-v1",
        "policy_revision": "policy-v7",
        "source_cursor": {"gateway_log": GATEWAY_CURSOR, "decision_journal": JOURNAL_CURSOR},
        "coverage_window": [100, 200],
        "redaction_revision": {
            "gateway_log": GATEWAY_PROJECTION,
            "decision_journal": JOURNAL_PROJECTION,
        },
        "operations": ["op-d1"],
    }
    attempts = view["attempts"]
    declared = {
        "attempts": len(attempts),
        "finished_attempts": sum(a["state"] == "finished" for a in attempts),
        "incomplete_attempts": sum(a["state"] == "incomplete" for a in attempts),
        "disputed_attempts": sum(a["state"] == "disputed" for a in attempts),
        "committed_decisions": view["committed_decisions"],
        "disputed_decisions": view["disputed_decisions"],
        "records": {
            "gateway_log": sums(
                receipts["application_request"]["pages"] + receipts["provider_response"]["pages"]
            ),
            "decision_journal": sums(receipts["application_commit"]["pages"]),
        },
        "per_attempt": {a["attempt_ref"]: declared_attempt(a) for a in attempts},
        "page_receipts": {
            "gateway_log": {
                "source_ref": GATEWAY,
                "cursor": GATEWAY_CURSOR,
                "page_ref": page_refs[GATEWAY],
            },
            "decision_journal": {
                "source_ref": JOURNAL,
                "cursor": JOURNAL_CURSOR,
                "page_ref": page_refs[JOURNAL],
            },
        },
    }
    commits = sorted(
        row["row"]["payload"]["commit_ref"]
        for row in rows
        if row["table"] == "decision_capture_streams" and row["row"]["kind"] == "commit"
    )
    manifest = {
        "schema": capture_originals.MANIFEST_SCHEMA,
        **scope,
        "attempts": sorted(a["attempt_ref"] for a in attempts),
        "commits": commits,
        "files": files,
        "declared": declared,
    }
    (export / "manifest.json").write_text(json.dumps(manifest, sort_keys=True))
    # The evaluator's sealed fixture is built from the generated scenario, not from the run.
    fixture = {
        "schema": capture_originals.FIXTURE_SCHEMA,
        "fixture_kind": "generated",
        **scope,
        "originals": {
            "gateway_log": hashlib.sha256(jsonl(fixture_w_gateway())).hexdigest(),
            "decision_journal": hashlib.sha256(jsonl(fixture_w_journal())).hexdigest(),
        },
        "surfaces": {
            "worker_log": {"status": "required", "format": "jsonl"},
            "exceptions": {"status": "required", "format": "jsonl"},
            "dead_letter": {"status": "required", "format": "jsonl"},
            "metadata_store": {"status": "required", "format": "jsonl"},
            "metadata_store_raw": {"status": "required", "format": "binary"},
            "inspection": {"status": "required", "format": "jsonl"},
            "slack_body": {"status": "unavailable", "reason": "surface_not_built_in_this_slice"},
            "slack_fallback": {
                "status": "unavailable",
                "reason": "surface_not_built_in_this_slice",
            },
            "zap_history": {"status": "unavailable", "reason": "surface_not_built_in_this_slice"},
        },
        "restricted": list(RESTRICTED),
        "approved_keys": {
            "metadata_store": sorted(STORE_KEYS),
            "dead_letter": sorted(STORE_KEYS),
            "worker_log": sorted(LOG_KEYS),
            "exceptions": sorted(EXCEPTION_KEYS),
        },
        "receipt_key": KEY.hex(),
        "expected": {
            "attempts": 2,
            "finished_attempts": 1,
            "incomplete_attempts": 1,
            "disputed_attempts": 0,
            "committed_decisions": 1,
            "disputed_decisions": 0,
            "unjoinable_commits": 0,
            "records": {
                "gateway_log": {"admitted": 9, "quarantined": 0, "dropped_fields": 11},
                "decision_journal": {"admitted": 1, "quarantined": 0, "dropped_fields": 1},
            },
            "per_attempt": EXPECTED_ATTEMPT,
        },
    }
    return fixture, export, {"view": view, "authority": authority, "errors": errors}


def reseal(export: Path, name: str, data: bytes, fixture: dict | None = None) -> None:
    """A cheating producer rewrites a file and its manifest digest (and maybe the pin)."""
    digest = write(export, name, data)
    manifest = json.loads((export / "manifest.json").read_text())
    manifest["files"][name] = digest
    (export / "manifest.json").write_text(json.dumps(manifest, sort_keys=True))
    if fixture is not None and name in fixture["originals"]:
        fixture["originals"][name] = digest


def edit_manifest(export: Path, change) -> None:
    manifest = json.loads((export / "manifest.json").read_text())
    change(manifest)
    (export / "manifest.json").write_text(json.dumps(manifest, sort_keys=True))


@pytest.fixture
def run(tmp_path):
    fixture, export, context = qualify(tmp_path / "run")
    pristine = tmp_path / "pristine"
    shutil.copytree(export, pristine)
    return fixture, export, pristine, context


def restored(export: Path, pristine: Path) -> None:
    shutil.rmtree(export)
    shutil.copytree(pristine, export)


def test_or_generated_qualification_recomputes_every_counter_from_originals(run):
    fixture, export, _, context = run
    result = verify_originals(fixture, export)
    assert result["failures"] == []
    assert result["status"] == "pass"
    assert result["recomputed"]["per_attempt"] == EXPECTED_ATTEMPT
    assert (result["recomputed"]["committed_decisions"], result["recomputed"]["attempts"]) == (1, 2)
    assert result["sentinel_scan"] == "partial"
    assert {
        name for name, item in result["surfaces"].items() if item["status"] == "unavailable"
    } == {
        "slack_body",
        "slack_fallback",
        "zap_history",
    }
    assert all(
        item["hits"] == 0 for item in result["surfaces"].values() if item["status"] == "scanned"
    )
    assert result["surfaces"]["exceptions"]["records"] == 5
    assert result["surfaces"]["dead_letter"]["records"] == 0
    assert (result["installed_witness"], result["fixture_kind"]) == ("unavailable", "generated")
    assert (result["integrity"], result["issuer_authenticated"]) == ("locally_keyed", False)
    claimed = dict(fixture, fixture_kind="installed")
    assert verify_originals(claimed, export)["installed_witness"] == "unavailable"


SURFACES = ("worker_log", "exceptions", "dead_letter", "metadata_store", "inspection")


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize(
    "fault",
    [
        {"unexpected": {"nested": [f"before {REQUEST_SENTINEL} after"]}},
        {"unexpected": [{RESPONSE_SENTINEL: True}]},
        {"unexpected": "x" + base64.b64encode(b"ab" + RESPONSE_SENTINEL.encode()).decode()},
        {"unexpected": REQUEST_SENTINEL.encode().hex().upper()},
        {"unexpected": hashlib.sha256(RESPONSE_SENTINEL.encode()).hexdigest()},
        {"unexpected": "".join(f"\\u{ord(c):04x}" for c in REQUEST_SENTINEL)},
    ],
)
def test_or_restricted_value_under_any_new_nested_field_turns_the_scan_red(run, surface, fault):
    fixture, export, pristine, _ = run
    lines = (export / surface).read_bytes().splitlines()
    record = json.loads(lines[0]) if lines else {}
    record["extra"] = fault
    reseal(export, surface, b"\n".join([json.dumps(record).encode(), *lines[1:]]) + b"\n")
    result = verify_originals(fixture, export)
    assert result["status"] == "fail"
    assert "restricted_value:" + surface in result["failures"]
    restored(export, pristine)
    assert verify_originals(fixture, export)["status"] == "pass"


@pytest.mark.parametrize("surface", ["worker_log", "metadata_store", "dead_letter", "exceptions"])
def test_or_unapproved_reference_only_field_turns_red_even_without_a_sentinel(run, surface):
    fixture, export, pristine, _ = run
    lines = (export / surface).read_bytes().splitlines()
    record = json.loads(lines[0]) if lines else {"table": "t", "row": {}}
    record["row" if "row" in record else "capture"] = {"debug_text": "generated harmless"}
    reseal(export, surface, b"\n".join([json.dumps(record).encode(), *lines[1:]]) + b"\n")
    assert "unapproved_field:" + surface in verify_originals(fixture, export)["failures"]
    restored(export, pristine)
    assert verify_originals(fixture, export)["status"] == "pass"


def test_or_raw_store_bytes_and_text_encodings_are_scanned(run):
    fixture, export, pristine, _ = run
    raw = (export / "metadata_store_raw").read_bytes()
    for leak in (RESPONSE_SENTINEL.encode("utf-16-le"), RESPONSE_SENTINEL.lower().encode()):
        reseal(export, "metadata_store_raw", raw + b"\x00" + leak)
        assert (
            "restricted_value:metadata_store_raw" in verify_originals(fixture, export)["failures"]
        )
        restored(export, pristine)
    assert verify_originals(fixture, export)["status"] == "pass"


def test_or_receipts_from_another_run_turn_red(run, tmp_path):
    fixture, export, pristine, _ = run
    other_fixture, other_export, _ = qualify(tmp_path / "other")
    other = json.loads((other_export / "manifest.json").read_text())
    edit_manifest(
        export,
        lambda m: m["declared"]["page_receipts"].update(other["declared"]["page_receipts"]),
    )
    result = verify_originals(fixture, export)
    assert "receipt_mismatch:gateway_log" in result["failures"]
    restored(export, pristine)
    reseal(export, "metadata_store", (other_export / "metadata_store").read_bytes())
    assert "foreign_row:metadata_store" in verify_originals(fixture, export)["failures"]
    restored(export, pristine)
    edit_manifest(export, lambda m: m.update(run="another-run"))
    assert "manifest_mismatch:run" in verify_originals(fixture, export)["failures"]
    restored(export, pristine)
    assert verify_originals(fixture, export)["status"] == "pass"
    assert verify_originals(other_fixture, other_export)["status"] == "pass"


def test_or_removed_or_replaced_original_turns_red(run):
    fixture, export, pristine, _ = run
    (export / "gateway_log").unlink()
    result = verify_originals(fixture, export)
    assert "missing_original:gateway_log" in result["failures"]
    assert result["recomputed"] is None
    restored(export, pristine)
    (export / "decision_journal").unlink()
    (export / "decision_journal").symlink_to(export / "gateway_log")
    assert "missing_original:decision_journal" in verify_originals(fixture, export)["failures"]
    restored(export, pristine)
    (export / "worker_log").unlink()
    assert "missing_surface:worker_log" in verify_originals(fixture, export)["failures"]
    restored(export, pristine)
    assert verify_originals(fixture, export)["status"] == "pass"


def test_or_duplicated_commit_with_a_conflicting_winner_turns_red(run):
    fixture, export, pristine, _ = run
    conflicting = jsonl(
        fixture_w_journal() + [commit("a2", "commit-d1-b", 121, "payload-review", "commit-d1-b")]
    )
    reseal(export, "decision_journal", conflicting)
    assert "original_differs:decision_journal" in verify_originals(fixture, export)["failures"]
    repinned = json.loads(json.dumps(fixture))
    reseal(export, "decision_journal", conflicting, repinned)
    result = verify_originals(repinned, export)
    assert result["recomputed"]["committed_decisions"] == 0
    assert result["recomputed"]["disputed_decisions"] == 1
    assert "expected_mismatch:committed_decisions" in result["failures"]
    assert "declared_mismatch:committed_decisions" in result["failures"]
    restored(export, pristine)
    assert verify_originals(fixture, export)["status"] == "pass"


def test_or_selectively_omitted_or_foreign_original_records_turn_red(run):
    fixture, export, pristine, _ = run
    kept = [r for r in fixture_w_gateway() if r["event_id"] != "chunk-a2-1"]
    repinned = json.loads(json.dumps(fixture))
    reseal(export, "gateway_log", jsonl(kept), repinned)
    result = verify_originals(repinned, export)
    assert result["recomputed"]["per_attempt"]["a2"]["missing_indices"] == [1]
    assert "expected_mismatch:per_attempt.a2.state" in result["failures"]
    assert "declared_mismatch:per_attempt.a2.exact_stream" in result["failures"]
    restored(export, pristine)
    foreign = fixture_w_gateway() + [
        {**fixture_w_gateway()[1], "operation_id": "op-foreign", "event_id": "x"}
    ]
    repinned = json.loads(json.dumps(fixture))
    reseal(export, "gateway_log", jsonl(foreign), repinned)
    assert "foreign_record:gateway_log" in verify_originals(repinned, export)["failures"]
    restored(export, pristine)
    assert verify_originals(fixture, export)["status"] == "pass"


def test_or_declared_counters_that_originals_do_not_reproduce_turn_red(run):
    fixture, export, pristine, _ = run
    for change, failure in (
        (lambda m: m["declared"].update(committed_decisions=2), "committed_decisions"),
        (lambda m: m["declared"]["per_attempt"].pop("a1"), "per_attempt.a1"),
        (
            lambda m: m["declared"]["records"]["gateway_log"].update(
                quarantined=0, dropped_fields=0
            ),
            "records.gateway_log.dropped_fields",
        ),
        (lambda m: m.update(attempts=["a2"]), "attempts_list"),
        (lambda m: m.update(commits=[]), "commits_list"),
    ):
        edit_manifest(export, change)
        assert "declared_mismatch:" + failure in verify_originals(fixture, export)["failures"]
        restored(export, pristine)
    assert verify_originals(fixture, export)["status"] == "pass"


def test_or_an_unbuilt_surface_cannot_be_claimed_as_scanned(run):
    fixture, export, pristine, _ = run
    claimed = json.loads(json.dumps(fixture))
    claimed["surfaces"]["slack_body"] = {"status": "required", "format": "json"}
    result = verify_originals(claimed, export)
    assert "missing_surface:slack_body" in result["failures"]
    (export / "slack_body").write_text("{}")
    edit_manifest(export, lambda m: m["files"].update(slack_body="0" * 64))
    assert "unexpected_file:slack_body" in verify_originals(fixture, export)["failures"]
    restored(export, pristine)
    assert verify_originals(fixture, export)["status"] == "pass"
    assert verify_originals({**fixture, "restricted": []}, export)["failures"] == [
        "fixture_invalid"
    ]


def test_or_evaluator_is_independent_of_the_product_and_runs_isolated(run, tmp_path):
    fixture, export, _, _ = run
    source = Path(capture_originals.__file__)
    tree = ast.parse(source.read_text())
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            modules.add((node.module or "").split(".")[0])
    assert modules <= set(sys.stdlib_module_names) | {"__future__"}
    evaluator = tmp_path / "evaluator"
    evaluator.mkdir()
    shutil.copy(source, evaluator / "check_capture_originals.py")
    (tmp_path / "fixture.json").write_text(json.dumps(fixture))
    command = [sys.executable, "-I", str(evaluator / "check_capture_originals.py")]
    passed = subprocess.run(
        [*command, str(tmp_path / "fixture.json"), str(export)],
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert passed.returncode == 0, passed.stdout[-400:]
    assert json.loads(passed.stdout)["status"] == "pass"
    reseal(export, "gateway_log", jsonl(fixture_w_gateway()[:-1]))
    failed = subprocess.run(
        [*command, str(tmp_path / "fixture.json"), str(export)],
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert failed.returncode == 1
    assert not any(value.encode() in failed.stdout + failed.stderr for value in RESTRICTED)
