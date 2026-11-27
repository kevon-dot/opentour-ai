"""Conformance suite a customer runs against its own ``TrainerTransport`` before connecting it.

``run_trainer_conformance`` exercises only the public job adapter (version declaration, idempotent
start on a client token, read-only lookup, cancel fencing, signed lineage, replay) and reports one
``TrainerCheck`` per obligation. A check the harness cannot exercise is NOT_EXERCISED and does not
pass. The connector-level obligations (reserve before dispatch, reconcile before retry) are proven
through ``Preflight`` in the connector tests, not here.

Simulated: the repository runs this against the generated ``ReferenceTrainer`` only.
Not proven: a green run is software conformance of that fixture (``EvidenceTier.FIXTURE``). It
establishes no provider or customer evidence and never qualifies a real customer trainer.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from opentour_cell.pilot.custom_trainer_connector import (
    Credential,
    TrainerJobRequest,
    TrainerRegistration,
    TrainerTransport,
    check_compatibility,
    sign_result,
)


class Outcome(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    NOT_EXERCISED = "not_exercised"


@dataclass(frozen=True)
class TrainerCheck:
    name: str
    outcome: Outcome
    detail: str = ""


@dataclass(frozen=True)
class TrainerHarness:
    """What a customer supplies so the suite can drive its trainer."""

    make_transport: Callable[[Path], TrainerTransport]
    credential: Credential
    registration: TrainerRegistration
    request: Callable[[str], TrainerJobRequest]
    complete_pending: Callable[[TrainerTransport], None]
    inputs: tuple[Mapping[str, object], ...]
    task: str
    model_revision: str


def all_passed(checks: tuple[TrainerCheck, ...]) -> bool:
    return bool(checks) and all(check.outcome is Outcome.PASSED for check in checks)


def _token(label: str) -> str:
    return hashlib.sha256(f"conformance:{label}".encode()).hexdigest()


def run_trainer_conformance(harness: TrainerHarness, workdir: Path) -> tuple[TrainerCheck, ...]:
    credential = harness.credential
    transport = harness.make_transport(workdir)
    checks: list[TrainerCheck] = []

    def check(name: str, body: Callable[[], str | None]) -> None:
        try:
            problem = body()
        except Exception as error:  # a crashing check is a failing check, never a pass
            checks.append(TrainerCheck(name, Outcome.FAILED, type(error).__name__))
            return
        checks.append(
            TrainerCheck(name, Outcome.FAILED if problem else Outcome.PASSED, problem or "")
        )

    def versions() -> str | None:
        verdict = check_compatibility(harness.registration, transport.describe(credential))
        return None if verdict.ok else verdict.reason

    def idempotent_start() -> str | None:
        request = harness.request(_token("idem"))
        first = transport.start(credential, request)
        second = transport.start(credential, request)
        return None if first.job_id == second.job_id else "duplicate_start_created_second_job"

    def lookup_read_only() -> str | None:
        if transport.find(credential, _token("never-started")) is not None:
            return "find_returned_unknown_job"
        if transport.find(credential, _token("never-started")) is not None:
            return "find_created_job"
        return None

    def lost_ack_recoverable() -> str | None:
        started = transport.start(credential, harness.request(_token("lost-ack")))
        found = transport.find(credential, _token("lost-ack"))
        if found is None or found.job_id != started.job_id:
            return "acknowledged_job_not_recoverable"
        return None

    def cancel_fences_result() -> str | None:
        started = transport.start(credential, harness.request(_token("cancel")))
        transport.request_cancel(credential, started.job_id)
        harness.complete_pending(transport)
        if transport.poll(credential, started.job_id).state != "canceled":
            return "canceled_job_changed_state"
        try:
            transport.fetch_result(credential, started.job_id)
        except LookupError:
            return None
        return "canceled_job_published_result"

    def signed_lineage() -> str | None:
        started = transport.start(credential, harness.request(_token("lineage")))
        harness.complete_pending(transport)
        if transport.poll(credential, started.job_id).state != "completed":
            return "job_did_not_complete"
        result = transport.fetch_result(credential, started.job_id)
        if result.parent_revision != harness.model_revision or result.client_token != _token(
            "lineage"
        ):
            return "lineage_mismatch"
        expected = sign_result(credential.value, result)
        return None if hmac.compare_digest(result.signature, expected) else "signature_invalid"

    def deterministic_replay() -> str | None:
        started = transport.start(credential, harness.request(_token("lineage")))
        harness.complete_pending(transport)
        artifact = transport.fetch_result(credential, started.job_id).artifact_ref
        first = transport.predict(credential, artifact, harness.inputs)
        second = transport.predict(credential, artifact, harness.inputs)
        if len(first) != len(harness.inputs):
            return "prediction_count_mismatch"
        return None if first == second else "replay_nondeterministic"

    def rejects_bad_credential() -> str | None:
        try:
            transport.describe(Credential("generated-wrong-secret"))
        except OSError:
            return None
        return "accepted_wrong_credential"

    def survives_restart() -> str | None:
        started = transport.start(credential, harness.request(_token("restart")))
        restarted = harness.make_transport(workdir)
        found = restarted.find(credential, _token("restart"))
        return None if found and found.job_id == started.job_id else "job_lost_on_restart"

    check("versions_declared_and_registered", versions)
    check("start_idempotent_on_client_token", idempotent_start)
    check("find_is_read_only", lookup_read_only)
    check("lost_acknowledgement_recoverable_by_find", lost_ack_recoverable)
    check("cancel_fences_result", cancel_fences_result)
    check("result_signed_and_parent_bound", signed_lineage)
    check("replay_deterministic", deterministic_replay)
    check("wrong_credential_refused", rejects_bad_credential)
    check("state_survives_restart", survives_restart)
    return tuple(checks)


__all__ = ["Outcome", "TrainerCheck", "TrainerHarness", "all_passed", "run_trainer_conformance"]
