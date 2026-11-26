"""Generated private exports traversing the installed intake and materialization entry points."""

import hmac
from dataclasses import asdict

import pytest
from opentour_cell.pilot import live_reader
from opentour_cell.pilot.intake_local import open_intake
from opentour_cell.pilot.intake_mcp import IntakeError
from opentour_cell.pilot.live_authority import SELECTION_SCHEMA
from opentour_cell.pilot.live_evidence import _canonical, _digest
from test_intake_mcp import _write_private
from test_live_authority import KEY, SignedCase, request
from test_live_evidence import DECISION, OUTCOME, decision, outcome


class Export:
    def __init__(self, path):
        self.case = SignedCase(path)
        self.directory = path
        self.selection = path / "live-worker.json"
        _write_private(
            self.selection,
            {
                "schema_version": SELECTION_SCHEMA,
                "tenant": "org-a",
                "workflow_ref": self.case.workflow,
                "actor": "worker",
            },
        )
        self.resources = {}

    def pin(self, filename, value):
        _write_private(self.directory / filename, value)
        return {"file": filename, "sha256": _digest(value)}

    def install(self, grant, batches, *, scope_override=None, immutable=False):
        pages = []
        for index, rows in enumerate(batches):
            ref = f"page-{grant.resource}-{index}"
            page = {"schema_version": live_reader.PAGE, "page_ref": ref, "rows": rows}
            complete = index == len(batches) - 1
            pages.append(
                {
                    "pin": self.pin(f"live-export-page-{ref}.json", page),
                    "page_ref": ref,
                    "checkpoint": grant.checkpoint + index + 1,
                    "read_metadata": {
                        "interval_start": 50,
                        "interval_end": 100,
                        "coverage_through": 100 if complete else None,
                        "complete": complete,
                        "next_cursor": None if complete else f"page-{grant.resource}-{index + 1}",
                    },
                }
            )
            pages[-1]["page_digest"] = _digest(
                [
                    grant.source_revision,
                    [
                        {"event_revision": 1, **r["event"]} if immutable else r["event"]
                        for r in rows
                    ],
                    [r["position"] for r in rows],
                    [r["epoch"] for r in rows],
                    pages[-1]["checkpoint"],
                    pages[-1]["read_metadata"],
                ]
            )
        scope = {
            k: self.case.body[k]
            for k in (
                "tenant",
                "workflow_ref",
                "engagement_epoch",
                "dataset_ref",
                "dataset_revision",
            )
        }
        catalog = {
            "schema_version": live_reader.CATALOG,
            "scope": scope | (scope_override or {}),
            "grant": asdict(grant),
            "event_revisions": "immutable_event_id" if immutable else "versioned",
            "pages": pages,
        }
        self.resources[grant.resource] = self.pin(
            f"live-export-catalog-{grant.resource}.json", catalog
        )
        self.sign()
        return pages

    def sign(self):
        mapping = {
            "schema_version": live_reader.MAPPING,
            "authority_revision": _digest(self.case.body),
            "resources": self.resources,
        }
        _write_private(
            self.directory / f"live-export-{self.case.workflow}.json",
            {
                "mapping": mapping,
                "seal_sha256": hmac.digest(KEY, _canonical(mapping).encode(), "sha256").hex(),
            },
        )

    def row(self, event, position, epoch=None):
        return {
            "position": position,
            "epoch": epoch or self.case.body["engagement_epoch"],
            "event": event,
        }

    def submit(self, grant):
        actor, intake = open_intake(self.selection)
        ref = self.case.connect(grant.resource)
        response = intake.submit_intake(
            actor,
            request(ref, source_revision=grant.source_revision, idempotency_key=grant.resource),
        )
        return response.run_ref

    def snapshot(self):
        return self.case.live.snapshot(self.case.owner, self.case.workflow)

    def receipts(self):
        return self.case.live.read_receipts(self.case.owner, self.case.workflow)


def test_actual_empty_read_and_restart(tmp_path):
    export = Export(tmp_path / "owner")
    export.install(DECISION, [[]])
    run = export.submit(DECISION)
    assert live_reader.materialize_live(export.selection, run)
    receipts = export.receipts()
    assert len(receipts) == 1 and receipts[0]["complete"]
    assert export.snapshot().decisions == 0
    assert not live_reader.materialize_live(export.selection, run)
    assert export.receipts() == receipts


def test_interrupted_outcomes_resume_and_reopen_through_actual_reader(tmp_path):
    export = Export(tmp_path / "owner")
    export.install(
        DECISION,
        [
            [
                export.row(decision(), 11),
                export.row(decision(), 11),
                export.row(decision("D2"), 11),
                export.row(decision("old"), 10),
                export.row(decision("late-old"), 11, "old-epoch"),
                export.row(decision("malformed", raw_reply="GENERATED_PRIVATE_SENTINEL"), 11),
            ]
        ],
    )
    run = export.submit(DECISION)
    assert live_reader.materialize_live(export.selection, run)
    assert (
        export.snapshot().decisions,
        export.snapshot().pending,
        export.snapshot().quarantined,
    ) == (2, 2, 3)
    pages = export.install(
        OUTCOME,
        [
            [export.row(outcome(), 21)],
            [
                export.row(
                    outcome(
                        event_id="reopen",
                        outcome_revision=2,
                        finality="reopened",
                        verified_action_ref=None,
                    ),
                    22,
                )
            ],
        ],
    )
    truth = export.submit(OUTCOME)
    second = export.directory / pages[1]["pin"]["file"]
    content = second.read_bytes()
    second.unlink()
    with pytest.raises(IntakeError, match="live_export_unavailable"):
        live_reader.materialize_live(export.selection, truth)
    assert export.snapshot().verified == 1 and not export.receipts()[-1]["complete"]
    (export.directory / pages[0]["pin"]["file"]).unlink()
    second.write_bytes(content)
    second.chmod(0o600)
    assert live_reader.materialize_live(export.selection, truth)
    assert export.snapshot().verified == 0 and export.snapshot().pending == 2
    assert len(export.receipts()) == 3
    assert "GENERATED_PRIVATE_SENTINEL" not in str(export.receipts())
    assert b"GENERATED_PRIVATE_SENTINEL" not in export.case.live.path.read_bytes()


@pytest.mark.parametrize("fault", ["foreign_scope", "digest", "revoked", "symlink"])
def test_export_holds_before_applying_untrusted_page(tmp_path, fault):
    export = Export(tmp_path / "owner")
    pages = export.install(
        DECISION,
        [[export.row(decision(), 11)]],
        scope_override={"tenant": "foreign"} if fault == "foreign_scope" else None,
    )
    run = export.submit(DECISION)
    page = export.directory / pages[0]["pin"]["file"]
    if fault == "digest":
        _write_private(page, {"schema_version": live_reader.PAGE, "page_ref": "other", "rows": []})
    if fault == "revoked":
        export.case.body["state"] = "revoked"
        export.case.write()
    if fault == "symlink":
        replacement = page.with_suffix(".copy")
        page.rename(replacement)
        page.symlink_to(replacement)
    with pytest.raises((IntakeError, ValueError)):
        live_reader.materialize_live(export.selection, run)
    with export.case.live._db() as db:
        assert db.execute("SELECT count(*) FROM live_events").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM live_pages").fetchone()[0] == 0


def test_late_committed_decision_joins_earlier_truth_on_immutable_feed(tmp_path):
    export = Export(tmp_path / "owner")
    event = outcome()
    del event["event_revision"]
    export.install(OUTCOME, [[export.row(event, 21)]], immutable=True)
    assert live_reader.materialize_live(export.selection, export.submit(OUTCOME))
    assert export.snapshot().decisions == 0
    export.install(DECISION, [[export.row(decision(), 11)]])
    assert live_reader.materialize_live(export.selection, export.submit(DECISION))
    snap = export.snapshot()
    assert (snap.decisions, snap.verified, snap.pending) == (1, 1, 0)
    assert snap.verified_roots[0].input_fidelity == "unavailable"


def test_mapping_revision_rechecked_before_page_effects(tmp_path, monkeypatch):
    export = Export(tmp_path / "owner")
    export.install(DECISION, [[export.row(decision(), 11)]])
    run = export.submit(DECISION)
    pinned = live_reader._pinned

    def rotated(directory, pin, limit):
        value = pinned(directory, pin, limit)
        if value["schema_version"] == live_reader.PAGE:
            export.case.body["actors"]["worker"] = []
            export.case.write()
        return value

    monkeypatch.setattr(live_reader, "_pinned", rotated)
    with pytest.raises(IntakeError):
        live_reader.materialize_live(export.selection, run)
    assert export.snapshot().decisions == 0 and export.receipts() == ()


def test_fresh_worker_cli_materializes_once_and_denies_without_effects(tmp_path):
    import subprocess
    import sys

    export = Export(tmp_path / "GENERATED_PRIVATE_PATH_SENTINEL")
    export.install(DECISION, [[export.row(decision(), 11)]])
    run = export.submit(DECISION)

    def invoke(run_ref):
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "opentour_cell.pilot.live_reader",
                str(export.selection),
                run_ref,
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )

    before = {p.name: p.read_bytes() for p in export.directory.iterdir() if p.is_file()}
    denied = invoke("private-unknown-run")
    assert (denied.returncode, denied.stdout, denied.stderr) == (1, "", "live_export_unavailable\n")
    assert before == {p.name: p.read_bytes() for p in export.directory.iterdir() if p.is_file()}
    success = invoke(run)
    assert (success.returncode, success.stdout, success.stderr) == (0, "materialized\n", "")
    assert export.snapshot().pending == 1
    receipts = export.receipts()
    replay = invoke(run)
    assert (replay.returncode, replay.stdout, replay.stderr) == (0, "already_materialized\n", "")
    assert export.receipts() == receipts
    export.case.body["state"] = "revoked"
    export.case.write()
    before = {p.name: p.read_bytes() for p in export.directory.iterdir() if p.is_file()}
    revoked = invoke(run)
    assert (revoked.returncode, revoked.stdout, revoked.stderr) == (
        1,
        "",
        "live_export_unavailable\n",
    )
    assert before == {p.name: p.read_bytes() for p in export.directory.iterdir() if p.is_file()}
