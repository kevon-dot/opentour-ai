"""Run the preallocated trainer on one offline host, using the installed worker's private volume."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib
import os
import re
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from opentour_training.training.eve_run import TERMINAL, Run
from opentour_training.training.eve_sft import BASE_REVISION


def staged_input(directory: Path, checksum: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{64}", checksum):
        raise ValueError("invalid_eve_input_digest")
    path = directory / (checksum + ".json")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_size > 8 * 1024 * 1024
            or hashlib.file_digest(handle, "sha256").hexdigest() != checksum
        ):
            raise ValueError("eve_input_digest_mismatch")
    return path


class TrainerHost:
    def __init__(
        self,
        exchange: Any,
        model: Path,
        inputs: Path,
        work: Path,
        image_id: str,
        *,
        spawn: Callable[[list[str]], subprocess.Popen[bytes]] | None = None,
    ) -> None:
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
            raise ValueError("verified_image_id_required")
        paths = (model, inputs, work, exchange.directory)
        if any(not p.is_absolute() or p.resolve(strict=True) != p or not p.is_dir() for p in paths):
            raise ValueError("invalid_eve_mounts")
        if any(
            a.is_relative_to(b) for i, a in enumerate(paths) for j, b in enumerate(paths) if i != j
        ):
            raise ValueError("separate_eve_mounts_required")
        if work.stat().st_mode & 0o077 or work.stat().st_uid != os.geteuid():
            raise ValueError("private_eve_work_required")
        self.exchange, self.model, self.inputs, self.work = exchange, model, inputs, work
        self.image_id = image_id
        self.spawn = spawn or self._spawn
        self.children: dict[str, subprocess.Popen[bytes]] = {}

    @staticmethod
    def _spawn(args: list[str]) -> subprocess.Popen[bytes]:
        return subprocess.Popen(
            [sys.executable, "-I", "-B", "-m", "opentour_training.training.eve_sft", *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )

    def _launch(self, job: dict[str, Any], root: Path, run: Run, *, resume: bool) -> None:
        spec = job["spec"]
        if spec["model_revision"] != BASE_REVISION or not 8 <= spec["steps"] <= 16:
            raise ValueError("wrong_eve_recipe")
        development = staged_input(self.inputs, spec["development_sha256"])
        protected = staged_input(self.inputs, spec["protected_sha256"])
        args = (
            ["resume", "--run-dir", str(run.directory)]
            if resume
            else [
                "fit",
                "--run-dir",
                str(run.directory),
                "--model-dir",
                str(self.model),
                "--development",
                str(development),
                "--protected-membership",
                str(protected),
                "--output",
                str(root / "candidate"),
                "--steps",
                str(job["total_steps"]),
                "--deadline",
                str(job["deadline"]),
                "--worker-database",
                str(self.exchange.path),
                "--reservation-ref",
                job["reservation_ref"],
                "--reservation-digest",
                job["spec_digest"],
                "--development-sha256",
                spec["development_sha256"],
                "--protected-sha256",
                spec["protected_sha256"],
                "--max-rows",
                str(spec["max_rows"]),
            ]
        )
        if self.exchange.begin_launch(job):
            self.children[job["run_ref"]] = self.spawn(args)

    def tick(self) -> None:
        job = self.exchange.active()
        if job is None:
            for key, child in list(self.children.items()):
                if child.poll() is not None:
                    del self.children[key]
            return
        try:
            self._tick(job)
        except (ValueError, OSError, KeyError, TypeError):
            # No candidate or new launch is authorized by a malformed receipt or staged input.
            self.exchange.observe(job, "held")

    def _tick(self, job: dict[str, Any]) -> None:
        spec = job["spec"]
        if job["launched"] < 0 and job["requested"] == "stop":
            self.exchange.observe(job, "stopped", seconds=0)
            return
        if spec["image_id"] != self.image_id or not re.fullmatch(
            r"eve-[a-f0-9]{32}", job["run_ref"]
        ):
            raise ValueError("wrong_eve_installation")
        root = self.work / job["run_ref"]
        root.mkdir(mode=0o700, exist_ok=True)
        if root.is_symlink() or root.stat().st_uid != os.geteuid() or root.stat().st_mode & 0o077:
            raise ValueError("private_eve_run_required")
        run = Run(root / "trainer")
        expired = time.time() >= job["deadline"]
        fenced = expired or time.time() >= job["permit_until"]
        requested = job["requested"]
        child = self.children.get(job["run_ref"])
        if not run.state_file.exists():
            if job["launched"] < 0:
                if requested == "stop" or fenced:
                    self.exchange.observe(job, "expired" if expired else "stopped", seconds=0)
                elif job["state"] == "queued":
                    self._launch(job, root, run, resume=False)
            elif child is not None and child.poll() is not None:
                self.exchange.observe(job, "failed")
            # A crash between recording launch and the child's first journal is ambiguous.
            # Keep its original reservation; a later tick can discover the original process.
            elif child is None and (fenced or time.time() - job["launched_at"] > 45):
                self.exchange.observe(job, "held", requested=requested)
            return
        state = run.control("status")
        if job["trainer_id"] is not None and state["run_id"] != job["trainer_id"]:
            raise ValueError("eve_trainer_identity_conflict")
        if state["step_cap"] != job["total_steps"] or state["deadline"] != job["deadline"]:
            raise ValueError("eve_trainer_terms_conflict")
        if state["status"] not in TERMINAL or state["status"] == "held":
            if fenced or requested == "stop":
                state = run.control("stop")
            elif requested == "pause":
                state = run.control("pause")
            elif requested == "resume" and state["status"] == "paused":
                if child is None or child.poll() is not None:
                    if job["launched"] != job["version"]:
                        self._launch(job, root, run, resume=True)
                    elif child is not None and child.returncode:
                        self.exchange.observe(job, "paused", checkpoint=state["completed_steps"])
                    elif child is None and time.time() - job["launched_at"] > 45:
                        self.exchange.observe(job, "held")
                # While waiting for the resume child, retain its pending control and revision.
                return
        observed = state["status"]
        if observed in {"expired", "completed", "stopped"}:
            # An in-flight kernel or model load may outlive the wall deadline. Only the
            # process lease proves that the old trainer can no longer consume this GPU.
            with run.worker() as idle:
                if not idle:
                    self.exchange.observe(
                        job,
                        "stopping" if fenced or requested == "stop" else job["state"],
                        requested="stop" if fenced or requested == "stop" else requested,
                        step=state["completed_steps"],
                        seconds=int(state["active_seconds"]),
                        trainer_id=state["run_id"],
                    )
                    return
        if observed == "stopped" and expired:
            observed = "expired"
        pending = {"pausing": "pause", "stopping": "stop", "resuming": "resume"}.get(observed)
        self.exchange.observe(
            job,
            observed,
            requested=pending,
            step=state["completed_steps"],
            seconds=int(state["active_seconds"]),
            trainer_id=state["run_id"],
            checkpoint=state["completed_steps"] if observed == "paused" else None,
        )


def serve(args: argparse.Namespace) -> None:
    exchange = importlib.import_module("opentour_cell.pilot.eve_exchange").Exchange(args.exchange)
    host = TrainerHost(exchange, args.model_dir, args.inputs, args.work, args.image_id)
    stopped = False

    def stop(_signal: int, _frame: object) -> None:
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    with (args.work / "supervisor.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        while not stopped:
            host.tick()
            if args.once:
                break
            time.sleep(1)
