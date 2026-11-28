"""Post-miss lineage for starter benchmarks: exposed misses become diagnostic only.

The named evaluator places an exposure from a persisted scored result. Placement
checks the result digest and re-checks its population and answer key against the
current freeze. Every reserved family observed in that result is exposed, and its
missed cases form the diagnostic set. A claim-bearing assessment of a revised
choice must be pre-registered before it runs, on a successor freeze in the same
workflow and epoch whose reserved families share no family or decision input with
an exposed family. Owner counterexamples in that successor are treated as
exposure-informed and excluded from the protected population.

This module never edits a freeze, a manifest or a scored result, never calls a
model, and never produces observed evidence or training credit. Records are
immutable private projections; reads re-derive and compare them.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from opentour_cell.pilot.intake_admission import IntakeWork
from opentour_cell.pilot.intake_mcp import IntakeError, IntakePrincipal, require_ref
from opentour_cell.pilot.operating_point import canonical, digest
from opentour_cell.pilot.receipt_recovery import _has_table, _open_db
from opentour_cell.pilot.repair_workflow import _private_json, _record
from opentour_cell.pilot.starter_cases import FrozenEvaluationInputs, StarterCases, _input_fields
from opentour_cell.pilot.starter_scoring import _metrics

Json = dict[str, Any]
EXPOSURE = "opentour.pilot.starter-exposure.v1"
REASSESSMENT = "opentour.pilot.starter-reassessment.v1"
ORIGIN = "expert_reviewed_generated_starter"
ARMS = ("model", "comparator")
_PATTERNS = {
    "starter-exposure": re.compile(r"starter-exposure-[0-9a-f]{40}"),
    "starter-reassessment": re.compile(r"starter-reassessment-[0-9a-f]{40}"),
}


def _require(ok: object, reason: str = "starter_diagnostic_unavailable") -> None:
    if not ok:
        raise IntakeError(reason)


@dataclass(frozen=True)
class RevisedChoice:
    """The exact choice a protected reassessment evaluates, fixed before it runs."""

    arm: str
    reference: str
    revision: str


def outcome(output: Json, target: str) -> str:
    """The shared exact-action rule: REVIEW status is the REVIEW action."""
    if output["status"] == "missing":
        return "missing"
    produced = "REVIEW" if output["status"] == "review" else output["label"]
    return "match" if produced == target else "miss"


def _fingerprint(decision_inputs: object) -> str:
    """The core's identical-input identity; the branch annotation is not part of it."""
    return digest(_input_fields(decision_inputs))


def _population(frozen: FrozenEvaluationInputs) -> str:
    return digest(
        sorted(
            [c.case_id, c.family_id, c.origin, _fingerprint(c.decision_inputs)]
            for c in frozen.cases
        )
    )


def _choice(result: Json, arm: str) -> Json:
    request = result["predictions"][arm]["request"]
    return {
        "reference": request["implementation_ref"],
        "revision": request["implementation_revision"],
    }


def _sealed(record: Json) -> Json:
    return record | {"seal": digest(record)}


def _object(text: str) -> Json:
    value = json.loads(text)
    _require(isinstance(value, dict), "starter_evaluation_journal_invalid")
    return dict(value)


class StarterDiagnostics:
    """Evaluator-side lineage records beside the private starter case and score stores."""

    def __init__(self, directory: Path, cases: StarterCases):
        self.directory = directory.absolute()
        self.cases = cases
        self.journal = self.directory / "starter-evaluations.sqlite3"

    @contextmanager
    def _serialized(self) -> Iterator[None]:
        """The scoring journal's directory lock, so no evaluation can start meanwhile.

        Only journal reads and one immutable publication run under it. Authority
        checks run before and after, never inside, so no nested lock is taken.
        """
        parent = self.directory
        _require(parent.resolve(strict=True) == parent and parent.is_dir())
        lock = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield
        finally:
            os.close(lock)

    def _evaluations(self) -> list[tuple[str, Json, Json | None]]:
        with _open_db(self.journal) as db:
            if db is None:
                return []
            _require(_has_table(db, "evaluations"), "starter_evaluation_journal_invalid")
            return [
                (
                    row["ref"],
                    _object(row["definition"]),
                    None if row["result"] is None else _object(row["result"]),
                )
                for row in db.execute("SELECT ref,definition,result FROM evaluations")
            ]

    def _current(
        self, actor: IntakePrincipal, work: IntakeWork
    ) -> tuple[FrozenEvaluationInputs, Json]:
        """Evaluator-only reserved inputs and answer key of the current freeze."""
        frozen = self.cases.evaluation_inputs(actor, work)
        targets = self.cases.evaluation_targets(actor, work, freeze_revision=frozen.freeze_revision)
        _require(set(targets) == {c.case_id for c in frozen.cases}, "starter_freeze_changed")
        return frozen, targets

    def _scored(self, execution_ref: str, frozen: FrozenEvaluationInputs, targets: Json) -> Json:
        """A persisted result whose seal, population and answer key match the current freeze."""
        found = [r for r in self._evaluations() if r[0] == execution_ref]
        _require(len(found) == 1 and found[0][2] is not None, "starter_scored_result_required")
        _, definition, sealed = found[0]
        assert sealed is not None
        result = {k: v for k, v in sealed.items() if k not in {"result_ref", "result_digest"}}
        _require(
            sealed.get("result_digest") == digest(result)
            and sealed.get("result_ref") == "starter-result-" + digest(result)[:40]
            and result["state"] == "scored"
            and result["execution_ref"] == execution_ref
            and result["evidence_origin"] == ORIGIN
            and result["freeze_ref"] == frozen.freeze_ref
            and result["freeze_revision"] == frozen.freeze_revision
            and definition["permission"]["freeze_revision"] == frozen.freeze_revision
            and set(result["predictions"]) == set(ARMS)
            and canonical(result["metrics"]) == canonical(_metrics(result["rows"])),
            "starter_scored_result_invalid",
        )
        rows = result["rows"]
        _require(
            len(rows) == len(frozen.cases)
            and {(r["case_id"], r["family_id"], r["origin"]) for r in rows}
            == {(c.case_id, c.family_id, c.origin) for c in frozen.cases}
            and all(r["expected"] == targets[r["case_id"]] for r in rows),
            "starter_scored_result_invalid",
        )
        return sealed

    def _exposure_record(
        self, actor: IntakePrincipal, work: IntakeWork, execution_ref: str
    ) -> tuple[Json, FrozenEvaluationInputs]:
        frozen, targets = self._current(actor, work)
        result = self._scored(execution_ref, frozen, targets)
        diagnostic = []
        for row in sorted(result["rows"], key=lambda r: r["case_id"]):
            for arm in ARMS:
                kind = outcome(row[arm], row["expected"]["target"])
                if kind != "match":
                    diagnostic.append(
                        {
                            "case_id": row["case_id"],
                            "family_id": row["family_id"],
                            "origin": row["origin"],
                            "arm": arm,
                            "outcome": kind,
                            "observed": row[arm],
                        }
                    )
        _require(diagnostic, "starter_miss_not_observed")
        record = {
            "schema_version": EXPOSURE,
            "exposure_ref": "starter-exposure-"
            + digest([frozen.freeze_revision, execution_ref])[:40],
            "tenant": actor.tenant,
            "workflow_ref": work.workflow_ref,
            "engagement_epoch": work.resource.engagement_epoch,
            "run_ref": work.run_ref,
            "evaluator": actor.actor,
            "freeze_ref": frozen.freeze_ref,
            "freeze_revision": frozen.freeze_revision,
            "plan_digest": digest(asdict(frozen.plan)),
            "population_digest": _population(frozen),
            "execution_ref": execution_ref,
            "result_ref": result["result_ref"],
            "result_digest": result["result_digest"],
            "exposed_choice": {arm: _choice(result, arm) for arm in ARMS},
            "exposed_families": sorted({c.family_id for c in frozen.cases}),
            "exposed_inputs": sorted({_fingerprint(c.decision_inputs) for c in frozen.cases}),
            "diagnostic_set": diagnostic,
            "role": "original_exposure",
            "evidence_origin": ORIGIN,
            "training_credit": 0,
        }
        return _sealed(record), frozen

    def place_exposure(
        self, actor: IntakePrincipal, work: IntakeWork, *, execution_ref: str
    ) -> Json:
        """Add the observed misses to a diagnostic set; the old manifest stays untouched."""
        require_ref(execution_ref)
        record, frozen = self._exposure_record(actor, work, execution_ref)

        def current() -> None:
            _require(self.cases.evaluation_inputs(actor, work) == frozen, "starter_freeze_changed")

        _record(self.directory / (record["exposure_ref"] + ".json"), record, before_publish=current)
        return record

    def _load(self, prefix: str, ref: str) -> Json:
        """Integrity of one stored record; content checks belong to the caller."""
        _require(_PATTERNS[prefix].fullmatch(ref), "starter_lineage_record_invalid")
        record = _private_json(self.directory / (ref + ".json"))
        body = {k: v for k, v in record.items() if k != "seal"}
        field = prefix.removeprefix("starter-") + "_ref"
        _require(
            record.get("seal") == digest(body) and record.get(field) == ref,
            "starter_lineage_record_invalid",
        )
        return record

    def _stored(self, prefix: str) -> list[Json]:
        return [
            self._load(prefix, path.stem)
            for path in sorted(self.directory.glob(prefix + "-*.json"))
        ]

    def exposure(self, actor: IntakePrincipal, work: IntakeWork, exposure_ref: str) -> Json:
        """Re-derive a stored exposure from the current freeze and its persisted result."""
        stored = self._load("starter-exposure", exposure_ref)
        _require(stored.get("schema_version") == EXPOSURE, "starter_exposure_invalid")
        expected, _ = self._exposure_record(actor, work, str(stored.get("execution_ref")))
        _require(canonical(expected) == canonical(stored), "starter_exposure_invalid")
        return stored

    def _sealed_exposure(self, exposure_ref: str) -> Json:
        """A predecessor no longer current is still bound by its sealed exposure."""
        stored = self._load("starter-exposure", exposure_ref)
        _require(
            stored.get("schema_version") == EXPOSURE
            and exposure_ref
            == "starter-exposure-"
            + digest([stored.get("freeze_revision"), stored.get("execution_ref")])[:40],
            "starter_exposure_invalid",
        )
        return stored

    def _reassessment_record(
        self,
        actor: IntakePrincipal,
        exposure: Json,
        successor: IntakeWork,
        execution_ref: str,
        revised: RevisedChoice,
    ) -> tuple[Json, FrozenEvaluationInputs]:
        for value in (execution_ref, revised.reference, revised.revision):
            require_ref(value)
        _require(revised.arm in ARMS, "invalid_revised_choice")
        _require(
            actor.tenant == exposure["tenant"]
            and successor.workflow_ref == exposure["workflow_ref"]
            and successor.resource.engagement_epoch == exposure["engagement_epoch"],
            "starter_successor_required",
        )
        frozen = self.cases.evaluation_inputs(actor, successor)
        _require(
            frozen.freeze_revision != exposure["freeze_revision"], "starter_successor_required"
        )
        plan = frozen.plan
        _require(
            plan.evaluated_model_ref in {None, revised.reference}
            if revised.arm == "model"
            else plan.comparator_ref == revised.reference,
            "revised_choice_not_frozen",
        )
        families, inputs = set(exposure["exposed_families"]), set(exposure["exposed_inputs"])
        _require(
            all(
                c.family_id not in families and _fingerprint(c.decision_inputs) not in inputs
                for c in frozen.cases
            ),
            "exposed_family_reused",
        )
        informed = sorted({c.family_id for c in frozen.cases if c.origin != "generated_starter"})
        protected = sorted({c.family_id for c in frozen.cases} - set(informed))
        _require(protected, "independent_reserved_families_required")
        exposed = exposure["exposed_choice"][revised.arm]
        record = {
            "schema_version": REASSESSMENT,
            "reassessment_ref": "starter-reassessment-"
            + digest([exposure["exposure_ref"], frozen.freeze_revision, execution_ref])[:40],
            "exposure_ref": exposure["exposure_ref"],
            "tenant": actor.tenant,
            "workflow_ref": successor.workflow_ref,
            "engagement_epoch": successor.resource.engagement_epoch,
            "evaluator": actor.actor,
            "predecessor": {
                "run_ref": exposure["run_ref"],
                "freeze_ref": exposure["freeze_ref"],
                "freeze_revision": exposure["freeze_revision"],
                "result_ref": exposure["result_ref"],
            },
            "successor": {
                "run_ref": successor.run_ref,
                "freeze_ref": frozen.freeze_ref,
                "freeze_revision": frozen.freeze_revision,
                "plan_digest": digest(asdict(plan)),
                "population_digest": _population(frozen),
            },
            "execution_ref": execution_ref,
            "revised_choice": asdict(revised),
            "exposed_choice": exposed,
            "revised": [revised.reference, revised.revision]
            != [exposed["reference"], exposed["revision"]],
            "protected_families": protected,
            "exposure_informed_families": informed,
            "role": "protected_reassessment",
            "evidence_origin": ORIGIN,
            "training_credit": 0,
        }
        return _sealed(record), frozen

    def place_reassessment(
        self,
        actor: IntakePrincipal,
        predecessor: IntakeWork,
        successor: IntakeWork,
        *,
        exposure_ref: str,
        execution_ref: str,
        revised: RevisedChoice,
    ) -> Json:
        """Pre-register the only claim-bearing run of a revised choice on fresh families."""
        exposure = self.exposure(actor, predecessor, exposure_ref)
        record, frozen = self._reassessment_record(
            actor, exposure, successor, execution_ref, revised
        )
        path = self.directory / (record["reassessment_ref"] + ".json")
        if path.exists():
            # A completed registration stays valid after its own run has started.
            _require(_private_json(path) == record, "starter_reassessment_conflict")
            return record
        with self._serialized():
            for ref, definition, _ in self._evaluations():
                _require(ref != execution_ref, "reassessment_must_precede_execution")
                _require(
                    definition["permission"]["freeze_revision"] != frozen.freeze_revision,
                    "successor_already_exposed",
                )
            _record(path, record)
        _require(self.cases.evaluation_inputs(actor, successor) == frozen, "starter_freeze_changed")
        return record

    def assessment(self, actor: IntakePrincipal, work: IntakeWork, *, execution_ref: str) -> Json:
        """Label a scored result by its lineage; only a registered run supports a claim."""
        require_ref(execution_ref)
        frozen, targets = self._current(actor, work)
        result = self._scored(execution_ref, frozen, targets)
        base = {
            "execution_ref": execution_ref,
            "freeze_ref": frozen.freeze_ref,
            "freeze_revision": frozen.freeze_revision,
            "result_ref": result["result_ref"],
            "choice": {arm: _choice(result, arm) for arm in ARMS},
            "evidence_origin": ORIGIN,
            "training_credit": 0,
        }
        registered = [
            r
            for r in self._stored("starter-reassessment")
            if r.get("schema_version") == REASSESSMENT
            and r.get("successor", {}).get("freeze_revision") == frozen.freeze_revision
        ]
        for record in registered:
            if record["execution_ref"] != execution_ref:
                continue
            revised = RevisedChoice(**record["revised_choice"])
            expected, _ = self._reassessment_record(
                actor,
                self._sealed_exposure(record["exposure_ref"]),
                work,
                execution_ref,
                revised,
            )
            _require(canonical(expected) == canonical(record), "starter_reassessment_invalid")
            if base["choice"][revised.arm] != {
                "reference": revised.reference,
                "revision": revised.revision,
            }:
                return base | {"role": "unregistered_choice", "claim": None}
            protected = set(record["protected_families"])
            rows = [r for r in result["rows"] if r["family_id"] in protected]
            return base | {
                "role": "protected_reassessment",
                "reassessment_ref": record["reassessment_ref"],
                "exposure_ref": record["exposure_ref"],
                "revised_choice": record["revised_choice"],
                "claim": {
                    "protected_families": sorted(protected),
                    "protected_cases": len(rows),
                    "exposure_informed_families": record["exposure_informed_families"],
                    "metrics": _metrics(rows),
                },
            }
        exposures = [
            self.exposure(actor, work, r["exposure_ref"])
            for r in self._stored("starter-exposure")
            if r.get("freeze_revision") == frozen.freeze_revision
        ]
        original = [e for e in exposures if e["execution_ref"] == execution_ref]
        if original:
            return base | {
                "role": "original_exposure",
                "exposure_ref": original[0]["exposure_ref"],
                "diagnostic_set": original[0]["diagnostic_set"],
                "claim": "original_starter_result_unchanged",
            }
        if exposures:
            by_case = {r["case_id"]: r for r in result["rows"]}
            entries = {(d["case_id"], d["arm"]): d for e in exposures for d in e["diagnostic_set"]}
            diagnosis = []
            for (case_id, arm), entry in sorted(entries.items()):
                now = outcome(by_case[case_id][arm], by_case[case_id]["expected"]["target"])
                diagnosis.append(
                    {
                        "case_id": case_id,
                        "arm": arm,
                        "original": entry["outcome"],
                        "now": "fixed" if now == "match" else "still_" + now,
                    }
                )
            return base | {"role": "exposed_diagnostic", "diagnosis": diagnosis, "claim": None}
        return base | {
            "role": "unregistered_choice" if registered else "unplaced",
            "claim": None,
        }
