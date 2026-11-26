"""Exercise real local enrollment, private permissions, and the initialized stdio contract."""

import concurrent.futures
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from opentour_cell.pilot import onboarding_mcp
from opentour_cell.pilot.custody import Closeout
from opentour_cell.pilot.mcp import PROTOCOL_VERSION
from opentour_cell.pilot.onboarding_mcp import CONFIRM, PREVIEW, Selection
from opentour_cell.pilot.text_classifier import TrainingError
from opentour_cell.pilot.training import LocalTrainingStore
from opentour_cell.pilot.training_data import (
    digest,
    generated_dataset,
    generated_evaluation_dataset,
)

MODULE = "opentour_cell.pilot.onboarding_mcp"
OWNER_PROGRAM = (
    "from opentour_cell.pilot.onboarding_mcp import authorize_main\n"
    "raise SystemExit(authorize_main())"
)


class TestDatasetOnboarding(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.directory = Path(folder.name)
        self.store = self.directory / "training.sqlite3"
        self.manifest = self.directory / "dataset.json"
        self.grant = self.directory / "permission.json"
        self.organization = "generated-private-org"
        self.reviewer = "generated-private-actor"
        self.attestation = "att-0123456789abcdef"
        self.data = generated_dataset(self.organization, self.reviewer)
        self.revision = digest(self.data)
        self.write(self.manifest, self.data)
        self.args = [
            "--organization",
            self.organization,
            "--store",
            str(self.store),
            "--manifest",
            str(self.manifest),
            "--authorization",
            str(self.grant),
            "--custodian-attestation",
            self.attestation,
        ]

    def write(self, path, data):
        path.write_text(json.dumps(data), encoding="utf-8")
        path.chmod(0o600)

    def authorize(self, *, revision=None, expires=None):
        return subprocess.run(
            [
                sys.executable,
                "-c",
                OWNER_PROGRAM,
                *self.args,
                "--dataset-revision",
                revision or self.revision,
                "--expires-at",
                str(expires or int(time.time()) + 900),
            ],
            capture_output=True,
            timeout=10,
        )

    def selection(self):
        return Selection(self.organization, self.store, self.manifest, self.grant, self.attestation)

    def select_data(self, data):
        self.data = data
        self.revision = digest(data)
        self.write(self.manifest, data)

    def stdio(self, calls):
        requests = [
            {
                "jsonrpc": "2.0",
                "id": 0,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "generated-test", "version": "1"},
                },
            },
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        ]
        for ordinal, (name, arguments) in enumerate(calls, start=2):
            requests.append(
                {
                    "jsonrpc": "2.0",
                    "id": ordinal,
                    "method": "tools/call",
                    "params": {
                        "name": name,
                        "arguments": arguments,
                    },
                }
            )
        frames = b"".join((json.dumps(r) + "\n").encode() for r in requests)
        p = subprocess.run(
            [sys.executable, "-m", MODULE, *self.args],
            input=frames,
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stderr, b"")
        for private in [
            self.organization,
            self.reviewer,
            self.attestation,
            str(self.directory),
            "invoice billing statement",
            "train-family-0",
            "test-family-0",
            "generated-intake",
            "generated-supplied",
            "supplied-1",
            "route-b",
        ]:
            self.assertNotIn(private.encode(), p.stdout)
        return [json.loads(line) for line in p.stdout.splitlines()]

    def test_initialized_preview_requires_private_owner_grant_and_fixed_arguments(self):
        out = self.stdio(
            [
                (CONFIRM, {"dataset_revision": self.revision}),
                (PREVIEW, {}),
                (CONFIRM, {"dataset_revision": self.revision}),
                (CONFIRM, {"approved": True}),
                (PREVIEW, {"path": "https://example.invalid/other"}),
                ("run_training", {}),
            ]
        )
        self.assertEqual([v["name"] for v in out[1]["result"]["tools"]], [PREVIEW, CONFIRM])
        self.assertTrue(out[1]["result"]["tools"][0]["annotations"]["readOnlyHint"])
        self.assertTrue(out[1]["result"]["tools"][1]["annotations"]["idempotentHint"])
        self.assertIn("preview_required", out[2]["result"]["content"][0]["text"])
        self.assertEqual(
            out[3]["result"]["structuredContent"],
            {
                "registration": "absent",
                "authorization": "authorization_required",
                "grant_expires_at": None,
                "mode": "training",
                "purpose": "local_text_classification",
                "permitted_actions": [
                    "inspect_metadata",
                    "score_frozen",
                    "train_cpu_text",
                    "preview_training_examples",
                ],
                "permitted_fields": self.data["visible_fields"],
                "destination": "owner_local_store",
                "retention": {"terms_ref": "terms-0123456789abcdef"},
                "dataset_revision": self.revision,
                "training_rows": 6,
                "heldout_rows": 4,
            },
        )
        for i in [4, 5, 6]:
            self.assertTrue(out[i]["result"]["isError"])
        self.assertEqual(out[7]["error"]["code"], -32602)
        self.assertFalse(self.store.exists())
        self.assertFalse(self.grant.exists())

    def test_exact_once_registration_reconnect_and_actual_authorized_catalog(self):
        self.assertEqual(self.authorize().returncode, 0)
        self.assertEqual(self.grant.stat().st_mode & 0o777, 0o600)
        first = self.stdio(
            [
                (PREVIEW, {}),
                (CONFIRM, {"dataset_revision": "0" * 64}),
                (CONFIRM, {"dataset_revision": self.revision}),
                (CONFIRM, {"dataset_revision": self.revision}),
                (PREVIEW, {}),
            ]
        )
        self.assertEqual(first[2]["result"]["structuredContent"]["authorization"], "authorized")
        self.assertTrue(first[3]["result"]["isError"])
        result = first[4]["result"]["structuredContent"]
        self.assertEqual(result, {"registration": "registered", "dataset_revision": self.revision})
        self.assertEqual(first[4]["result"], first[5]["result"])
        self.assertEqual(first[6]["result"]["structuredContent"]["registration"], "registered")
        again = self.stdio([(PREVIEW, {}), (CONFIRM, {"dataset_revision": self.revision})])
        self.assertEqual(again[3]["result"]["structuredContent"], result)
        catalog = LocalTrainingStore(self.store, self.organization).catalog()
        self.assertEqual(
            [(row["dataset_id"], row["dataset_revision"]) for row in catalog],
            [(self.data["dataset_id"], self.revision)],
        )
        with sqlite3.connect(self.store) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM datasets").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM jobs").fetchone()[0], 0)
        self.grant.unlink()
        denied = self.stdio([(PREVIEW, {}), (CONFIRM, {"dataset_revision": self.revision})])
        self.assertEqual(denied[2]["result"]["structuredContent"]["registration"], "registered")
        self.assertEqual(
            denied[2]["result"]["structuredContent"]["authorization"], "authorization_required"
        )
        self.assertTrue(denied[3]["result"]["isError"])

    def test_stale_scope_revision_and_expired_or_public_grants_deny_writes(self):
        self.assertEqual(self.authorize(revision="a" * 64).returncode, 2)
        self.assertEqual(self.authorize(expires=int(time.time()) - 1).returncode, 2)
        self.assertFalse(self.grant.exists())
        self.assertEqual(self.authorize().returncode, 0)
        original = json.loads(self.grant.read_text())
        self.assertEqual(self.authorize().returncode, 2)
        selected = self.selection()
        selected.call(PREVIEW, {})
        for key, bad in [
            ("schema_version", "opentour.pilot.onboarding-authorization.v1"),
            ("organization", "another"),
            ("dataset_id", "another"),
            ("dataset_revision", "x" * 64),
            ("scope", "run_training"),
            ("custodian_attestation", "att-ffffffffffffffff"),
            ("mode", "evaluation_only"),
            ("purpose", "release"),
            ("permitted_fields", ["private"]),
            ("permitted_actions", ["consume_external"]),
            ("destination", "dest-3333333333333333"),
            ("retention", {"terms_ref": "terms-bbbbbbbbbbbbbbbb"}),
            ("store_binding", "invalid"),
            ("expires_at", int(time.time()) - 1),
            ("granted_by_model", True),
        ]:
            with self.subTest(key=key):
                self.write(self.grant, original | {key: bad})
                self.assertTrue(
                    selected.call(CONFIRM, {"dataset_revision": self.revision})["isError"]
                )
                self.assertFalse(self.store.exists())
        self.write(self.grant, original)
        different_selection = Selection(
            self.organization, self.store, self.manifest, self.grant, "att-eeeeeeeeeeeeeeee"
        )
        self.assertEqual(
            different_selection.call(PREVIEW, {})["structuredContent"]["authorization"],
            "authorization_scope_mismatch",
        )
        self.assertTrue(
            different_selection.call(CONFIRM, {"dataset_revision": self.revision})["isError"]
        )
        self.grant.chmod(0o644)
        self.assertTrue(selected.call(CONFIRM, {"dataset_revision": self.revision})["isError"])
        self.grant.chmod(0o600)
        other = self.directory / "protected"
        self.grant.rename(other)
        self.grant.symlink_to(other)
        self.assertTrue(selected.call(CONFIRM, {"dataset_revision": self.revision})["isError"])
        self.grant.unlink()
        os.link(other, self.grant)
        self.assertTrue(selected.call(CONFIRM, {"dataset_revision": self.revision})["isError"])
        self.assertFalse(self.store.exists())

    def test_concurrent_clients_confirm_once_during_initial_database_creation(self):
        self.select_data(generated_evaluation_dataset(self.organization))
        self.assertEqual(self.authorize().returncode, 0)
        clients = 8
        barrier = threading.Barrier(clients, timeout=8)

        def confirm(_):
            selected = self.selection()
            selected.call(PREVIEW, {})
            barrier.wait()
            return selected.call(CONFIRM, {"dataset_revision": self.revision})

        with concurrent.futures.ThreadPoolExecutor(clients) as workers:
            replies = list(workers.map(confirm, range(clients)))
        self.assertEqual(
            [r.get("structuredContent") for r in replies],
            [
                {
                    "registration": "registered",
                    "dataset_revision": self.revision,
                }
            ]
            * clients,
        )
        self.assertEqual(len(LocalTrainingStore(self.store, self.organization).catalog()), 1)

    def test_first_registration_observes_empty_file_then_waits_for_owner_initialization(self):
        self.assertEqual(self.authorize().returncode, 0)
        first, second = self.selection(), self.selection()
        for client in (first, second):
            self.assertEqual(
                client.call(PREVIEW, {})["structuredContent"]["registration"], "absent"
            )
        arguments = {"dataset_revision": self.revision}
        created = threading.Event()
        checking = threading.Event()
        first_thread = []
        original_db = LocalTrainingStore._db
        original_registration = second.registration

        @contextmanager
        def pause_initialization(store, *args, **kwargs):
            if kwargs.get("check_owner") is False and first_thread == [threading.get_ident()]:
                created.set()
                if not checking.wait(5):
                    raise AssertionError("Second client never attempted to inspect the new store.")
            with original_db(store, *args, **kwargs) as db:
                yield db

        def check_second():
            checking.set()
            return original_registration()

        def first_confirmation():
            first_thread.append(threading.get_ident())
            return first.call(CONFIRM, arguments)

        with (
            patch.object(LocalTrainingStore, "_db", pause_initialization),
            patch.object(second, "registration", check_second),
            concurrent.futures.ThreadPoolExecutor(2) as pool,
        ):
            one = pool.submit(first_confirmation)
            self.assertTrue(created.wait(5))
            two = pool.submit(second.call, CONFIRM, arguments)
            for future in (one, two):
                self.assertEqual(
                    future.result(timeout=10)["structuredContent"],
                    {"registration": "registered", **arguments},
                )
            self.assertTrue(checking.is_set())
        store = LocalTrainingStore(self.store, self.organization)
        self.assertEqual(len(store.catalog()), 1)

    def test_evaluation_only_receipt_grants_scoring_and_requires_explicit_retention(self):
        data = generated_evaluation_dataset(self.organization)
        data.update(
            destination="dest-a0a0a0a0a0a0a0a0",
            retention={"disposition": "return", "date": "2030-12-31"},
        )
        self.select_data(data)
        expiry = int(time.time()) + 300
        self.assertEqual(self.authorize(expires=expiry).returncode, 0)
        out = self.stdio([(PREVIEW, {}), (CONFIRM, {"dataset_revision": self.revision})])
        self.assertEqual(
            out[2]["result"]["structuredContent"],
            {
                "registration": "absent",
                "authorization": "authorized",
                "grant_expires_at": expiry,
                "mode": "evaluation_only",
                "purpose": "evaluation_only",
                "permitted_actions": ["inspect_metadata", "score_frozen"],
                "permitted_fields": data["visible_fields"],
                "destination": data["destination"],
                "retention": data["retention"],
                "dataset_revision": self.revision,
                "training_rows": 0,
                "heldout_rows": 3,
            },
        )
        store = LocalTrainingStore(self.store, self.organization)
        with self.assertRaisesRegex(TrainingError, "training_not_authorized"):
            store.propose(data["dataset_id"])
        with sqlite3.connect(self.store) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM train_data").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)
        store.withdraw()
        denied = self.stdio([(PREVIEW, {}), (CONFIRM, {"dataset_revision": self.revision})])
        self.assertEqual(denied[2]["result"]["structuredContent"], {"registration": "unavailable"})
        self.assertTrue(denied[3]["result"]["isError"])

    def test_restored_database_under_custody_hold_returns_only_unavailable(self):
        self.assertEqual(self.authorize().returncode, 0)
        selected = self.selection()
        selected.call(PREVIEW, {})
        self.assertIn(
            "structuredContent", selected.call(CONFIRM, {"dataset_revision": self.revision})
        )
        pristine = self.directory / "before-withdrawal.sqlite3"
        shutil.copyfile(self.store, pristine)
        Closeout(self.directory, self.organization).withdraw(
            agreement="terms-a",
            custodian="custodian-a",
            support="support-a",
            due="2040-04-01",
            trigger="withdrawal",
        )
        shutil.copyfile(pristine, self.store)
        with patch.object(
            selected, "_current_manifest", side_effect=AssertionError("read after hold")
        ):
            self.assertIn(
                "private_selection_unavailable",
                selected.call(CONFIRM, {"dataset_revision": self.revision})["content"][0]["text"],
            )
            self.assertEqual(
                selected.call(PREVIEW, {})["structuredContent"], {"registration": "unavailable"}
            )
        # Reconnecting a closed organization also works when its private input has been removed.
        # The server checks custody first and never reopens that file.
        self.manifest.unlink()
        replies = self.stdio([(PREVIEW, {}), (CONFIRM, {"dataset_revision": self.revision})])
        self.assertEqual(replies[2]["result"]["structuredContent"], {"registration": "unavailable"})
        self.assertTrue(replies[3]["result"]["isError"])
        self.assertNotIn(self.revision, json.dumps(replies))
        self.grant.unlink()
        self.assertEqual(self.authorize().returncode, 2)
        self.assertFalse(self.grant.exists())

    def test_legacy_training_without_terms_and_unapproved_training_cannot_enroll(self):
        legacy = dict(self.data)
        del legacy["retention"]
        self.select_data(legacy)
        self.assertEqual(self.authorize().returncode, 2)
        replies = self.stdio([(PREVIEW, {}), (CONFIRM, {"dataset_revision": self.revision})])
        self.assertEqual(
            replies[2]["result"]["structuredContent"]["authorization"], "retention_terms_required"
        )
        self.assertTrue(replies[3]["result"]["isError"])
        self.select_data(legacy | {"train_approved": False})
        self.assertEqual(self.authorize().returncode, 2)
        self.assertFalse(self.store.exists())

    def test_permission_changed_after_preview_requires_another_preview(self):
        self.assertEqual(self.authorize().returncode, 0)
        selected = self.selection()
        first = selected.call(PREVIEW, {})["structuredContent"]
        grant = json.loads(self.grant.read_text())
        self.write(self.grant, grant | {"expires_at": first["grant_expires_at"] + 1})
        result = selected.call(CONFIRM, {"dataset_revision": self.revision})
        self.assertIn("authorization_changed_preview_again", result["content"][0]["text"])
        self.assertFalse(self.store.exists())
        selected.call(PREVIEW, {})
        self.assertEqual(
            selected.call(CONFIRM, {"dataset_revision": self.revision})["structuredContent"][
                "registration"
            ],
            "registered",
        )

    def test_expired_grant_during_writer_lock_prevents_registration(self):
        now = int(time.time())
        expiry = now + 600
        self.assertEqual(self.authorize(expires=expiry).returncode, 0)
        store = LocalTrainingStore(self.store, self.organization)
        selected = self.selection()
        selected.call(PREVIEW, {})
        checked = threading.Event()
        current = [now]
        original = selected._check_grant

        def before_lock():
            result = original()
            checked.set()
            return result

        with (
            sqlite3.connect(self.store) as lock,
            patch.object(onboarding_mcp, "time", SimpleNamespace(time=lambda: current[0])),
            patch.object(selected, "_check_grant", before_lock),
            concurrent.futures.ThreadPoolExecutor(1) as pool,
        ):
            lock.execute("BEGIN IMMEDIATE")
            future = pool.submit(selected.call, CONFIRM, {"dataset_revision": self.revision})
            try:
                self.assertTrue(checked.wait(3))
                current[0] = expiry
            finally:
                lock.commit()
            result = future.result(timeout=5)
        self.assertIn("authorization_expired", result["content"][0]["text"])
        self.assertEqual(store.catalog(), [])

    def test_replaced_selection_foreign_store_and_revoked_membership_fail_closed(self):
        self.assertEqual(self.authorize().returncode, 0)
        selected = self.selection()
        selected.call(PREVIEW, {})
        changed = self.data | {"error_ceiling_bps": 10}
        self.write(self.manifest, changed)
        self.assertTrue(selected.call(CONFIRM, {"dataset_revision": self.revision})["isError"])
        self.assertTrue(selected.call(PREVIEW, {})["isError"])
        self.assertFalse(self.store.exists())
        self.write(self.manifest, self.data)
        other = LocalTrainingStore(self.store, "foreign-organization")
        self.assertEqual(
            selected.call(PREVIEW, {})["structuredContent"], {"registration": "unavailable"}
        )
        self.assertTrue(selected.call(CONFIRM, {"dataset_revision": self.revision})["isError"])
        self.assertEqual(other.catalog(), [])
        self.store.unlink()
        LocalTrainingStore(self.store, self.organization).register(changed)
        self.assertEqual(selected.call(PREVIEW, {})["structuredContent"]["registration"], "stale")
        self.assertTrue(selected.call(CONFIRM, {"dataset_revision": self.revision})["isError"])
        LocalTrainingStore(self.store, self.organization).revoke(self.data["dataset_id"])
        self.assertEqual(
            selected.call(PREVIEW, {})["structuredContent"], {"registration": "unavailable"}
        )
        self.assertTrue(selected.call(CONFIRM, {"dataset_revision": self.revision})["isError"])

    def test_bad_selection_fails_at_launch_without_disclosing_the_path(self):
        self.write(self.manifest, self.data | {"organization": "foreign-organization"})
        p = subprocess.run(
            [sys.executable, "-m", MODULE, *self.args], input=b"", capture_output=True, timeout=10
        )
        self.assertEqual(p.returncode, 2)
        self.assertFalse(p.stdout)
        self.assertNotIn(str(self.directory).encode(), p.stderr)
        self.manifest.chmod(0o644)
        self.assertEqual(self.authorize().returncode, 2)
        self.assertFalse(self.store.exists())
