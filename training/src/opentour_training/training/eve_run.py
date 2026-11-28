"""Durable controls for one trainer on a local POSIX filesystem."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

TERMINAL = {"completed", "stopped", "expired", "held"}
ADAPTER_FILES = ("adapter_config.json", "adapter_model.safetensors")


def file_hashes(directory: Path, names: tuple[str, ...]) -> dict[str, str]:
    hashes = {}
    for name in names:
        path = directory / name
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"missing_staged_file:{name}")
        with path.open("rb") as handle:
            hashes[name] = hashlib.file_digest(handle, "sha256").hexdigest()
    return hashes


def sync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class Run:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.state_file = directory / "state.json"

    @contextmanager
    def worker(self) -> Iterator[bool]:
        """Only the lease holder can execute optimizer steps or publish a candidate."""
        with (self.directory / "worker.lock").open("a+b") as lease:
            try:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
            else:
                yield True

    @contextmanager
    def edit(self) -> Iterator[dict[str, Any]]:
        with (self.directory / "state.lock").open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            original = self.state_file.read_text() if self.state_file.exists() else "{}"
            state = json.loads(original)
            yield state
            updated = json.dumps(state, sort_keys=True)
            if original == updated:
                return
            with tempfile.NamedTemporaryFile(dir=self.directory, delete=False, mode="w") as tmp:
                try:
                    tmp.write(updated)
                    tmp.flush()
                    os.fsync(tmp.fileno())
                    os.replace(tmp.name, self.state_file)
                    sync_directory(self.directory)
                finally:
                    Path(tmp.name).unlink(missing_ok=True)

    def start(self, settings: dict[str, Any]) -> bool:
        with self.edit() as state:
            if state:
                if state["settings"] != settings:
                    raise ValueError("existing_run_settings_mismatch")
                self.reconcile_idle(state)
                return False
            if Path(settings["output"]).exists():
                raise FileExistsError(settings["output"])
            state.update(
                run_id=uuid4().hex,
                status="running",
                settings=settings,
                completed_steps=0,
                active_seconds=0.0,
                peak_gpu_bytes=0,
                pause_at=None,
                checkpoint=None,
            )
        return True

    @staticmethod
    def expire(state: dict[str, Any]) -> None:
        deadline = state["settings"]["deadline"]
        if deadline is not None and time.time() >= deadline and state["status"] not in TERMINAL:
            state["status"] = "expired"

    @staticmethod
    def reconcile_idle(state: dict[str, Any]) -> None:
        """A lost process cannot admit another fit. Recover a completed atomic rename if present."""
        if state["status"] == "resuming":
            # The runner has not yet been admitted to its next optimizer step.
            state["status"] = "paused"
            return
        if state["status"] not in {"running", "pausing", "stopping"}:
            return
        output = Path(state["settings"]["output"])
        if (
            state["completed_steps"] == state["settings"]["steps"]
            and (output / "manifest.json").is_file()
        ):
            try:
                receipt = json.loads((output / "manifest.json").read_text())
                if (
                    isinstance(receipt, dict)
                    and receipt.get("run_id") == state["run_id"]
                    and receipt.get("steps") == state["completed_steps"]
                    and file_hashes(output, ADAPTER_FILES) == receipt.get("adapter_files")
                ):
                    state["status"] = "completed"
                    return
            except (OSError, ValueError):
                state["status"] = "held"
                return
        state["status"] = "stopped" if state["status"] == "stopping" else "held"

    def control(self, action: str, after_step: int | None = None) -> dict[str, Any]:
        with self.edit() as state:
            if not state:
                raise ValueError("unknown_run")
            with self.worker() as idle:
                if idle:
                    self.reconcile_idle(state)
            self.expire(state)
            if action == "stop" and state["status"] not in {"completed", "stopped", "expired"}:
                state["status"] = "stopped" if idle or state["status"] == "paused" else "stopping"
            elif action == "pause" and state["status"] in {"running", "pausing", "resuming"}:
                target = state["completed_steps"] if after_step is None else after_step
                if not 0 <= target <= state["settings"]["steps"]:
                    raise ValueError("pause_step_out_of_range")
                previous = state["pause_at"]
                state["pause_at"] = target if previous is None else min(previous, target)
                if state["status"] == "resuming":
                    if state["pause_at"] <= state["completed_steps"]:
                        state["status"] = "paused"
                else:
                    state["status"] = "pausing"
            return self.summary(state)

    @staticmethod
    def summary(state: dict[str, Any]) -> dict[str, Any]:
        fields = ("run_id", "status", "completed_steps", "active_seconds", "peak_gpu_bytes")
        return {k: state[k] for k in fields} | {
            "step_cap": state["settings"]["steps"],
            "deadline": state["settings"]["deadline"],
            "resumed_from_step": state.get("resumed_from_step"),
        }

    def boundary(
        self,
        state: dict[str, Any],
        step: int,
        elapsed: Callable[[], float],
        peak: int,
        checkpoint: Callable[[], dict[str, Any]],
    ) -> bool:
        """Called between complete optimizer steps, and under the same lock as final publication."""
        self.expire(state)
        state["completed_steps"] = step
        state["peak_gpu_bytes"] = max(peak, state["peak_gpu_bytes"])
        if state["status"] == "stopping":
            state["status"] = "stopped"
        if state["status"] == "pausing" and step >= state["pause_at"]:
            state.update(checkpoint=checkpoint(), status="paused")
            self.expire(state)
        state["active_seconds"] = elapsed()
        return state["status"] not in {"running", "pausing"}
