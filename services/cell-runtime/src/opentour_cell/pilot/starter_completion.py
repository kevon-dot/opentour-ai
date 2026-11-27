"""Evaluator-signed, single-benchmark completion and separately authorized adoption."""

import base64
import hashlib
import hmac
import json
import os
import re
import tempfile
import time
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from opentour_cell.pilot.intake_admission import IntakeWork
from opentour_cell.pilot.intake_mcp import IntakeError, require_ref
from opentour_cell.pilot.model_registry import ModelRegistry
from opentour_cell.pilot.onboarding_mcp import _read_private
from opentour_cell.pilot.operating_point import canonical, digest
from opentour_cell.pilot.receipt_recovery import _open_db
from opentour_cell.pilot.repair_bundle import _crypto, _write_private
from opentour_cell.pilot.repair_workflow import _record
from opentour_cell.pilot.starter_evaluation_authority import StarterEvaluationAuthority
from opentour_cell.pilot.starter_evaluation_session import StarterEvaluationSession
from opentour_cell.pilot.starter_evaluation_worker import CURRENT, _Invocation
from opentour_cell.pilot.starter_local_baseline import _private_bytes
from opentour_cell.pilot.starter_map_confirmation import StarterCaseSession
from opentour_cell.pilot.starter_score_inspection import InspectionScope, _read
from opentour_cell.pilot.starter_scoring import PredictionPort, PredictionRequest
from opentour_cell.pilot.workflow_benchmarks import (
    BenchmarkScope,
    MaterializedBenchmark,
    WorkflowBenchmarks,
)

Json = dict[str, Any]
GRANT = "opentour.pilot.starter-completion-publication-grant.v1"
RECEIPT = "opentour.pilot.starter-completion.v1"
_FIELDS = {
    "schema_version",
    "grant_ref",
    "expires_at",
    "tenant",
    "epoch",
    "workflow_ref",
    "run_ref",
    "correlation_ref",
    "admission_digest",
    "benchmark_ref",
    "benchmark_version",
    "source_revision",
    "freeze_ref",
    "freeze_revision",
    "custodian_ref",
    "reader",
    "evaluator",
    "invocation_ref",
    "invocation_digest",
    "execution_ref",
    "execution_grant_ref",
    "execution_grant_digest",
    "execution_grant_file",
    "public_key_sha256",
}


def _require(value: object) -> None:
    if not value:
        raise IntakeError("starter_completion_unavailable")


class _Publication:
    def __init__(self, directory: Path, run: str):
        self.directory = directory
        self.path = directory / f"starter-publication-{digest(require_ref(run))}.json"
        self.cap = self._current()
        _require(self.cap["run_ref"] == run)
        self.receipt = directory / f"starter-completion-{digest(self.cap['benchmark_ref'])}.json"

    def _current(self) -> Json:
        packet = _read_private(self.path, 8192)
        _require(set(packet) == {"grant", "seal_sha256"})
        cap = packet["grant"]
        _require(isinstance(cap, dict) and set(cap) == _FIELDS and cap["schema_version"] == GRANT)
        assert isinstance(cap, dict)
        key = bytes.fromhex(_read_private(self.directory / "starter-intake-key.json", 4096)["key"])
        expected = hmac.digest(
            hmac.digest(key, GRANT.encode(), "sha256"), canonical(cap).encode(), "sha256"
        )
        _require(len(key) == 32 and hmac.compare_digest(packet["seal_sha256"], expected.hex()))
        for field in _FIELDS - {"expires_at", "benchmark_version"}:
            require_ref(cap[field])
        _require(type(cap["expires_at"]) is int and time.time() < cap["expires_at"])
        _require(type(cap["benchmark_version"]) is int and cap["benchmark_version"] > 0)
        for field in (
            "freeze_revision",
            "admission_digest",
            "invocation_digest",
            "public_key_sha256",
            "execution_grant_digest",
        ):
            _require(re.fullmatch(r"[a-f0-9]{64}", cap[field]))
        _require(cap["freeze_ref"] == "starter-freeze-" + cap["freeze_revision"][:40])
        _require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}\.json", cap["execution_grant_file"]))
        for prefix, ref in (
            ("starter-publication", cap["grant_ref"]),
            ("starter-evaluation", cap["execution_grant_ref"]),
            ("starter-worker", cap["invocation_ref"]),
        ):
            _require(not os.path.lexists(self.directory / f"{prefix}-withdrawn-{ref}.json"))
        current = _read_private(self.directory / "starter-worker-current.json", 16384)
        _require(
            current.get("schema_version") == CURRENT
            and current["invocations"].get(cap["invocation_ref"]) == cap["invocation_digest"]
        )
        selected = _read_private(self.directory / "starter-evaluation-selection.json", 16384)
        _require(selected["executions"].get(cap["execution_ref"]) == cap["execution_grant_file"])
        execution = _read_private(self.directory / cap["execution_grant_file"], 32768)
        _require(digest(execution) == cap["execution_grant_digest"])
        _require(
            time.time()
            < min(execution["grant"]["expires_at"], execution["grant"]["permission"]["expires_at"])
        )
        return cap

    def check(self) -> None:
        _require(self._current() == self.cap)
        self.public_key()

    def public_key(self) -> bytes:
        public = _private_bytes(self.directory / "starter-completion-public.pem", 8192)
        _require(hashlib.sha256(public).hexdigest() == self.cap["public_key_sha256"])
        return public

    def benchmark(self, work: IntakeWork) -> MaterializedBenchmark:
        c = self.cap
        _require(work.mode == "starter" and work.benchmark_current)
        _require(
            (
                work.workflow_ref,
                work.run_ref,
                work.resource.engagement_epoch,
                work.benchmark_version,
                work.revisions[0],
                digest(asdict(work)),
            )
            == (
                c["workflow_ref"],
                c["run_ref"],
                c["epoch"],
                c["benchmark_version"],
                c["source_revision"],
                c["admission_digest"],
            )
        )
        with _open_db(self.directory / "intake.sqlite3") as db:
            _require(db is not None)
            assert db is not None
            row = db.execute(
                "SELECT correlation_ref FROM intake_runs "
                "WHERE tenant=? AND workflow_ref=? AND run_ref=?",
                (c["tenant"], work.workflow_ref, work.run_ref),
            ).fetchone()
            _require(row is not None and row[0] == c["correlation_ref"])
        scope = BenchmarkScope(c["tenant"], c["epoch"], work.workflow_ref)
        entry = WorkflowBenchmarks(self.directory).inspect(
            scope, c["benchmark_ref"], current_run=work.run_ref
        )
        _require(
            entry
            and entry["version"] == work.benchmark_version
            and entry["benchmark_state"] in {"frozen_unscored", "scored"}
        )
        assert entry is not None
        freeze = MaterializedBenchmark(**entry["materialized"])
        _require(
            (freeze.definition_ref, freeze.definition_digest, freeze.source_revision)
            == (c["freeze_ref"], c["freeze_revision"], c["source_revision"])
        )
        return freeze

    def packet(self, result_ref: str, result_digest: str) -> Json:
        _require(
            re.fullmatch("[0-9a-f]{64}", result_digest)
            and result_ref == "starter-result-" + result_digest[:40]
        )
        return dict(
            schema_version=RECEIPT,
            publication_digest=digest(self.cap),
            benchmark_ref=self.cap["benchmark_ref"],
            result_ref=result_ref,
            result_digest=result_digest,
        )

    def verify(self, packet: Json) -> Json:
        _require(set(packet) == {"receipt", "signature"})
        body = packet["receipt"]
        _require(body == self.packet(body["result_ref"], body["result_digest"]))
        with tempfile.TemporaryDirectory() as temp:
            public, sig = Path(temp) / "public", Path(temp) / "signature"
            _write_private(public, self.public_key())
            _write_private(sig, base64.b64decode(packet["signature"], validate=True))
            _crypto(
                ["dgst", "-sha256", "-verify", str(public), "-signature", str(sig)],
                canonical(body).encode(),
                "starter_completion_unavailable",
            )
        return dict(body)


def attest(selector: Path) -> None:
    """Reconcile the original committed score through its existing evaluator receipts."""
    inv = _Invocation(selector)
    cap = inv.cap
    pub = _Publication(inv.directory, cap["run_ref"])
    _require(
        all(
            cap[k] == pub.cap[k]
            for k in (
                "tenant",
                "epoch",
                "workflow_ref",
                "run_ref",
                "custodian_ref",
                "evaluator",
                "freeze_ref",
                "freeze_revision",
                "invocation_ref",
                "execution_ref",
                "execution_grant_ref",
                "execution_grant_file",
                "execution_grant_digest",
            )
        )
    )
    _require(digest(inv.envelope) == pub.cap["invocation_digest"])
    authority = StarterEvaluationAuthority(inv.settings, inv.actor)
    work = authority.work(cap["execution_ref"])
    pub.benchmark(work)
    with authority.guard(inv.actor, work, cap["execution_ref"]) as p:
        frozen = authority.cases.evaluation_inputs(inv.actor, work)
        targets = authority.cases.evaluation_targets(
            inv.actor, work, freeze_revision=p.freeze_revision
        )
        inputs = {
            "starter-case-" + digest([p.freeze_revision, row.case_id])[:40]: dict(
                row.decision_inputs
            )
            for row in frozen.cases
        }
        population = [
            dict(
                case_id=row.case_id,
                family_id=row.family_id,
                origin=row.origin,
                expected=targets[row.case_id],
            )
            for row in frozen.cases
        ]
        context = authority.comparator_context(inv.actor, work, p)
        scope = InspectionScope(
            inv.actor,
            p.revision,
            p.expires_at,
            p,
            digest(inputs),
            context.comparator_ref,
            digest(population),
            len(inputs),
            frozen.unresolved,
            frozen.excluded,
        )
        session = StarterEvaluationSession(inv.settings, inv.actor)
        path = inv.directory / "starter-evaluations.sqlite3"
        _require(
            not any(os.path.lexists(str(path) + suffix) for suffix in ("-wal", "-shm", "-journal"))
        )
        with _open_db(path) as db:
            _require(db is not None and _read(db, scope)["state"] == "scored")
            assert db is not None
            result = json.loads(
                db.execute(
                    "SELECT result FROM evaluations WHERE ref=?", (p.execution_ref,)
                ).fetchone()[0]
            )
            ports: tuple[PredictionPort, PredictionPort] = session._ports(
                authority,
                work,
                p,
                ModelRegistry(inv.directory / "model-registration.json", inv.actor.tenant),
            )
            for arm, port in zip(("model", "comparator"), ports, strict=True):
                req = PredictionRequest(**result["predictions"][arm]["request"])
                durable = port.lookup(req)
                row = db.execute(
                    "SELECT receipt FROM predictions WHERE ref=?", (req.operation_ref,)
                ).fetchone()
                _require(
                    durable is not None
                    and row is not None
                    and canonical(asdict(durable)) == canonical(json.loads(row[0]))
                )
            body = pub.packet(result["result_ref"], result["result_digest"])
        if os.path.lexists(pub.receipt):
            _require(pub.verify(_read_private(pub.receipt, 8192)) == body)
        else:
            with tempfile.TemporaryDirectory() as temp:
                key = Path(temp) / "key"
                _write_private(
                    key, _private_bytes(inv.directory / "starter-completion-signing.pem", 16384)
                )
                sig = _crypto(
                    ["dgst", "-sha256", "-sign", str(key), "-passin", "pass:"],
                    canonical(body).encode(),
                    "starter_completion_unavailable",
                )
            packet = dict(receipt=body, signature=base64.b64encode(sig).decode("ascii"))
            pub.verify(packet)
            authority.revalidate(inv.actor, work, p)
            inv.settings()
            pub.check()
            _record(pub.receipt, packet)
        pub.check()


def adopt(session: StarterCaseSession, run: str) -> str:
    pub = _Publication(session.directory, run)
    _require((pub.cap["reader"], pub.cap["tenant"]) == (session.actor.actor, session.actor.tenant))
    with session._pin("materialize") as pre:
        _require(
            pre.scope.work.run_ref == run and pre.cap["custodian_ref"] == pub.cap["custodian_ref"]
        )
        pub.benchmark(pre.scope.work)
    packet = _read_private(pub.receipt, 8192)
    body = pub.verify(packet)
    with (
        WorkflowBenchmarks(session.directory).worker_guard(),
        session._pin("materialize") as pinned,
    ):
        work = pinned.scope.work
        previous = pub.benchmark(work)
        receipt = replace(
            previous,
            state="scored",
            result_ref=body["result_ref"],
            result_digest=body["result_digest"],
        )

        def check() -> None:
            session._current(pinned)
            _require(pinned.cap["custodian_ref"] == pub.cap["custodian_ref"])
            pub.check()
            _require(_read_private(pub.receipt, 8192) == packet)

        check()
        ledger = WorkflowBenchmarks(session.directory, check_write=check)
        op, _, old = ledger.admit_materialization(session.actor.tenant, work, "scored")
        if op is None:
            _require(old == receipt)
        else:
            ledger.publish(session.actor.tenant, work, receipt, operation_ref=op)
        check()
    return receipt.state
