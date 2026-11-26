"""Generated owner-confirmed maps prove core software only, not an installed producer."""

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, replace

import pytest
from opentour_cell.pilot.intake_admission import AuthorizedSource, IntakeWork
from opentour_cell.pilot.intake_mcp import IntakeError, IntakePrincipal
from opentour_cell.pilot.starter_cases import (
    ConfirmedBranch,
    ConfirmedStarterMap,
    FrozenPlan,
    ProposedCase,
    StarterCases,
)
from test_workflow_benchmarks import ACTOR as INTAKE_ACTOR
from test_workflow_benchmarks import STARTER

pytest_plugins = ("test_workflow_benchmarks",)

OWNER = IntakePrincipal("generated-org", "owner", "untrusted-wire-role")
WORKER = IntakePrincipal("generated-org", "worker", "untrusted-wire-role")
WORK = IntakeWork(
    "run-1",
    "workflow-1",
    "starter",
    AuthorizedSource("workflow-1", "epoch-1", "starter", "policy", "grant-1"),
    ("policy-v1", "settings-v1"),
    1,
    None,
    True,
)
PLAN = FrozenPlan("baseline-pending", "comparator-v1", "exact-action-v1", "review-v1", "v1")


class Confirmation:
    """Independent generated owner/worker grants; no role string is trusted."""

    def __init__(self):
        self.allowed = True
        self.outputs_seen = False
        self.scope = ConfirmedStarterMap(
            OWNER.tenant,
            OWNER.actor,
            WORK,
            "confirmed-map-v1",
            "owner-confirmation-1",
            "a" * 64,
            "policy-v1",
            "settings-v1",
            (
                ConfirmedBranch(
                    "route",
                    ("category", "po", "amount", "variant"),
                    ("SOFTWARE", "FREIGHT", "SUPPLIES", "UTILITIES", "TAX", "REVIEW"),
                    ("policy-section-1",),
                    "exact-action-v1",
                    True,
                ),
            ),
            PLAN,
        )

    @contextmanager
    def guard(self, actor, work, *, purpose):
        allowed = {OWNER.actor} if purpose != "materialize" else {OWNER.actor, WORKER.actor}
        if (
            not self.allowed
            or actor.tenant != OWNER.tenant
            or actor.actor not in allowed
            or work != self.scope.work
        ):
            raise IntakeError("confirmation_denied")
        if self.outputs_seen and purpose in {"propose", "adjudicate", "freeze"}:
            raise IntakeError("outputs_already_observed")
        yield self.scope


def case(name, *, suggestion=None, family=None, template=None, **fields):
    return ProposedCase(
        name.lower(),
        family or name.lower(),
        template or name.lower(),
        "route",
        tuple(sorted(({"category": name} | fields).items())),
        suggestion or name,
        "Generated policy case",
        "policy-section-1",
    )


def seven():
    return tuple(
        case(n, suggestion="SOFTWARE" if n == "FREIGHT" else "REVIEW" if n == "MIXED" else n)
        for n in ("SOFTWARE", "FREIGHT", "SUPPLIES", "UTILITIES", "TAX", "REVIEW", "MIXED")
    )


@pytest.fixture
def core(tmp_path):
    tmp_path.chmod(0o700)
    auth = Confirmation()
    return StarterCases(tmp_path, auth), auth


def adjudicate_all(service, cases, work=WORK):
    for i, item in enumerate(cases):
        service.adjudicate(
            OWNER,
            work,
            case_id=item.case_id,
            target=None
            if item.case_id == "mixed"
            else "FREIGHT"
            if item.case_id == "freight"
            else item.suggestion,
            reason="Owner checked cited policy",
            expected_revision=i,
            operation_ref=f"owner-action-{i}",
        )


def freeze_seven(service, work=WORK):
    cases = seven()
    service.propose(OWNER, work, cases)
    adjudicate_all(service, cases, work)
    splits = {
        c.family_id: "excluded"
        if c.case_id == "mixed"
        else "development"
        if c.case_id in {"utilities", "tax"}
        else "reserved"
        for c in cases
    }
    return service.freeze(OWNER, work, splits=splits, plan=PLAN, expected_revision=7), splits


def test_seven_family_owner_edit_unscored_reopen_without_models(core):
    service, auth = core
    frozen, splits = freeze_seven(service)
    assert (
        frozen.proposed,
        frozen.reviewed,
        frozen.resolved,
        frozen.unresolved,
        frozen.development,
        frozen.reserved,
        frozen.excluded,
    ) == (7, 7, 6, 1, 2, 4, 1)
    assert len(set(frozen.reserved_family_refs)) == 4
    private = service.review(OWNER, WORK)
    freight = next(c for c in private["cases"] if c["case_id"] == "freight")
    assert freight["suggestion"] == "SOFTWARE"
    assert freight["owner_adjudicated_target"] == "FREIGHT"
    assert freight["disposition"] == "edit" and freight["adjudicator"] == OWNER.actor
    assert freight["adjudicated_at"] and freight["adjudication_reason"]
    reopened = StarterCases(service.path.parent, auth)
    assert reopened.freeze(OWNER, WORK, splits=splits, plan=PLAN, expected_revision=7) == frozen
    receipt = reopened.materialize(WORKER, WORK, operation_ref="freeze-worker")
    assert receipt.state == "frozen_unscored"
    assert receipt.result_ref is None and receipt.result_digest is None
    assert receipt == reopened.readback(WORKER, WORK, operation_ref="freeze-worker")
    assert "owner_adjudicated_target" not in json.dumps(asdict(receipt))
    assert sorted(p.name for p in service.path.parent.iterdir()) == ["starter-cases.sqlite3"]


@pytest.mark.parametrize("denial", ["absent", "revoked", "foreign", "worker", "stale", "draft"])
def test_missing_current_confirmation_denies_before_storage(core, denial):
    service, auth = core
    actor, work = OWNER, WORK
    if denial == "absent":
        service.confirmation = None
    elif denial == "revoked":
        auth.allowed = False
    elif denial == "foreign":
        actor = replace(OWNER, tenant="foreign")
    elif denial == "worker":
        actor = replace(WORKER, role="owner")
    elif denial == "stale":
        work = replace(WORK, revisions=("old-policy", "settings-v1"))
    else:
        auth.scope = replace(auth.scope, confirmation_ref="")
    with pytest.raises(IntakeError):
        service.propose(actor, work, seven())
    assert not service.path.exists()


@pytest.mark.parametrize(
    "alteration", ["label", "field", "citation", "origin", "template", "input"]
)
def test_invalid_proposal_never_writes(core, alteration):
    service, _ = core
    a = case("SOFTWARE")
    b = case("FREIGHT")
    if alteration == "label":
        a = replace(a, suggestion="UNSUPPORTED")
    elif alteration == "field":
        a = replace(a, decision_inputs=(("future_model_output", "SOFTWARE"),))
    elif alteration == "citation":
        a = replace(a, citation="uncited")
    elif alteration == "origin":
        a = replace(a, case_origin="verified_live")
    elif alteration == "template":
        b = replace(b, template_id=a.template_id)
    else:
        b = replace(b, decision_inputs=a.decision_inputs)
    with pytest.raises(IntakeError):
        service.propose(OWNER, WORK, (a, b))
    assert not service.path.exists()


def test_stale_action_replay_and_post_output_denial(core):
    service, auth = core
    service.propose(OWNER, WORK, seven())
    kw = dict(
        case_id="software",
        target="SOFTWARE",
        reason="Cited rule",
        expected_revision=0,
        operation_ref="action",
    )
    assert service.adjudicate(OWNER, WORK, **kw) == 1
    assert service.adjudicate(OWNER, WORK, **kw) == 1
    with pytest.raises(IntakeError, match="action_conflict"):
        service.adjudicate(OWNER, WORK, **(kw | {"target": "FREIGHT"}))
    with pytest.raises(IntakeError, match="revision_changed"):
        service.adjudicate(OWNER, WORK, **(kw | {"operation_ref": "different"}))
    auth.outputs_seen = True
    with pytest.raises(IntakeError, match="outputs_already_observed"):
        service.adjudicate(OWNER, WORK, **(kw | {"expected_revision": 1}))
    assert service.review(OWNER, WORK)["revision"] == 1


def test_freeze_is_immutable_and_unresolved_cannot_enter_reserved(core):
    service, _ = core
    frozen, splits = freeze_seven(service)
    with pytest.raises(IntakeError, match="successor_required"):
        service.freeze(
            OWNER, WORK, splits=splits | {"tax": "reserved"}, plan=PLAN, expected_revision=7
        )
    with pytest.raises(IntakeError, match="successor_required"):
        service.adjudicate(
            OWNER,
            WORK,
            case_id="software",
            target="FREIGHT",
            reason="Changed",
            expected_revision=7,
            operation_ref="after-freeze",
        )
    assert service.readback(OWNER, WORK, operation_ref="read").definition_digest == frozen.revision


def test_no_second_reservation_even_with_renamed_family_and_template(core):
    service, auth = core
    freeze_seven(service)
    newer = replace(
        WORK,
        run_ref="run-2",
        revisions=("policy-v2", "settings-v1"),
        benchmark_version=2,
        benchmark_parent_run="run-1",
    )
    auth.scope = replace(auth.scope, work=newer, policy_revision="policy-v2", map_ref="map-v2")
    cases = tuple(
        replace(c, family_id="new-" + c.family_id, template_id="new-" + c.template_id)
        for c in seven()
    )
    service.propose(OWNER, newer, cases)
    adjudicate_all(service, cases, newer)
    splits = {c.family_id: "excluded" if c.case_id == "mixed" else "reserved" for c in cases}
    with pytest.raises(IntakeError, match="family_already_assigned"):
        service.freeze(OWNER, newer, splits=splits, plan=PLAN, expected_revision=7)
    assert service.readback(OWNER, newer, operation_ref="read") is None


@pytest.mark.parametrize("invalid", ["unreviewed", "unresolved", "missing-family"])
def test_incomplete_review_and_split_hold(core, invalid):
    service, _ = core
    cases = seven()
    service.propose(OWNER, WORK, cases)
    if invalid != "unreviewed":
        adjudicate_all(service, cases)
    splits = {c.family_id: "reserved" for c in cases}
    if invalid == "missing-family":
        splits.pop("mixed")
    with pytest.raises(IntakeError):
        service.freeze(
            OWNER,
            WORK,
            splits=splits,
            plan=PLAN,
            expected_revision=0 if invalid == "unreviewed" else 7,
        )
    assert service.readback(OWNER, WORK, operation_ref="read") is None


def test_purchase_coverage_zero_review_and_paraphrases_count_one(core):
    service, auth = core
    branch = ConfirmedBranch(
        "purchase",
        ("po", "amount", "variant"),
        ("APPROVE", "MANUAL", "REVIEW"),
        ("purchase-policy",),
        "exact-v1",
        True,
    )
    auth.scope = replace(auth.scope, branches=(branch,))
    cases = tuple(
        ProposedCase(
            f"approve-{i}",
            "approve-family",
            "approve-template",
            "purchase",
            (("po", True), ("amount", 249.99), ("variant", i)),
            "APPROVE",
            "Generated paraphrase",
            "purchase-policy",
        )
        for i in range(100)
    )
    cases += tuple(
        ProposedCase(
            f"manual-{i}",
            f"manual-{i}",
            f"manual-{i}",
            "purchase",
            (("po", True), ("amount", amount)),
            "MANUAL",
            "Boundary",
            "purchase-policy",
        )
        for i, amount in enumerate((250, 250.01))
    )
    service.propose(OWNER, WORK, cases)
    adjudicate_all(service, cases)
    frozen = service.freeze(
        OWNER,
        WORK,
        splits={c.family_id: "reserved" for c in cases},
        plan=PLAN,
        expected_revision=len(cases),
    )
    assert frozen.coverage == (
        ("purchase", "APPROVE", 1),
        ("purchase", "MANUAL", 2),
        ("purchase", "REVIEW", 0),
    )
    assert frozen.uncovered_critical == ("purchase",)
    assert frozen.reserved == 3 and frozen.state == "frozen_unscored"


def test_revocation_foreign_worker_and_partial_restore_deny(core):
    service, auth = core
    freeze_seven(service)
    auth.allowed = False
    with pytest.raises(IntakeError):
        service.readback(WORKER, WORK, operation_ref="read")
    auth.allowed = True
    with pytest.raises(IntakeError):
        service.review(WORKER, WORK)
    with sqlite3.connect(service.path) as db:
        body = json.loads(db.execute("SELECT body FROM packs").fetchone()[0])
        body["frozen"] = None
        db.execute("UPDATE packs SET body=?", (json.dumps(body),))
    with pytest.raises(IntakeError, match="receipt_invalid"):
        service.readback(WORKER, WORK, operation_ref="read")


def test_actual_durable_intake_adopts_only_current_frozen_owner_receipt(prepared):
    intake, intake_authority = prepared
    admitted = intake.submit_intake(INTAKE_ACTOR, STARTER)
    with intake.intake.admitted_work(INTAKE_ACTOR, admitted.run_ref) as work:
        captured = work

    class InstalledFixtureConfirmation:
        @contextmanager
        def guard(self, actor, work, *, purpose):
            with intake_authority.guard(actor):
                assert actor == INTAKE_ACTOR and work == captured
                yield replace(
                    Confirmation().scope,
                    tenant=actor.tenant,
                    owner_id=actor.actor,
                    work=work,
                    policy_revision=work.revisions[0],
                    settings_revision=work.revisions[1],
                )

    service = StarterCases(intake_authority.directory, InstalledFixtureConfirmation())
    proposed = (case("SOFTWARE"),)
    service.propose(INTAKE_ACTOR, captured, proposed)
    service.adjudicate(
        INTAKE_ACTOR,
        captured,
        case_id="software",
        target="SOFTWARE",
        reason="Owner checked actual admitted policy",
        expected_revision=0,
        operation_ref="owner-label",
    )
    service.freeze(
        INTAKE_ACTOR, captured, splits={"software": "reserved"}, plan=PLAN, expected_revision=1
    )
    assert intake.materialize(
        INTAKE_ACTOR, admitted.run_ref, service, target_state="frozen_unscored"
    )
    status = intake.get_run(INTAKE_ACTOR, admitted.run_ref)
    assert status.benchmark.benchmark_state == "frozen_unscored"
    assert intake.materialize(
        INTAKE_ACTOR, admitted.run_ref, service, target_state="frozen_unscored"
    )
    intake_authority.allowed = False
    with pytest.raises(IntakeError):
        intake.materialize(INTAKE_ACTOR, admitted.run_ref, service, target_state="frozen_unscored")


def test_all_five_purchase_families_reserved_before_any_predictions(core):
    service, auth = core
    auth.scope = replace(
        auth.scope,
        branches=(
            ConfirmedBranch(
                "purchase",
                ("po", "amount"),
                ("APPROVE", "MANUAL", "REVIEW"),
                ("purchase-policy",),
                "exact-v1",
                True,
            ),
        ),
    )
    vectors = (
        (False, 50, "REVIEW"),
        (False, 250, "REVIEW"),
        (True, 249.99, "APPROVE"),
        (True, 250, "MANUAL"),
        (True, 250.01, "MANUAL"),
    )
    cases = tuple(
        ProposedCase(
            f"purchase-{i}",
            f"family-{i}",
            f"template-{i}",
            "purchase",
            (("po", po), ("amount", amount)),
            target,
            "Cited branch",
            "purchase-policy",
        )
        for i, (po, amount, target) in enumerate(vectors)
    )
    service.propose(OWNER, WORK, cases)
    adjudicate_all(service, cases)
    frozen = service.freeze(
        OWNER, WORK, splits={c.family_id: "reserved" for c in cases}, plan=PLAN, expected_revision=5
    )
    assert frozen.coverage == (
        ("purchase", "APPROVE", 1),
        ("purchase", "MANUAL", 2),
        ("purchase", "REVIEW", 2),
    )
    assert frozen.uncovered_critical == ()
    assert frozen.state == "frozen_unscored" and frozen.reserved == 5


def test_partial_restore_of_authentic_draft_is_detected(core):
    service, _ = core
    service.propose(OWNER, WORK, seven())
    with sqlite3.connect(service.path) as db:
        old_body, old_seal = db.execute("SELECT body,seal FROM packs").fetchone()
    freeze_seven(service)
    with sqlite3.connect(service.path) as db:
        db.execute("UPDATE packs SET body=?,seal=?", (old_body, old_seal))
    with pytest.raises(IntakeError, match="receipt_invalid"):
        service.review(OWNER, WORK)


def test_reservation_restore_and_unconfirmed_plan_hold(core):
    service, _ = core
    _, splits = freeze_seven(service)
    with pytest.raises(IntakeError, match="plan_changed"):
        service.freeze(
            OWNER,
            WORK,
            splits=splits,
            plan=replace(PLAN, comparator_ref="other"),
            expected_revision=7,
        )
    with sqlite3.connect(service.path) as db:
        db.execute("DELETE FROM families WHERE family='family:software'")
    with pytest.raises(IntakeError, match="reservation_invalid"):
        service.readback(WORKER, WORK, operation_ref="read")


def test_authoring_owner_evaluated_and_evaluator_identities_stay_distinct(core):
    service, auth = core
    auth.scope = replace(
        auth.scope,
        frozen_plan=replace(PLAN, evaluator_ref="evaluator", evaluated_model_ref="evaluated-model"),
    )
    for identity in (OWNER.actor, "evaluator", "evaluated-model"):
        with pytest.raises(IntakeError, match="identity_conflict"):
            service.propose(OWNER, WORK, (replace(case("SOFTWARE"), authoring_identity=identity),))
    assert not service.path.exists()
    service.propose(OWNER, WORK, (replace(case("SOFTWARE"), authoring_identity="authoring-model"),))
    assert service.review(OWNER, WORK)["cases"][0]["authoring_identity"] == "authoring-model"
