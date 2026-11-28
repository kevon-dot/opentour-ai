"""Generated reference EVE backend: a scripted stand-in model and an isolated trainer transport.

This is the fixture-tier backend the EVE worker entry (``eve_worker``) and the separate evaluator
(``eve_evaluator``) are qualified against, in the same spirit as ``reference_customer_trainer``.
It implements the OPE-506 ``Connector`` protocol for the EVE task and a base-model inference for
the baseline and simple-repair arms, over one state directory:

* ``behaviour.json``, provisioned beside it as generated sentinel data, says what the served
  model outputs for each generated case: the base model under each context revision, and the
  adapter a fit produces. It is a script, not a model: nothing here learns.
* A fit is a real job record in the state directory, submitted once per idempotency key and
  persisted, so a restarted backend, a second process or the evaluator sees the same job.
  ``fit_state`` says whether a submitted job reports completed or still running. The adapter's
  artifact revision is a digest of its parent revision, the development payload digest and the
  adapter behaviour, so a different payload or a different adapter is a different artifact. The
  payload the trainer received is kept beside the job, as a trainer would keep it.
* Every payload the backend receives (fit payloads and replay requests) is appended to
  ``received.jsonl``, so a test can check that no protected answer ever reached the model side.

It refuses any binding whose destinations are not ``simulated:`` and any model reference that is
not a generated sentinel, so it cannot stand in for a real endpoint. It never reads an answer key.

Simulated: everything here. Not proven: no EVE checkpoint, tokenizer, GPU, trainer or provider is
involved; passing against this backend is ``generated`` software evidence only.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
import tempfile
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from opentour_cell.pilot import model_connector as mc
from opentour_cell.pilot.eve_replay import ArmOutput, ArmSpec, ReplayRequest
from opentour_cell.pilot.eve_task import (
    canonical,
    digest,
    intent_digest,
    serialize_development,
)

BEHAVIOUR = "opentour.pilot.eve-reference-behaviour.v1"
SOURCE_REF = "simulated:eve-reference-observation"
EVIDENCE_REF = "simulated:eve-reference-fit-evidence"
REPLAY_REF = "simulated:eve-reference-replay-evidence"
_OBSERVATION_SECONDS = 300
_BEHAVIOUR_KEYS = frozenset(
    {
        "schema_version",
        "base_revision",
        "training_supported",
        "fit_state",
        "estimate",
        "base",
        "adapter",
    }
)
_FIT_STATES = frozenset({mc.JobState.COMPLETED.value, mc.JobState.RUNNING.value})


class ReferenceBackendError(LookupError):
    """A fixed reason from the reference backend; never carries case text."""


def _private_bytes(path: Path) -> bytes | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise ReferenceBackendError("reference_backend_not_private") from None
        raise
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise ReferenceBackendError("reference_backend_not_private")
        return handle.read()


def _write_once(path: Path, data: bytes) -> bool:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".pending-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        try:
            os.link(tmp, path)
        except FileExistsError:
            return False
        return True
    finally:
        os.unlink(tmp)


def _output(raw: Any) -> dict[str, Any] | None:
    """One scripted output, validated when the behaviour is loaded."""
    if raw is None:
        return None
    if (
        not isinstance(raw, dict)
        or set(raw) != {"answer", "citations", "abstained"}
        or (raw["answer"] is not None and type(raw["answer"]) is not str)
        or not isinstance(raw["citations"], list)
        or not all(type(c) is str for c in raw["citations"])
        or type(raw["abstained"]) is not bool
    ):
        raise ReferenceBackendError("reference_behaviour_invalid")
    try:  # the same boundary rule every arm output meets, checked once at load
        ArmOutput("generated-probe", raw["answer"], tuple(raw["citations"]), raw["abstained"])
    except (TypeError, ValueError):
        raise ReferenceBackendError("reference_behaviour_invalid") from None
    return {
        "answer": raw["answer"],
        "citations": list(raw["citations"]),
        "abstained": raw["abstained"],
    }


def _script(raw: Any) -> dict[str, dict[str, Any] | None]:
    if not isinstance(raw, dict) or not all(type(k) is str and k for k in raw):
        raise ReferenceBackendError("reference_behaviour_invalid")
    return {case_id: _output(value) for case_id, value in raw.items()}


class ReferenceEveBackend:
    """The generated reference backend: connector, trainer transport and base inference."""

    def __init__(self, root: Path, *, clock: Callable[[], float] = time.time) -> None:
        if not root.is_absolute():
            raise ReferenceBackendError("reference_backend_unavailable")
        self.root = root
        self.clock = clock
        raw = _private_bytes(root / "behaviour.json")
        if raw is None:
            raise ReferenceBackendError("reference_backend_unavailable")
        try:
            behaviour = json.loads(raw)
        except ValueError:
            raise ReferenceBackendError("reference_behaviour_invalid") from None
        if (
            not isinstance(behaviour, dict)
            or set(behaviour) != _BEHAVIOUR_KEYS
            or behaviour["schema_version"] != BEHAVIOUR
            or type(behaviour["base_revision"]) is not str
            or type(behaviour["training_supported"]) is not bool
            or behaviour["fit_state"] not in _FIT_STATES
            or not isinstance(behaviour["base"], dict)
        ):
            raise ReferenceBackendError("reference_behaviour_invalid")
        estimate = behaviour["estimate"]
        if (
            not isinstance(estimate, list)
            or len(estimate) != 5
            or type(estimate[0]) is not str
            or not all(type(v) is int for v in estimate[1:])
        ):
            raise ReferenceBackendError("reference_behaviour_invalid")
        self.base_revision: str = behaviour["base_revision"]
        self.training_supported: bool = behaviour["training_supported"]
        self.fit_state = mc.JobState(behaviour["fit_state"])  # what a submitted job reports
        self._estimate = mc.FitBound(*estimate)
        self._base = {ctx: _script(script) for ctx, script in behaviour["base"].items()}
        self._adapter = _script(behaviour["adapter"])
        self._adapter_digest = digest(
            {k: v for k, v in sorted(self._adapter.items())}  # the scripted adapter, by content
        )
        for sub in ("jobs", "payloads"):
            (root / sub).mkdir(mode=0o700, exist_ok=True)

    # ---- bookkeeping
    def _receive(self, kind: str, payload: object) -> None:
        line = canonical({"kind": kind, "payload": payload}) + b"\n"
        fd = os.open(self.root / "received.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "ab") as handle:
            handle.write(line)

    @staticmethod
    def _require_simulated(binding: mc.ModelBinding) -> None:
        if not binding.account_or_host.startswith("simulated:") or not binding.model_ref.startswith(
            "generated"
        ):
            raise ReferenceBackendError("reference_backend_refuses_real_destination")

    def _job_path(self, idempotency_key: str) -> Path:
        name = hashlib.sha256(idempotency_key.encode()).hexdigest()
        return self.root / "jobs" / f"{name}.json"

    def _job(self, intent: mc.FitIntent) -> dict[str, Any] | None:
        raw = _private_bytes(self._job_path(intent.idempotency_key))
        if raw is None:
            return None
        record = json.loads(raw)
        if record.get("intent") != intent_digest(intent):
            raise ReferenceBackendError("reference_job_intent_conflict")
        return dict(record)

    def executions(self) -> int:
        """How many fits this backend has actually run."""
        return len(list((self.root / "jobs").glob("*.json")))

    def artifact_revision(self, intent: mc.FitIntent) -> str:
        material = [
            intent.binding.immutable_revision,
            intent.development_digest,
            self._adapter_digest,
        ]
        return "generated-eve-adapter-" + digest(material)[7:47]

    # ---- the OPE-506 connector
    def observe(self, binding: mc.ModelBinding, purpose: mc.Purpose) -> mc.Observation:
        self._require_simulated(binding)
        now = int(self.clock())
        return mc.Observation(
            binding,
            SOURCE_REF,
            now,
            now + _OBSERVATION_SECONDS,
            self.base_revision,
            True,
            self.training_supported,
            True,
            True,
            True,
            False,
            {},
        )

    def serialize_development(
        self, intent: mc.FitIntent, rows: tuple[Mapping[str, object], ...]
    ) -> bytes:
        return serialize_development(rows)

    def estimate(self, intent: mc.FitIntent) -> mc.FitBound:
        return self._estimate

    def submit(self, intent: mc.FitIntent, development: bytes) -> mc.JobReceipt:
        self._require_simulated(intent.binding)
        if not self.training_supported:
            raise ReferenceBackendError("reference_training_unsupported")
        existing = self._job(intent)
        if existing is None:
            if hashlib.sha256(development).hexdigest() != intent.development_digest:
                raise ReferenceBackendError("reference_payload_digest_mismatch")
            self._receive("fit_payload", development.decode())
            name = self._job_path(intent.idempotency_key).stem
            _write_once(self.root / "payloads" / f"{name}.json", development)
            record = {
                "intent": intent_digest(intent),
                "state": self.fit_state.value,
                "job_ref": f"simulated:eve-job-{name[:16]}",
                "evidence_ref": EVIDENCE_REF,
                "artifact": self.artifact_revision(intent),
            }
            _write_once(self._job_path(intent.idempotency_key), canonical(record))
            existing = self._job(intent)
            if existing is None:
                raise ReferenceBackendError("reference_backend_unavailable")
        return mc.JobReceipt(
            intent, mc.JobState(existing["state"]), existing["job_ref"], existing["evidence_ref"]
        )

    def lookup(self, intent: mc.FitIntent) -> mc.JobReceipt | None:
        record = self._job(intent)
        if record is None:
            return None
        return mc.JobReceipt(
            intent, mc.JobState(record["state"]), record["job_ref"], record["evidence_ref"]
        )

    def status(self, job: mc.JobReceipt) -> mc.JobReceipt:
        found = self.lookup(job.intent)
        if found is None:
            raise ReferenceBackendError("reference_job_unknown")
        return found

    def cancel(self, job: mc.JobReceipt) -> mc.JobReceipt:
        return self.status(job)  # a completed generated job has nothing left to stop

    def collect(self, job: mc.JobReceipt) -> mc.Candidate:
        record = self._job(job.intent)
        if record is None or record["job_ref"] != job.job_ref:
            raise ReferenceBackendError("reference_job_unknown")
        return mc.Candidate(
            job.intent,
            job.job_ref,
            job.intent.binding.immutable_revision,
            record["artifact"],
            REPLAY_REF,
        )

    def predict_for_replay(
        self, candidate: mc.Candidate, inputs: tuple[Mapping[str, object], ...]
    ) -> tuple[Mapping[str, object], ...]:
        record = self._job(candidate.intent)
        if (
            record is None
            or record["job_ref"] != candidate.job_ref
            or record["artifact"] != candidate.artifact_or_serving_revision
            or self.artifact_revision(candidate.intent) != candidate.artifact_or_serving_revision
        ):
            raise ReferenceBackendError("reference_candidate_unknown")
        self._receive("candidate_replay", [dict(i) for i in inputs])
        rows = []
        for item in inputs:
            out = self._adapter.get(str(item.get("case_id")))
            if out is not None:
                rows.append({"case_id": item["case_id"], **out})
        return tuple(rows)

    # ---- base-model inference for the baseline and simple-repair arms
    def base_inference(self) -> ReferenceBaseInference:
        return ReferenceBaseInference(self)

    def _base_outputs(
        self, arm: ArmSpec, requests: tuple[ReplayRequest, ...]
    ) -> tuple[ArmOutput, ...]:
        script = self._base.get(arm.context_revision)
        if script is None:
            raise OSError("no served behaviour for this context revision")
        self._receive("base_replay", [r.as_mapping() for r in requests])
        outputs = []
        for request in requests:
            out = script.get(request.case_id)
            if out is not None:
                outputs.append(
                    ArmOutput(
                        request.case_id, out["answer"], tuple(out["citations"]), out["abstained"]
                    )
                )
        return tuple(outputs)


class ReferenceBaseInference:
    """The reference backend's base model, serving the registered base revision."""

    def __init__(self, backend: ReferenceEveBackend) -> None:
        self._backend = backend

    @property
    def served_revision(self) -> str:
        return self._backend.base_revision

    def generate(self, arm: ArmSpec, requests: tuple[ReplayRequest, ...]) -> tuple[ArmOutput, ...]:
        return self._backend._base_outputs(arm, requests)
