"""EVE registration and protected-replay scoring, over generated sentinel data only.

Acceptance IDs below are this implementation's own (the ticket has no registered IDs):
A1 registration/identity, A2 inference-only zero fit, A3 stale lineage and protected overlap,
A4 fit-row mapping, V1 replay binding and drift, V2 protected-answer leakage, V3 scoring rules,
V4 retry/restart/duplicate/late callback, V5 broken scorer cannot stage, V6 causal disclosure,
V7 credential and qualification fail-closed, E1 end-to-end product path.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from opentour_cell.pilot import eve_replay as rp
from opentour_cell.pilot import eve_scorer as sc
from opentour_cell.pilot import eve_task as task
from opentour_cell.pilot import model_connector as mc

BASE = "a" * 40
TOKENIZER = "sha256:" + "b" * 64
TEMPLATE = "sha256:" + "c" * 64
DECODING = task.DecodingPolicy(0, 1000, 256, 7)
BOUND = mc.FitBound("USD", 0, 60, 16, 100)
BENCH = "generated-benchmark-rev-1"
SENTINEL = "generated-protected-answer-sentinel"


def registration(*, train: bool = True) -> task.EveTaskRegistration:
    return task.EveTaskRegistration(
        model_ref="generated-eve",
        base_revision=BASE,
        tokenizer_digest=TOKENIZER,
        template_digest=TEMPLATE,
        decoding=DECODING,
        inference_destination="simulated:eve-inference.invalid",
        trainer_destination="simulated:eve-trainer.invalid" if train else None,
        run_control_destination="simulated:run-control.invalid",
        capabilities=frozenset(
            {task.Capability.GENERATE, task.Capability.TRAIN}
            if train
            else {task.Capability.GENERATE}
        ),
    )


def case(n: int, family: str | None = None) -> task.EveCase:
    return task.EveCase(
        f"generated-p{n}",
        family or f"generated-fam{n}",
        f"generated question {n}?",
        (
            task.Passage(f"p{n}-a", f"generated passage {n} a"),
            task.Passage(f"p{n}-b", f"generated passage {n} b"),
        ),
    )


def answer(n: int) -> str:
    return f"{SENTINEL}-{n}"


PROTECTED = tuple(case(n, "generated-famX" if n in (2, 3) else None) for n in range(1, 7))


def key() -> rp.AnswerKey:
    entries = {}
    for n in range(1, 7):
        expectation = {
            4: rp.Expectation.ABSTAIN,
            6: rp.Expectation.DISPUTED,
        }.get(n, rp.Expectation.ANSWER)
        entries[f"generated-p{n}"] = rp.ProtectedAnswer(
            expectation,
            "" if expectation is rp.Expectation.ABSTAIN else answer(n),
            frozenset({f"p{n}-a"}) if expectation is rp.Expectation.ANSWER else frozenset(),
        )
    return rp.AnswerKey(entries)


def scorer() -> sc.DeterministicScorer:
    return sc.DeterministicScorer("generated-scorer-v1", "generated-rubric-r1")


def output(n: int, kind: str) -> rp.ArmOutput:
    cid = f"generated-p{n}"
    if kind == "correct":
        return rp.ArmOutput(cid, answer(n), (f"p{n}-a",), False)
    if kind == "wrong":
        return rp.ArmOutput(cid, "generated-wrong-claim", (f"p{n}-a",), False)
    if kind == "unverifiable":
        return rp.ArmOutput(cid, answer(n), ("generated-invented-passage",), False)
    return rp.ArmOutput(cid, None, (), True)


# Per-arm behavior of the generated model: case number -> output kind.
BASELINE = {1: "correct", 2: "wrong", 3: "unverifiable", 4: "wrong", 5: "refuse"}
SIMPLE = {1: "correct", 2: "wrong", 3: "wrong", 4: "refuse", 5: "refuse"}
TUNED = {1: "correct", 2: "correct", 3: "correct", 4: "refuse", 5: "correct"}


class ScriptedInference:
    def __init__(self, served: str, script: Mapping[int, str]) -> None:
        self._served = served
        self.script = dict(script)
        self.calls = 0
        self.seen: list[bytes] = []
        self.fail_next = False

    @property
    def served_revision(self) -> str:
        return self._served

    def generate(
        self, arm: rp.ArmSpec, requests: tuple[rp.ReplayRequest, ...]
    ) -> tuple[rp.ArmOutput, ...]:
        self.calls += 1
        self.seen.append(task.canonical([r.as_mapping() for r in requests]))
        if self.fail_next:
            self.fail_next = False
            raise OSError("simulated transport loss")
        return tuple(output(int(r.case_id[-1]), self.script[int(r.case_id[-1])]) for r in requests)


class TrainerTransport:
    """Isolated trainer transport double. Counts real job creations."""

    def __init__(self, reg: task.EveTaskRegistration, *, training: bool = True) -> None:
        self.reg = reg
        self.training = training
        self.jobs: dict[str, mc.JobReceipt] = {}
        self.executions = 0
        self.payloads: list[bytes] = []
        self.resolved: str | None = reg.base_revision
        self.tuned = TUNED

    def observe(self, binding: mc.ModelBinding, purpose: mc.Purpose) -> mc.Observation:
        return mc.Observation(
            binding, "simulated:capability-receipt", 5, 50, self.resolved, True,
            self.training, True, True, True, True, {},
        )  # fmt: skip

    def serialize_development(
        self, intent: mc.FitIntent, rows: tuple[Mapping[str, object], ...]
    ) -> bytes:
        return task.serialize_development(rows)

    def estimate(self, intent: mc.FitIntent) -> mc.FitBound:
        return mc.FitBound("USD", 0, 30, 8, 50)

    def submit(self, intent: mc.FitIntent, development: bytes) -> mc.JobReceipt:
        if intent.idempotency_key not in self.jobs:
            self.executions += 1
            self.payloads.append(development)
            self.jobs[intent.idempotency_key] = mc.JobReceipt(
                intent, mc.JobState.COMPLETED, f"simulated:job-{self.executions}", "simulated:ev"
            )
        return self.jobs[intent.idempotency_key]

    def lookup(self, intent: mc.FitIntent) -> mc.JobReceipt | None:
        return self.jobs.get(intent.idempotency_key)

    def status(self, job: mc.JobReceipt) -> mc.JobReceipt:
        return job

    def cancel(self, job: mc.JobReceipt) -> mc.JobReceipt:
        return job

    def collect(self, job: mc.JobReceipt) -> mc.Candidate:
        return mc.Candidate(
            job.intent, job.job_ref, job.intent.binding.immutable_revision,
            "generated-adapter-digest-1", "simulated:replay-evidence",
        )  # fmt: skip

    def predict_for_replay(
        self, candidate: mc.Candidate, inputs: tuple[Mapping[str, object], ...]
    ) -> tuple[Mapping[str, object], ...]:
        self.seen = inputs
        result = []
        for item in inputs:
            out = output(int(str(item["case_id"])[-1]), self.tuned[int(str(item["case_id"])[-1])])
            result.append(
                {
                    "case_id": out.case_id,
                    "answer": out.answer,
                    "citations": list(out.citations),
                    "abstained": out.abstained,
                }
            )
        return tuple(result)


class Credentials:
    def __init__(self, available: bool = True) -> None:
        self.available = available

    def is_available(self, credential_ref: str) -> bool:
        return self.available


class Env:
    def __init__(self, tmp_path: Path, *, train: bool = True, training_supported: bool = True):
        self.root = tmp_path
        self.reg = registration(train=train)
        self.registry = task.EveRegistry(tmp_path / "eve-registry.json")
        self.registry.register(self.reg)
        self.transport = TrainerTransport(self.reg, training=training_supported)
        self.binding = task.binding_for(
            self.reg, tenant="generated-tenant", route=mc.Route.LOCAL,
            region="simulated:region", credential_ref="simulated:credential-ref",
        )  # fmt: skip
        self.grants = {
            purpose: mc.Grant(
                purpose.value, self.binding, purpose, "grant-v1", 30, task.ROW_FIELDS, 40,
                BOUND if purpose is mc.Purpose.TRAINING else None,
                frozenset({BENCH}) if purpose is mc.Purpose.TRAINING else frozenset(),
            )
            for purpose in mc.Purpose
        }  # fmt: skip
        self.preflight = mc.Preflight(self.transport, self.grant, clock=lambda: 10)
        self.logs: list[str] = []
        self.credentials = Credentials()
        self.key = key()
        self.scorer = scorer()
        self.manifest = rp.freeze(
            benchmark_id="generated-benchmark", benchmark_revision=BENCH,
            registration=self.reg, scorer=self.scorer, key=self.key, cases=PROTECTED,
        )  # fmt: skip

    def grant(self, actor: str, binding: mc.ModelBinding, purpose: mc.Purpose) -> mc.Grant | None:
        g = self.grants.get(purpose)
        return g if g and (g.actor, g.binding) == (actor, binding) else None

    def worker(self, name: str = "journal.jsonl") -> rp.EveReplayWorker:
        return rp.EveReplayWorker(
            registry=self.registry,
            preflight=self.preflight,
            credentials=self.credentials,
            journal=rp.ReplayJournal(self.root / name),
            keys=rp.CommitmentKeyStore(self.root / "commitment-keys" / f"{name}.keys"),
            log=self.logs.append,
        )

    def staging(
        self, journal: rp.ReplayJournal | None = None, scorer_: rp.Scorer | None = None
    ) -> dict[str, Any]:
        """What ``stage_decision`` needs to derive the result itself (OPE-573)."""
        return {
            "manifest": self.manifest,
            "journal": journal or rp.ReplayJournal(self.root / "journal.jsonl"),
            "scorer": scorer_ or self.scorer,
            "key": self.key,
        }

    def arm(self, arm_id: str, served: str = BASE, **changes: str) -> rp.ArmSpec:
        spec = rp.ArmSpec(
            arm_id, served, "generated-snapshot-1", "generated-retrieval-1",
            "generated-context-1", DECODING.digest,
        )  # fmt: skip
        return replace(spec, **changes)

    def replay(
        self,
        arm: rp.ArmSpec,
        inference: rp.GenerativeInference,
        *,
        worker: rp.EveReplayWorker | None = None,
        retriever: rp.Retriever | None = None,
        manifest: rp.ReplayManifest | None = None,
        cases: tuple[task.EveCase, ...] = PROTECTED,
        key_: rp.AnswerKey | None = None,
        scorer_: rp.Scorer | None = None,
        binding: mc.ModelBinding | None = None,
    ) -> rp.ArmRecord:
        return (worker or self.worker()).replay_arm(
            actor="baseline_inference",
            binding=binding or self.binding,
            manifest=manifest or self.manifest,
            arm=arm,
            cases=cases,
            scorer=scorer_ or self.scorer,
            key=key_ or self.key,
            inference=inference,
            retriever=retriever,
        )

    def baseline_and_simple(self, worker: rp.EveReplayWorker | None = None) -> dict[str, Any]:
        worker = worker or self.worker()
        return {
            "baseline": self.replay(
                self.arm("baseline"), ScriptedInference(BASE, BASELINE), worker=worker
            ),
            "simple_repair": self.replay(
                self.arm("simple_repair"), ScriptedInference(BASE, SIMPLE), worker=worker
            ),
        }

    def result(
        self,
        arms: Mapping[str, rp.ArmRecord],
        *,
        journal: rp.ReplayJournal | None = None,
        scorer_: rp.Scorer | None = None,
    ) -> dict[str, Any]:
        return rp.build_result(
            self.manifest, arms, scorer=scorer_ or self.scorer,
            journal=journal or rp.ReplayJournal(self.root / "journal.jsonl"), key=self.key,
        )  # fmt: skip

    def candidate_inference(self, tuned: Mapping[int, str] = TUNED) -> rp.CandidateInference:
        self.transport.tuned = tuned
        job, candidate = self.fit_and_collect()
        return rp.CandidateInference(self.preflight, "training", job, candidate, self.reg)

    def fit_and_collect(self) -> tuple[mc.JobReceipt, mc.Candidate]:
        plan = rp.plan_guarded_fit(
            key=self.key, manifest=self.manifest, protected_cases=PROTECTED,
            registration=self.reg, binding=self.binding,
            capability=self.preflight.discover("training", self.binding, mc.Purpose.TRAINING),
            partition=partition(), grant_revision="grant-v1", benchmark_revision=BENCH,
            idempotency_key="generated-fit-intent-1", bound=BOUND,
        )  # fmt: skip
        receipt = self.preflight.submit_reserved_fit(
            "training", plan.intent, plan.rows, reservation_guard=nullcontext
        )
        return receipt, self.preflight.collect_candidate("training", receipt)


def partition(**overrides: Any) -> task.Partition:
    dev = tuple(
        task.DevelopmentExample(
            task.EveCase(f"generated-d{n}", f"generated-devfam{n}", f"dev question {n}?",
                         (task.Passage(f"d{n}-a", f"dev passage {n}"),)),
            f"generated-dev-answer-{n}",
        )
        for n in range(1, 4)
    )  # fmt: skip
    fields: dict[str, Any] = {
        "development": dev,
        "protected_case_ids": frozenset(c.case_id for c in PROTECTED),
        "protected_family_ids": frozenset(c.family_id for c in PROTECTED),
    }
    fields.update(overrides)
    return task.Partition(**fields)


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def refuses(code: str) -> Any:
    return pytest.raises(task.EveRefusal, match=f"^{code}$")


def full_result(env: Env, worker: rp.EveReplayWorker | None = None) -> dict[str, Any]:
    worker = worker or env.worker()
    arms = env.baseline_and_simple(worker)
    job, candidate = env.fit_and_collect()
    infer = rp.CandidateInference(env.preflight, "training", job, candidate, env.reg)
    arms["candidate"] = env.replay(
        env.arm("candidate", candidate.artifact_or_serving_revision), infer, worker=worker
    )
    return env.result(arms, journal=worker.journal)


# ---------------------------------------------------------------- A1 registration
def test_a1_registration_pins_identity_and_survives_restart(tmp_path: Path) -> None:
    reg = registration()
    registry = task.EveRegistry(tmp_path / "r.json")
    registry.register(reg)
    registry.register(reg)  # identical re-registration is a no-op
    assert task.EveRegistry(tmp_path / "r.json").require("generated-eve") == reg
    assert reg.identity.startswith("sha256:")
    drifted = replace(reg, template_digest="sha256:" + "d" * 64)
    assert drifted.identity != reg.identity
    with refuses("eve_registration_conflict"):
        registry.register(drifted)
    with refuses("model_unregistered"):
        registry.require("generated-unknown")


def test_a1_binding_is_derived_from_registration_not_hardcoded() -> None:
    reg = registration()
    binding = task.binding_for(
        reg, tenant="t", route=mc.Route.LOCAL, region="r", credential_ref="c"
    )
    assert (binding.immutable_revision, binding.task) == (BASE, task.TASK)
    assert binding.preprocessing_revision == reg.fingerprint
    changed = replace(reg, tokenizer_digest="sha256:" + "e" * 64)
    assert changed.fingerprint != reg.fingerprint


@pytest.mark.parametrize(
    "change",
    [
        {"run_control_destination": "simulated:eve-inference.invalid"},
        {"base_revision": "moving-alias"},
        {"tokenizer_digest": "not-a-digest"},
        {"trainer_destination": None},
        {"capabilities": frozenset()},
    ],
)
def test_a1_invalid_or_shared_endpoint_registration_refused(change: dict[str, Any]) -> None:
    with pytest.raises(task.EveRefusal):
        replace(registration(), **change)


# ---------------------------------------------------------------- A2 inference-only zero fit
def test_a2_inference_only_registration_can_baseline_but_never_fit(tmp_path: Path) -> None:
    env = Env(tmp_path, train=False, training_supported=False)
    record = env.replay(env.arm("baseline"), ScriptedInference(BASE, BASELINE))
    assert record.spec.arm_id == "baseline"
    capability = env.preflight.discover("training", env.binding, mc.Purpose.TRAINING)
    with refuses("fit_unavailable_inference_only"):
        task.plan_fit(
            registration=env.reg, binding=env.binding, capability=capability,
            partition=partition(), grant_revision="grant-v1", benchmark_revision=BENCH,
            idempotency_key="k", bound=BOUND,
            bound_membership_digest=env.manifest.membership_digest,
        )  # fmt: skip
    assert env.transport.executions == 0


def test_a2_train_declared_but_target_not_trainable_is_zero_fit(tmp_path: Path) -> None:
    env = Env(tmp_path, training_supported=False)
    capability = env.preflight.discover("training", env.binding, mc.Purpose.TRAINING)
    assert capability.state is mc.State.BENCHMARK_ONLY
    with refuses("fit_unavailable_inference_only"):
        task.plan_fit(
            registration=env.reg, binding=env.binding, capability=capability,
            partition=partition(), grant_revision="grant-v1", benchmark_revision=BENCH,
            idempotency_key="k", bound=BOUND,
            bound_membership_digest=env.manifest.membership_digest,
        )  # fmt: skip
    assert env.transport.executions == 0


# ---------------------------------------------------------------- A3 lineage and overlap
def test_a3_protected_case_or_family_overlap_is_refused() -> None:
    protected_case = PROTECTED[0]
    leaky_case = task.DevelopmentExample(
        task.EveCase(protected_case.case_id, "generated-other", "q", ()), "a"
    )
    with refuses("protected_overlap"):
        partition(development=(leaky_case,))
    leaky_family = task.DevelopmentExample(
        task.EveCase("generated-dx", protected_case.family_id, "q", ()), "a"
    )
    with refuses("protected_overlap"):
        partition(development=(leaky_family,))


def test_a3_stale_or_wrong_lineage_candidate_is_not_replayable(env: Env) -> None:
    job, candidate = env.fit_and_collect()
    rp.CandidateInference(env.preflight, "training", job, candidate, env.reg)
    for bad in (
        replace(candidate, parent_revision="f" * 40),
        replace(candidate, job_ref="simulated:other-job"),
        replace(candidate, artifact_or_serving_revision=""),
        replace(candidate, intent=replace(candidate.intent, idempotency_key="other")),
    ):
        with refuses("candidate_lineage_unverified"):
            rp.CandidateInference(env.preflight, "training", job, bad, env.reg)
    other = replace(env.reg, tokenizer_digest="sha256:" + "9" * 64)
    with refuses("candidate_lineage_unverified"):
        rp.CandidateInference(env.preflight, "training", job, candidate, other)
    wrong_state = replace(job, state=mc.JobState.RUNNING)
    with refuses("candidate_lineage_unverified"):
        rp.CandidateInference(env.preflight, "training", wrong_state, candidate, env.reg)


def test_a3_candidate_serving_a_different_revision_than_its_arm_is_refused(env: Env) -> None:
    job, candidate = env.fit_and_collect()
    infer = rp.CandidateInference(env.preflight, "training", job, candidate, env.reg)
    with refuses("served_revision_mismatch"):
        env.replay(env.arm("candidate", "generated-adapter-other"), infer)
    with refuses("served_revision_mismatch"):
        env.replay(env.arm("baseline", "generated-adapter-digest-1"), infer)


# ---------------------------------------------------------------- A4 fit rows
def test_a4_fit_rows_are_masked_development_only_with_membership_fingerprint(env: Env) -> None:
    plan = task.plan_fit(
        registration=env.reg, binding=env.binding,
        capability=env.preflight.discover("training", env.binding, mc.Purpose.TRAINING),
        partition=partition(), grant_revision="grant-v1", benchmark_revision=BENCH,
        idempotency_key="k", bound=BOUND,
        bound_membership_digest=env.manifest.membership_digest,
    )  # fmt: skip
    assert len(plan.rows) == 3
    assert all(r["loss_mask"] == task.LOSS_MASK and set(r) == task.ROW_FIELDS for r in plan.rows)
    assert plan.protected_membership_fingerprint == partition().membership_digest
    blob = task.canonical([dict(r) for r in plan.rows]).decode()
    assert not any(c.case_id in blob or c.family_id in blob for c in PROTECTED)
    rp.guard_fit_payload(env.key, plan)
    receipt = env.preflight.submit_reserved_fit(
        "training", plan.intent, plan.rows, reservation_guard=nullcontext
    )
    assert receipt.state is mc.JobState.COMPLETED
    assert not env.key.leaks(env.transport.payloads[0].decode())
    with refuses("training_payload_invalid"):
        task.serialize_development(({"question": "q"},))
    with refuses("no_development_rows"):
        task.development_rows(partition(development=()))


# ---------------------------------------------------------------- V1 binding and drift
def test_v1_replay_set_that_is_not_the_bound_one_is_refused(env: Env) -> None:
    swapped = (*PROTECTED[:5], case(7))
    with refuses("replay_not_bound"):
        env.replay(env.arm("baseline"), ScriptedInference(BASE, BASELINE), cases=swapped)
    subset = PROTECTED[:5]
    with refuses("replay_not_bound"):
        env.replay(env.arm("baseline"), ScriptedInference(BASE, BASELINE), cases=subset)
    reshuffled_family = tuple(replace(c, family_id="generated-famZ") for c in PROTECTED)
    with refuses("replay_not_bound"):
        env.replay(env.arm("baseline"), ScriptedInference(BASE, BASELINE), cases=reshuffled_family)


def test_v1_version_drift_is_refused_not_coerced(env: Env) -> None:
    infer = ScriptedInference(BASE, BASELINE)
    # model drift: the registered identity no longer matches the frozen manifest
    drifted_reg = replace(env.reg, decoding=task.DecodingPolicy(0, 900, 256, 7))
    other = task.EveRegistry(env.root / "other.json")
    other.register(drifted_reg)
    worker = rp.EveReplayWorker(
        registry=other, preflight=env.preflight, credentials=env.credentials,
        journal=rp.ReplayJournal(env.root / "j2.jsonl"),
        keys=rp.CommitmentKeyStore(env.root / "commitment-keys" / "j2.keys"),
    )  # fmt: skip
    with refuses("model_version_drift"):
        env.replay(env.arm("baseline"), infer, worker=worker)
    with refuses("scorer_version_drift"):
        env.replay(
            env.arm("baseline"), infer, scorer_=sc.DeterministicScorer("generated-scorer-v2", "r")
        )
    with refuses("scorer_version_drift"):
        env.replay(
            env.arm("baseline"),
            infer,
            scorer_=sc.DeterministicScorer("generated-scorer-v1", "generated-rubric-r2"),
        )
    altered = dict(env.key._entries)  # noqa: SLF001
    altered["generated-p1"] = rp.ProtectedAnswer(
        rp.Expectation.ANSWER, "generated-edited", frozenset()
    )
    with refuses("answer_key_drift"):
        env.replay(env.arm("baseline"), infer, key_=rp.AnswerKey(altered))
    with refuses("decoding_drift"):
        env.replay(env.arm("baseline", decoding_digest="sha256:other"), infer)
    assert infer.calls == 0


def test_v1_binding_not_derived_from_registration_is_refused(env: Env) -> None:
    infer = ScriptedInference(BASE, BASELINE)
    forged = replace(env.binding, immutable_revision="f" * 40)
    with refuses("registration_binding_mismatch"):
        env.replay(env.arm("baseline"), infer, binding=forged)
    assert infer.calls == 0


# ---------------------------------------------------------------- V2 leakage
def test_v2_protected_answers_never_cross_any_outbound_surface(env: Env) -> None:
    worker = env.worker()
    infers = {
        "baseline": ScriptedInference(BASE, BASELINE),
        "simple_repair": ScriptedInference(BASE, SIMPLE),
    }
    arms = {name: env.replay(env.arm(name), inf, worker=worker) for name, inf in infers.items()}
    job, candidate = env.fit_and_collect()
    arms["candidate"] = env.replay(
        env.arm("candidate", candidate.artifact_or_serving_revision),
        rp.CandidateInference(env.preflight, "training", job, candidate, env.reg),
        worker=worker,
    )
    result = env.result(arms, journal=worker.journal)
    surfaces = {
        "request": b"".join(b for inf in infers.values() for b in inf.seen),
        "candidate_request": task.canonical([dict(i) for i in env.transport.seen]),
        "fit_payload": b"".join(env.transport.payloads),
        "log": "\n".join(env.logs).encode(),
        "journal": (env.root / "journal.jsonl").read_bytes(),
        "result": task.canonical(result),
        "registry": (env.root / "eve-registry.json").read_bytes(),
    }
    for name, blob in surfaces.items():
        assert blob, name
        assert SENTINEL.encode() not in blob, f"protected answer leaked on {name}"
        assert b"generated-wrong-claim" not in blob or name == "request"
    assert env.logs
    assert "generated-p1" not in json.dumps(result)  # per-case refs are hashed
    assert SENTINEL not in repr(env.key) and answer(1) not in repr(env.key)


def test_v2_retrieval_returning_a_protected_answer_is_refused_before_inference(env: Env) -> None:
    class Leaky:
        def retrieve(self, arm: rp.ArmSpec, case_: task.EveCase) -> tuple[task.Passage, ...]:
            return (*case_.context, task.Passage("leak", f"note: {answer(1)}"))

    infer = ScriptedInference(BASE, BASELINE)
    with refuses("protected_answer_exposed_retrieval"):
        env.replay(env.arm("baseline"), infer, retriever=Leaky())
    assert infer.calls == 0
    assert not (env.root / "journal.jsonl").exists()


def test_v2_leaky_context_in_the_replay_set_is_refused_before_inference(env: Env) -> None:
    leaky_case = replace(
        PROTECTED[0], context=(task.Passage("p1-a", f"contains {answer(1)} inline"),)
    )
    cases = (leaky_case, *PROTECTED[1:])
    manifest = rp.freeze(
        benchmark_id="b", benchmark_revision=BENCH, registration=env.reg,
        scorer=env.scorer, key=env.key, cases=cases,
    )  # fmt: skip
    infer = ScriptedInference(BASE, BASELINE)
    with refuses("protected_answer_exposed_retrieval"):
        env.replay(env.arm("baseline"), infer, manifest=manifest, cases=cases)
    assert infer.calls == 0


def test_v2_fit_payload_or_log_carrying_a_protected_answer_is_refused(env: Env) -> None:
    leaky = partition(
        development=(
            task.DevelopmentExample(
                task.EveCase("generated-dleak", "generated-dleakfam", "q", ()), answer(2)
            ),
        )
    )
    plan = task.plan_fit(
        registration=env.reg, binding=env.binding,
        capability=env.preflight.discover("training", env.binding, mc.Purpose.TRAINING),
        partition=leaky, grant_revision="grant-v1", benchmark_revision=BENCH,
        idempotency_key="k-leak", bound=BOUND,
        bound_membership_digest=env.manifest.membership_digest,
    )  # fmt: skip
    with refuses("protected_answer_exposed_fit_payload"):
        rp.guard_fit_payload(env.key, plan)
    worker = env.worker()
    with refuses("protected_answer_exposed_log"):
        worker._emit(env.key, f"debug {answer(3)}")  # noqa: SLF001
    assert not any(SENTINEL in line for line in env.logs)


def test_v2_result_containing_a_protected_answer_is_refused(env: Env) -> None:
    poisoned = rp.freeze(
        benchmark_id=f"title {answer(1)}", benchmark_revision=BENCH, registration=env.reg,
        scorer=env.scorer, key=env.key, cases=PROTECTED,
    )  # fmt: skip
    worker = env.worker("poison.jsonl")
    arms = {
        "baseline": env.replay(
            env.arm("baseline"),
            ScriptedInference(BASE, BASELINE),
            worker=worker,
            manifest=poisoned,
        )
    }
    with refuses("protected_answer_exposed_result"):
        rp.build_result(poisoned, arms, scorer=env.scorer, journal=worker.journal, key=env.key)


# ---------------------------------------------------------------- V3 scoring rules
def test_v3_unverifiable_claims_never_count_as_correct(env: Env) -> None:
    arms = env.baseline_and_simple()
    base = arms["baseline"]
    verdicts = {r.ref: r.verdict for r in base.rows}
    assert rp.Verdict.UNVERIFIED in verdicts.values()
    result = env.result(arms)
    body = result["body"]["arms"]["baseline"]
    # p1 correct; p2 wrong; p3 unverifiable; p4 should abstain but answered; p5 refused
    assert (body["eligible"], body["excluded_unscored"], body["answered"]) == (5, 1, 4)
    assert body["unverified_answered"] == 1
    assert body["verified_answered"] == 3
    assert (body["correct_answer"], body["serious_errors"], body["inappropriate_refusal"]) == (
        1,
        2,
        1,
    )
    assert body["correct"] == 1
    assert body["coverage_answered_over_eligible"] == [4, 5]
    assert body["serious_error_over_verified_answered"] == [2, 3]


def test_v3_correct_abstention_and_unknown_denominators_stay_unknown(env: Env) -> None:
    arms = env.baseline_and_simple()
    simple = env.result(arms)["body"]["arms"]["simple_repair"]
    assert simple["correct_abstention"] == 1
    # an arm that answers nothing has no verified-answered population: unknown, not zero
    silent = env.replay(
        env.arm("baseline", source_snapshot_id="generated-snapshot-2"),
        ScriptedInference(BASE, dict.fromkeys(range(1, 7), "refuse")),
    )
    body = env.result({"baseline": silent})["body"]
    assert body["arms"]["baseline"]["serious_error_over_verified_answered"] == [0, None]
    assert body["improvement_claim"] == "none_baseline_only"
    assert body["excluded_redacted"][0][1] == "disputed"


def test_v3_missing_output_stays_visible_and_blocks_changed_denominator(env: Env) -> None:
    arms = env.baseline_and_simple()
    infer = env.candidate_inference()
    real = env.transport.predict_for_replay
    setattr(env.transport, "predict_for_replay", lambda c, i: real(c, i)[:-1])  # noqa: B010
    dropped = env.replay(env.arm("candidate", infer.served_revision), infer)
    assert sum(r.verdict is rp.Verdict.MISSING for r in dropped.rows) == 1
    arms["candidate"] = dropped
    body = env.result(arms)["body"]
    assert body["arms"]["candidate"]["missing_output"] == 1
    assert body["improvement_claim"] == "blocked_changed_denominator"
    only_two = env.result({"candidate": dropped})["body"]
    assert only_two["improvement_claim"] == "hold_missing_arm"


def test_v3_output_for_an_unrequested_or_duplicate_case_is_refused(env: Env) -> None:
    class Extra(ScriptedInference):
        def generate(self, arm: rp.ArmSpec, requests: tuple[rp.ReplayRequest, ...]) -> Any:
            return (*super().generate(arm, requests), output(6, "correct"))

    with refuses("output_case_mismatch"):
        env.replay(env.arm("baseline"), Extra(BASE, BASELINE))

    class Twice(ScriptedInference):
        def generate(self, arm: rp.ArmSpec, requests: tuple[rp.ReplayRequest, ...]) -> Any:
            out = super().generate(arm, requests)
            return (*out, out[0])

    with refuses("output_case_mismatch"):
        env.replay(env.arm("baseline"), Twice(BASE, BASELINE))


# ---------------------------------------------------------------- V4 retry/restart/callbacks
def test_v4_retry_after_transport_loss_records_one_result(env: Env) -> None:
    infer = ScriptedInference(BASE, BASELINE)
    infer.fail_next = True
    with refuses("inference_unavailable"):
        env.replay(env.arm("baseline"), infer)
    assert not (env.root / "journal.jsonl").exists()
    first = env.replay(env.arm("baseline"), infer)
    again = env.replay(env.arm("baseline"), infer)
    assert first == again
    assert infer.calls == 2  # one failed attempt, one recorded run, duplicate dispatch reused
    events = [json.loads(x) for x in (env.root / "journal.jsonl").read_text().splitlines()]
    assert sum(e["event"] == "arm" for e in events) == 1


def test_v4_restart_replays_identical_result_without_new_inference(env: Env) -> None:
    first = full_result(env)
    calls_before = env.transport.executions
    restarted = Env(env.root)  # new process state on the same files
    restarted.transport = env.transport
    restarted.preflight = mc.Preflight(env.transport, restarted.grant, clock=lambda: 10)
    worker = restarted.worker()
    infer = ScriptedInference(BASE, BASELINE)
    record = restarted.replay(restarted.arm("baseline"), infer, worker=worker)
    assert infer.calls == 0
    assert record.document() == rp.ArmRecord.parse(record.document()).document()
    second = full_result(restarted, worker)
    assert second["result_digest"] == first["result_digest"]
    assert env.transport.executions == calls_before  # same fit intent: no second execution


def test_v4_replaying_the_same_protected_set_is_deterministic_across_journals(env: Env) -> None:
    a = full_result(env, env.worker("a.jsonl"))
    env.transport.jobs.clear()
    b = full_result(env, env.worker("b.jsonl"))
    assert a["result_digest"] == b["result_digest"]


def test_v4_duplicate_and_late_scoring_callbacks_change_nothing(env: Env) -> None:
    worker = env.worker()
    result = full_result(env, worker)
    digest = result["result_digest"]
    journal = worker.journal
    assert journal.settle(env.manifest.digest, digest) is rp.Settlement.RECORDED
    assert journal.settle(env.manifest.digest, digest) is rp.Settlement.DUPLICATE
    with refuses("scoring_already_final"):
        journal.settle(env.manifest.digest, "sha256:" + "0" * 64)
    with refuses("stale_scoring_callback"):
        journal.settle("sha256:" + "1" * 64, digest)
    before = (env.root / "journal.jsonl").read_bytes()
    assert before.count(b'"event":"settle"') == 1
    with refuses("scoring_already_final"):
        journal.settle(env.manifest.digest, "sha256:" + "2" * 64)
    assert (env.root / "journal.jsonl").read_bytes() == before


def test_v4_one_execution_for_a_duplicate_fit_dispatch(env: Env) -> None:
    env.fit_and_collect()
    env.fit_and_collect()
    assert env.transport.executions == 1


# ---------------------------------------------------------------- V5 scorer and staging
def test_v5_broken_scorer_cannot_stage(env: Env) -> None:
    class AlwaysCorrect:
        version = "generated-scorer-v1"
        rubric_revision = "generated-rubric-r1"

        def score(self, case_, expected, out) -> rp.Verdict:  # type: ignore[no-untyped-def]
            return rp.Verdict.CORRECT_ANSWER

    assert rp.scorer_selfcheck(env.scorer)
    assert not rp.scorer_selfcheck(AlwaysCorrect())

    class Raising(AlwaysCorrect):
        def score(self, case_, expected, out):  # type: ignore[no-untyped-def]
            raise ValueError("boom")

    assert not rp.scorer_selfcheck(Raising())
    worker = env.worker()
    arms = env.baseline_and_simple(worker)
    job, candidate = env.fit_and_collect()
    arms["candidate"] = env.replay(
        env.arm("candidate", candidate.artifact_or_serving_revision),
        rp.CandidateInference(env.preflight, "training", job, candidate, env.reg),
        worker=worker,
    )
    good = env.result(arms, journal=worker.journal)
    assert rp.stage_decision(
        good, recomputed_digest=good["result_digest"], **env.staging(worker.journal)
    ).stageable
    broken = env.result(arms, journal=worker.journal, scorer_=AlwaysCorrect())
    assert broken["body"]["scorer"]["selfcheck_passed"] is False
    decision = rp.stage_decision(
        broken,
        recomputed_digest=broken["result_digest"],
        **env.staging(worker.journal, AlwaysCorrect()),
    )
    assert not decision.stageable and "scorer_selfcheck_failed" in decision.reasons
    mismatch = rp.stage_decision(
        good, recomputed_digest="sha256:" + "3" * 64, **env.staging(worker.journal)
    )
    assert not mismatch.stageable and "recompute_mismatch" in mismatch.reasons
    forged_body = {
        **good["body"],
        "improvement_claim": "observed_net_fixes",
        "fit_authorized": True,
    }
    tampered = {"body": forged_body, "result_digest": good["result_digest"]}
    assert not rp.stage_decision(
        tampered, recomputed_digest=good["result_digest"], **env.staging(worker.journal)
    ).stageable
    assert good["body"]["fit_authorized"] is False and good["body"]["release_authorized"] is False


def test_v5_no_net_fixes_is_not_stageable(env: Env) -> None:
    arms = env.baseline_and_simple()
    infer = env.candidate_inference(SIMPLE)  # the tuned adapter behaves exactly like the repair
    arms["candidate"] = env.replay(env.arm("candidate", infer.served_revision), infer)
    result = env.result(arms)
    assert result["body"]["improvement_claim"] == "no_net_fixes"
    decision = rp.stage_decision(result, recomputed_digest=result["result_digest"], **env.staging())
    assert not decision.stageable and decision.reasons == ("improvement_no_net_fixes",)


# ---------------------------------------------------------------- V6 causal disclosure
def test_v6_changed_retrieval_is_disclosed_and_blocks_a_causal_claim(env: Env) -> None:
    class Shifted:
        def retrieve(self, arm: rp.ArmSpec, case_: task.EveCase) -> tuple[task.Passage, ...]:
            return case_.context[:1] if case_.case_id.endswith("1") else case_.context

    arms = env.baseline_and_simple()
    infer = env.candidate_inference()
    arms["candidate"] = env.replay(
        env.arm("candidate", infer.served_revision, retrieval_settings_id="generated-retrieval-2"),
        infer,
        retriever=Shifted(),
    )
    causal = env.result(arms)["body"]["causal_tuning_comparison"]
    assert causal["available"] is False
    assert causal["differing_settings"] == ["retrieval_settings_id"]
    assert causal["retrieval_changed_cases"] == 1


def test_v6_matched_settings_make_the_tuning_comparison_available(env: Env) -> None:
    result = full_result(env)
    body = result["body"]
    assert body["causal_tuning_comparison"] == {
        "available": True,
        "differing_settings": [],
        "retrieval_changed_cases": 0,
    }
    pair = body["paired_comparison"]
    assert (pair["control"], pair["treatment"]) == ("simple_repair", "candidate")
    assert (pair["fixes"], pair["regressions"]) == (3, 0)
    assert body["evidence_tier"] == "generated" and body["claim_scope"] == "implementation_only"
    assert set(body["unsatisfied_external_gates"]) == set(rp.EXTERNAL_GATES)


# ---------------------------------------------------------------- V7 fail closed
def test_v7_missing_credentials_fail_closed_without_leaking(env: Env) -> None:
    env.credentials.available = False
    infer = ScriptedInference(BASE, BASELINE)
    with pytest.raises(task.EveRefusal) as caught:
        env.replay(env.arm("baseline"), infer)
    assert str(caught.value) == "credential_unavailable"
    assert env.binding.credential_ref not in str(caught.value)
    assert infer.calls == 0 and not (env.root / "journal.jsonl").exists()


def test_v7_unregistered_model_cannot_be_scored(env: Env) -> None:
    ghost = replace(env.binding, model_ref="generated-ghost")
    with refuses("model_unregistered"):
        env.replay(env.arm("baseline"), ScriptedInference(BASE, BASELINE), binding=ghost)


def test_v7_inference_only_registration_without_generate_is_unqualified(tmp_path: Path) -> None:
    env = Env(tmp_path)
    with pytest.raises(task.EveRefusal):
        replace(env.reg, capabilities=frozenset({task.Capability.TRAIN}), trainer_destination=None)


def test_v7_unverified_served_revision_or_stale_evidence_is_not_qualified(env: Env) -> None:
    infer = ScriptedInference(BASE, BASELINE)
    env.transport.resolved = "moving-alias"
    with pytest.raises(task.EveRefusal) as caught:
        env.replay(env.arm("baseline"), infer)
    assert str(caught.value) in {"model_not_qualified", "exact_model_revision_unverified"}
    env.transport.resolved = BASE
    del env.grants[mc.Purpose.BASELINE]
    with refuses("model_not_qualified"):
        env.replay(env.arm("baseline"), infer)
    assert infer.calls == 0


# ---------------------------------------------------------------- E1 end to end
def test_e1_registered_task_runs_eligible_arms_candidate_and_recomputable_scorecard(
    env: Env,
) -> None:
    worker = env.worker()
    result = full_result(env, worker)
    body = result["body"]
    assert body["improvement_claim"] == "observed_net_fixes"
    assert env.transport.executions == 1
    # independent recompute: fresh journal, fresh scorer, same registered model
    other = Env(env.root / "recompute")
    other.transport.jobs = env.transport.jobs
    recomputed = full_result(other, other.worker())
    assert recomputed["result_digest"] == result["result_digest"]
    assert rp.stage_decision(
        result, recomputed_digest=recomputed["result_digest"], **other.staging()
    ).stageable
    candidate_body = body["arms"]["candidate"]
    assert candidate_body["served_revision"] == "generated-adapter-digest-1"
    assert candidate_body["correct"] == 5 and body["arms"]["baseline"]["correct"] == 1
    # the lower-level model smoke and the real Slack-to-GPU run stay separate, open gates
    assert "real_eve_inference_capacity_unavailable" in body["unsatisfied_external_gates"]
    assert body["fit_authorized"] is False and body["release_authorized"] is False


# ---------------------------------------------------------------- closer: false-trust probes
HARD_ANSWER = 'Zurich "north" gate\nline two \u00e9\\end'


def hard_key() -> rp.AnswerKey:
    entries = dict(key()._entries)  # noqa: SLF001
    entries["generated-p1"] = rp.ProtectedAnswer(
        rp.Expectation.ANSWER, HARD_ANSWER, frozenset({"p1-a"})
    )
    return rp.AnswerKey(entries)


@pytest.mark.parametrize(
    "carrier",
    [
        lambda a: {"nested": [a]},
        lambda a: a.upper(),
        lambda a: " ".join(a.split()),
        lambda a: f"prefix {a} suffix".encode(),
        lambda a: {a: 1},
    ],
)
def test_x_leak_scan_sees_escaped_reformatted_and_nested_answers(carrier: Any) -> None:
    assert hard_key().leaks(carrier(HARD_ANSWER))
    assert not hard_key().leaks({"harmless": ["generated unrelated text"]})


def test_x_hard_answer_in_retrieval_is_refused_before_inference(env: Env) -> None:
    k = hard_key()
    manifest = rp.freeze(
        benchmark_id="b", benchmark_revision=BENCH, registration=env.reg, scorer=env.scorer,
        key=k, cases=PROTECTED,
    )  # fmt: skip

    class Leaky:
        def retrieve(self, arm: rp.ArmSpec, case_: task.EveCase) -> tuple[task.Passage, ...]:
            return (*case_.context, task.Passage("leak", HARD_ANSWER.upper()))

    infer = ScriptedInference(BASE, BASELINE)
    with refuses("protected_answer_exposed_retrieval"):
        env.replay(env.arm("baseline"), infer, manifest=manifest, key_=k, retriever=Leaky())
    assert infer.calls == 0


def test_x_protected_answer_repr_does_not_show_the_answer() -> None:
    entry = rp.ProtectedAnswer(rp.Expectation.ANSWER, HARD_ANSWER, frozenset())
    assert "Zurich" not in repr(entry)


def test_x_forged_candidate_the_connector_never_produced_is_refused(env: Env) -> None:
    intent = mc.FitIntent(
        env.binding, "grant-v1", BENCH, "a" * 64, "generated-forged-key", task.ROW_FIELDS, BOUND
    )
    job = mc.JobReceipt(intent, mc.JobState.COMPLETED, "simulated:forged", "simulated:ev")
    forged = mc.Candidate(intent, "simulated:forged", BASE, "generated-adapter-forged", "ev")
    with refuses("candidate_lineage_unverified"):
        rp.CandidateInference(env.preflight, "training", job, forged, env.reg)
    assert env.transport.executions == 0


def test_x_candidate_arm_needs_lineage_checked_inference_for_this_benchmark(env: Env) -> None:
    with refuses("candidate_lineage_unverified"):
        env.replay(env.arm("candidate", BASE), ScriptedInference(BASE, TUNED))
    # a real candidate fitted for a different benchmark revision is not this benchmark's candidate
    env.grants[mc.Purpose.TRAINING] = replace(
        env.grants[mc.Purpose.TRAINING], benchmark_revisions=frozenset({BENCH, "generated-other"})
    )
    plan = task.plan_fit(
        registration=env.reg, binding=env.binding,
        capability=env.preflight.discover("training", env.binding, mc.Purpose.TRAINING),
        partition=partition(), grant_revision="grant-v1", benchmark_revision="generated-other",
        idempotency_key="generated-other-fit", bound=BOUND,
        bound_membership_digest=env.manifest.membership_digest,
    )  # fmt: skip
    job = env.preflight.submit_reserved_fit(
        "training", plan.intent, plan.rows, reservation_guard=nullcontext
    )
    other = rp.CandidateInference(
        env.preflight, "training", job, env.preflight.collect_candidate("training", job), env.reg
    )
    with refuses("candidate_benchmark_mismatch"):
        env.replay(env.arm("candidate", other.served_revision), other)
    own = env.candidate_inference()
    with refuses("served_revision_mismatch"):
        env.replay(env.arm("candidate", BASE), own)


def test_x_fit_cannot_declare_a_smaller_protected_set_than_the_replay_froze(env: Env) -> None:
    sneaky = task.Partition(
        (
            task.DevelopmentExample(
                task.EveCase("generated-sneak", "generated-sneakfam", "dev q?", ()), "dev a"
            ),
            task.DevelopmentExample(PROTECTED[0], "dev a2"),
        ),
        frozenset(),
        frozenset(),
    )
    common: dict[str, Any] = {
        "key": env.key, "manifest": env.manifest, "protected_cases": PROTECTED,
        "registration": env.reg, "binding": env.binding,
        "capability": env.preflight.discover("training", env.binding, mc.Purpose.TRAINING),
        "grant_revision": "grant-v1", "benchmark_revision": BENCH,
        "idempotency_key": "generated-sneaky", "bound": BOUND,
    }  # fmt: skip
    with refuses("protected_membership_mismatch"):
        rp.plan_guarded_fit(partition=sneaky, **common)
    # the same protected question under a new case and family id is still protected overlap
    renamed = task.Partition(
        (
            task.DevelopmentExample(
                task.EveCase(
                    "generated-new-id", "generated-new-fam", PROTECTED[0].question.upper(), ()
                ),
                "dev a",
            ),
        ),
        partition().protected_case_ids,
        partition().protected_family_ids,
    )
    with refuses("protected_overlap"):
        rp.plan_guarded_fit(partition=renamed, **common)
    with refuses("replay_not_bound"):
        rp.plan_guarded_fit(partition=partition(), **{**common, "protected_cases": PROTECTED[:3]})
    assert env.transport.executions == 0


def test_x_replay_with_altered_question_or_context_is_refused(env: Env) -> None:
    infer = ScriptedInference(BASE, BASELINE)
    reworded = (replace(PROTECTED[0], question="a different question?"), *PROTECTED[1:])
    with refuses("replay_not_bound"):
        env.replay(env.arm("baseline"), infer, cases=reworded)
    swapped = replace(PROTECTED[0], context=(task.Passage("p1-a", "swapped"),))
    recontexted = (swapped, *PROTECTED[1:])
    with refuses("replay_not_bound"):
        env.replay(env.arm("baseline"), infer, cases=recontexted)
    assert infer.calls == 0


def test_x_selfcheck_is_computed_and_arms_must_be_journaled(env: Env) -> None:
    arms = env.baseline_and_simple()
    handbuilt = rp.ArmRecord(
        env.arm("candidate", "generated-claimed"),
        tuple(replace(r, verdict=rp.Verdict.CORRECT_ANSWER) for r in arms["baseline"].rows),
    )
    with refuses("arm_not_journaled"):
        env.result({**arms, "candidate": handbuilt})
    tampered = rp.ArmRecord(
        arms["baseline"].spec,
        tuple(replace(r, verdict=rp.Verdict.CORRECT_ANSWER) for r in arms["baseline"].rows),
    )
    with refuses("arm_not_journaled"):
        env.result({"baseline": tampered})
    with refuses("scorer_version_drift"):
        env.result(arms, scorer_=sc.DeterministicScorer("generated-scorer-v9", "r"))


def test_x_a_redigested_forged_body_is_not_stageable(env: Env) -> None:
    result = full_result(env)
    assert rp.stage_decision(
        result, recomputed_digest=result["result_digest"], **env.staging()
    ).stageable
    for change in (
        {"evidence_tier": "customer_witnessed"},
        {"claim_scope": "release"},
        {"release_authorized": True},
        {"fit_authorized": True},
        {"schema_version": "other"},
    ):
        body = {**result["body"], **change}
        forged = {"body": body, "result_digest": task.digest(body)}
        decision = rp.stage_decision(
            forged, recomputed_digest=forged["result_digest"], **env.staging()
        )
        assert not decision.stageable, change
    assert not rp.stage_decision({"body": {}}, recomputed_digest="x", **env.staging()).stageable


def test_x_credentials_are_checked_before_any_provider_observation(env: Env) -> None:
    env.credentials.available = False
    seen: list[str] = []
    real = env.transport.observe

    def spy(binding: mc.ModelBinding, purpose: mc.Purpose) -> mc.Observation:
        seen.append(purpose.value)
        return real(binding, purpose)

    setattr(env.transport, "observe", spy)  # noqa: B010
    with refuses("credential_unavailable"):
        env.replay(env.arm("baseline"), ScriptedInference(BASE, BASELINE))
    assert seen == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("case_id", 1),
        ("answer", {"answer": "generated unexpected object"}),
        ("citations", "x"),
        ("citations", [1]),
        ("abstained", "false"),
    ],
)
def test_candidate_transport_rejects_malformed_typed_output(env: Env, monkeypatch, field, value):
    infer = env.candidate_inference()
    predict = env.transport.predict_for_replay

    def malformed(candidate, inputs):
        rows = predict(candidate, inputs)
        return ({**rows[0], field: value}, *rows[1:])

    monkeypatch.setattr(env.transport, "predict_for_replay", malformed)
    with refuses("output_shape_invalid"):
        env.replay(env.arm("candidate", infer.served_revision), infer)


# --- Replay boundary hardening: ArmOutput invariants hold wherever one is constructed. ---------


def _requests() -> tuple[rp.ReplayRequest, ...]:
    return tuple(rp.ReplayRequest(c.case_id, c.question, c.context) for c in PROTECTED[:5])


def _serve(env: Env, monkeypatch, edit: Any) -> rp.CandidateInference:
    """A candidate whose connector returns the genuine rows after ``edit`` has rewritten them."""
    infer = env.candidate_inference()
    predict = env.transport.predict_for_replay
    monkeypatch.setattr(
        env.transport,
        "predict_for_replay",
        lambda candidate, inputs: edit(predict(candidate, inputs)),
    )
    return infer


class FlippingCitations(list):
    """Generated sentinel: iterates differently on each pass, so check-then-use would diverge."""

    def __init__(self, passes: list[list[Any]]) -> None:
        super().__init__(passes[0])
        self._passes = passes
        self.reads = 0

    def __iter__(self) -> Iterator[Any]:
        content = self._passes[min(self.reads, len(self._passes) - 1)]
        self.reads += 1
        return iter(content)


def test_candidate_citations_are_read_once_so_a_flipping_iterable_cannot_smuggle_a_non_string(
    env: Env, monkeypatch
) -> None:
    flipping = FlippingCitations([["ok"], [1]])
    infer = _serve(env, monkeypatch, lambda rows: ({**rows[0], "citations": flipping}, *rows[1:]))
    outputs = infer.generate(env.arm("candidate", infer.served_revision), _requests())
    assert all(isinstance(c, str) for out in outputs for c in out.citations)
    assert outputs[0].citations == ("ok",)
    assert flipping.reads == 1


def test_candidate_citations_materialised_non_string_is_refused_even_if_a_later_pass_is_clean(
    env: Env, monkeypatch
) -> None:
    flipping = FlippingCitations([[1], ["ok"]])
    infer = _serve(env, monkeypatch, lambda rows: ({**rows[0], "citations": flipping}, *rows[1:]))
    with refuses("output_shape_invalid"):
        infer.generate(env.arm("candidate", infer.served_revision), _requests())
    assert flipping.reads == 1


@pytest.mark.parametrize(
    "edit",
    [
        pytest.param(lambda rows: (None, *rows[1:]), id="row-none"),
        pytest.param(lambda rows: ("generated-p1", *rows[1:]), id="row-not-mapping"),
        pytest.param(lambda rows: (["generated-p1"], *rows[1:]), id="row-list"),
        pytest.param(
            lambda rows: ({k: v for k, v in rows[0].items() if k != "citations"}, *rows[1:]),
            id="missing-citations",
        ),
        pytest.param(
            lambda rows: ({k: v for k, v in rows[0].items() if k != "case_id"}, *rows[1:]),
            id="missing-case-id",
        ),
        pytest.param(lambda rows: list(rows), id="outer-list"),
        pytest.param(lambda rows: None, id="outer-none"),
        pytest.param(lambda rows: iter(rows), id="outer-iterator"),
        pytest.param(lambda rows: ({**rows[0], "case_id": ""}, *rows[1:]), id="empty-case-id"),
    ],
)
def test_candidate_transport_rejects_malformed_rows_and_outer_results(env: Env, monkeypatch, edit):
    infer = _serve(env, monkeypatch, edit)
    with refuses("output_shape_invalid"):
        env.replay(env.arm("candidate", infer.served_revision), infer)


@pytest.mark.parametrize("key", ["protected_answer", "extra", "Answer"])
def test_candidate_transport_refuses_unknown_row_keys(env: Env, monkeypatch, key) -> None:
    """A row must carry exactly the four expected keys; a stray key, however named, is loud."""
    extra = "generated-extra-field-sentinel"
    infer = _serve(env, monkeypatch, lambda rows: ({**rows[0], key: extra}, *rows[1:]))
    with refuses("output_shape_invalid"):
        env.replay(env.arm("candidate", infer.served_revision), infer)


def test_candidate_empty_answer_is_refused_but_none_is_still_an_abstention(
    env: Env, monkeypatch
) -> None:
    """Side by side: "" is malformed output; None is a genuine abstain and scores as before."""
    infer = _serve(env, monkeypatch, lambda rows: ({**rows[0], "answer": ""}, *rows[1:]))
    with refuses("output_shape_invalid"):
        env.replay(env.arm("candidate", infer.served_revision), infer)

    def abstain_first(rows):
        return ({**rows[0], "answer": None, "citations": [], "abstained": True}, *rows[1:])

    env2 = Env(env.root / "abstain")
    infer2 = env2.candidate_inference()
    predict = env2.transport.predict_for_replay
    monkeypatch.setattr(
        env2.transport, "predict_for_replay", lambda c, i: abstain_first(predict(c, i))
    )
    record = env2.replay(env2.arm("candidate", infer2.served_revision), infer2)
    first = record.rows[0]
    assert first.verdict is rp.Verdict.INAPPROPRIATE_REFUSAL
    assert first.answered is False


def test_candidate_valid_output_with_list_citations_still_scores_normally(env: Env) -> None:
    infer = env.candidate_inference()
    arm = env.arm("candidate", infer.served_revision)
    record = env.replay(arm, infer)
    verdicts = {r.verdict for r in record.rows}
    assert verdicts <= {rp.Verdict.CORRECT_ANSWER, rp.Verdict.CORRECT_ABSTENTION}
    assert rp.Verdict.CORRECT_ANSWER in verdicts
    outputs = infer.generate(arm, _requests())
    assert all(isinstance(c, str) for out in outputs for c in out.citations)


BAD_ARM_OUTPUTS = [
    pytest.param(dict(answer=""), ValueError, id="answer-empty"),
    pytest.param(dict(answer="   "), ValueError, id="answer-spaces"),
    pytest.param(dict(answer="\t"), ValueError, id="answer-tab"),
    pytest.param(dict(answer="\n\r\n"), ValueError, id="answer-newline"),
    pytest.param(dict(answer="\u00a0"), ValueError, id="answer-nbsp"),
    pytest.param(dict(answer="\u2003\u3000"), ValueError, id="answer-unicode-spaces"),
    pytest.param(dict(answer="\u200b"), ValueError, id="answer-zero-width-space"),
    pytest.param(dict(answer="\u200d\u2060\ufeff"), ValueError, id="answer-zero-width-joiners"),
    pytest.param(dict(answer=" \u200b\u00ad\t"), ValueError, id="answer-mixed-invisible"),
    pytest.param(dict(answer="\u3164"), ValueError, id="answer-hangul-filler"),
    pytest.param(dict(case_id=1), TypeError, id="case-id-int"),
    pytest.param(dict(case_id=None), TypeError, id="case-id-none"),
    pytest.param(dict(case_id=""), ValueError, id="case-id-empty"),
    pytest.param(dict(answer={"a": 1}), TypeError, id="answer-dict"),
    pytest.param(dict(answer=["x"]), TypeError, id="answer-list"),
    pytest.param(dict(answer=1), TypeError, id="answer-int"),
    pytest.param(dict(answer=float("nan")), TypeError, id="answer-nan"),
    pytest.param(dict(answer=float("inf")), TypeError, id="answer-inf"),
    pytest.param(dict(citations=None), TypeError, id="citations-none"),
    pytest.param(dict(citations="x"), TypeError, id="citations-string"),
    pytest.param(dict(citations='["x"]'), TypeError, id="citations-json-string"),
    pytest.param(dict(citations={"x"}), TypeError, id="citations-set"),
    pytest.param(dict(citations={"x": 1}), TypeError, id="citations-dict"),
    pytest.param(dict(citations=(1,)), TypeError, id="citations-int-element"),
    pytest.param(dict(citations=(["x"],)), TypeError, id="citations-nested-list"),
    pytest.param(dict(citations=(float("nan"),)), TypeError, id="citations-nan-element"),
    pytest.param(dict(abstained="false"), TypeError, id="abstained-string"),
    pytest.param(dict(abstained=1), TypeError, id="abstained-int"),
    pytest.param(dict(abstained=None), TypeError, id="abstained-none"),
]


def _arm_output(**changes: Any) -> rp.ArmOutput:
    fields: dict[str, Any] = dict(
        case_id="generated-p1", answer="generated-answer", citations=("p1-a",), abstained=False
    )
    return rp.ArmOutput(**{**fields, **changes})


@pytest.mark.parametrize(("changes", "error"), BAD_ARM_OUTPUTS)
def test_arm_output_refuses_a_bad_field_wherever_it_is_constructed(changes, error) -> None:
    with pytest.raises(error):
        _arm_output(**changes)


def test_arm_output_materialises_citations_once_and_stores_a_tuple() -> None:
    flipping = FlippingCitations([["ok"], [1]])
    out = _arm_output(citations=flipping)
    assert flipping.reads == 1
    assert out.citations == ("ok",)
    assert type(out.citations) is tuple
    with pytest.raises(TypeError):
        _arm_output(citations=FlippingCitations([[1], ["ok"]]))
    assert _arm_output(citations=["a", "b"]).citations == ("a", "b")
    assert _arm_output(answer=None, citations=(), abstained=True).answer is None


def test_non_candidate_arm_cannot_deliver_a_malformed_arm_output_to_the_scorer(env: Env) -> None:
    class Hostile(ScriptedInference):
        def generate(self, arm: rp.ArmSpec, requests: tuple[rp.ReplayRequest, ...]) -> Any:
            first, *rest = super().generate(arm, requests)
            return (_arm_output(case_id=first.case_id, answer={"generated": "object"}), *rest)

    worker = env.worker()
    with pytest.raises(TypeError):
        env.replay(env.arm("baseline"), Hostile(BASE, BASELINE), worker=worker)
    assert worker.journal.arm(rp.run_id_for(env.manifest, env.arm("baseline"))) is None


def test_candidate_subclass_overriding_generate_cannot_skip_the_arm_output_check(
    env: Env,
) -> None:
    job, candidate = env.fit_and_collect()

    class Overriding(rp.CandidateInference):
        def generate(self, arm: rp.ArmSpec, requests: tuple[rp.ReplayRequest, ...]) -> Any:
            return (_arm_output(case_id=requests[0].case_id, citations=[1]),)

    infer = Overriding(env.preflight, "training", job, candidate, env.reg)
    with pytest.raises(TypeError):
        env.replay(env.arm("candidate", infer.served_revision), infer)


def test_absent_abstain_and_wrong_are_three_different_things(env: Env, monkeypatch) -> None:
    """Absent (no content) is refused; None is an abstain; placeholder text is a wrong answer."""
    for blank in ("", "  ", "\t", "\n", "\u00a0", "\u200b"):
        env_b = Env(env.root / f"blank-{ord(blank[:1] or ' ')}-{len(blank)}")
        infer_b = _serve(
            env_b, monkeypatch, lambda rows, b=blank: ({**rows[0], "answer": b}, *rows[1:])
        )
        with refuses("output_shape_invalid"):
            env_b.replay(env_b.arm("candidate", infer_b.served_revision), infer_b)

    def first_row_verdict(name: str, edit: Any) -> tuple[rp.Verdict, bool]:
        e = Env(env.root / name)
        infer = _serve(e, monkeypatch, edit)
        record = e.replay(e.arm("candidate", infer.served_revision), infer)
        return record.rows[0].verdict, record.rows[0].answered

    abstain = first_row_verdict(
        "abstain",
        lambda rows: ({**rows[0], "answer": None, "citations": [], "abstained": True}, *rows[1:]),
    )
    assert abstain == (rp.Verdict.INAPPROPRIATE_REFUSAL, False)
    for n, placeholder in enumerate(("N/A", "none", "-", "unknown", "\u200bN/A")):
        wrong = first_row_verdict(
            f"wrong-{n}", lambda rows, p=placeholder: ({**rows[0], "answer": p}, *rows[1:])
        )
        assert wrong == (rp.Verdict.SERIOUS_ERROR, True)
