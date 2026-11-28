"""Run controls exercised without a model, including interrupted publication and expiry."""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from opentour_training.training import eve_sft
from opentour_training.training.eve_run import ADAPTER_FILES, Run


class RunTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.directory = Path(folder.name)
        self.run = Run(self.directory)
        self.output = self.directory / "candidate"
        self.settings = {"steps": 8, "output": str(self.output), "deadline": None}

    def test_one_worker_and_immutable_step_cap(self):
        with self.run.worker() as owner:
            self.assertTrue(owner)
            self.assertTrue(self.run.start(self.settings))
            with Run(self.directory).worker() as duplicate:
                self.assertFalse(duplicate)
            with self.assertRaisesRegex(ValueError, "settings_mismatch"):
                self.run.start(dict(self.settings, steps=9))
            with self.assertRaisesRegex(ValueError, "out_of_range"):
                self.run.control("pause", after_step=9)

    def test_pause_request_waits_for_boundary_and_survives_restart(self):
        with self.run.worker():
            self.run.start(self.settings)
            self.run.control("pause", after_step=4)
            self.run.control("pause", after_step=6)
            with self.run.edit() as state:
                self.assertFalse(self.run.boundary(state, 3, lambda: 1.0, 0, self.fail))
                self.assertTrue(
                    self.run.boundary(state, 4, lambda: 2.0, 0, lambda: {"sha256": "proof"})
                )
        status = Run(self.directory).control("status")
        self.assertEqual(
            (status["status"], status["completed_steps"], status["active_seconds"]),
            ("paused", 4, 2.0),
        )

    def test_deadline_is_absolute_across_a_pause(self):
        with (
            patch("opentour_training.training.eve_run.time.time", return_value=100),
            self.run.worker(),
        ):
            self.run.start(dict(self.settings, deadline=105))
            self.run.control("pause", after_step=4)
            with self.run.edit() as state:
                self.run.boundary(state, 4, lambda: 3.0, 0, lambda: {"sha256": "proof"})
        with patch("opentour_training.training.eve_run.time.time", return_value=106):
            receipt = self.run.control("status")
        self.assertEqual(
            (receipt["status"], receipt["deadline"], receipt["completed_steps"]),
            ("expired", 105, 4),
        )
        self.assertFalse(self.output.exists())

    def test_stop_wins_over_pending_pause_and_publication(self):
        with self.run.worker():
            self.run.start(self.settings)
            self.run.control("pause", after_step=8)
            self.assertEqual(self.run.control("stop")["status"], "stopping")
            with self.run.edit() as state:
                self.assertTrue(self.run.boundary(state, 8, lambda: 2.0, 0, self.fail))
        self.assertEqual(self.run.control("status")["status"], "stopped")
        self.assertFalse(self.output.exists())

    def test_lost_process_is_held_without_repeating_work(self):
        with self.run.worker():
            self.run.start(self.settings)
            with self.run.edit() as state:
                self.run.boundary(state, 3, lambda: 1.0, 0, self.fail)
        self.assertEqual(self.run.control("status")["status"], "held")
        self.assertFalse(self.run.start(self.settings))
        self.assertEqual(self.run.control("stop")["completed_steps"], 3)

    def test_pause_or_stop_during_restore_prevents_next_optimizer_step(self):
        with self.run.worker():
            self.run.start(self.settings)
            with self.run.edit() as state:
                state.update(status="resuming", completed_steps=4, checkpoint={"sha256": "proof"})
            self.assertEqual(self.run.control("pause")["status"], "paused")
            with self.run.edit() as state:
                state["status"] = "resuming"
            self.assertEqual(self.run.control("stop")["status"], "stopping")
        self.assertEqual(self.run.control("status")["status"], "stopped")

    def test_lost_process_in_restore_keeps_last_confirmed_pause(self):
        with self.run.worker():
            self.run.start(self.settings)
            with self.run.edit() as state:
                state.update(status="resuming", completed_steps=4, checkpoint={"sha256": "proof"})
        self.assertEqual(self.run.control("status")["status"], "paused")

    def test_final_rename_recovers_only_the_matching_adapter(self):
        with self.run.worker():
            self.run.start(self.settings)
            with self.run.edit() as state:
                run_id = state["run_id"]
                self.run.boundary(state, 8, lambda: 2.0, 0, self.fail)
        self.output.mkdir()
        hashes = {}
        for name in ("adapter_config.json", "adapter_model.safetensors"):
            (self.output / name).write_bytes(b"generated")
            hashes[name] = hashlib.sha256(b"generated").hexdigest()
        manifest = {"steps": 8, "run_id": run_id, "adapter_files": hashes}
        (self.output / "manifest.json").write_text(json.dumps(manifest))
        self.assertEqual(self.run.control("stop")["status"], "completed")
        with self.run.edit() as state:
            state["status"] = "running"
        (self.output / "adapter_model.safetensors").write_bytes(b"mismatch")
        self.assertEqual(self.run.control("status")["status"], "held")
        with self.run.edit() as state:
            state["status"] = "running"
        (self.output / "manifest.json").write_text("{")
        self.assertEqual(self.run.control("status")["status"], "held")
        self.assertEqual(self.run.control("stop")["status"], "held")

    def test_fsync_failure_after_publication_cannot_report_a_stopped_candidate(self):
        def finish(args, run, saved, elapsed):
            with run.edit() as state:
                run.boundary(state, args.steps, elapsed, 0, self.fail)
            stage = self.directory / "generated-stage"
            stage.mkdir()
            for name in ADAPTER_FILES:
                (stage / name).write_bytes(b"generated adapter fixture")
            receipt = {
                "steps": args.steps,
                "run_id": saved["run_id"],
                "adapter_files": eve_sft.file_hashes(stage, ADAPTER_FILES),
            }
            (stage / "manifest.json").write_text(json.dumps(receipt))
            with run.edit() as state:
                eve_sft.publish_candidate(stage, args.output)
                state["status"] = "completed"

        args = argparse.Namespace(
            action="fit",
            output=self.output,
            run_dir=self.directory,
            model_dir=self.directory,
            development=self.directory / "dev",
            protected_membership=self.directory / "held",
            steps=8,
            deadline=None,
        )
        with (
            patch.object(eve_sft, "train", finish),
            patch.object(
                eve_sft, "sync_directory", side_effect=OSError("interrupted directory fsync")
            ),
        ):
            with self.assertRaisesRegex(OSError, "directory fsync"):
                eve_sft.fit(args)
        self.assertTrue((self.output / "manifest.json").is_file())
        self.assertEqual(self.run.control("stop")["status"], "completed")
