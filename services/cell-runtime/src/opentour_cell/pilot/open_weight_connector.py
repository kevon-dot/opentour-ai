"""Open-weight connector: the local route behind the OPE-506 model connector contract.

Implements ``model_connector.Connector`` for a customer-owned machine. One connector instance
serves one model family. A durable, append-only file journal makes ``submit`` idempotent, makes a
lost acknowledgement recoverable through ``lookup``, and records cancellation and fencing before
any process is stopped. A job that is not provably complete after a restart is FAILED, never
silently re-run: a retry needs a new fit intent and therefore a new reservation owned by the caller.

Simulated: tests drive the connector with generated toy model families through a real subprocess.
Not proven: no real checkpoint is loaded here and no GPU is used. The connector never reserves or
settles money; it enforces the bound it is given and reports measured usage.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from opentour_cell.pilot import model_connector as mc
from opentour_cell.pilot.connector_ext import FenceReceipt, Usage, UsageSource
from opentour_cell.pilot.model_family import (
    FamilyError,
    ModelFamilyProfile,
    verify_model_directory,
)

VERSION = "opentour.pilot.open-weight-connector.v1"
_TERMINAL = {"completed", "failed", "canceled"}


class ConnectorRefusal(ValueError):
    """A fixed UPPER_SNAKE refusal. Raised before any process starts or after a safe stop."""


class RunInterrupted(Exception):
    """The supervisor stopped the worker. ``reason`` is a fixed UPPER_SNAKE code."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class FitOutput:
    files: Mapping[str, bytes]
    evidence: Mapping[str, Any]


class RunControl(Protocol):
    def stop_reason(self) -> str | None:
        """Return a fixed reason when the worker must stop now, else ``None``."""
        ...


class FamilyRunner(Protocol):
    """Family-specific numeric execution behind the connector. Holds no authority."""

    profile: ModelFamilyProfile

    def installed_revision(self) -> str:
        """Verify the approved installation, metadata and digests only. Raise on mismatch."""
        ...

    def fit(
        self, rows: tuple[Mapping[str, Any], ...], steps: int, seconds: int, control: RunControl
    ) -> FitOutput: ...

    def predict(
        self, artifact_dir: Path | None, inputs: tuple[Mapping[str, Any], ...]
    ) -> tuple[Mapping[str, Any], ...]:
        """Fresh-process inference. ``None`` serves the unmodified pinned base."""
        ...


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def serialize_rows(rows: Sequence[Mapping[str, Any]]) -> bytes:
    """Canonical, order-independent encoding of development rows."""
    encoded = sorted(_canonical(dict(row)).decode() for row in rows)
    return _canonical({"rows": encoded})


def development_digest(rows: Sequence[Mapping[str, Any]]) -> str:
    return _sha(serialize_rows(rows))


def intent_digest(intent: mc.FitIntent) -> str:
    binding = intent.binding
    return _sha(
        _canonical(
            [
                [
                    binding.tenant,
                    binding.model_ref,
                    binding.immutable_revision,
                    binding.task,
                    binding.input_schema,
                    binding.output_schema,
                    binding.preprocessing_revision,
                    binding.route.value,
                    binding.account_or_host,
                    binding.region,
                    binding.credential_ref,
                ],
                intent.grant_revision,
                intent.benchmark_revision,
                intent.development_digest,
                intent.idempotency_key,
                sorted(intent.fields),
                [
                    intent.bound.currency,
                    intent.bound.maximum_minor_units,
                    intent.bound.maximum_seconds,
                    intent.bound.maximum_rows,
                    intent.bound.maximum_steps,
                ],
            ]
        )
    )


def _append(path: Path, event: Mapping[str, Any]) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        os.write(descriptor, _canonical(dict(event)) + b"\n")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _events(path: Path) -> list[dict[str, Any]]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return []
    events = []
    for line in raw.splitlines():
        event = json.loads(line)
        if not isinstance(event, dict) or not isinstance(event.get("event"), str):
            raise ValueError("journal_corrupt")
        events.append(event)
    return events


def _marker(path: Path, payload: Mapping[str, Any]) -> None:
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        return
    try:
        os.write(descriptor, _canonical(dict(payload)))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class _Lock:
    """An exclusive advisory lock held by the one process executing a job."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.descriptor: int | None = None

    def acquire(self) -> bool:
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(descriptor)
            return False
        self.descriptor = descriptor
        return True

    def release(self) -> None:
        if self.descriptor is not None:
            os.close(self.descriptor)
            self.descriptor = None


@dataclass
class _Control:
    job_dir: Path
    root: Path
    deadline: float
    clock: Callable[[], float]

    def stop_reason(self) -> str | None:
        if (self.root / "FENCED").exists():
            return "CONNECTOR_FENCED"
        if (self.job_dir / "CANCEL").exists():
            return "JOB_CANCELED"
        if self.clock() >= self.deadline:
            return "DEADLINE_EXCEEDED"
        return None


class OpenWeightConnector:
    """``model_connector.Connector`` for one pinned family on the owner's own machine."""

    def __init__(
        self,
        runner: FamilyRunner,
        state_dir: Path,
        *,
        clock: Callable[[], float] = time.time,
        observation_seconds: int = 300,
        allow_fixture_profiles: bool = False,
        residual_gate: Callable[[mc.FitIntent], bool] | None = None,
        cancel_wait_seconds: float = 10.0,
    ) -> None:
        self.runner = runner
        self.profile = runner.profile
        self.root = state_dir
        self.clock = clock
        self.observation_seconds = observation_seconds
        self.allow_fixture_profiles = allow_fixture_profiles
        self.residual_gate = residual_gate
        self.cancel_wait_seconds = cancel_wait_seconds
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        (self.root / "jobs").mkdir(mode=0o700, exist_ok=True)

    # ------------------------------------------------------------------ capability
    def _matches(self, binding: mc.ModelBinding) -> bool:
        profile = self.profile
        return (
            binding.route is mc.Route.LOCAL
            and binding.task == profile.task
            and binding.preprocessing_revision == profile.formatter
            and (self.allow_fixture_profiles or not profile.is_fixture)
        )

    def observe(self, binding: mc.ModelBinding, purpose: mc.Purpose) -> mc.Observation:
        if not self._matches(binding):
            raise ValueError("MODEL_FAMILY_UNSUPPORTED")
        resolved: str | None
        try:
            resolved = self.runner.installed_revision()
        except (OSError, ValueError, LookupError):
            resolved = None
        now = self.clock()
        ready = resolved is not None
        return mc.Observation(
            binding=binding,
            source_ref=f"simulated:installation:{self.profile.identity[:16]}",
            observed_at=int(now),
            expires_at=int(now) + self.observation_seconds,
            resolved_revision=resolved,
            inference_supported=ready,
            training_supported=ready,
            hard_cost_cap=True,
            cancellation=True,
            progress=False,
            checkpoint_recovery=False,
            capture={},
        )

    def estimate(self, intent: mc.FitIntent) -> mc.FitBound | None:
        if not self._matches(intent.binding):
            raise ValueError("MODEL_FAMILY_UNSUPPORTED")
        resource = self.profile.resource
        return mc.FitBound(
            intent.bound.currency,
            resource.incremental_cost_minor_units,
            resource.max_seconds,
            resource.max_rows,
            resource.max_steps,
        )

    def serialize_development(
        self, intent: mc.FitIntent, rows: tuple[Mapping[str, object], ...]
    ) -> bytes:
        if not rows or len(rows) > self.profile.resource.max_rows:
            raise ValueError("TRAINING_ROWS_OUTSIDE_PROFILE")
        if any(set(row) != set(intent.fields) for row in rows):
            raise ValueError("TRAINING_ROW_FIELDS_OUTSIDE_SCOPE")
        return serialize_rows(rows)

    # ------------------------------------------------------------------ job storage
    def _key(self, intent: mc.FitIntent) -> str:
        return _sha(intent.idempotency_key.encode())[:32]

    def _job_dir(self, key: str) -> Path:
        return self.root / "jobs" / key

    def _job_ref(self, key: str) -> str:
        return f"simulated:open-weight-job:{key}"

    def _key_from_ref(self, job_ref: str) -> str:
        prefix = "simulated:open-weight-job:"
        key = job_ref.removeprefix(prefix)
        if not job_ref.startswith(prefix) or len(key) != 32 or not key.isalnum():
            raise ValueError("JOB_REFERENCE_UNKNOWN")
        return key

    def _receipt(self, intent: mc.FitIntent, job_dir: Path, key: str) -> mc.JobReceipt:
        journal = job_dir / "journal.jsonl"
        events = _events(journal)
        if not events or events[0]["event"] != "accepted":
            raise ValueError("journal_corrupt")
        if events[0]["intent_sha256"] != intent_digest(intent):
            raise ConnectorRefusal("IDEMPOTENCY_CONFLICT")
        last = events[-1]["event"]
        if last not in _TERMINAL:
            lock = _Lock(job_dir / "lock")
            if lock.acquire():
                try:
                    # No process owns this job: it was interrupted. Fail closed, never re-run.
                    _append(journal, {"event": "failed", "reason": "INTERRUPTED_BEFORE_COMPLETION"})
                finally:
                    lock.release()
                events = _events(journal)
                last = "failed"
        if last in _TERMINAL:
            state = {
                "completed": mc.JobState.COMPLETED,
                "failed": mc.JobState.FAILED,
                "canceled": mc.JobState.CANCELED,
            }[last]
        else:
            state = mc.JobState.RUNNING if len(events) > 1 else mc.JobState.QUEUED
        evidence = "simulated:journal:" + _sha(journal.read_bytes())[:32]
        return mc.JobReceipt(intent, state, self._job_ref(key), evidence)

    # ------------------------------------------------------------------ lifecycle
    def lookup(self, intent: mc.FitIntent) -> mc.JobReceipt | None:
        key = self._key(intent)
        job_dir = self._job_dir(key)
        if not (job_dir / "journal.jsonl").is_file():
            return None
        return self._receipt(intent, job_dir, key)

    def status(self, job: mc.JobReceipt) -> mc.JobReceipt:
        key = self._key_from_ref(job.job_ref)
        return self._receipt(job.intent, self._job_dir(key), key)

    def _refuse_if_fenced(self) -> None:
        if (self.root / "FENCED").exists():
            raise ConnectorRefusal("CONNECTOR_FENCED")

    def submit(self, intent: mc.FitIntent, development: bytes) -> mc.JobReceipt:
        self._refuse_if_fenced()
        key = self._key(intent)
        job_dir = self._job_dir(key)
        with _SubmitLock(self.root):
            if (job_dir / "journal.jsonl").is_file():
                # Duplicate dispatch: report the one existing execution, never start another.
                return self._receipt(intent, job_dir, key)
            rows = self._validated_rows(intent, development)
            job_dir.mkdir(mode=0o700)
            lock = _Lock(job_dir / "lock")
            lock.acquire()
            _append(
                job_dir / "journal.jsonl",
                {"event": "accepted", "intent_sha256": intent_digest(intent), "rows": len(rows)},
            )
        try:
            self._execute(intent, rows, job_dir)
        finally:
            lock.release()
        return self._receipt(intent, job_dir, key)

    def _validated_rows(
        self, intent: mc.FitIntent, development: bytes
    ) -> tuple[Mapping[str, Any], ...]:
        if not self._matches(intent.binding):
            raise ConnectorRefusal("MODEL_FAMILY_UNSUPPORTED")
        resource = self.profile.resource
        bound = intent.bound
        if (
            resource.max_steps > bound.maximum_steps
            or resource.max_seconds > bound.maximum_seconds
            or resource.incremental_cost_minor_units > bound.maximum_minor_units
        ):
            raise ConnectorRefusal("BOUND_EXCEEDED_BEFORE_DISPATCH")
        if _sha(development) != intent.development_digest:
            raise ConnectorRefusal("DEVELOPMENT_DIGEST_MISMATCH")
        try:
            decoded = json.loads(development)
            rows = tuple(json.loads(item) for item in decoded["rows"])
        except (ValueError, KeyError, TypeError) as exc:
            raise ConnectorRefusal("DEVELOPMENT_PAYLOAD_INVALID") from exc
        if (
            not rows
            or len(rows) > min(resource.max_rows, bound.maximum_rows)
            or any(not isinstance(row, dict) or set(row) != set(intent.fields) for row in rows)
        ):
            raise ConnectorRefusal("BOUND_EXCEEDED_BEFORE_DISPATCH")
        if self.residual_gate is not None and not self.residual_gate(intent):
            raise ConnectorRefusal("APPROVED_RESIDUAL_REQUIRED")
        try:
            revision = self.runner.installed_revision()
        except (OSError, ValueError, LookupError) as exc:
            raise ConnectorRefusal("INSTALLED_MODEL_MISMATCH") from exc
        if revision != intent.binding.immutable_revision:
            raise ConnectorRefusal("INSTALLED_MODEL_MISMATCH")
        return rows

    def _execute(
        self, intent: mc.FitIntent, rows: tuple[Mapping[str, Any], ...], job_dir: Path
    ) -> None:
        journal = job_dir / "journal.jsonl"
        started = self.clock()
        seconds = min(intent.bound.maximum_seconds, self.profile.resource.max_seconds)
        control = _Control(job_dir, self.root, started + seconds, self.clock)
        _append(journal, {"event": "started"})
        try:
            output = self.runner.fit(rows, self.profile.resource.max_steps, seconds, control)
        except RunInterrupted as stop:
            terminal = "canceled" if stop.reason != "DEADLINE_EXCEEDED" else "failed"
            _append(journal, {"event": terminal, "reason": stop.reason})
            return
        except (OSError, ValueError, LookupError) as exc:
            reason = str(exc) if str(exc).isupper() else "WORKER_FAILED"
            _append(journal, {"event": "failed", "reason": reason[:64]})
            return
        # A stop that arrives while publishing wins: a stopped job never publishes a winner.
        stop_reason = control.stop_reason()
        if stop_reason is not None:
            terminal = "failed" if stop_reason == "DEADLINE_EXCEEDED" else "canceled"
            _append(journal, {"event": terminal, "reason": stop_reason})
            return
        artifact = self._publish(job_dir, output)
        _append(
            journal,
            {
                "event": "completed",
                "artifact_sha256": artifact,
                "seconds": max(0, int(self.clock() - started)),
                "rows": len(rows),
                "steps": self.profile.resource.max_steps,
            },
        )

    def _publish(self, job_dir: Path, output: FitOutput) -> str:
        names = sorted(output.files)
        if not names or any("/" in name or name.startswith(".") for name in names):
            raise ValueError("ARTIFACT_FILES_INVALID")
        listing = [[name, _sha(output.files[name])] for name in names]
        artifact = _sha(
            _canonical([self.profile.identity, listing, _canonical(dict(output.evidence)).decode()])
        )
        stage = Path(tempfile.mkdtemp(prefix=".artifact-", dir=job_dir))
        for name in names:
            with (stage / name).open("xb") as handle:
                handle.write(output.files[name])
                handle.flush()
                os.fsync(handle.fileno())
        receipt = {
            "schema": VERSION,
            "family_identity": self.profile.identity,
            "parent_revision": self.profile.revision,
            "files": listing,
            "evidence": dict(output.evidence),
            "artifact_sha256": artifact,
        }
        (stage / "connector-receipt.json").write_bytes(_canonical(receipt))
        stage.rename(job_dir / "artifact")
        return artifact

    def cancel(self, job: mc.JobReceipt) -> mc.JobReceipt:
        key = self._key_from_ref(job.job_ref)
        job_dir = self._job_dir(key)
        # Durable intent first; the supervisor and the publish step both honor it.
        _marker(job_dir / "CANCEL", {"requested": True})
        return self._settle_stop(job.intent, job_dir, key)

    def _settle_stop(self, intent: mc.FitIntent, job_dir: Path, key: str) -> mc.JobReceipt:
        deadline = time.monotonic() + self.cancel_wait_seconds
        while True:
            receipt = self._receipt(intent, job_dir, key)
            if receipt.state not in (mc.JobState.QUEUED, mc.JobState.RUNNING):
                return receipt
            lock = _Lock(job_dir / "lock")
            if lock.acquire():
                try:
                    _append(
                        job_dir / "journal.jsonl", {"event": "canceled", "reason": "JOB_CANCELED"}
                    )
                finally:
                    lock.release()
                continue
            if time.monotonic() >= deadline:
                return receipt
            time.sleep(0.02)

    def fence(self, scope: str) -> FenceReceipt:
        """Durably block new work, stop live jobs and block collection and replay."""
        _marker(self.root / "FENCED", {"scope": scope})
        canceled = []
        for job_dir in sorted((self.root / "jobs").iterdir()):
            journal = job_dir / "journal.jsonl"
            events = _events(journal)
            if not events or events[-1]["event"] in _TERMINAL:
                continue
            _marker(job_dir / "CANCEL", {"requested": True})
            deadline = time.monotonic() + self.cancel_wait_seconds
            while time.monotonic() < deadline:
                lock = _Lock(job_dir / "lock")
                if lock.acquire():
                    try:
                        if _events(journal)[-1]["event"] not in _TERMINAL:
                            _append(journal, {"event": "canceled", "reason": "CONNECTOR_FENCED"})
                    finally:
                        lock.release()
                    break
                time.sleep(0.02)
            canceled.append(self._job_ref(job_dir.name))
        return FenceReceipt(
            scope,
            tuple(canceled),
            "simulated:fence:" + _sha((self.root / "FENCED").read_bytes())[:32],
        )

    def collect(self, job: mc.JobReceipt) -> mc.Candidate:
        self._refuse_if_fenced()
        key = self._key_from_ref(job.job_ref)
        job_dir = self._job_dir(key)
        current = self._receipt(job.intent, job_dir, key)
        if current != job or job.state is not mc.JobState.COMPLETED:
            raise ValueError("CANDIDATE_LINEAGE_UNVERIFIED")
        manifest = self._manifest(job_dir)
        if manifest["parent_revision"] != job.intent.binding.immutable_revision:
            raise ValueError("CANDIDATE_LINEAGE_UNVERIFIED")
        return mc.Candidate(
            job.intent,
            job.job_ref,
            job.intent.binding.immutable_revision,
            manifest["artifact_sha256"],
            "simulated:manifest:"
            + _sha((job_dir / "artifact/connector-receipt.json").read_bytes())[:32],
        )

    def _manifest(self, job_dir: Path) -> dict[str, Any]:
        artifact = job_dir / "artifact"
        if artifact.is_symlink() or not artifact.is_dir():
            raise ValueError("ARTIFACT_UNAVAILABLE")
        manifest = json.loads((artifact / "connector-receipt.json").read_bytes())
        events = _events(job_dir / "journal.jsonl")
        if (
            manifest["family_identity"] != self.profile.identity
            or events[-1].get("artifact_sha256") != manifest["artifact_sha256"]
        ):
            raise ValueError("ARTIFACT_LINEAGE_MISMATCH")
        for name, digest in manifest["files"]:
            file = artifact / name
            if file.is_symlink() or not file.is_file() or _sha(file.read_bytes()) != digest:
                raise ValueError("ARTIFACT_DIGEST_MISMATCH")
        return dict(manifest)

    def predict_for_replay(
        self, candidate: mc.Candidate, inputs: tuple[Mapping[str, object], ...]
    ) -> tuple[Mapping[str, object], ...]:
        self._refuse_if_fenced()
        job_dir = self._job_dir(self._key_from_ref(candidate.job_ref))
        manifest = self._manifest(job_dir)
        if (
            manifest["artifact_sha256"] != candidate.artifact_or_serving_revision
            or candidate.parent_revision != manifest["parent_revision"]
            or self.runner.installed_revision() != candidate.parent_revision
        ):
            raise ValueError("CANDIDATE_LINEAGE_UNVERIFIED")
        return self.runner.predict(job_dir / "artifact", tuple(dict(item) for item in inputs))

    def evidence(self, job: mc.JobReceipt) -> Mapping[str, Any]:
        """Measured fit evidence recorded with the immutable artifact."""
        manifest = self._manifest(self._job_dir(self._key_from_ref(job.job_ref)))
        evidence: Mapping[str, Any] = manifest["evidence"]
        return evidence

    # ------------------------------------------------------------------ serving and rollback
    def _serving_events(self) -> list[dict[str, Any]]:
        return _events(self.root / "serving.jsonl")

    def serving(self) -> str:
        """The artifact digest currently served, or ``base`` for the unmodified pinned base."""
        stack: list[str] = []
        for event in self._serving_events():
            if event["event"] == "promote":
                stack.append(str(event["artifact_sha256"]))
            elif event["event"] == "rollback" and stack:
                stack.pop()
        return stack[-1] if stack else "base"

    def promote(self, candidate: mc.Candidate) -> None:
        self._refuse_if_fenced()
        job_dir = self._job_dir(self._key_from_ref(candidate.job_ref))
        if self._manifest(job_dir)["artifact_sha256"] != candidate.artifact_or_serving_revision:
            raise ConnectorRefusal("CANDIDATE_LINEAGE_UNVERIFIED")
        _append(
            self.root / "serving.jsonl",
            {
                "event": "promote",
                "artifact_sha256": candidate.artifact_or_serving_revision,
                "job_ref": candidate.job_ref,
            },
        )

    def rollback(self) -> str:
        """Restore the previously authorized serving target (ultimately the pinned base)."""
        if self.serving() == "base":
            raise ConnectorRefusal("NOTHING_TO_ROLL_BACK")
        _append(self.root / "serving.jsonl", {"event": "rollback"})
        return self.serving()

    def predict_base(self, inputs: tuple[Mapping[str, Any], ...]) -> tuple[Mapping[str, Any], ...]:
        """Serve the unmodified pinned base. Refuses if the installation no longer matches."""
        self._refuse_if_fenced()
        if self.runner.installed_revision() != self.profile.revision:
            raise ConnectorRefusal("INSTALLED_MODEL_MISMATCH")
        return self.runner.predict(None, inputs)

    def serve(self, inputs: tuple[Mapping[str, Any], ...]) -> tuple[Mapping[str, Any], ...]:
        self._refuse_if_fenced()
        current = self.serving()
        if current == "base":
            return self.runner.predict(None, inputs)
        for event in reversed(self._serving_events()):
            if event["event"] == "promote" and event["artifact_sha256"] == current:
                job_dir = self._job_dir(self._key_from_ref(str(event["job_ref"])))
                self._manifest(job_dir)
                return self.runner.predict(job_dir / "artifact", inputs)
        raise ConnectorRefusal("SERVING_TARGET_UNAVAILABLE")

    # ------------------------------------------------------------------ usage
    def usage(self, job: mc.JobReceipt) -> Usage | None:
        try:
            current = self.status(job)
            events = _events(self._job_dir(self._key_from_ref(job.job_ref)) / "journal.jsonl")
        except (OSError, ValueError, LookupError):
            return None
        if current.state not in (mc.JobState.COMPLETED, mc.JobState.FAILED, mc.JobState.CANCELED):
            return None
        done = next((e for e in events if e["event"] == "completed"), None)
        return Usage(
            job.job_ref,
            job.intent.bound.currency,
            0,
            int(done["seconds"]) if done else 0,
            int(done["rows"]) if done else 0,
            int(done["steps"]) if done else 0,
            UsageSource.MEASURED,
        )


class _SubmitLock:
    def __init__(self, root: Path) -> None:
        self.descriptor = -1
        self.path = root / "submit.lock"

    def __enter__(self) -> None:
        self.descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        fcntl.flock(self.descriptor, fcntl.LOCK_EX)

    def __exit__(self, *_exc: object) -> None:
        os.close(self.descriptor)


class SubprocessFamilyRunner:
    """Run a pinned worker script in a separate interpreter, one JSON request per process."""

    def __init__(
        self,
        profile: ModelFamilyProfile,
        model_dir: Path,
        worker: Path,
        profile_path: Path,
        *,
        python: str | None = None,
        request_extras: Mapping[str, Any] | None = None,
    ) -> None:
        self.request_extras = dict(request_extras or {})
        self.profile = profile
        self.model_dir = model_dir
        self.worker = worker
        self.profile_path = profile_path
        self.python = python or sys.executable

    def installed_revision(self) -> str:
        try:
            return verify_model_directory(self.profile, self.model_dir)
        except FamilyError as exc:
            raise ValueError(str(exc)) from exc

    def _call(
        self, request: dict[str, Any], seconds: int, control: RunControl | None
    ) -> dict[str, Any]:
        request["model_dir"] = str(self.model_dir)
        request.update(self.request_extras)
        env = {"LANG": "C.UTF-8", "PYTHONHASHSEED": "0"}
        deadline = time.monotonic() + seconds
        with subprocess.Popen(
            [self.python, "-I", "-B", str(self.worker), str(self.profile_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
        ) as proc:
            pending: bytes | None = _canonical(request)
            try:
                while True:
                    reason = control.stop_reason() if control is not None else None
                    if reason is not None:
                        raise RunInterrupted(reason)
                    if time.monotonic() >= deadline:
                        raise RunInterrupted("DEADLINE_EXCEEDED")
                    try:
                        answer, _ = proc.communicate(pending, timeout=0.05)
                    except subprocess.TimeoutExpired:
                        pending = None
                        continue
                    break
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
        try:
            result = json.loads(answer)
        except ValueError as exc:
            raise ValueError("WORKER_INTERRUPTED") from exc
        if proc.returncode or not isinstance(result, dict) or "error" in result:
            reason = str(result.get("error", "")) if isinstance(result, dict) else ""
            raise ValueError(reason if reason.isupper() else "WORKER_FAILED")
        return result

    def fit(
        self, rows: tuple[Mapping[str, Any], ...], steps: int, seconds: int, control: RunControl
    ) -> FitOutput:
        result = self._call(
            {"operation": "fit", "rows": [dict(row) for row in rows], "steps": steps},
            seconds,
            control,
        )
        adapter = _canonical(result["adapter"])
        return FitOutput({"adapter.json": adapter}, dict(result["evidence"]))

    def predict(
        self, artifact_dir: Path | None, inputs: tuple[Mapping[str, Any], ...]
    ) -> tuple[Mapping[str, Any], ...]:
        adapter = (
            None
            if artifact_dir is None
            else json.loads((artifact_dir / "adapter.json").read_bytes())
        )
        result = self._call(
            {"operation": "predict", "inputs": [dict(i) for i in inputs], "adapter": adapter},
            self.profile.resource.max_seconds,
            None,
        )
        return tuple(result["predictions"])
