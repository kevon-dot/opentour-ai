"""OPE-509 acceptance tests for the customer-trainer job adapter (generated sentinel trainer).

Acceptance IDs are this ticket's own (no repository binding exists for OPE-509):
A1 real update/collect/replay through the public contract only; A2 version handling;
A3 unverifiable identity; A4 unsupported task; A5 missing cost/stop semantics and benchmark access;
A6 evidence tiers; A7 conformance suite; R1-R9 reconciliation, duplicate, callback, cancel,
credential, restart, fence, bound and lineage behaviour. Fixture kind: generated.
Software behavior only: no real trainer, GPU, provider or customer evidence.
"""

import hashlib
import json
import threading
import time
from contextlib import nullcontext
from dataclasses import replace

import pytest
from opentour_cell.pilot import connector_conformance as cc
from opentour_cell.pilot import model_connector as mc
from opentour_cell.pilot import trainer_conformance as tc
from opentour_cell.pilot.connector_ext import EvidenceTier, Usage
from opentour_cell.pilot.custom_trainer_connector import (
    ADAPTER_VERSION,
    WITNESS_ROLE_CUSTOMER_OWNER,
    Credential,
    CustomerWitness,
    CustomTrainerConnector,
    IntegrationVerdict,
    TrainerJobRequest,
    TrainerRegistration,
    TrainerUsage,
    TrainerView,
    WitnessSubject,
    check_compatibility,
    classify_integration,
    integration_tier,
    is_fixture_trainer,
    replay_matches,
    settle_callback,
    sign_result,
)
from opentour_cell.pilot.reference_customer_trainer import (
    SCORER_VERSION,
    SERIALIZER_VERSION,
    TRAINER_VERSION,
    ReferenceTrainer,
    reference_manifest,
)

SECRET = "generated-trainer-secret"
REVISION = "generated-customer-model-r1"
TASK = "generated-ticket-route"
ROWS = (
    {"text": "refund invoice overdue", "label": "billing"},
    {"text": "invoice refund request", "label": "billing"},
    {"text": "password reset login", "label": "access"},
    {"text": "login locked password", "label": "access"},
)
FROZEN = (
    {"text": "please refund this invoice"},
    {"text": "cannot login password expired"},
)
EXPECTED = ({"label": "billing"}, {"label": "access"})
LIMIT = mc.FitBound("USD", 500, 60, 20, 100)
TOKEN = hashlib.sha256(b"generated-fit-intent-1").hexdigest()


def binding(**changes):
    base = mc.ModelBinding(
        tenant="generated-tenant",
        model_ref="generated-customer-model",
        immutable_revision=REVISION,
        task=TASK,
        input_schema="text-v1",
        output_schema="label-v1",
        preprocessing_revision="preprocessing-v1",
        route=mc.Route.CUSTOM,
        account_or_host="trainer.generated.invalid",
        region="generated-region",
        credential_ref="generated-credential-ref",
    )
    return replace(base, **changes)


def registration(**changes):
    base = TrainerRegistration(
        tenant="generated-tenant",
        account_or_host="trainer.generated.invalid",
        supported_adapter_versions=frozenset({ADAPTER_VERSION}),
        supported_trainer_versions=frozenset({TRAINER_VERSION}),
        serializer_version=SERIALIZER_VERSION,
        scorer_version=SCORER_VERSION,
        replay_mode="deterministic",
        protected_fields=frozenset({"gold_answer"}),
    )
    return replace(base, **changes)


class Env:
    def __init__(self, tmp_path, *, manifest=None, wrap=None, connector_secret=SECRET):
        self.tmp = tmp_path
        self.trainer = ReferenceTrainer(
            tmp_path / "trainer", SECRET, REVISION, frozenset({TASK}), manifest=manifest
        )
        self.transport = wrap(self.trainer) if wrap else self.trainer
        self.secret = connector_secret
        self.binding = binding()
        self.registration = registration()
        self.clock_value = 100
        self.connector = self.make_connector()
        self.grants = {
            purpose: mc.Grant(
                "training",
                self.binding,
                purpose,
                "grant-v1",
                1000,
                frozenset({"text", "label"}),
                1000,
                LIMIT if purpose is mc.Purpose.TRAINING else None,
                frozenset({"benchmark-v1"}) if purpose is mc.Purpose.TRAINING else frozenset(),
            )
            for purpose in mc.Purpose
        }
        self.preflight = self.make_preflight(self.connector)
        draft = mc.FitIntent(
            self.binding,
            "grant-v1",
            "benchmark-v1",
            "pending",
            "generated-fit-intent-1",
            frozenset({"text", "label"}),
            LIMIT,
        )
        digest = hashlib.sha256(self.connector.serialize_development(draft, ROWS)).hexdigest()
        self.intent = replace(draft, development_digest=digest)

    def make_connector(self, transport=None):
        return CustomTrainerConnector(
            transport or self.transport,
            [self.registration],
            lambda tenant, ref: Credential(self.secret) if self.secret else None,
            self.tmp / "connector",
            clock=lambda: self.clock_value,
        )

    def make_preflight(self, connector):
        def grant(actor, selected, purpose):
            held = self.grants.get(purpose)
            return held if held and (held.actor, held.binding) == (actor, selected) else None

        return mc.Preflight(connector, grant, clock=lambda: self.clock_value)

    def dispatch(self, preflight=None, intent=None):
        return (preflight or self.preflight).submit_reserved_fit(
            "training", intent or self.intent, ROWS, reservation_guard=nullcontext
        )

    def direct_submit(self, intent=None):
        intent = intent or self.intent
        return self.connector.submit(intent, self.connector.serialize_development(intent, ROWS))

    def executions(self):
        return len(list((self.tmp / "trainer" / "jobs").glob("*.json")))


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


class Proxy:
    def __init__(self, inner):
        self.inner = inner

    def __getattr__(self, name):
        return getattr(self.inner, name)


class LostAck(Proxy):
    def start(self, credential, request):
        self.inner.start(credential, request)
        raise OSError("simulated:lost-acknowledgement")


class NeverArrives(Proxy):
    def start(self, credential, request):
        raise OSError("simulated:request-never-arrived")


class Blind(LostAck):
    def find(self, credential, client_token):
        raise OSError("simulated:lookup-unreachable")


# ---- A1: real update through the public contract only ----


def test_reference_trainer_end_to_end_real_update_collect_replay(env):
    receipt = env.dispatch()
    assert receipt.state is mc.JobState.QUEUED and receipt.job_ref and receipt.evidence_ref
    env.trainer.run_pending()
    done = env.connector.status(receipt)
    assert done.state is mc.JobState.COMPLETED
    candidate = env.preflight.collect_candidate("training", done)
    assert candidate.parent_revision == REVISION
    assert candidate.artifact_or_serving_revision.startswith("generated-artifact-")
    assert env.connector.predict_for_replay(candidate, FROZEN) == EXPECTED
    assert env.connector.predict_for_replay(candidate, FROZEN) == EXPECTED
    assert env.executions() == 1


def test_collect_before_completion_is_refused(env):
    receipt = env.dispatch()
    with pytest.raises(ValueError, match="job_not_completed"):
        env.connector.collect(receipt)
    with pytest.raises(mc.CapabilityError, match="candidate_lineage_unverified"):
        env.preflight.collect_candidate("training", receipt)


def test_connector_uses_only_the_public_transport_methods(tmp_path):
    public = {
        "describe",
        "quote",
        "start",
        "find",
        "poll",
        "request_cancel",
        "fetch_result",
        "predict",
    }
    seen = set()

    class Spy(Proxy):
        def __getattribute__(self, name):
            if not name.startswith("_") and name != "inner":
                seen.add(name)
            return object.__getattribute__(self, name)

    spy = Env(tmp_path, wrap=Spy)
    receipt = spy.dispatch()
    spy.trainer.run_pending()
    spy.connector.collect(spy.connector.status(receipt))
    assert seen and seen <= public


# ---- A2: versions are explicit, never coerced ----


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"trainer_version": "generated-reference-trainer-9.9"}, "trainer_version_unsupported"),
        ({"trainer_version": ""}, "trainer_version_unknown"),
        ({"adapter_version": "opentour.pilot.trainer-adapter.v2"}, "adapter_version_mismatch"),
        ({"adapter_version": ""}, "adapter_version_unknown"),
        ({"serializer_version": "generated-other-serializer"}, "serializer_version_mismatch"),
        ({"scorer_version": "generated-other-scorer"}, "scorer_version_mismatch"),
        ({"replay_mode": "tolerance", "replay_tolerance": 0.1}, "replay_declaration_mismatch"),
    ],
)
def test_unsupported_or_unknown_version_is_refused_explicitly(tmp_path, changes, reason):
    manifest = replace(reference_manifest(REVISION, frozenset({TASK})), **changes)
    env = Env(tmp_path, manifest=manifest)
    verdict = env.connector.check_compatibility(env.binding)
    assert (verdict.ok, verdict.reason) == (False, reason)
    with pytest.raises(ValueError, match=reason):
        env.connector.observe(env.binding, mc.Purpose.TRAINING)
    capability = env.preflight.discover("training", env.binding, mc.Purpose.TRAINING)
    assert capability.state is mc.State.NEEDS_CONFIGURATION
    with pytest.raises(mc.CapabilityError):
        env.dispatch()
    assert env.executions() == 0


def test_trainer_upgraded_between_observe_and_submit_is_refused(env):
    env.preflight.discover("training", env.binding, mc.Purpose.TRAINING)
    env.trainer.manifest = replace(env.trainer.manifest, trainer_version="generated-upgraded-2")
    with pytest.raises(ValueError, match="trainer_version_unsupported"):
        env.direct_submit()
    assert env.executions() == 0


def test_trainer_version_change_between_accept_and_collect_is_refused(env):
    receipt = env.dispatch()
    env.trainer.run_pending()
    done = env.connector.status(receipt)
    env.trainer.manifest = replace(env.trainer.manifest, trainer_version="generated-upgraded-2")
    with pytest.raises(ValueError, match="trainer_version_unsupported"):
        env.connector.collect(done)


def test_compatible_versions_report_compatible(env):
    verdict = env.connector.check_compatibility(env.binding)
    assert verdict.ok and verdict.reason == "compatible"
    assert check_compatibility(env.registration, env.trainer.manifest).ok


# ---- A3 / A4 / A5: identity, task, cost and stop semantics ----


def test_unverifiable_model_identity_is_refused(tmp_path):
    env = Env(tmp_path, manifest=reference_manifest("", frozenset({TASK})))
    capability = env.preflight.discover("training", env.binding, mc.Purpose.TRAINING)
    assert (capability.state, capability.reason) == (
        mc.State.NEEDS_CONFIGURATION,
        "exact_model_revision_unverified",
    )


def test_installed_model_revision_mismatch_is_not_substituted(env):
    env.trainer.manifest = replace(env.trainer.manifest, model_revision="generated-other-r2")
    capability = env.preflight.discover("training", env.binding, mc.Purpose.TRAINING)
    assert capability.reason == "exact_model_revision_unverified"
    with pytest.raises(ValueError, match="installed_model_mismatch"):
        env.direct_submit()
    assert env.executions() == 0


def test_unsupported_task_is_refused(env):
    other = binding(task="generated-unsupported-task")
    observed = env.connector.observe(other, mc.Purpose.TRAINING)
    assert not observed.inference_supported and not observed.training_supported
    env.grants = {p: replace(g, binding=other) for p, g in env.grants.items()}
    capability = env.preflight.discover("training", other, mc.Purpose.TRAINING)
    assert capability.state is not mc.State.TRAINABLE
    assert capability.reason == "training_unavailable" or capability.reason.startswith("exact")
    inference = env.preflight.discover("training", other, mc.Purpose.BASELINE)
    assert inference.state is mc.State.UNSUPPORTED


@pytest.mark.parametrize("missing", ["stop_semantics", "hard_cost_cap", "cancellation"])
def test_missing_cost_or_stop_semantics_refused_under_hard_cap(tmp_path, missing):
    manifest = replace(reference_manifest(REVISION, frozenset({TASK})), **{missing: False})
    env = Env(tmp_path, manifest=manifest)
    assert env.connector.observe(env.binding, mc.Purpose.TRAINING).hard_cost_cap is False
    capability = env.preflight.discover("training", env.binding, mc.Purpose.TRAINING)
    assert capability.reason == "enforceable_training_budget_required"
    with pytest.raises(mc.CapabilityError, match="enforceable_training_cap_unavailable"):
        env.dispatch()
    assert env.executions() == 0
    # benchmark access is unaffected by the training refusal
    baseline = env.preflight.discover("training", env.binding, mc.Purpose.BASELINE)
    assert baseline.state is mc.State.BENCHMARK_ONLY


def test_trainer_that_cannot_quote_a_bound_is_refused(tmp_path):
    class NoQuote(Proxy):
        def quote(self, credential, task, bound):
            return None

    env = Env(tmp_path, wrap=NoQuote)
    with pytest.raises(mc.CapabilityError, match="known_training_cost_within_budget_required"):
        env.dispatch()
    assert env.executions() == 0


def test_trainer_quote_above_authorized_bound_is_refused(tmp_path):
    class Greedy(Proxy):
        def quote(self, credential, task, bound):
            return replace(bound, maximum_minor_units=bound.maximum_minor_units + 1)

    env = Env(tmp_path, wrap=Greedy)
    with pytest.raises(mc.CapabilityError, match="known_training_cost_within_budget_required"):
        env.dispatch()
    assert env.executions() == 0


# ---- A6: evidence tiers ----


REAL_ORIGIN = "customer-trainer-a"
WITNESS_SUBJECT = WitnessSubject(
    tenant="tenant-sentinel-1",
    engagement="engagement-sentinel-1",
    trainer_id=REAL_ORIGIN,
    trainer_revision="trainer-revision-sentinel-1",
    job_ref="job-sentinel-1",
)


def _witness(**changes):
    base = CustomerWitness(
        tenant=WITNESS_SUBJECT.tenant,
        engagement=WITNESS_SUBJECT.engagement,
        trainer_id=WITNESS_SUBJECT.trainer_id,
        trainer_revision=WITNESS_SUBJECT.trainer_revision,
        job_ref=WITNESS_SUBJECT.job_ref,
        witness_role=WITNESS_ROLE_CUSTOMER_OWNER,
        attestation="generated-attestation-sentinel",
    )
    return replace(base, **changes)


class AcceptingWitnessVerifierDouble:
    """Test double only: accepts every record. No production verifier exists (OPE-566/OPE-582)."""

    def __init__(self):
        self.calls = []

    def verify_customer_witness(self, witness, subject):
        self.calls.append((witness, subject))
        return True


class RefusingWitnessVerifierDouble:
    def verify_customer_witness(self, witness, subject):
        return False


class RaisingWitnessVerifierDouble:
    def verify_customer_witness(self, witness, subject):
        raise RuntimeError("generated verifier failure")


class TruthyWitnessVerifierDouble:
    def verify_customer_witness(self, witness, subject):
        return "yes"


def _classify(origin=REAL_ORIGIN, qualification="qualification-1", witness=None, **kwargs):
    kwargs.setdefault("subject", replace(WITNESS_SUBJECT, trainer_id=origin))
    return classify_integration(origin, qualification, witness, **kwargs)


def test_generated_trainer_is_fixture_tier_whatever_refs_are_claimed():
    for origin in ("generated-reference-trainer", "simulated:trainer", "trainer.generated.invalid"):
        assert integration_tier(origin, "qual-ref", "witness-ref") is EvidenceTier.FIXTURE
    assert integration_tier("", "qual-ref", "witness-ref") is EvidenceTier.FIXTURE


def test_real_origin_tiers_need_their_own_refs():
    assert integration_tier(REAL_ORIGIN, None, None) is EvidenceTier.FIXTURE
    assert integration_tier(REAL_ORIGIN, "  ", None) is EvidenceTier.FIXTURE
    assert integration_tier(REAL_ORIGIN, "qualification-1", None) is (
        EvidenceTier.SOFTWARE_QUALIFIED
    )


def test_bare_witness_string_never_reaches_customer_witnessed():
    # The old classifier returned CUSTOMER_WITNESSED for any non-empty string.
    verifier = AcceptingWitnessVerifierDouble()
    for bare in ("witness-1", "customer_witnessed", "signed-by-customer"):
        verdict = _classify(witness=bare, witness_verifier=verifier)
        assert verdict == IntegrationVerdict(
            EvidenceTier.SOFTWARE_QUALIFIED, "customer_witness_unstructured"
        )
        assert integration_tier(REAL_ORIGIN, "qualification-1", bare) is (
            EvidenceTier.SOFTWARE_QUALIFIED
        )
    assert verifier.calls == []


def test_empty_or_whitespace_witness_is_not_a_witness():
    verifier = AcceptingWitnessVerifierDouble()
    for bare in ("", " ", "\t\n"):
        assert _classify(witness=bare, witness_verifier=verifier).tier is (
            EvidenceTier.SOFTWARE_QUALIFIED
        )
    for field_name in ("tenant", "engagement", "trainer_id", "job_ref", "attestation"):
        for blank in ("", "   ", "job​"):
            verdict = _classify(witness=_witness(**{field_name: blank}), witness_verifier=verifier)
            assert verdict == IntegrationVerdict(
                EvidenceTier.SOFTWARE_QUALIFIED, "customer_witness_malformed"
            )
    assert verifier.calls == []


def test_structured_witness_without_a_verifier_is_unverifiable():
    verdict = _classify(witness=_witness())
    assert verdict == IntegrationVerdict(
        EvidenceTier.SOFTWARE_QUALIFIED, "customer_witness_unverifiable"
    )
    assert integration_tier(
        REAL_ORIGIN, "qualification-1", _witness(), subject=WITNESS_SUBJECT
    ) is (EvidenceTier.SOFTWARE_QUALIFIED)


def test_a_verifier_that_refuses_raises_or_answers_truthy_is_a_refusal():
    for verifier in (
        RefusingWitnessVerifierDouble(),
        RaisingWitnessVerifierDouble(),
        TruthyWitnessVerifierDouble(),
    ):
        verdict = _classify(witness=_witness(), witness_verifier=verifier)
        assert verdict == IntegrationVerdict(
            EvidenceTier.SOFTWARE_QUALIFIED, "customer_witness_refused"
        )


@pytest.mark.parametrize(
    "field_name", ["tenant", "engagement", "trainer_id", "trainer_revision", "job_ref"]
)
def test_witness_for_another_subject_is_refused_before_verification(field_name):
    verifier = AcceptingWitnessVerifierDouble()
    wrong = _witness(**{field_name: "other-" + getattr(WITNESS_SUBJECT, field_name)})
    verdict = _classify(witness=wrong, witness_verifier=verifier)
    assert verdict == IntegrationVerdict(
        EvidenceTier.SOFTWARE_QUALIFIED, "customer_witness_binding_mismatch"
    )
    # A case-only difference is a different binding, not a normalised match.
    upper = _witness(**{field_name: getattr(WITNESS_SUBJECT, field_name).upper()})
    assert _classify(witness=upper, witness_verifier=verifier).tier is (
        EvidenceTier.SOFTWARE_QUALIFIED
    )
    assert verifier.calls == []


def test_witness_needs_a_subject_for_this_trainer_and_the_customer_owner_role():
    verifier = AcceptingWitnessVerifierDouble()
    assert _classify(witness=_witness(), subject=None, witness_verifier=verifier).reason == (
        "customer_witness_binding_mismatch"
    )
    other_trainer = replace(WITNESS_SUBJECT, trainer_id="customer-trainer-b")
    assert _classify(
        witness=_witness(), subject=other_trainer, witness_verifier=verifier
    ).reason == ("customer_witness_binding_mismatch")
    for role in ("operator_custodian", "Customer_Owner", "auditor"):
        verdict = _classify(witness=_witness(witness_role=role), witness_verifier=verifier)
        assert verdict == IntegrationVerdict(
            EvidenceTier.SOFTWARE_QUALIFIED, "customer_witness_role_refused"
        )
    assert verifier.calls == []


def test_witness_cannot_stand_in_for_missing_software_qualification():
    verdict = _classify(
        qualification=None, witness=_witness(), witness_verifier=AcceptingWitnessVerifierDouble()
    )
    assert verdict == IntegrationVerdict(EvidenceTier.FIXTURE, "software_qualification_absent")


def test_duck_typed_witness_record_is_unstructured():
    class LookalikeWitness:
        tenant = WITNESS_SUBJECT.tenant
        engagement = WITNESS_SUBJECT.engagement
        trainer_id = WITNESS_SUBJECT.trainer_id
        trainer_revision = WITNESS_SUBJECT.trainer_revision
        job_ref = WITNESS_SUBJECT.job_ref
        witness_role = WITNESS_ROLE_CUSTOMER_OWNER
        attestation = "generated-attestation-sentinel"

    verdict = _classify(
        witness=LookalikeWitness(), witness_verifier=AcceptingWitnessVerifierDouble()
    )
    assert verdict.reason == "customer_witness_unstructured"


@pytest.mark.parametrize(
    "origin",
    [
        "Generated-trainer",
        "GENERATED-trainer",
        "GENERATED-",
        "generated_trainer",
        "Simulated:trainer",
        "acme-reference-trainer",
        "Reference-Trainer",
        "FIXTURE trainer",
        "Ｇｅｎｅｒａｔｅｄ-trainer",  # fullwidth "Generated"
        "ＧＥＮＥＲＡＴＥＤ－trainer",  # fullwidth upper
        "gen​erated-trainer",  # zero-width space
        "​generated-trainer",
        "generated⁠-trainer",  # word joiner
        "﻿customer-trainer-a",  # byte-order mark on an otherwise real id
        "gеnerated-trainer",  # Cyrillic homoglyph
        "trainer.GENERATED.INVALID",
        "customer-trainer.invalid.",
        "  ",
    ],
)
def test_generated_variants_are_fixture_even_with_a_verified_witness(origin):
    verifier = AcceptingWitnessVerifierDouble()
    verdict = _classify(
        origin=origin, witness=_witness(trainer_id=origin), witness_verifier=verifier
    )
    assert verdict.tier is EvidenceTier.FIXTURE
    assert verdict.reason in ("fixture_trainer", "trainer_identity_unverifiable")
    assert is_fixture_trainer(origin)
    assert verifier.calls == []


def test_reference_trainer_with_a_valid_looking_witness_stays_fixture():
    verifier = AcceptingWitnessVerifierDouble()
    for origin in ("generated-reference-trainer", TRAINER_VERSION, "reference-trainer"):
        witness = _witness(trainer_id=origin, trainer_revision=TRAINER_VERSION)
        subject = replace(WITNESS_SUBJECT, trainer_id=origin, trainer_revision=TRAINER_VERSION)
        verdict = _classify(
            origin=origin, witness=witness, subject=subject, witness_verifier=verifier
        )
        assert verdict == IntegrationVerdict(EvidenceTier.FIXTURE, "fixture_trainer")
    assert verifier.calls == []


def test_non_string_trainer_identity_is_fixture():
    for origin in (None, 7, b"customer-trainer-a"):
        assert integration_tier(origin, "qualification-1", _witness()) is EvidenceTier.FIXTURE


def test_customer_witnessed_only_with_an_injected_verifier_that_authenticates_the_record():
    verifier = AcceptingWitnessVerifierDouble()
    verdict = _classify(witness=_witness(), witness_verifier=verifier)
    assert verdict == IntegrationVerdict(
        EvidenceTier.CUSTOMER_WITNESSED, "customer_witness_verified"
    )
    assert verifier.calls == [(_witness(), WITNESS_SUBJECT)]
    assert (
        integration_tier(
            REAL_ORIGIN,
            "qualification-1",
            _witness(),
            subject=WITNESS_SUBJECT,
            witness_verifier=AcceptingWitnessVerifierDouble(),
        )
        is EvidenceTier.CUSTOMER_WITNESSED
    )


# ---- A7: conformance suite ----


def _harness(root, make=None):
    def default(path):
        return ReferenceTrainer(root / "conf", SECRET, REVISION, frozenset({TASK}))

    def request(token):
        payload = json.dumps({"rows": list(ROWS)}, sort_keys=True).encode()
        return TrainerJobRequest(
            ADAPTER_VERSION,
            token,
            TASK,
            REVISION,
            SERIALIZER_VERSION,
            SCORER_VERSION,
            "benchmark-v1",
            hashlib.sha256(payload).hexdigest(),
            payload,
            LIMIT,
        )

    return tc.TrainerHarness(
        make or default,
        Credential(SECRET),
        registration(),
        request,
        lambda transport: transport.run_pending(),
        FROZEN,
        TASK,
        REVISION,
    )


def test_conformance_suite_passes_for_reference_trainer(tmp_path):
    results = tc.run_trainer_conformance(_harness(tmp_path), tmp_path)
    assert len(results) == 9
    assert tc.all_passed(results), [r for r in results if r.outcome is not tc.Outcome.PASSED]


def test_shared_connector_conformance_for_custom_trainer(tmp_path):
    class ImmediateTrainer(Proxy):
        def start(self, credential, request):
            receipt = self.inner.start(credential, request)
            self.inner.run_pending()
            return self.inner.poll(credential, receipt.job_id)

    def connector(root, wrap=None):
        environment = Env(root, wrap=wrap)
        environment.clock_value = int(time.time())
        return environment.connector

    selected = binding()
    template = Env(tmp_path / "intent").intent
    harness = cc.ConnectorHarness(
        make_connector=lambda root: connector(root, ImmediateTrainer),
        binding=selected,
        mismatched_binding=binding(immutable_revision="generated-stale-revision"),
        make_intent=lambda key: replace(template, idempotency_key=key),
        rows=ROWS,
        executions=lambda root: sum(
            json.loads(job.read_text())["state"] == "completed"
            for job in (root / "trainer" / "jobs").glob("*.json")
        ),
        make_slow_connector=connector,
        fence_scope=selected.tenant,
    )
    results = cc.run_conformance(harness, tmp_path / "shared")
    assert len(results) == 10
    assert cc.all_passed(results), [r for r in results if r.outcome is not cc.Outcome.PASSED]


def test_conformance_suite_fails_a_nonidempotent_trainer(tmp_path):
    class Duplicating(ReferenceTrainer):
        def start(self, credential, request):
            self._authorize(credential)
            job_id = f"generated-job-{len(list((self.root / 'jobs').glob('*.json')))}"
            self._save(
                {
                    "job_id": job_id,
                    "client_token": request.client_token,
                    "state": "queued",
                    "request": {
                        "model_revision": request.model_revision,
                        "development": request.development.decode(),
                        "bound_rows": 1,
                    },
                }
            )
            return self._view(self._load(job_id))

    def make(path):
        return Duplicating(tmp_path / "dup", SECRET, REVISION, frozenset({TASK}))

    results = tc.run_trainer_conformance(_harness(tmp_path, make), tmp_path)
    assert "start_idempotent_on_client_token" in {
        r.name for r in results if r.outcome is tc.Outcome.FAILED
    }
    assert not tc.all_passed(results)


def test_conformance_suite_fails_unsigned_results(tmp_path):
    class Unsigned(ReferenceTrainer):
        def fetch_result(self, credential, job_id):
            return replace(super().fetch_result(credential, job_id), signature="")

    def make(path):
        return Unsigned(tmp_path / "unsigned", SECRET, REVISION, frozenset({TASK}))

    results = tc.run_trainer_conformance(_harness(tmp_path, make), tmp_path)
    failed = {r.name for r in results if r.outcome is tc.Outcome.FAILED}
    assert "result_signed_and_parent_bound" in failed


def test_conformance_empty_result_is_not_a_pass():
    assert not tc.all_passed(())


# ---- R1/R2: lost response reconciles, never blind retry, unknown is not success ----


def test_lost_acknowledgement_is_recovered_by_lookup_not_resend(tmp_path):
    env = Env(tmp_path, wrap=LostAck)
    receipt = env.dispatch()
    assert receipt.state is mc.JobState.QUEUED and receipt.job_ref
    assert env.executions() == 1  # exactly one execution despite the lost reply


def test_request_that_never_arrived_is_unknown_and_never_resent(tmp_path):
    env = Env(tmp_path, wrap=NeverArrives)
    receipt = env.dispatch()
    assert receipt.state is mc.JobState.UNKNOWN
    assert env.executions() == 0
    again = env.dispatch()  # UNKNOWN does not authorize a retry
    assert again.state is mc.JobState.UNKNOWN and env.executions() == 0
    with pytest.raises(mc.CapabilityError, match="candidate_lineage_unverified"):
        env.preflight.collect_candidate("training", again)


def test_unverifiable_trainer_after_lost_ack_is_unknown_not_success(tmp_path):
    env = Env(tmp_path, wrap=Blind)
    receipt = env.dispatch()
    assert receipt.state is mc.JobState.UNKNOWN
    assert env.executions() == 1
    with pytest.raises(mc.CapabilityError):
        env.preflight.collect_candidate("training", receipt)


def test_unrecognized_trainer_state_is_unknown_never_completed(env):
    receipt = env.dispatch()

    class Odd(Proxy):
        def poll(self, credential, job_id):
            return replace(self.inner.poll(credential, job_id), state="succeeded-ish")

    odd = env.make_connector(Odd(env.trainer))
    reported = odd.status(receipt)
    assert reported.state is mc.JobState.UNKNOWN
    with pytest.raises(ValueError):
        odd.collect(reported)


def test_trainer_reporting_foreign_job_identity_is_refused(env):
    receipt = env.dispatch()

    class Foreign(Proxy):
        def poll(self, credential, job_id):
            return TrainerView("generated-job-other", "0" * 64, "completed")

    with pytest.raises(ValueError, match="trainer_identity_mismatch"):
        env.make_connector(Foreign(env.trainer)).status(receipt)


def test_lookup_never_creates_and_unknown_intent_returns_none(env):
    assert env.connector.lookup(env.intent) is None
    assert env.executions() == 0


def test_transport_fault_during_poll_surfaces_and_changes_nothing(env):
    receipt = env.dispatch()

    class Down(Proxy):
        def poll(self, credential, job_id):
            raise OSError("simulated:trainer-down")

    with pytest.raises(OSError):
        env.make_connector(Down(env.trainer)).status(receipt)
    assert env.connector.status(receipt).state is mc.JobState.QUEUED


# ---- R3: duplicate dispatch, conflict ----


def test_duplicate_dispatch_does_not_double_execute(env):
    first = env.dispatch()
    second = env.dispatch()
    third = env.direct_submit()
    assert first == second == third
    assert env.executions() == 1


def test_concurrent_duplicate_dispatch_executes_once(env):
    def go():
        try:
            env.dispatch()
        except (OSError, ValueError, mc.CapabilityError):
            pass  # a loser may be unconfirmed; it must never start a job

    threads = [threading.Thread(target=go) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert env.executions() == 1


def test_same_key_with_different_intent_is_refused(env):
    env.dispatch()
    changed = replace(env.intent, benchmark_revision="benchmark-v2")
    with pytest.raises(ValueError, match="idempotency_conflict"):
        env.direct_submit(changed)
    assert env.executions() == 1


# ---- R4: late callback / settled state ----


def test_late_callback_for_settled_job_changes_nothing(env):
    receipt = env.dispatch()
    env.trainer.run_pending()
    done = env.connector.status(receipt)
    assert done.state is mc.JobState.COMPLETED
    late = TrainerView(receipt.job_ref, TOKEN, "failed")
    assert settle_callback(env.connector, env.intent, late) is False
    assert env.connector.status(receipt) == done


def test_late_trainer_state_after_terminal_changes_nothing(env):
    receipt = env.dispatch()
    env.trainer.run_pending()
    done = env.connector.status(receipt)

    class Reopens(Proxy):
        def poll(self, credential, job_id):
            return replace(self.inner.poll(credential, job_id), state="running")

    assert env.make_connector(Reopens(env.trainer)).status(receipt) == done


def test_callback_with_wrong_token_or_unknown_job_is_ignored(env):
    receipt = env.dispatch()
    assert (
        settle_callback(env.connector, env.intent, TrainerView(receipt.job_ref, "0" * 64, "failed"))
        is False
    )
    other = replace(env.intent, idempotency_key="generated-never-submitted")
    token = hashlib.sha256(b"generated-never-submitted").hexdigest()
    assert (
        settle_callback(env.connector, other, TrainerView("generated-job-x", token, "failed"))
        is False
    )
    assert env.connector.status(receipt).state is mc.JobState.QUEUED


def test_valid_callback_settles_once(env):
    receipt = env.dispatch()

    class Failed(Proxy):
        def poll(self, credential, job_id):
            return replace(self.inner.poll(credential, job_id), state="failed")

    connector = env.make_connector(Failed(env.trainer))
    hint = TrainerView(receipt.job_ref, TOKEN, "failed")
    assert settle_callback(connector, env.intent, hint)
    assert not settle_callback(
        connector, env.intent, TrainerView(receipt.job_ref, TOKEN, "completed")
    )
    assert env.connector.status(receipt).state is mc.JobState.FAILED


# ---- R5: cancel is fenced ----


def test_cancel_before_completion_never_publishes_a_candidate(env):
    receipt = env.dispatch()
    canceled = env.connector.cancel(receipt)
    assert canceled.state is mc.JobState.CANCELED
    env.trainer.run_pending()
    assert env.connector.status(receipt).state is mc.JobState.CANCELED
    with pytest.raises(ValueError, match="job_canceled"):
        env.connector.collect(receipt)
    with pytest.raises(mc.CapabilityError):
        env.preflight.collect_candidate("training", canceled)


def test_trainer_finishing_despite_cancel_is_still_canceled(env):
    receipt = env.dispatch()

    class Ignores(Proxy):
        def request_cancel(self, credential, job_id):
            return self.inner.poll(credential, job_id)  # the trainer ignores the stop

    connector = env.make_connector(Ignores(env.trainer))
    assert connector.cancel(receipt).state is mc.JobState.RUNNING
    env.trainer.run_pending()
    assert connector.status(receipt).state is mc.JobState.CANCELED
    with pytest.raises(ValueError, match="job_canceled"):
        connector.collect(receipt)


def test_cancel_intent_is_durable_before_the_stop_is_sent(env):
    receipt = env.dispatch()

    class Dies(Proxy):
        def request_cancel(self, credential, job_id):
            raise OSError("simulated:crash-before-stop")

    with pytest.raises(OSError):
        env.make_connector(Dies(env.trainer)).cancel(receipt)
    env.trainer.run_pending()
    restarted = env.make_connector()
    assert restarted.status(receipt).state is mc.JobState.CANCELED
    with pytest.raises(ValueError, match="job_canceled"):
        restarted.collect(receipt)


def test_cancel_on_trainer_without_cancellation_is_refused(env):
    receipt = env.dispatch()
    env.trainer.manifest = replace(env.trainer.manifest, cancellation=False)
    with pytest.raises(ValueError, match="cancellation_unsupported"):
        env.connector.cancel(receipt)


def test_cancel_after_completion_leaves_settled_job_unchanged(env):
    receipt = env.dispatch()
    env.trainer.run_pending()
    done = env.connector.status(receipt)
    assert env.connector.cancel(done).state is mc.JobState.COMPLETED


# ---- R6: credentials and destinations fail closed without leaking ----


def test_missing_credential_fails_closed_without_leaking(tmp_path):
    env = Env(tmp_path, connector_secret=None)
    with pytest.raises(ValueError) as caught:
        env.connector.observe(env.binding, mc.Purpose.TRAINING)
    assert str(caught.value) == "credential_unavailable"
    capability = env.preflight.discover("training", env.binding, mc.Purpose.TRAINING)
    assert capability.state is mc.State.NEEDS_CONFIGURATION
    with pytest.raises(mc.CapabilityError):
        env.dispatch()
    assert env.executions() == 0


def test_wrong_credential_is_rejected_by_trainer_and_never_echoed(tmp_path):
    env = Env(tmp_path, connector_secret="generated-wrong-secret")
    with pytest.raises(OSError) as caught:
        env.connector.observe(env.binding, mc.Purpose.TRAINING)
    assert "generated-wrong-secret" not in str(caught.value)
    assert SECRET not in str(caught.value)
    assert env.executions() == 0


def test_credential_is_redacted_everywhere():
    credential = Credential("generated-very-secret")
    assert "generated-very-secret" not in f"{credential!r}{credential}"


def test_unregistered_destination_is_refused(env):
    rogue = binding(account_or_host="rogue.generated.invalid")
    with pytest.raises(ValueError, match="destination_not_registered"):
        env.connector.observe(rogue, mc.Purpose.TRAINING)


def test_other_tenant_cannot_use_the_registration(env):
    with pytest.raises(ValueError, match="destination_not_registered"):
        env.connector.observe(binding(tenant="generated-other-tenant"), mc.Purpose.TRAINING)


def test_non_custom_route_is_refused(env):
    with pytest.raises(ValueError, match="route_not_supported"):
        env.connector.observe(binding(route=mc.Route.MANAGED), mc.Purpose.TRAINING)


def test_protected_answers_never_reach_the_payload(env):
    leaky = replace(env.intent, fields=frozenset({"text", "label", "gold_answer"}))
    with pytest.raises(ValueError, match="protected_field_in_development"):
        env.connector.serialize_development(leaky, ROWS)
    payload = env.connector.serialize_development(env.intent, ROWS)
    assert b"gold_answer" not in payload and SECRET.encode() not in payload


def test_development_rows_must_match_intent_fields(env):
    with pytest.raises(ValueError, match="development_rows_invalid"):
        env.connector.serialize_development(env.intent, ({"text": "only"},))
    with pytest.raises(ValueError, match="development_rows_invalid"):
        env.connector.serialize_development(env.intent, ())


def test_payload_digest_mismatch_is_refused_at_the_connector(env):
    with pytest.raises(ValueError, match="development_digest_mismatch"):
        env.connector.submit(env.intent, b"tampered")
    assert env.executions() == 0


# ---- R7: restart / replay ----


def test_restart_resumes_the_same_job_without_a_second_execution(env):
    receipt = env.dispatch()
    env.trainer.run_pending()
    fresh_trainer = ReferenceTrainer(env.tmp / "trainer", SECRET, REVISION, frozenset({TASK}))
    restarted = env.make_connector(fresh_trainer)
    fresh = env.make_preflight(restarted)
    again = env.dispatch(fresh)
    assert again.job_ref == receipt.job_ref
    done = restarted.status(again)
    assert done.state is mc.JobState.COMPLETED
    assert env.executions() == 1
    candidate = fresh.collect_candidate("training", done)
    assert restarted.predict_for_replay(candidate, FROZEN) == EXPECTED


def test_restart_after_crash_before_ack_reconciles_without_resend(tmp_path):
    env = Env(tmp_path, wrap=LostAck)
    with pytest.raises(OSError):
        env.direct_submit()
    restarted = env.make_connector(env.trainer)
    again = restarted.lookup(env.intent)
    assert again is not None and again.state is mc.JobState.QUEUED
    assert env.executions() == 1


def test_journal_torn_final_write_is_ignored(env):
    env.dispatch()
    with env.connector.journal.path.open("a") as handle:
        handle.write('{"t":"state","k":"')
    assert env.make_connector().lookup(env.intent).state is mc.JobState.QUEUED


# ---- R8: fence preserves the prior usable target ----


def test_fence_blocks_new_work_stops_live_jobs_and_preserves_prior_target(env):
    receipt = env.dispatch()
    fence = env.connector.fence("generated-tenant")
    assert fence.scope == "generated-tenant" and receipt.job_ref in fence.canceled_job_refs
    assert fence.evidence_ref.startswith("fence-receipt:")
    other = replace(env.intent, idempotency_key="generated-fit-intent-2")
    with pytest.raises(ValueError, match="connector_fenced"):
        env.direct_submit(other)
    with pytest.raises(ValueError, match="connector_fenced"):
        env.connector.collect(receipt)
    assert env.connector.usable_target(env.binding) == REVISION
    restarted = env.make_connector()  # durable across restart
    with pytest.raises(ValueError, match="connector_fenced"):
        restarted.submit(other, restarted.serialize_development(other, ROWS))
    assert env.executions() == 1


def test_fence_blocks_replay_of_a_collected_candidate(env):
    receipt = env.dispatch()
    env.trainer.run_pending()
    candidate = env.preflight.collect_candidate("training", env.connector.status(receipt))
    env.connector.fence("generated-tenant")
    with pytest.raises(ValueError, match="connector_fenced"):
        env.connector.predict_for_replay(candidate, FROZEN)


def test_fence_with_unreachable_trainer_still_blocks_collect(env):
    receipt = env.dispatch()

    class Down(Proxy):
        def request_cancel(self, credential, job_id):
            raise OSError("simulated:trainer-down")

    connector = env.make_connector(Down(env.trainer))
    with pytest.raises(ValueError, match="FENCE_JOB_NOT_STOPPED"):
        connector.fence("generated-tenant")
    env.trainer.run_pending()
    with pytest.raises(ValueError, match="connector_fenced"):
        connector.collect(receipt)


# ---- R9: bound enforcement, lineage, replay, usage ----


def test_trainer_reported_usage_over_bound_is_stopped_and_fails(env):
    receipt = env.dispatch()
    stops = []

    class Overspend(Proxy):
        def poll(self, credential, job_id):
            view = self.inner.poll(credential, job_id)
            return replace(view, state="running", usage=TrainerUsage("USD", 501, 1, 1, 1))

        def request_cancel(self, credential, job_id):
            stops.append(job_id)
            return self.inner.request_cancel(credential, job_id)

    connector = env.make_connector(Overspend(env.trainer))
    assert connector.status(receipt).state is mc.JobState.FAILED
    assert stops == [receipt.job_ref]
    with pytest.raises(ValueError):
        connector.collect(receipt)


def test_usage_reports_trainer_values_and_none_when_unknown(env):
    receipt = env.dispatch()
    assert env.connector.usage(receipt) is None
    env.trainer.run_pending()
    usage = env.connector.usage(receipt)
    assert usage.incurred_minor_units == 3 and usage.source.value == "provider_reported"


def _tampered(env, change):
    receipt = env.dispatch()
    env.trainer.run_pending()

    class Forge(Proxy):
        def fetch_result(self, credential, job_id):
            return change(self.inner.fetch_result(credential, job_id))

    connector = env.make_connector(Forge(env.trainer))
    return connector, connector.status(receipt)


def test_tampered_result_signature_is_rejected(env):
    connector, done = _tampered(env, lambda r: replace(r, artifact_ref="generated-forged"))
    with pytest.raises(ValueError, match="result_signature_invalid"):
        connector.collect(done)


def test_result_for_a_different_parent_revision_is_rejected(env):
    connector, done = _tampered(env, lambda r: replace(r, parent_revision="generated-other-r2"))
    with pytest.raises(ValueError, match="parent_revision_mismatch"):
        connector.collect(done)


def test_result_without_artifact_is_rejected(env):
    def blank(result):
        result = replace(result, artifact_ref="")
        return replace(result, signature=sign_result(SECRET, result))

    connector, done = _tampered(env, blank)
    with pytest.raises(ValueError, match="candidate_artifact_missing"):
        connector.collect(done)


def _collected(env):
    receipt = env.dispatch()
    env.trainer.run_pending()
    return env.preflight.collect_candidate("training", env.connector.status(receipt))


def test_replay_requires_a_collected_candidate_from_this_job(env):
    forged = replace(_collected(env), artifact_or_serving_revision="generated-artifact-other")
    with pytest.raises(ValueError, match="candidate_not_collected"):
        env.connector.predict_for_replay(forged, FROZEN)


def test_nondeterministic_replay_is_refused_when_declared_deterministic(env):
    candidate = _collected(env)
    counter = []

    class Flaky(Proxy):
        def predict(self, credential, artifact_ref, inputs):
            counter.append(1)
            return tuple({"label": f"l{len(counter)}"} for _ in inputs)

    with pytest.raises(ValueError, match="replay_nondeterministic"):
        env.make_connector(Flaky(env.trainer)).predict_for_replay(candidate, FROZEN)


def test_replay_wrong_prediction_count_is_refused(env):
    candidate = _collected(env)

    class Short(Proxy):
        def predict(self, credential, artifact_ref, inputs):
            return ({"label": "billing"},)

    with pytest.raises(ValueError, match="replay_nondeterministic"):
        env.make_connector(Short(env.trainer)).predict_for_replay(candidate, FROZEN)


def test_declared_tolerance_accepts_small_numeric_drift_only():
    near = ({"score": 0.5, "label": "a"},)
    drift = ({"score": 0.54, "label": "a"},)
    assert replay_matches(near, drift, "tolerance", 0.05)
    assert not replay_matches(near, drift, "deterministic", 0.0)
    assert not replay_matches(near, ({"score": 0.5, "label": "b"},), "tolerance", 0.05)
    assert not replay_matches(near, drift, "tolerance", 0.01)
    assert not replay_matches(near, (), "tolerance", 0.05)


def test_status_of_unknown_job_is_a_lookup_error(env):
    receipt = env.dispatch()
    with pytest.raises(LookupError):
        env.connector.status(replace(receipt, job_ref="generated-job-forged"))


def test_reservation_guard_is_entered_once_per_dispatch(env):
    entries = []

    def guard():
        entries.append(1)
        return nullcontext()

    env.preflight.submit_reserved_fit("training", env.intent, ROWS, reservation_guard=guard)
    assert entries == [1]


# ---- independent review (OPE-509 closer): trust that must be verified, not assumed ----


def test_forged_callback_cannot_settle_a_running_job(env):
    """A callback is only a wake-up: the token is derived from the key and is not a secret."""
    receipt = env.dispatch()
    forged = TrainerView(receipt.job_ref, TOKEN, "failed")
    assert settle_callback(env.connector, env.intent, forged) is False
    assert env.connector.status(receipt).state is mc.JobState.QUEUED
    forged_done = TrainerView(receipt.job_ref, TOKEN, "completed")
    assert settle_callback(env.connector, env.intent, forged_done) is False
    assert env.connector.status(receipt).state is mc.JobState.QUEUED


def test_callback_is_confirmed_by_the_trainer_before_it_settles(env):
    receipt = env.dispatch()
    env.trainer.run_pending()
    hint = TrainerView(receipt.job_ref, TOKEN, "failed")  # lies about the outcome
    assert settle_callback(env.connector, env.intent, hint) is True
    assert env.connector.status(receipt).state is mc.JobState.COMPLETED


def test_collect_rechecks_versions_with_the_trainer_now(env):
    receipt = env.dispatch()
    env.trainer.run_pending()
    done = env.connector.status(receipt)
    upgraded = replace(env.trainer.manifest, trainer_version="generated-upgraded-2")

    class Upgraded(Proxy):
        def describe(self, credential):
            return upgraded  # results still claim the version the job started under

    with pytest.raises(ValueError, match="trainer_version_unsupported"):
        env.make_connector(Upgraded(env.trainer)).collect(done)


def test_collect_refuses_a_supported_but_different_trainer_version(tmp_path):
    other = "generated-reference-trainer-1.1"
    env = Env(tmp_path)
    env.registration = registration(supported_trainer_versions=frozenset({TRAINER_VERSION, other}))
    env.connector = env.make_connector()
    receipt = env.direct_submit()
    env.trainer.run_pending()
    done = env.connector.status(receipt)
    changed = replace(env.trainer.manifest, trainer_version=other)

    class Changed(Proxy):
        def describe(self, credential):
            return changed

    with pytest.raises(ValueError, match="trainer_version_changed"):
        env.make_connector(Changed(env.trainer)).collect(done)


@pytest.mark.parametrize(
    "version", [TRAINER_VERSION.upper(), f" {TRAINER_VERSION}", f"{TRAINER_VERSION}\t"]
)
def test_version_strings_differing_in_case_or_whitespace_are_unsupported(tmp_path, version):
    manifest = replace(reference_manifest(REVISION, frozenset({TASK})), trainer_version=version)
    env = Env(tmp_path, manifest=manifest)
    verdict = env.connector.check_compatibility(env.binding)
    assert (verdict.ok, verdict.reason) == (False, "trainer_version_unsupported")


def test_signature_cannot_be_reused_across_shifted_field_boundaries(env):
    receipt = env.dispatch()
    env.trainer.run_pending()

    class Moved(Proxy):
        def fetch_result(self, credential, job_id):
            base = self.inner.fetch_result(credential, job_id)
            signed = replace(base, artifact_digest="x|y")
            signed = replace(signed, signature=sign_result(SECRET, signed))
            # same signature, different field meaning
            return replace(signed, artifact_ref=signed.artifact_ref + "|x", artifact_digest="y")

    done = env.connector.status(receipt)
    with pytest.raises(ValueError, match="result_signature_invalid"):
        env.make_connector(Moved(env.trainer)).collect(done)


def test_unsigned_result_and_signature_under_another_key_are_rejected(env):
    foreign = sign_result(
        "generated-other-secret",
        {
            "job_id": "a",
            "client_token": "b",
            "trainer_version": "c",
            "parent_revision": "d",
            "artifact_ref": "e",
            "artifact_digest": "f",
        },
    )
    for signature in ("", foreign):
        connector, done = _tampered(env, lambda r, s=signature: replace(r, signature=s))
        with pytest.raises(ValueError, match="result_signature_invalid"):
            connector.collect(done)


def test_a_second_collect_cannot_swap_the_collected_artifact(env):
    receipt = env.dispatch()
    env.trainer.run_pending()
    done = env.connector.status(receipt)
    first = env.connector.collect(done)

    class Swap(Proxy):
        def fetch_result(self, credential, job_id):
            base = self.inner.fetch_result(credential, job_id)
            swapped = replace(base, artifact_ref="generated-artifact-swapped")
            return replace(swapped, signature=sign_result(SECRET, swapped))

    with pytest.raises(ValueError, match="candidate_artifact_changed"):
        env.make_connector(Swap(env.trainer)).collect(done)
    assert env.connector.collect(done) == first
    assert env.connector.predict_for_replay(first, FROZEN) == EXPECTED


def test_overspend_whose_stop_was_not_honored_is_not_reported_failed(env):
    receipt = env.dispatch()

    class Stubborn(Proxy):
        def poll(self, credential, job_id):
            view = self.inner.poll(credential, job_id)
            return replace(view, state="running", usage=TrainerUsage("USD", 501, 1, 1, 1))

        def request_cancel(self, credential, job_id):
            return replace(self.inner.poll(credential, job_id), state="running")

    connector = env.make_connector(Stubborn(env.trainer))
    reported = connector.status(receipt)
    assert reported.state is mc.JobState.RUNNING  # still spending: never release on FAILED
    with pytest.raises(ValueError, match="job_canceled"):
        connector.collect(receipt)


@pytest.mark.parametrize(
    "usage",
    [
        TrainerUsage("USD", float("nan"), 1, 1, 1),
        TrainerUsage("USD", -1, 1, 1, 1),
        TrainerUsage("USD", True, 1, 1, 1),
        TrainerUsage("USD", 1.5, 1, 1, 1),
        TrainerUsage("EUR", 1, 1, 1, 1),
        TrainerUsage("", 1, 1, 1, 1),
    ],
)
def test_malformed_or_foreign_currency_usage_cannot_bypass_the_cap(env, usage):
    receipt = env.dispatch()
    stops = []

    class Garbage(Proxy):
        def poll(self, credential, job_id):
            return replace(self.inner.poll(credential, job_id), state="running", usage=usage)

        def request_cancel(self, credential, job_id):
            stops.append(job_id)
            return self.inner.request_cancel(credential, job_id)

    connector = env.make_connector(Garbage(env.trainer))
    assert connector.status(receipt).state is mc.JobState.FAILED
    assert stops == [receipt.job_ref]


@pytest.mark.parametrize("bad", [-1, 1.5, True, float("nan")])
def test_usage_report_rejects_malformed_values(env, bad):
    receipt = env.dispatch()

    class Garbage(Proxy):
        def poll(self, credential, job_id):
            view = self.inner.poll(credential, job_id)
            return replace(view, usage=TrainerUsage("USD", bad, 1, 1, 1))

    with pytest.raises(ValueError, match="INVALID_USAGE"):
        env.make_connector(Garbage(env.trainer)).usage(receipt)


def test_usage_is_never_estimated_or_settleable_by_string(env):
    with pytest.raises(ValueError, match="INVALID_USAGE"):
        Usage("job", "USD", 1, 1, 1, 1, "bogus")  # type: ignore[arg-type]
    estimated = Usage("job", "USD", 1, 1, 1, 1, "estimated")  # type: ignore[arg-type]
    assert estimated.source.value == "estimated" and not estimated.settleable
    receipt = env.dispatch()
    env.trainer.run_pending()
    assert env.connector.usage(receipt).settleable


def test_cancel_is_not_blocked_by_a_trainer_upgrade(env):
    receipt = env.dispatch()
    env.trainer.manifest = replace(env.trainer.manifest, trainer_version="generated-upgraded-2")
    assert env.connector.cancel(receipt).state is mc.JobState.CANCELED


def test_cancel_response_for_another_job_is_not_trusted(env):
    receipt = env.dispatch()

    class Liar(Proxy):
        def request_cancel(self, credential, job_id):
            return TrainerView("generated-job-other", "0" * 64, "canceled")

    with pytest.raises(ValueError, match="trainer_identity_mismatch"):
        env.make_connector(Liar(env.trainer)).cancel(receipt)
    assert env.connector.status(receipt).state is mc.JobState.QUEUED


def _payload(rows, **changes):
    base = {
        "serializer": SERIALIZER_VERSION,
        "task": TASK,
        "input_schema": "text-v1",
        "output_schema": "label-v1",
        "preprocessing": "preprocessing-v1",
        "fields": ["label", "text"],
        "row_count": len(rows),
        "rows": rows,
    }
    return {**base, **changes}


def _craft(env, payload, **intent_changes):
    development = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(development).hexdigest()
    return development, replace(env.intent, development_digest=digest, **intent_changes)


def test_direct_submit_refuses_protected_fields_even_with_a_matching_digest(env):
    rows = [{"gold_answer": "x", "label": "a", "text": "t"}]
    development, intent = _craft(
        env,
        _payload(rows, fields=["gold_answer", "label", "text"]),
        fields=frozenset({"text", "label", "gold_answer"}),
    )
    with pytest.raises(ValueError, match="protected_field_in_development"):
        env.connector.submit(intent, development)
    assert env.executions() == 0


def test_direct_submit_refuses_rows_beyond_the_bound_and_malformed_payloads(env):
    rows = [{"label": "a", "text": f"t{i}"} for i in range(LIMIT.maximum_rows + 1)]
    development, intent = _craft(env, _payload(rows))
    with pytest.raises(ValueError, match="development_rows_invalid"):
        env.connector.submit(intent, development)
    few = rows[:2]
    for broken in (
        _payload(few, serializer="generated-other"),
        _payload(few, row_count=99),
        _payload(few, task="generated-other-task"),
        _payload([{"label": "a"}]),
        _payload(few, extra=1),
        _payload(few, fields=["text"]),
    ):
        development, intent = _craft(env, broken)
        with pytest.raises(ValueError, match="development_(rows|payload)_invalid"):
            env.connector.submit(intent, development)
    junk = b"not-json"
    junk_intent = replace(env.intent, development_digest=hashlib.sha256(junk).hexdigest())
    with pytest.raises(ValueError, match="development_payload_invalid"):
        env.connector.submit(junk_intent, junk)
    assert env.executions() == 0


def test_replay_refuses_a_candidate_built_around_another_intent(env):
    candidate = _collected(env)
    other = replace(candidate.intent, benchmark_revision="benchmark-v2")
    with pytest.raises(ValueError, match="candidate_not_collected"):
        env.connector.predict_for_replay(replace(candidate, intent=other), FROZEN)


def test_replay_tolerance_rejects_nan_inf_and_invalid_declarations():
    one = ({"score": 0.5},)
    nan = ({"score": float("nan")},)
    assert not replay_matches(one, nan, "tolerance", 0.5)
    assert not replay_matches(nan, nan, "tolerance", 0.5)
    assert not replay_matches(one, ({"score": float("inf")},), "tolerance", 0.5)
    assert not replay_matches(one, one, "tolerance", float("nan"))
    assert not replay_matches(one, one, "tolerance", -1.0)
    assert not replay_matches(one, one, "bogus", 0.0)


def test_lookup_does_not_journal_a_foreign_trainer_identity(tmp_path):
    env = Env(tmp_path, wrap=LostAck)
    with pytest.raises(OSError):
        env.direct_submit()

    class Poison(Proxy):
        def find(self, credential, client_token):
            return TrainerView("generated-job-evil", "0" * 64, "running")

    with pytest.raises(ValueError, match="trainer_identity_mismatch"):
        env.make_connector(Poison(env.trainer)).lookup(env.intent)
    genuine = env.make_connector(env.trainer).lookup(env.intent)
    assert genuine is not None and genuine.job_ref != "generated-job-evil"


def test_fence_that_cannot_stop_a_live_job_does_not_claim_it_stopped(env):
    receipt = env.dispatch()

    class Down(Proxy):
        def request_cancel(self, credential, job_id):
            raise OSError("simulated:trainer-down")

    connector = env.make_connector(Down(env.trainer))
    with pytest.raises(ValueError, match="FENCE_JOB_NOT_STOPPED"):
        connector.fence("generated-tenant")
    with pytest.raises(ValueError, match="connector_fenced"):  # the marker is still durable
        env.make_connector().collect(receipt)
    assert env.trainer.poll(Credential(SECRET), receipt.job_ref).state == "queued"


def test_fence_does_not_trust_a_stop_confirmation_for_another_job(env):
    receipt = env.dispatch()

    class Liar(Proxy):
        def request_cancel(self, credential, job_id):
            return TrainerView("generated-job-other", "0" * 64, "canceled")

    with pytest.raises(ValueError, match="FENCE_JOB_NOT_STOPPED"):
        env.make_connector(Liar(env.trainer)).fence("generated-tenant")
    assert env.trainer.poll(Credential(SECRET), receipt.job_ref).state == "queued"


def test_fence_reaches_a_job_whose_acknowledgement_was_lost(tmp_path):
    env = Env(tmp_path, wrap=LostAck)
    with pytest.raises(OSError):
        env.direct_submit()
    receipt = env.make_connector(env.trainer).fence("generated-tenant")
    assert receipt.scope == "generated-tenant"
    stored = env.trainer.find(Credential(SECRET), TOKEN)
    assert stored is not None and stored.state == "canceled"


def test_fence_with_an_unconfirmable_dispatch_is_not_reported_stopped(tmp_path):
    env = Env(tmp_path, wrap=NeverArrives)
    env.dispatch()  # UNKNOWN: nothing is known about the trainer's side
    with pytest.raises(ValueError, match="FENCE_JOB_NOT_STOPPED"):
        env.connector.fence("generated-tenant")


def test_conformance_replay_honours_a_declared_tolerance(tmp_path):
    class Drifting(ReferenceTrainer):
        calls = 0

        def predict(self, credential, artifact_ref, inputs):
            Drifting.calls += 1
            return tuple({"score": 0.5 + 0.001 * (Drifting.calls % 2)} for _ in inputs)

    def make(path):
        return Drifting(tmp_path / "drift", SECRET, REVISION, frozenset({TASK}))

    harness = _harness(tmp_path, make)
    harness = replace(
        harness, registration=registration(replay_mode="tolerance", replay_tolerance=0.01)
    )
    results = tc.run_trainer_conformance(harness, tmp_path)
    assert {r.name: r.outcome for r in results}["replay_deterministic"] is tc.Outcome.PASSED
