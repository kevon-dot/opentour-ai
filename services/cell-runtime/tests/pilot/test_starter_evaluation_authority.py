"""Actual owner-frozen pack and separately signed evaluator/retention grants."""

import hmac
import json
import time
from dataclasses import asdict

import pytest
from opentour_cell.pilot.intake_mcp import IntakeError, IntakePrincipal
from opentour_cell.pilot.operating_point import canonical, digest
from opentour_cell.pilot.starter_cases import FrozenPlan
from opentour_cell.pilot.starter_evaluation_authority import (
    EXECUTION,
    INSPECTION,
    SELECTION,
    StarterEvaluationAuthority,
    StarterInspectionAuthority,
    _context,
)
from opentour_cell.pilot.starter_score_inspection import StarterScoreInspection
from opentour_cell.pilot.starter_scoring import EvaluationPermission
from test_intake_mcp import _write_private
from test_slack_adapter import ACTOR, OTHER
from test_starter_map_confirmation import FiveFamilies, adjudicate, sign_case

pytest_plugins = ("test_starter_map_confirmation",)


def seal(directory, filename, grant):
    key = json.loads((directory / "starter-intake-key.json").read_text())["key"]
    _write_private(
        directory / filename,
        dict(
            grant=grant,
            seal_sha256=hmac.digest(bytes.fromhex(key), canonical(grant).encode(), "sha256").hex(),
        ),
    )


@pytest.fixture
def authorized(joined):
    h, owner, cap, maps, setup = joined
    cap["frozen_plan"] = asdict(
        FrozenPlan(
            "baseline-pending",
            "simple-policy-v1",
            "exact-action-v1",
            "review-v1",
            "v1",
            "independent-evaluator",
            "selected-model",
        )
    )
    sign_case(h.path.parent, cap)
    owner.propose(FiveFamilies())
    adjudicate(owner)
    frozen = owner.freeze(splits={f"family-{i}": "reserved" for i in range(5)}, expected_revision=5)
    (authority_path,) = h.path.parent.glob("starter-case-authority-*.json")
    original = authority_path.read_bytes()
    record = json.loads(original)
    context = _context(maps.preview(owner.actor), FrozenPlan(**cap["frozen_plan"]))
    permission = EvaluationPermission(
        owner.actor.tenant,
        cap["workflow_ref"],
        cap["engagement_epoch"],
        cap["run_ref"],
        "independent-evaluator",
        "execution-a",
        "evaluation-a",
        frozen.reference,
        frozen.revision,
        digest(cap["frozen_plan"]),
        context.comparator_revision,
        "selected-model",
        "model-v1",
        "baseline-grant-v1",
        ("po", "total"),
        5,
        int(time.time()) + 500,
    )
    execution = dict(
        schema_version=EXECUTION,
        grant_ref="evaluation-a",
        tenant=cap["tenant"],
        custodian_ref=cap["custodian_ref"],
        case_authority_ref=authority_path.stem,
        case_envelope_digest=digest(record["envelope"]),
        permission=asdict(permission),
        expires_at=int(time.time()) + 500,
    )
    inspection = execution | dict(
        schema_version=INSPECTION,
        grant_ref="inspection-a",
        viewer_actor=ACTOR,
        owner_ref=cap["owner_ref"],
    )
    seal(h.path.parent, "execution-a.json", execution)
    seal(h.path.parent, "inspection-a.json", inspection)
    _write_private(
        h.path.parent / "starter-evaluation-selection.json",
        dict(
            schema_version=SELECTION,
            executions={"execution-a": "execution-a.json"},
            inspections={"execution-a": "inspection-a.json"},
        ),
    )
    evaluator = IntakePrincipal(owner.actor.tenant, "independent-evaluator", "untrusted")
    authority = StarterEvaluationAuthority(owner.settings, evaluator)
    return (
        h,
        owner,
        cap,
        maps,
        authority,
        permission,
        execution,
        inspection,
        authority_path,
        original,
    )


def files(directory):
    return {
        str(p.relative_to(directory)): p.read_bytes() for p in directory.rglob("*") if p.is_file()
    }


def test_separate_evaluator_reads_same_freeze_without_owner_alias(authorized):
    h, owner, cap, maps, authority, p, _, _, path, original = authorized
    before = files(h.path.parent)
    work = authority.work(p.execution_ref)
    with authority.guard(authority.actor, work, p.execution_ref) as actual:
        assert actual == p
        inputs = authority.cases.evaluation_inputs(authority.actor, work)
        assert inputs.freeze_ref == p.freeze_ref and inputs.freeze_revision == p.freeze_revision
        assert len(inputs.cases) == 5 and "target" not in repr(inputs)
        targets = authority.cases.evaluation_targets(
            authority.actor, work, freeze_revision=p.freeze_revision
        )
        assert [targets[f"case-{i}"]["target"] for i in range(5)] == [
            "REVIEW",
            "REVIEW",
            "APPROVE",
            "MANUAL",
            "MANUAL",
        ]
        context = authority.comparator_context(authority.actor, work, p)
        assert context.comparator_revision == p.comparator_revision
        assert context.confirmed_map_ref == cap["map_confirmation_ref"]
        assert (
            "independent-evaluator"
            not in maps._capability(owner.actor, maps.authority._read_grant())["readers"]
        )
        with pytest.raises(IntakeError):
            authority.cases.review(authority.actor, work)
    assert path.read_bytes() == original
    assert files(h.path.parent) == before


@pytest.mark.parametrize("who", [ACTOR, OTHER, "intake-worker", "other-evaluator"])
def test_other_roles_do_not_inherit_execution(authorized, who):
    _, owner, _, _, _, p, *_ = authorized
    authority = StarterEvaluationAuthority(
        owner.settings, IntakePrincipal(owner.actor.tenant, who, "evaluator")
    )
    with pytest.raises(IntakeError):
        authority.work(p.execution_ref)


def test_inspection_independent_of_execution_expiry_or_model_access(authorized, monkeypatch):
    h, owner, _, _, authority, p, execution, inspection, *_ = authorized
    reader = StarterInspectionAuthority(owner.settings, owner.actor)
    before = files(h.path.parent)
    assert reader.entry(p.execution_ref)
    summary = StarterScoreInspection(h.path.parent, reader).summary(
        owner.actor, p.workflow_ref, p.execution_ref
    )
    assert summary["state"] == "frozen_unscored" and summary["reserved"] == 5
    assert "metrics" not in summary
    assert files(h.path.parent) == before
    _write_private(h.path.parent / "starter-evaluation-withdrawn-evaluation-a.json", {})
    with pytest.raises(IntakeError):
        authority.work(p.execution_ref)
    assert (
        StarterScoreInspection(h.path.parent, reader).summary(
            owner.actor, p.workflow_ref, p.execution_ref
        )
        == summary
    )


@pytest.mark.parametrize("change", ["case", "source", "map", "execution", "owner"])
def test_withdrawal_or_rebinding_fences_current_execution(authorized, change):
    h, owner, cap, maps, authority, p, *_ = authorized
    work = authority.work(p.execution_ref)
    with pytest.raises((IntakeError, ValueError)):
        with authority.guard(authority.actor, work, p.execution_ref) as actual:
            if change == "case":
                _write_private(h.path.parent / "starter-case-withdrawn-case-permission.json", {})
            elif change == "source":
                _write_private(h.path.parent / "starter-intake-withdrawn-policy-scope.json", {})
            elif change == "map":
                _write_private(h.path.parent / "map-review-withdrawn-map-review-v1.json", {})
                grant = json.loads((h.path.parent / maps.selection["grant_file"]).read_text())[
                    "grant"
                ]
                _write_private(
                    h.path.parent / f"map-review-withdrawn-{grant['grant_ref']}.json", {}
                )
            elif change == "execution":
                _write_private(h.path.parent / "starter-evaluation-withdrawn-evaluation-a.json", {})
            else:
                h.config["starter_setup"]["slack_owner"] = OTHER
                h.save()
            authority.revalidate(authority.actor, work, actual)


def test_unknown_execution_and_inspector_roles_hold_without_writes(authorized):
    h, owner, _, _, authority, p, *_ = authorized
    before = files(h.path.parent)
    with pytest.raises(IntakeError):
        authority.work("unsanctioned-rerun")
    reader = StarterInspectionAuthority(owner.settings, authority.actor)
    assert reader.entry(p.execution_ref) is None
    with pytest.raises(IntakeError):
        StarterScoreInspection(h.path.parent, reader).summary(
            authority.actor, p.workflow_ref, p.execution_ref
        )
    assert files(h.path.parent) == before


def test_home_entry_reads_no_policy_cases_or_score_journal(authorized, monkeypatch):
    from opentour_cell.pilot import starter_evaluation_authority as implementation
    from opentour_cell.pilot.starter_cases import StarterCases

    h, owner, _, _, _, p, *_ = authorized
    before = files(h.path.parent)
    monkeypatch.setattr(
        implementation, "_read_policy", lambda *a: pytest.fail("Home raw policy read")
    )
    monkeypatch.setattr(StarterCases, "_db", lambda *a, **k: pytest.fail("Home case read"))
    reader = StarterInspectionAuthority(owner.settings, owner.actor)
    assert reader.entry(p.execution_ref) == dict(
        workflow_ref=p.workflow_ref,
        execution_ref=p.execution_ref,
        revision=digest(reader._grant(p.execution_ref)[0]),
    )
    assert files(h.path.parent) == before


@pytest.mark.parametrize("revoke", ["case", "map", "execution"])
def test_withdrawal_after_map_lookup_prevents_policy_bytes(authorized, monkeypatch, revoke):
    from opentour_cell.pilot import starter_evaluation_authority as implementation
    from opentour_cell.pilot.map_review import MapReviewService

    h, _, _, maps, authority, p, *_ = authorized
    original = MapReviewService._records
    map_ref = json.loads((h.path.parent / maps.selection["grant_file"]).read_text())["grant"][
        "grant_ref"
    ]
    tombstone = {
        "case": "starter-case-withdrawn-case-permission.json",
        "map": f"map-review-withdrawn-{map_ref}.json",
        "execution": "starter-evaluation-withdrawn-evaluation-a.json",
    }[revoke]

    def withdraw(service, db):
        records = original(service, db)
        _write_private(h.path.parent / tombstone, {})
        return records

    monkeypatch.setattr(MapReviewService, "_records", withdraw)
    monkeypatch.setattr(
        implementation, "_read_policy", lambda *a: pytest.fail("Withdrawn raw read")
    )
    with pytest.raises(IntakeError):
        authority.work(p.execution_ref)


@pytest.fixture
def scorer(authorized):
    from opentour_cell.pilot import model_connector as model
    from opentour_cell.pilot.model_registry import ModelRegistry
    from opentour_cell.pilot.starter_scoring import PredictionReceipt, StarterScoring
    from test_model_registry import write
    from test_starter_scoring import Port

    h, owner, _, _, authority, p, *_ = authorized
    binding = model.ModelBinding(
        p.tenant,
        p.model_ref,
        p.model_revision,
        "classification",
        "po-total-v1",
        "actions-v1",
        "identity-v1",
        model.Route.LOCAL,
        "generated-host",
        "local",
        "local-credential-ref",
    )
    entry = asdict(binding)
    entry.pop("tenant")
    registration = dict(
        schema_version="opentour.pilot.model-registration.v1",
        tenant=p.tenant,
        models=[entry],
        grants=[
            dict(
                actor=p.evaluator,
                model_ref=p.model_ref,
                immutable_revision=p.model_revision,
                purpose=model.Purpose.BASELINE,
                revision=p.baseline_grant_revision,
                expires_at=int(time.time()) + 300,
                retention_until=int(time.time()) + 600,
                permitted_fields=list(p.input_fields),
                fit_bound=None,
                benchmark_revisions=[],
            )
        ],
    )
    path = h.path.parent / "model-registration.json"
    write(path, registration)

    class PurchasePort(Port):
        def execute(self, request, inputs):
            assert all(set(fields) == {"po", "total"} for fields in inputs.values())
            self.calls.append((request, inputs))
            outputs = {}
            for handle, fields in inputs.items():
                if fields["po"] is None:
                    outputs[handle] = {"status": "review"}
                else:
                    label = "APPROVE" if fields["total"] < 250 else "MANUAL"
                    if self.reference == p.model_ref and fields["total"] == 250:
                        label = "APPROVE"
                    outputs[handle] = {"status": "automatic", "label": label}
            receipt = PredictionReceipt(request, "receipt-" + digest(asdict(request)), outputs)
            self.receipts[request.operation_ref] = receipt
            return receipt

    baseline = PurchasePort(p.model_ref, p.model_revision, binding)
    comparator = PurchasePort("simple-policy-v1", p.comparator_revision)
    service = StarterScoring(
        h.path.parent,
        authority.cases,
        authority,
        ModelRegistry(path, p.tenant),
        baseline,
        comparator,
    )
    return authorized, service, registration, path


def test_actual_scoring_retry_explicit_rerun_and_retained_inspection(scorer):
    from opentour_cell.pilot.starter_scoring import StarterScoring
    from test_model_registry import write

    authorized, service, registration, path = scorer
    h, owner, _, _, authority, p, execution, inspection, authority_path, original = authorized
    work = authority.work(p.execution_ref)
    result = service.evaluate(authority.actor, work, p.execution_ref)
    assert result["state"] == "scored"
    assert result["metrics"]["model"]["exact_action"]["numerator"] == 4
    assert result["metrics"]["comparator"]["exact_action"]["numerator"] == 5
    assert service.evaluate(authority.actor, work, p.execution_ref) == result
    assert len(service.baseline.calls) == len(service.comparator.calls) == 1
    assert authority_path.read_bytes() == original
    reader = StarterInspectionAuthority(owner.settings, owner.actor)
    before = files(h.path.parent)
    summary = StarterScoreInspection(h.path.parent, reader).summary(
        owner.actor, p.workflow_ref, p.execution_ref
    )
    assert summary["metrics"] == result["metrics"]
    assert files(h.path.parent) == before
    # A distinct signed execution, not a transport key, is the only rerun authority.
    new_permission = dict(
        execution["permission"], execution_ref="execution-b", revision="evaluation-b"
    )
    seal(
        h.path.parent,
        "execution-b.json",
        execution | dict(grant_ref="evaluation-b", permission=new_permission),
    )
    selected = json.loads((h.path.parent / "starter-evaluation-selection.json").read_text())
    selected["executions"]["execution-b"] = "execution-b.json"
    _write_private(h.path.parent / "starter-evaluation-selection.json", selected)
    successor = StarterEvaluationAuthority(owner.settings, authority.actor)
    rerun = StarterScoring(
        h.path.parent,
        successor.cases,
        successor,
        service.registry,
        service.baseline,
        service.comparator,
    )
    second = rerun.evaluate(successor.actor, successor.work("execution-b"), "execution-b")
    assert second["freeze_revision"] == result["freeze_revision"]
    assert second["execution_ref"] != result["execution_ref"]
    assert len(service.baseline.calls) == len(service.comparator.calls) == 2
    # Independent retained inspection survives loss of inference and execution rights.
    registration["grants"] = []
    write(path, registration)
    _write_private(h.path.parent / "starter-evaluation-withdrawn-evaluation-a.json", {})
    reader = StarterInspectionAuthority(owner.settings, owner.actor)
    assert (
        StarterScoreInspection(h.path.parent, reader).summary(
            owner.actor, p.workflow_ref, p.execution_ref
        )
        == summary
    )
    assert authority_path.read_bytes() == original


def test_late_execution_withdrawal_prevents_scoring_commit(scorer, monkeypatch):
    import sqlite3

    from opentour_cell.pilot import starter_scoring

    authorized, service, _, _ = scorer
    h, _, _, _, authority, p, *_ = authorized
    work = authority.work(p.execution_ref)
    original = starter_scoring._metrics

    def withdraw(rows):
        _write_private(h.path.parent / "starter-evaluation-withdrawn-evaluation-a.json", {})
        return original(rows)

    monkeypatch.setattr(starter_scoring, "_metrics", withdraw)
    with pytest.raises(IntakeError):
        service.evaluate(authority.actor, work, p.execution_ref)
    with sqlite3.connect(service.path) as db:
        assert (
            db.execute("SELECT count(*) FROM evaluations WHERE result IS NOT NULL").fetchone()[0]
            == 0
        )


@pytest.mark.parametrize(
    "change", ["epoch", "envelope", "evaluator", "expiry", "foreign", "freeze"]
)
def test_malformed_or_stale_execution_identity_holds(authorized, change):
    h, _, _, _, authority, p, execution, *_ = authorized
    if change == "envelope":
        execution["case_envelope_digest"] = "0" * 64
    elif change == "foreign":
        execution["tenant"] = "another-tenant"
    else:
        field, value = {
            "epoch": ("epoch", "older-epoch"),
            "evaluator": ("evaluator", ACTOR),
            "expiry": ("expires_at", 1),
            "freeze": ("freeze_revision", "0" * 64),
        }[change]
        execution["permission"][field] = value
    seal(h.path.parent, "execution-a.json", execution)
    with pytest.raises(IntakeError):
        work = authority.work(p.execution_ref)
        with authority.guard(authority.actor, work, p.execution_ref):
            pytest.fail("Invalid execution admitted")


def test_actual_durable_map_comparator_and_original_freeze_join(scorer):
    import sqlite3

    from opentour_cell.pilot.starter_map_comparator import StarterMapComparator, initialize_journal

    authorized, service, _, _ = scorer
    h, owner, _, _, authority, p, _, _, authority_path, original = authorized
    work = authority.work(p.execution_ref)
    initialize_journal(h.path.parent)
    service.comparator = StarterMapComparator(
        h.path.parent,
        authority.actor,
        work,
        authority.cases,
        authority,
        p,
        "simple-policy-v1",
        b"generated-comparator-receipt-key-01",
    )
    result = service.evaluate(authority.actor, work, p.execution_ref)
    assert result["metrics"]["comparator"]["exact_action"] == dict(
        numerator=5, denominator=5, value=1
    )
    assert result["metrics"]["comparator"]["coverage"]["numerator"] == 3
    assert result["metrics"]["comparator"]["wrong_automatic"]["numerator"] == 0
    assert service.evaluate(authority.actor, work, p.execution_ref) == result
    with sqlite3.connect(service.comparator.path) as db:
        assert db.execute("SELECT count(*) FROM receipts").fetchone()[0] == 1
    reader = StarterInspectionAuthority(owner.settings, owner.actor)
    assert (
        StarterScoreInspection(h.path.parent, reader).summary(
            owner.actor, p.workflow_ref, p.execution_ref
        )["metrics"]
        == result["metrics"]
    )
    assert authority_path.read_bytes() == original


def test_inspection_disclosure_rechecks_separate_retention_after_read(authorized, monkeypatch):
    from contextlib import contextmanager

    from opentour_cell.pilot import starter_score_inspection as implementation

    h, owner, _, _, _, p, *_ = authorized
    reader = StarterInspectionAuthority(owner.settings, owner.actor)
    actual = implementation._open_db

    @contextmanager
    def revoke(path):
        with actual(path) as db:
            yield db
            _write_private(h.path.parent / "starter-evaluation-withdrawn-inspection-a.json", {})

    monkeypatch.setattr(implementation, "_open_db", revoke)
    with pytest.raises(IntakeError):
        StarterScoreInspection(h.path.parent, reader).summary(
            owner.actor, p.workflow_ref, p.execution_ref
        )


def test_inspection_reconciles_original_freeze_not_result_claims(authorized):
    import sqlite3

    h, owner, _, _, _, p, *_ = authorized
    reader = StarterInspectionAuthority(owner.settings, owner.actor)
    with sqlite3.connect(owner.core.path) as db:
        (raw,) = db.execute("SELECT body FROM packs").fetchone()
        body = json.loads(raw)
        body["cases"][0]["owner_adjudicated_target"] = "MANUAL"
        db.execute("UPDATE packs SET body=?,seal=?", (canonical(body), digest(body)))
    with pytest.raises(IntakeError):
        StarterScoreInspection(h.path.parent, reader).summary(
            owner.actor, p.workflow_ref, p.execution_ref
        )


def test_restored_consistent_rows_still_must_match_independently_granted_freeze(authorized):
    import sqlite3

    h, owner, _, _, authority, p, *_ = authorized
    work = authority.work(p.execution_ref)
    with sqlite3.connect(owner.core.path) as db:
        ref, raw = db.execute("SELECT ref,body FROM packs").fetchone()
        body = json.loads(raw)
        body["cases"][0]["owner_adjudicated_target"] = "MANUAL"
        seal = digest(body)
        db.execute("UPDATE packs SET body=?,seal=?", (canonical(body), seal))
        db.execute("UPDATE freezes SET seal=? WHERE ref=?", (seal, ref))
    with pytest.raises(IntakeError):
        with authority.guard(authority.actor, work, p.execution_ref):
            pytest.fail("Restored truth does not match the granted freeze digest")
    reader = StarterInspectionAuthority(owner.settings, owner.actor)
    with pytest.raises(IntakeError):
        StarterScoreInspection(h.path.parent, reader).summary(
            owner.actor, p.workflow_ref, p.execution_ref
        )


def test_retention_outlives_original_execution_expiry(authorized, monkeypatch):
    h, owner, _, _, authority, p, _, inspection, *_ = authorized
    seal(h.path.parent, "inspection-a.json", inspection | {"expires_at": p.expires_at + 80})
    monkeypatch.setattr(time, "time", lambda: p.expires_at + 1)
    with pytest.raises(IntakeError):
        authority.work(p.execution_ref)
    reader = StarterInspectionAuthority(owner.settings, owner.actor)
    assert (
        StarterScoreInspection(h.path.parent, reader).summary(
            owner.actor, p.workflow_ref, p.execution_ref
        )["state"]
        == "frozen_unscored"
    )


@pytest.mark.parametrize(
    "journal", ["training.sqlite3", "learning-mandates.sqlite3", "map-reviews.sqlite3"]
)
def test_home_does_not_recreate_closed_wal_sidecars(authorized, journal):
    import sqlite3

    h, owner, _, _, _, p, *_ = authorized
    db = sqlite3.connect(h.path.parent / journal)
    assert db.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    db.close()
    assert not (h.path.parent / (journal + "-wal")).exists()
    before = files(h.path.parent)
    reader = StarterInspectionAuthority(owner.settings, owner.actor)
    assert reader.entry(p.execution_ref) is None
    assert files(h.path.parent) == before


@pytest.mark.parametrize("change", ["evaluator", "plan", "model", "freeze", "fields"])
def test_signed_successor_cannot_read_policy_outside_original_freeze_identity(
    authorized, monkeypatch, change
):
    from dataclasses import replace

    from opentour_cell.pilot import starter_evaluation_authority as implementation

    h, owner, _, _, authority, p, execution, *_ = authorized
    actor = authority.actor
    if change == "evaluator":
        actor = replace(actor, actor="another-evaluator")
        execution["permission"]["evaluator"] = actor.actor
    elif change == "plan":
        execution["permission"]["plan_digest"] = "0" * 64
    elif change == "model":
        execution["permission"]["model_ref"] = "another-model"
    elif change == "freeze":
        execution["permission"]["freeze_ref"] = "another-freeze"
    else:
        execution["permission"]["input_fields"] = ["unpermitted_field"]
    seal(h.path.parent, "execution-a.json", execution)
    replacement = StarterEvaluationAuthority(owner.settings, actor)
    monkeypatch.setattr(
        implementation, "_read_policy", lambda *a: pytest.fail("unauthorized policy read")
    )
    with pytest.raises(IntakeError):
        replacement.work(p.execution_ref)
