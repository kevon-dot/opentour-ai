"""Run the registered EVE task on the customer worker through its normal entry (OPE-563).

``python -m opentour_cell.pilot.eve_worker run --config <private config>`` registers the typed
EVE question-and-evidence task (OPE-506 registry), freezes the protected replay, replays the
baseline and simple-repair arms, plans the guarded fit and dispatches it through the OPE-418
reservation and job contract (``Preflight.submit_reserved_fit``), collects and lineage-checks the
candidate, replays it, builds the versioned result and settles it in the journal. The result,
the frozen manifest and the fit record (job and candidate, never a development row) are written
to the worker's private outbox for the separate evaluator.

``python -m opentour_cell.pilot.eve_worker stage --config <private config> --verdict <file>``
asks the one staging gate (``eve_staging``) whether the settled result may be offered for Slack
review. It requires the worker rebuild, the aggregator and the independent evaluator's verdict.
Stageable is never a release or fit authorization.

Zero fit, by construction: a registration without the train capability, a target that is not
observed trainable for its exact revision, a protected case or family in the development set,
a fit payload carrying a protected answer, or any refused dispatch leaves the backend's job count
unchanged and is reported as ``zero_fit`` with its fixed reason. A dispatched job that is not
complete is never treated as a candidate (a pending request is not an admitted job).

Configuration. The private config (owner-only regular file in a 0700 directory) names the tenant,
the registration, the benchmark, the worker's own scorer stand-in, each arm's pinned settings,
the current grants (re-read at every grant check, so removing one withdraws it mid-run), the fit
intent and its bound, the inputs file, the commitment key directory (never under the worker
directory) and the backend directory. Grants and credentials are references only; no secret value
is ever read or written here.

Reservation. EVE has no standing learning mandate yet (``learning_mandate`` serves the
open-weight classification route only), so the reservation here is a durable, write-once record
per idempotency key held under an exclusive lock while the dispatch runs; a second reservation
for the same key with a different intent is refused. A real EVE mandate is a recorded gap.

Backend. The only backend is the generated reference backend (``eve_reference_backend``), which
refuses any destination that is not ``simulated:``. A real EVE serving endpoint and trainer
(OPE-558, OPE-561, OPE-565) are separate evidence and are not claimed here.

Output is bounded: states, digests, counts and fixed reasons, never a case, answer or prediction.
Packaging this command into the ``opentour-slack.pyz`` kit is the kit owner's change.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Never

from opentour_cell.pilot import model_connector as mc
from opentour_cell.pilot.eve_reference_backend import ReferenceBackendError, ReferenceEveBackend
from opentour_cell.pilot.eve_replay import (
    AnswerKey,
    ArmRecord,
    ArmSpec,
    CandidateInference,
    CommitmentKeyStore,
    EveReplayWorker,
    Expectation,
    ProtectedAnswer,
    ReplayJournal,
    ReplayManifest,
    Settlement,
    build_result,
    freeze,
    plan_guarded_fit,
)
from opentour_cell.pilot.eve_scorer import DeterministicScorer
from opentour_cell.pilot.eve_staging import stage_decision
from opentour_cell.pilot.eve_task import (
    Capability,
    DevelopmentExample,
    EveCase,
    EveRefusal,
    EveRegistry,
    EveTaskRegistration,
    FitPlan,
    Partition,
    Passage,
    binding_for,
    canonical,
    fit_record,
    intent_digest,
    parse_registration,
)

CONFIG = "opentour.pilot.eve-worker-config.v1"
INPUTS = "opentour.pilot.eve-worker-inputs.v1"
RESULT = "opentour.pilot.eve-worker-result.v1"
REGISTRY = "eve-registry.json"
JOURNAL = "replay-journal.jsonl"
KEYS = "commitment.keys"
OUTBOX = "outbox"
RESERVATIONS = "fit-reservations"
BASELINE_ACTOR = "baseline_inference"
TRAINING_ACTOR = "training"
_MAX_BYTES = 4 << 20
_CONFIG_KEYS = frozenset(
    {
        "schema_version",
        "tenant",
        "region",
        "credential_ref",
        "available_credentials",
        "registration",
        "benchmark_id",
        "benchmark_revision",
        "scorer",
        "arms",
        "grants",
        "fit",
        "inputs",
        "keys_dir",
        "backend_dir",
    }
)
_GRANT_KEYS = frozenset(
    {
        "actor",
        "purpose",
        "revision",
        "expires_at",
        "retention_until",
        "permitted_fields",
        "fit_bound",
        "benchmark_revisions",
    }
)
_FIXED_REASON = re.compile(r"[a-z][a-z0-9_]{0,95}")


def fixed_reason(error: BaseException, fallback: str = "eve_worker_unavailable") -> str:
    """The refusal code to print: a fixed lowercase reason from a refusal type that never carries
    case text, answers or credentials, or the fallback for anything else (a raw exception's text
    is never printed)."""
    if isinstance(error, EveRefusal | mc.CapabilityError | ReferenceBackendError):
        reason = str(error)
        if _FIXED_REASON.fullmatch(reason):
            return reason
    return fallback


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for name, value in pairs:
        if name in found:
            raise ValueError("duplicate JSON member")
        found[name] = value
    return found


def _read_private(path: Path) -> bytes | None:
    """A regular file owned by this uid, 0600 at most, opened without following a link."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise EveRefusal("invalid_worker_configuration") from None
        raise
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise EveRefusal("invalid_worker_configuration")
        content = handle.read(_MAX_BYTES + 1)
    if len(content) > _MAX_BYTES:
        raise EveRefusal("invalid_worker_configuration")
    return content


def _load(path: Path, reason: str) -> Any:
    raw = _read_private(path)
    if raw is None:
        raise EveRefusal(reason)
    try:
        return json.loads(raw, object_pairs_hook=_unique)
    except ValueError:
        raise EveRefusal(reason) from None


def _private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=False, exist_ok=True)
    info = os.lstat(path)
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise EveRefusal("invalid_worker_configuration")


def _write_private(path: Path, value: Any) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".pending-")
    with os.fdopen(fd, "wb") as handle:
        handle.write(canonical(value))
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _absolute(value: Any) -> Path:
    if type(value) is not str or not value or not Path(value).is_absolute():
        raise EveRefusal("invalid_worker_configuration")
    return Path(value)


def _texts(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(type(v) is str and v for v in value):
        raise EveRefusal("invalid_worker_configuration")
    return tuple(value)


def _bound(value: Any) -> mc.FitBound:
    if (
        not isinstance(value, list)
        or len(value) != 5
        or type(value[0]) is not str
        or not all(type(v) is int for v in value[1:])
    ):
        raise EveRefusal("invalid_worker_configuration")
    return mc.FitBound(*value)


@dataclass(frozen=True)
class WorkerInputs:
    """The worker's protected replay set, its answer key and the verified development rows."""

    cases: tuple[EveCase, ...]
    key: AnswerKey
    development: tuple[DevelopmentExample, ...]


_CASE_KEYS = frozenset({"case_id", "family_id", "question", "context"})


def _case(item: Any, extra: frozenset[str] = frozenset()) -> EveCase:
    if not isinstance(item, dict) or set(item) != _CASE_KEYS | extra:
        raise ValueError("case")
    values = (item["case_id"], item["family_id"], item["question"])
    passages = tuple(Passage(*p) for p in item["context"])
    if not all(type(v) is str and v for v in values) or not all(
        type(p.passage_id) is str and type(p.text) is str for p in passages
    ):
        raise ValueError("case")
    return EveCase(*values, passages)


def load_inputs(path: Path) -> WorkerInputs:
    raw = _load(path, "worker_inputs_invalid")
    try:
        if (
            not isinstance(raw, dict)
            or set(raw) != {"schema_version", "cases", "answers", "development"}
            or raw["schema_version"] != INPUTS
        ):
            raise ValueError("inputs")
        cases = tuple(_case(item) for item in raw["cases"])
        entries = {}
        for case_id, entry in raw["answers"].items():
            if set(entry) != {"expectation", "answer", "required_citations"}:
                raise ValueError("answer")
            required = entry["required_citations"]
            if type(entry["answer"]) is not str or not all(type(c) is str for c in required):
                raise ValueError("answer")
            entries[case_id] = ProtectedAnswer(
                Expectation(entry["expectation"]), entry["answer"], frozenset(required)
            )
        development = []
        for item in raw["development"]:
            if type(item.get("answer")) is not str or not item["answer"]:
                raise ValueError("development")
            case = _case(item, frozenset({"answer"}))
            development.append(DevelopmentExample(case, item["answer"]))
        return WorkerInputs(cases, AnswerKey(entries), tuple(development))
    except (KeyError, TypeError, ValueError, AttributeError):
        raise EveRefusal("worker_inputs_invalid") from None


class WorkerConfig:
    """The worker's private configuration. Grants and credentials are re-read on every check."""

    def __init__(self, path: Path) -> None:
        if not path.is_absolute() or path.parent.resolve() != path.parent:
            raise EveRefusal("invalid_worker_configuration")
        self.path = path
        self.directory = path.parent
        _private_dir(self.directory)
        raw = self._read()
        self.registration: EveTaskRegistration = parse_registration(raw["registration"])
        self.tenant: str = raw["tenant"]
        self.region: str = raw["region"]
        self.credential_ref: str = raw["credential_ref"]
        self.benchmark_id: str = raw["benchmark_id"]
        self.benchmark_revision: str = raw["benchmark_revision"]
        scorer = _texts(raw["scorer"])
        if len(scorer) != 3:
            raise EveRefusal("invalid_worker_configuration")
        scorer_id, version, rubric = scorer
        self.scorer = DeterministicScorer(version, rubric, scorer_id)
        arms = raw["arms"]
        if not isinstance(arms, dict) or set(arms) != {"baseline", "simple_repair", "candidate"}:
            raise EveRefusal("invalid_worker_configuration")
        self.arm_settings = {name: _texts(value) for name, value in arms.items()}
        if any(len(v) != 3 for v in self.arm_settings.values()):
            raise EveRefusal("invalid_worker_configuration")
        fit = raw["fit"]
        fit_keys = {"grant_revision", "idempotency_key", "bound"}
        if fit is not None and (
            not isinstance(fit, dict)
            or set(fit) != fit_keys
            or not all(
                type(fit[k]) is str and fit[k] for k in ("grant_revision", "idempotency_key")
            )
        ):
            raise EveRefusal("invalid_worker_configuration")
        self.fit = fit
        self.inputs = _absolute(raw["inputs"])
        self.keys_dir = _absolute(raw["keys_dir"])
        self.backend_dir = _absolute(raw["backend_dir"])
        self.binding = binding_for(
            self.registration,
            tenant=self.tenant,
            route=mc.Route.LOCAL,
            region=self.region,
            credential_ref=self.credential_ref,
        )

    def _read(self) -> dict[str, Any]:
        raw = _load(self.path, "invalid_worker_configuration")
        if not isinstance(raw, dict) or set(raw) != _CONFIG_KEYS or raw["schema_version"] != CONFIG:
            raise EveRefusal("invalid_worker_configuration")
        for name in ("tenant", "region", "credential_ref", "benchmark_id", "benchmark_revision"):
            if type(raw[name]) is not str or not raw[name]:
                raise EveRefusal("invalid_worker_configuration")
        return raw

    def grant(self, actor: str, binding: mc.ModelBinding, purpose: mc.Purpose) -> mc.Grant | None:
        """The current grant for ``actor`` and ``purpose``, read from the config at this moment."""
        try:
            grants = self._read()["grants"]
        except EveRefusal:
            return None
        for item in grants if isinstance(grants, list) else ():
            if not isinstance(item, dict) or set(item) != _GRANT_KEYS:
                continue
            if item["actor"] != actor or item["purpose"] != purpose.value:
                continue
            if (
                type(item["revision"]) is not str
                or not item["revision"]
                or type(item["expires_at"]) is not int
                or type(item["retention_until"]) is not int
            ):
                return None  # a malformed grant is no grant
            revisions = item["benchmark_revisions"]
            try:
                return mc.Grant(
                    actor,
                    binding,
                    purpose,
                    item["revision"],
                    item["expires_at"],
                    frozenset(_texts(item["permitted_fields"])),
                    item["retention_until"],
                    _bound(item["fit_bound"]) if item["fit_bound"] is not None else None,
                    frozenset(_texts(revisions)) if revisions else frozenset(),
                )
            except EveRefusal:
                return None
        return None

    def is_available(self, credential_ref: str) -> bool:
        """Whether the credential reference is configured. No secret value is read."""
        try:
            return credential_ref in _texts(self._read()["available_credentials"])
        except EveRefusal:
            return False


class FitReservations:
    """A durable, write-once reservation per fit idempotency key, held under an exclusive lock."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    @contextmanager
    def guard(self, intent: mc.FitIntent) -> Iterator[None]:
        _private_dir(self.directory)
        fd = os.open(self.directory / "lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            name = hashlib.sha256(intent.idempotency_key.encode()).hexdigest()
            path = self.directory / f"{name}.json"
            record = {
                "intent": intent_digest(intent),
                "bound": [
                    intent.bound.currency,
                    intent.bound.maximum_minor_units,
                    intent.bound.maximum_seconds,
                    intent.bound.maximum_rows,
                    intent.bound.maximum_steps,
                ],
            }
            existing = _read_private(path)
            if existing is None:
                _write_private(path, {**record, "reserved_at": int(time.time())})
            elif {k: v for k, v in json.loads(existing).items() if k != "reserved_at"} != record:
                raise EveRefusal("fit_reservation_conflict")
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


@dataclass
class _Run:
    config: WorkerConfig
    inputs: WorkerInputs
    registry: EveRegistry
    backend: ReferenceEveBackend
    preflight: mc.Preflight
    journal: ReplayJournal
    keys: CommitmentKeyStore
    worker: EveReplayWorker
    manifest: ReplayManifest

    @property
    def outbox(self) -> Path:
        return self.config.directory / OUTBOX


def _open(config_path: Path, *, clock: Callable[[], float] = time.time) -> _Run:
    config = WorkerConfig(config_path)
    inputs = load_inputs(config.inputs)
    registry = EveRegistry(config.directory / REGISTRY)
    registry.register(config.registration)  # same identity again is a no-op; another conflicts
    backend = ReferenceEveBackend(config.backend_dir, clock=clock)
    preflight = mc.Preflight(backend, config.grant, clock=clock)
    journal = ReplayJournal(config.directory / JOURNAL)
    keys = CommitmentKeyStore(config.keys_dir / KEYS)
    worker = EveReplayWorker(
        registry=registry, preflight=preflight, credentials=config, journal=journal, keys=keys
    )
    manifest = freeze(
        benchmark_id=config.benchmark_id,
        benchmark_revision=config.benchmark_revision,
        registration=registry.require(config.registration.model_ref),
        scorer=config.scorer,
        key=inputs.key,
        cases=inputs.cases,
    )
    return _Run(config, inputs, registry, backend, preflight, journal, keys, worker, manifest)


def _spec(run: _Run, arm_id: str, served: str) -> ArmSpec:
    snapshot, retrieval, context = run.config.arm_settings[arm_id]
    decoding = run.config.registration.decoding.digest
    return ArmSpec(arm_id, served, snapshot, retrieval, context, decoding)


def _replay(run: _Run, spec: ArmSpec, inference: Any) -> ArmRecord:
    return run.worker.replay_arm(
        actor=BASELINE_ACTOR,
        binding=run.config.binding,
        manifest=run.manifest,
        arm=spec,
        cases=run.inputs.cases,
        scorer=run.config.scorer,
        key=run.inputs.key,
        inference=inference,
    )


def _plan(run: _Run) -> FitPlan:
    """The guarded fit plan, or a refusal naming why no fit may start."""
    registration, fit = run.config.registration, run.config.fit
    if Capability.TRAIN not in registration.capabilities or fit is None:
        raise EveRefusal("fit_unavailable_inference_only")
    try:
        capability = run.preflight.discover(TRAINING_ACTOR, run.config.binding, mc.Purpose.TRAINING)
    except mc.CapabilityError as error:
        raise EveRefusal(str(error)) from None
    partition = Partition(
        development=run.inputs.development,
        protected_case_ids=frozenset(c.case_id for c in run.inputs.cases),
        protected_family_ids=frozenset(c.family_id for c in run.inputs.cases),
    )
    return plan_guarded_fit(
        key=run.inputs.key,
        manifest=run.manifest,
        protected_cases=run.inputs.cases,
        registration=registration,
        binding=run.config.binding,
        capability=capability,
        partition=partition,
        grant_revision=fit["grant_revision"],
        benchmark_revision=run.config.benchmark_revision,
        idempotency_key=fit["idempotency_key"],
        bound=_bound(fit["bound"]),
    )


def _fit(run: _Run) -> tuple[dict[str, Any], tuple[mc.JobReceipt, mc.Candidate] | None]:
    """Dispatch the one authorized fit, or report a zero fit with its reason."""
    before = run.backend.executions()
    try:
        plan = _plan(run)
    except EveRefusal as refusal:
        return {"state": "zero_fit", "reason": str(refusal), "executions": 0}, None
    reservations = FitReservations(run.config.directory / RESERVATIONS)
    try:
        receipt = run.preflight.submit_reserved_fit(
            TRAINING_ACTOR,
            plan.intent,
            plan.rows,
            reservation_guard=lambda: reservations.guard(plan.intent),
        )
    except (mc.CapabilityError, EveRefusal, ReferenceBackendError) as error:
        state = {"state": "zero_fit", "reason": str(error)}
        return {**state, "executions": run.backend.executions() - before}, None
    executions = run.backend.executions() - before
    if receipt.state is not mc.JobState.COMPLETED:
        # A pending or unknown job is not a candidate; reconcile it later, never assume it.
        state = {"state": receipt.state.value, "reason": "fit_not_completed"}
        return {**state, "executions": executions}, None
    try:
        candidate = run.preflight.collect_candidate(TRAINING_ACTOR, receipt)
    except mc.CapabilityError as error:
        return {"state": "uncollected", "reason": str(error), "executions": executions}, None
    return {"state": "collected", "reason": None, "executions": executions}, (receipt, candidate)


def execute_run(config_path: Path, *, clock: Callable[[], float] = time.time) -> dict[str, Any]:
    run = _open(config_path, clock=clock)
    _private_dir(run.outbox)
    manifest_path = run.outbox / "manifest.json"
    existing = _read_private(manifest_path)
    if existing is not None and json.loads(existing) != run.manifest.document():
        raise EveRefusal("manifest_changed")
    _write_private(manifest_path, run.manifest.document())
    base = run.config.registration.base_revision
    inference = run.backend.base_inference()
    arms = {
        arm_id: _replay(run, _spec(run, arm_id, base), inference)
        for arm_id in ("baseline", "simple_repair")
    }
    fit, collected = _fit(run)
    if collected is not None:
        receipt, candidate = collected
        served = CandidateInference(
            run.preflight, TRAINING_ACTOR, receipt, candidate, run.config.registration
        )
        arms["candidate"] = _replay(
            run, _spec(run, "candidate", candidate.artifact_or_serving_revision), served
        )
        _write_private(run.outbox / "fit.json", fit_record(receipt, candidate))
    result = build_result(
        run.manifest, arms, scorer=run.config.scorer, journal=run.journal, key=run.inputs.key
    )
    settlement = run.journal.settle(run.manifest.digest, result["result_digest"])
    _write_private(run.outbox / "result.json", result)
    body = result["body"]
    return {
        "schema_version": RESULT,
        "status": "completed",
        "manifest_digest": run.manifest.digest,
        "result_digest": result["result_digest"],
        "settlement": settlement.value if isinstance(settlement, Settlement) else None,
        "arms": sorted(body["arms"]),
        "improvement_claim": body["improvement_claim"],
        "fit": fit,
        "fit_authorized": body["fit_authorized"],
        "release_authorized": body["release_authorized"],
    }


def execute_stage(config_path: Path, verdict_path: Path) -> dict[str, Any]:
    """Ask the staging gate about the settled result, with the evaluator's exported verdict."""
    run = _open(config_path)
    stored = _read_private(run.outbox / "manifest.json")
    if stored is None or json.loads(stored) != run.manifest.document():
        raise EveRefusal("manifest_changed")
    result = _load(run.outbox / "result.json", "worker_outbox_unavailable")
    try:
        verdict = _load(verdict_path, "verdict_unreadable") if verdict_path.exists() else None
    except EveRefusal:
        verdict = None
    supplied = result.get("result_digest") if isinstance(result, dict) else None
    decision = stage_decision(
        result,
        recomputed_digest=supplied if isinstance(supplied, str) else "",
        manifest=run.manifest,
        journal=run.journal,
        scorer=run.config.scorer,
        key=run.inputs.key,
        verdict=verdict,
    )
    return {
        "schema_version": RESULT,
        "status": "stageable" if decision.stageable else "held",
        "manifest_digest": run.manifest.digest,
        "result_digest": supplied,
        "reasons": list(decision.reasons),
        "aggregate_mismatches": list(decision.aggregate_mismatches),
        "release_authorized": False,
    }


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise EveRefusal("invalid_worker_arguments")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _Parser(description="Run the registered EVE task on the customer worker.")
    parser.add_argument("command", choices=("run", "stage"))
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--verdict", type=Path)
    try:
        args = parser.parse_args(argv)
        if args.command == "run":
            if args.verdict is not None:
                raise EveRefusal("invalid_worker_arguments")
            result = execute_run(args.config)
            code = 0
        else:
            if args.verdict is None or not args.verdict.is_absolute():
                raise EveRefusal("invalid_worker_arguments")
            result = execute_stage(args.config, args.verdict)
            code = 0 if result["status"] == "stageable" else 2
    except (EveRefusal, mc.CapabilityError, OSError, ValueError, TypeError, LookupError) as error:
        result = {"schema_version": RESULT, "status": "held", "reason": fixed_reason(error)}
        code = 2
    print(canonical(result).decode(), flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
