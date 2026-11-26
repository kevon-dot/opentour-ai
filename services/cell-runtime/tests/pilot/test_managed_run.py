import concurrent.futures
import json
import os
import select
import sqlite3
import stat
import tempfile
import time
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import patch

from opentour_cell.pilot.managed_run import ManagedError, ManagedSelection, plan_revision
from opentour_cell.pilot.training import LocalTrainingStore
from opentour_cell.pilot.training_data import digest


def check_ready(directory, organization, manifest):
    assert manifest["organization"] == organization
    assert directory.is_dir()
    if (directory / "source-revoked").exists():
        raise ValueError("private-customer-name")
    return {"state": "ready"}


def evaluate(directory, organization, manifest, run_id):
    return {
        "result_revision": digest([organization, run_id, manifest["intake_revision"]]),
        "case_count": 14,
        "outcome": "simple_repair",
    }


class TestManagedRuns(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.directory = Path(folder.name)
        self.store = LocalTrainingStore(self.directory / "training.sqlite3", "generated-org")
        self.plan = {
            "schema_version": "opentour.pilot.managed-repair.v1",
            "organization": self.store.organization,
            "engagement": self.store.engagement_id,
            "intake_revision": "1" * 64,
            "private_input_for_test": "private-customer-name",
        }
        self.path = self.directory / "selected.json"
        self.auth = self.directory / "scope.json"
        self.save()
        self.selection = ManagedSelection(
            self.directory, self.store.organization, self.path, self.auth, prerequisites=check_ready
        )

    def save(self):
        self.path.write_text(json.dumps(self.plan))
        self.path.chmod(0o600)
        self.revision = plan_revision(self.plan)

    def approve(self, wait=None, ttl=6 * 3600):
        self.selection.authorize(
            self.revision, "att-0123456789abcdef", int(time.time()) + ttl, wait, check_ready
        )

    def test_durable_reconnect_and_aggregate_only_first_result(self):
        self.approve(wait=125)
        self.assertEqual(self.selection.once(evaluate), {"state": "idle"})
        self.assertFalse(self.selection.lease_path.exists())
        accepted = self.selection.start(self.revision, "request-A")
        database = self.selection.path.stat()
        self.assertEqual(accepted["state"], "queued")
        self.assertEqual(accepted, self.selection.start(self.revision, "request-A"))
        self.assertNotIn("private-customer-name", json.dumps(accepted))
        active = ManagedSelection(
            self.directory, self.store.organization, self.path, self.auth, prerequisites=check_ready
        )
        emitted = active.once(evaluate)
        self.assertEqual(emitted["state"], "ready")
        self.assertEqual(emitted["run_id"], accepted["run_id"])
        self.assertEqual(emitted["result"]["case_count"], 14)
        self.assertEqual(emitted["timing"]["customer_wait_seconds"], 125)
        self.assertTrue(emitted["timing"]["target_met"])
        self.assertIsInstance(emitted["timing"]["first_result_unix_ms"], int)
        self.assertTrue(os.path.samestat(database, self.selection.path.stat()))
        lease = self.selection.lease_path
        self.assertEqual(lease.read_bytes(), b"")
        self.assertEqual(stat.S_IMODE(lease.stat().st_mode), 0o600)
        self.assertEqual(lease.stat().st_uid, os.getuid())
        files = {p.name: p.read_bytes() for p in self.directory.iterdir()}
        self.assertEqual(active.status(accepted["run_id"]), emitted)
        self.assertEqual(files, {p.name: p.read_bytes() for p in self.directory.iterdir()})
        self.assertEqual(active.once(evaluate), {"state": "idle"})
        self.assertNotIn("private-customer-name", json.dumps(emitted))

    def test_concurrent_admissions_and_living_worker_never_runs_twice(self):
        self.approve()
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            results = list(
                pool.map(
                    lambda i: self.selection.start(self.revision, "same" if i % 2 else "other"),
                    range(10),
                )
            )
            run = results[0]["run_id"]
            self.assertEqual({r["run_id"] for r in results}, {run})
            entered, finish = Event(), Event()

            def slow(*args):
                entered.set()
                self.assertTrue(finish.wait(5))
                return evaluate(*args)

            task = pool.submit(self.selection.once, slow)
            self.assertTrue(entered.wait(5))
            try:
                self.assertEqual(self.selection.status(run)["state"], "running")
                self.assertEqual(self.selection.once(evaluate), {"state": "busy"})
            finally:
                finish.set()
            self.assertEqual(task.result(timeout=5)["state"], "ready")
        with sqlite3.connect(self.selection.path) as db:
            self.assertEqual(
                db.execute("SELECT count(*),max(attempts) FROM runs").fetchone(), (1, 1)
            )

    def test_actual_process_death_releases_lease_and_preserves_original_run(self):
        self.approve()
        run = self.selection.start(self.revision, "dead-process")["run_id"]
        marker = self.directory / "durable-work-id"
        child = os.fork()
        if child == 0:

            def interrupt(directory, organization, manifest, run_id):
                marker.write_text(run_id)
                os._exit(71)

            try:
                self.selection.once(interrupt)
            finally:
                os._exit(72)
        _, outcome = os.waitpid(child, 0)
        self.assertEqual(os.waitstatus_to_exitcode(outcome), 71)
        self.assertEqual(self.selection.status(run)["state"], "running")

        def resume(directory, organization, manifest, run_id):
            self.assertEqual(marker.read_text(), run_id)
            return evaluate(directory, organization, manifest, run_id)

        self.assertEqual(self.selection.once(resume)["run_id"], run)
        with sqlite3.connect(self.selection.path) as db:
            self.assertEqual(db.execute("SELECT attempts FROM runs").fetchone()[0], 2)

    def test_processes_serialize_one_queue_without_blocking_another(self):
        self.approve()
        run = self.selection.start(self.revision, "one-worker")["run_id"]
        other = self.directory / "other-queue"
        other.mkdir(mode=0o700)
        other_store = LocalTrainingStore(other / "training.sqlite3", self.store.organization)
        other_plan = {**self.plan, "engagement": other_store.engagement_id}
        other_path = other / "selected.json"
        other_path.touch(mode=0o600)
        other_path.write_text(json.dumps(other_plan))
        other_selection = ManagedSelection(
            other,
            self.store.organization,
            other_path,
            other / "scope.json",
            prerequisites=check_ready,
        )
        other_revision = plan_revision(other_plan)
        other_selection.authorize(
            other_revision, "att-0123456789abcdef", int(time.time()) + 60, None, check_ready
        )
        other_run = other_selection.start(other_revision, "other-worker")["run_id"]
        ready_read, ready_write = os.pipe()
        finish_read, finish_write = os.pipe()
        child = os.fork()
        if child == 0:
            os.close(ready_read)
            os.close(finish_write)

            def work(*args):
                os.write(ready_write, b"1")
                if os.read(finish_read, 1) != b"1":
                    os._exit(73)
                return evaluate(*args)

            try:
                result = self.selection.once(work)
                os._exit(0 if result["state"] == "ready" else 72)
            finally:
                os._exit(74)
        os.close(ready_write)
        os.close(finish_read)
        try:
            self.assertTrue(select.select([ready_read], [], [], 5)[0])
            self.assertEqual(os.read(ready_read, 1), b"1")
            self.assertEqual(self.selection.status(run)["state"], "running")
            self.assertEqual(self.selection.once(evaluate), {"state": "busy"})
            self.assertEqual(other_selection.once(evaluate)["run_id"], other_run)
            os.write(finish_write, b"1")
        finally:
            os.close(ready_read)
            os.close(finish_write)
            _, outcome = os.waitpid(child, 0)
        self.assertEqual(os.waitstatus_to_exitcode(outcome), 0)
        self.assertEqual(self.selection.status(run)["state"], "ready")
        with sqlite3.connect(self.selection.path) as db:
            self.assertEqual(db.execute("SELECT attempts FROM runs").fetchone()[0], 1)

    def test_worker_refuses_unsafe_existing_lease_without_admitting(self):
        self.approve()
        run = self.selection.start(self.revision, "bad-worker-lease")["run_id"]
        lease = self.selection.lease_path
        target = self.directory / "generated-file"
        target.touch(mode=0o600)
        for label, install in (
            ("symlink", lambda: lease.symlink_to(target)),
            ("hardlink", lambda: os.link(target, lease)),
            ("group readable", lambda: lease.touch(mode=0o640)),
            ("directory", lambda: lease.mkdir(mode=0o700)),
            ("nonempty", lambda: lease.write_bytes(b"generated sentinel")),
        ):
            with self.subTest(lease=label):
                install()
                if label == "group readable":
                    lease.chmod(0o640)
                if label == "nonempty":
                    lease.chmod(0o600)
                with self.assertRaisesRegex(ManagedError, "private_state_unavailable"):
                    self.selection.once(evaluate)
                if label == "directory":
                    lease.rmdir()
                else:
                    lease.unlink()
        self.assertEqual(self.selection.status(run)["state"], "queued")
        with sqlite3.connect(self.selection.path) as db:
            self.assertEqual(db.execute("SELECT attempts FROM runs").fetchone()[0], 0)

    def test_worker_refuses_to_publish_if_the_lease_is_replaced(self):
        self.approve()
        run = self.selection.start(self.revision, "replaced-lease")["run_id"]
        lease = self.selection.lease_path

        def work(*args):
            lease.rename(lease.with_name("previous-worker.lock"))
            lease.touch(mode=0o600)
            return evaluate(*args)

        with self.assertRaisesRegex(ManagedError, "private_state_unavailable"):
            self.selection.once(work)
        result = self.selection.status(run)
        self.assertEqual(result["state"], "running")
        self.assertIsNone(result["result"])

    def test_stale_changed_plan_altered_grant_and_foreign_scope_are_denied(self):
        with self.assertRaisesRegex(ManagedError, "authorization_required"):
            self.selection.start(self.revision, "key")
        self.assertFalse(self.selection.path.exists())
        self.approve()
        first = self.selection.start(self.revision, "key")["run_id"]
        self.plan["intake_revision"] = "2" * 64
        prior = self.revision
        self.save()
        with self.assertRaisesRegex(ManagedError, "plan_revision_mismatch"):
            self.selection.start(prior, "key")
        with self.assertRaisesRegex(ManagedError, "authorization_scope_mismatch"):
            self.selection.start(self.revision, "key")
        self.auth.unlink()
        self.approve()
        with self.assertRaisesRegex(ManagedError, "idempotency_conflict"):
            self.selection.start(self.revision, "key")
        with self.assertRaisesRegex(ManagedError, "run_unavailable"):
            self.selection.status(first)
        self.assertNotEqual(self.selection.start(self.revision, "new-key")["run_id"], first)
        self.plan["engagement"] = "unrelated"
        self.save()
        with self.assertRaisesRegex(ManagedError, "engagement_unavailable"):
            self.selection.start(self.revision, "distinct")
        self.plan["organization"] = "foreign-org"
        self.save()
        with self.assertRaisesRegex(ManagedError, "plan_scope_mismatch"):
            self.selection.start(self.revision, "distinct")

    def test_expired_and_revoked_grants_do_not_write_or_disclose(self):
        self.approve(ttl=120)
        accepted = self.selection.start(self.revision, "held")["run_id"]
        files = {p.name: p.read_bytes() for p in self.directory.iterdir()}
        with patch(
            "opentour_cell.pilot.managed_run._now", return_value=int(time.time() * 1000) + 200000
        ):
            with self.assertRaisesRegex(ManagedError, "authorization_expired"):
                self.selection.status(accepted)
            with self.assertRaisesRegex(ManagedError, "authorization_expired"):
                self.selection.once(evaluate)
        self.assertEqual(files, {p.name: p.read_bytes() for p in self.directory.iterdir()})
        (self.directory / "source-revoked").touch()
        with self.assertRaisesRegex(ManagedError, "workflow_prerequisites_unavailable"):
            self.selection.status(accepted)
        (self.directory / "source-revoked").unlink()
        self.store.withdraw()
        with self.assertRaisesRegex(ManagedError, "engagement_withdrawn"):
            self.selection.start(self.revision, "repeat")
        with self.assertRaisesRegex(ManagedError, "engagement_withdrawn"):
            self.selection.status(accepted)

    def test_withdrawal_during_work_fences_publication_and_private_error(self):
        self.approve()
        run = self.selection.start(self.revision, "in-flight")["run_id"]

        def withdraw(*args):
            result = evaluate(*args)
            self.store.withdraw()
            return result

        with self.assertRaisesRegex(ManagedError, "engagement_withdrawn"):
            self.selection.once(withdraw)
        with sqlite3.connect(self.selection.path) as db:
            self.assertEqual(
                db.execute(
                    "SELECT state,result_revision,result_ms FROM runs WHERE id=?", (run,)
                ).fetchone(),
                ("running", None, None),
            )

    def test_terminal_hold_failure_and_invalid_output_never_release_sensitive_output(self):
        for expected_state, outcome in (
            ("held", {"result_revision": "a" * 64, "case_count": 4, "outcome": "hold"}),
            ("failed", {"private": "private-customer-name"}),
            ("failed", RuntimeError("private-customer-name")),
            ("held", ValueError("private-customer-name")),
        ):
            with self.subTest(expected_state=expected_state, outcome=type(outcome).__name__):
                self.plan["intake_revision"] = digest([type(outcome).__name__, str(outcome)])
                self.save()
                self.auth.unlink(missing_ok=True)
                self.approve()
                run = self.selection.start(self.revision, self.revision)["run_id"]

                def work(*_, value=outcome):
                    if isinstance(value, Exception):
                        raise value
                    return value

                terminal = self.selection.once(work)
                self.assertEqual(terminal["state"], expected_state)
                self.assertEqual(terminal, self.selection.status(run))
                self.assertIsNone(terminal["timing"]["target_met"])
                self.assertNotIn("private-customer-name", json.dumps(terminal))

    def test_target_miss_is_measured_and_distinguished_from_customer_wait(self):
        self.approve(wait=15000)
        run = self.selection.start(self.revision, "timing")["run_id"]
        with patch(
            "opentour_cell.pilot.managed_run._now", return_value=int(time.time() * 1000) + 7_300_000
        ):
            result = self.selection.once(evaluate)
            self.assertEqual(result["run_id"], run)
            self.assertFalse(result["timing"]["target_met"])
            self.assertGreater(result["timing"]["ready_to_result_ms"], 7_200_000)
            self.assertEqual(result["timing"]["customer_wait_seconds"], 15000)

    def test_new_grant_requires_explicit_restart_and_small_private_snapshot_is_checked(self):
        self.approve(ttl=600)
        run = self.selection.start(self.revision, "original")["run_id"]
        self.auth.unlink()
        self.approve(ttl=1200)
        self.assertEqual(self.selection.once(evaluate), {"state": "idle"})
        with self.assertRaisesRegex(ManagedError, "run_unavailable"):
            self.selection.status(run)
        self.assertEqual(self.selection.start(self.revision, "original")["run_id"], run)
        self.assertEqual(self.selection.once(evaluate)["state"], "ready")
        with sqlite3.connect(self.selection.path) as db:
            db.execute("UPDATE runs SET reason=? WHERE id=?", ("private-case-body", run))
        with self.assertRaisesRegex(ManagedError, "private_state_unavailable"):
            self.selection.status(run)
