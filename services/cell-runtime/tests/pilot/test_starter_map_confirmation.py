"""Actual starter/map services plus independently sealed generated case permissions."""

import hashlib
import hmac
import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import asdict
from decimal import Decimal

import pytest
from opentour_cell.pilot import map_review
from opentour_cell.pilot.guided_policy import GuidedPolicySource
from opentour_cell.pilot.intake_admission import DurableIntake
from opentour_cell.pilot.intake_mcp import IntakeError, IntakePrincipal
from opentour_cell.pilot.map_review import BranchEdit, MapSettingsEdit
from opentour_cell.pilot.operating_point import canonical
from opentour_cell.pilot.slack_adapter import Settings
from opentour_cell.pilot.starter_cases import FrozenPlan, ProposedCase
from opentour_cell.pilot.starter_map_confirmation import GRANT, SELECTION, StarterCaseSession
from opentour_cell.pilot.starter_setup import StarterSetup
from opentour_cell.pilot.workflow_benchmarks import BenchmarkIntake
from test_guided_policy import PURCHASE_POLICY
from test_intake_mcp import _sealed_starter, _write_private
from test_map_review import stage_map_review
from test_slack_adapter import ACTOR, OTHER

pytest_plugins = ("test_starter_setup",)


def sign_case(directory, cap):
    key = json.loads((directory / "starter-intake-key.json").read_text())["key"]
    seal = hmac.digest(bytes.fromhex(key), canonical(cap).encode(), "sha256").hex()
    _write_private(directory / "starter-case-grant.json", {"grant": cap, "seal_sha256": seal})


@pytest.fixture
def joined(founder):
    h = founder
    directory = h.path.parent

    def settings():
        return Settings.load(h.path)

    selected = json.loads((directory / "selected.json").read_text())
    previous = json.loads((directory / "starter-intake-grant-policy-scope.json").read_text())
    policy = directory / "policy.md"
    policy.write_bytes(PURCHASE_POLICY)
    policy.chmod(0o600)
    _write_private(
        directory / "starter-intake-grant-policy-scope.json",
        _sealed_starter(
            selected,
            source_revision=hashlib.sha256(PURCHASE_POLICY).hexdigest(),
            setup_policy_revision=previous["grant"]["setup_policy_revision"],
        ),
    )
    setup = StarterSetup(settings, ACTOR)
    saved = setup.save(setup.preview().revision, "admit-cases")
    workflow, run = saved.draft["workflow_ref"], saved.draft["run_ref"]
    source = GuidedPolicySource(setup, policy)
    maps, _ = stage_map_review(
        source,
        workflow,
        editors={ACTOR: "workflow-owner", OTHER: "map-editor"},
        readers=["intake-worker"],
    )
    actor = IntakePrincipal(h.store.organization, ACTOR, "untrusted")
    snap = maps.preview(actor)
    confirmed = maps.save(
        actor,
        expected_revision=snap.review_revision,
        operation_ref="map-confirm",
        changes=tuple(
            BranchEdit(r.branch_ref, "accepted", "Named owner checked") for r in snap.branches
        ),
        settings=MapSettingsEdit("Invoice arrives", "REVIEW", "Owner confirms scope"),
    )
    grant = setup._read_grant()
    cap = dict(
        schema_version=GRANT,
        grant_ref="case-permission",
        tenant=actor.tenant,
        engagement_epoch=setup.selected.engagement_epoch,
        workflow_ref=workflow,
        run_ref=run,
        starter_grant_revision=grant.revision,
        source_revision=grant.source_revision,
        settings_revision=grant.setup_policy_revision,
        source_digest=confirmed.source_digest,
        map_confirmation_ref=confirmed.confirmed_ref,
        map_settings_ref=confirmed.settings_ref,
        owner_ref="workflow-owner",
        owner_actor=ACTOR,
        custodian_ref=grant.custodian_id,
        expires_at=int(time.time()) + 600,
        owner_purposes=["propose", "review", "adjudicate", "freeze"],
        worker_purposes={"intake-worker": ["materialize"]},
        frozen_plan=asdict(
            FrozenPlan(
                "baseline-unconnected", "simple-policy-v1", "exact-action-v1", "review-v1", "v1"
            )
        ),
        critical_branches=[r.branch_ref for r in confirmed.branches],
    )
    _write_private(
        directory / "starter-case-selection.json",
        {"schema_version": SELECTION, "grant_file": "starter-case-grant.json"},
    )
    sign_case(directory, cap)
    return h, StarterCaseSession(settings, actor), cap, maps, setup


class FiveFamilies:
    def __init__(self):
        self.calls = 0

    def propose(self, snap):
        self.calls += 1
        cases = [(None, 50), (None, 250), ("po", 249.99), ("po", 250), ("po", 250.01)]
        result = []
        for i, (po, total) in enumerate(cases):
            expected, citations = snap.expected_action({"po": po, "total": Decimal(str(total))})
            branch = next(r for r in snap.branches if r.citation_ref == citations[0])
            result.append(
                ProposedCase(
                    f"case-{i}",
                    f"family-{i}",
                    f"template-{i}",
                    branch.branch_ref,
                    (("po", po), ("total", total)),
                    "MANUAL" if i == 2 else expected,
                    "Generated boundary suggestion",
                    citations[0],
                )
            )
        return tuple(result)


def adjudicate(session):
    labels = ("REVIEW", "REVIEW", "APPROVE", "MANUAL", "MANUAL")
    for i, label in enumerate(labels):
        session.adjudicate(
            case_id=f"case-{i}",
            target=label,
            reason="Owner reviewed source",
            expected_revision=i,
            operation_ref=f"owner-label-{i}",
        )


def test_actual_owner_map_to_frozen_unscored_and_worker_readback(joined):
    h, session, cap, _, setup = joined
    provider = FiveFamilies()
    proposed = session.propose(provider)
    before = session.review()
    assert len(before["cases"]) == 5
    assert all(row["owner_adjudicated_target"] is None for row in before["cases"])
    assert before["cases"][2]["suggestion"] == "MANUAL"
    assert set(before["authority"]["allowed_actions"]) == {"REVIEW", "APPROVE", "MANUAL"}
    assert before["authority"]["map_settings_ref"] == cap["map_settings_ref"]
    assert before["authority"]["can_adjudicate"] and before["authority"]["can_freeze"]
    adjudicate(session)
    reviewed = session.review()
    assert reviewed["cases"][2]["owner_adjudicated_target"] == "APPROVE"
    assert reviewed["cases"][2]["suggestion"] == "MANUAL"
    assert all(row["adjudicator"] == "workflow-owner" for row in reviewed["cases"])
    splits = {f"family-{i}": "development" if i in (1, 4) else "reserved" for i in range(5)}
    frozen = session.freeze(splits=splits, expected_revision=5)
    assert frozen.state == "frozen_unscored"
    assert (frozen.proposed, frozen.resolved, frozen.development, frozen.reserved) == (5, 5, 2, 3)
    assert frozen.uncovered_critical == ()
    reopened = StarterCaseSession(session.settings, session.actor)
    assert reopened.freeze(splits=splits, expected_revision=5) == frozen
    records = list(h.path.parent.glob("starter-case-authority-*.json"))
    assert len(records) == 1
    receipt = json.loads(records[0].read_bytes())
    assert receipt["owner_binding"]["authenticated_owner"] == ACTOR
    assert receipt["owner_binding"]["registered_owner"] == "workflow-owner"
    assert receipt["envelope"]["grant"] == cap
    worker = setup.principal
    producer = StarterCaseSession(session.settings, worker)
    intake = DurableIntake(h.path.parent, setup)
    benchmark = BenchmarkIntake(intake)
    assert benchmark.materialize(worker, cap["run_ref"], producer, target_state="frozen_unscored")
    materialized = benchmark.get_run(worker, cap["run_ref"]).benchmark
    assert materialized.benchmark_state == "frozen_unscored"
    assert session.propose(provider) == proposed
    assert provider.calls == 1
    with h.store._db() as db:
        assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0


@pytest.mark.parametrize("actor", [OTHER, "intake-worker", "UNGRANTED"])
def test_map_editor_or_worker_cannot_impersonate_case_owner(joined, actor):
    h, owner, _, _, _ = joined
    caller = IntakePrincipal(owner.actor.tenant, actor, "owner")
    unauthorized = StarterCaseSession(owner.settings, caller)
    provider = FiveFamilies()
    with pytest.raises(IntakeError, match="starter_case_owner_required"):
        unauthorized.propose(provider)
    assert provider.calls == 0 and not unauthorized.core.path.exists()
    assert not list(h.path.parent.glob("starter-case-authority-*.json"))


@pytest.mark.parametrize(
    "change", ["owner-rebound", "case-expired", "case-withdrawn", "setup-withdrawn", "map-changed"]
)
def test_current_identity_and_scope_required_before_proposal(joined, change):
    h, session, cap, maps, _ = joined
    if change == "owner-rebound":
        h.config["starter_setup"]["slack_owner"] = OTHER
        h.save()
    elif change == "case-expired":
        sign_case(h.path.parent, cap | {"expires_at": 1})
    elif change == "case-withdrawn":
        _write_private(h.path.parent / "starter-case-withdrawn-case-permission.json", {})
    elif change == "setup-withdrawn":
        _write_private(h.path.parent / "starter-intake-withdrawn-policy-scope.json", {})
    else:
        snap = maps.preview(session.actor)
        maps.save(
            session.actor,
            expected_revision=snap.review_revision,
            operation_ref="map-successor",
            changes=(),
            settings=MapSettingsEdit("Changed trigger", "MANUAL", "New context"),
        )
    provider = FiveFamilies()
    with pytest.raises(ValueError):
        session.propose(provider)
    assert provider.calls == 0 and not session.core.path.exists()


@pytest.mark.parametrize("change", ["owner-rebound", "case-withdrawn", "setup-withdrawn"])
def test_permission_loss_during_save_rolls_back_target(joined, monkeypatch, change):
    h, session, _, _, _ = joined
    session.propose(FiveFamilies())
    with closing(sqlite3.connect(session.core.path)) as db:
        before = db.execute("SELECT body,seal FROM packs").fetchone()
    original = session.core._save

    def revoke(db, scope, body):
        original(db, scope, body)
        if change == "owner-rebound":
            h.config["starter_setup"]["slack_owner"] = OTHER
            h.save()
        elif change == "case-withdrawn":
            _write_private(h.path.parent / "starter-case-withdrawn-case-permission.json", {})
        else:
            _write_private(h.path.parent / "starter-intake-withdrawn-policy-scope.json", {})

    monkeypatch.setattr(session.core, "_save", revoke)
    with pytest.raises(ValueError):
        session.adjudicate(
            case_id="case-0",
            target="REVIEW",
            reason="Revoked during save",
            expected_revision=0,
            operation_ref="withdrawn-label",
        )
    with closing(sqlite3.connect(session.core.path)) as db:
        assert db.execute("SELECT body,seal FROM packs").fetchone() == before


def test_missing_freeze_and_evaluate_purposes_hold(joined):
    h, session, cap, _, _ = joined
    sign_case(h.path.parent, cap | {"owner_purposes": ["propose", "review", "adjudicate"]})
    session.propose(FiveFamilies())
    adjudicate(session)
    with pytest.raises(IntakeError, match="starter_case_owner_required"):
        session.freeze(splits={f"family-{i}": "reserved" for i in range(5)}, expected_revision=5)
    assert session.review()["frozen"] is None
    assert not session.review()["authority"]["can_freeze"]
    assert not session.entry().can_freeze
    with pytest.raises(IntakeError, match="starter_case_worker_required"):
        with session._pin("evaluate"):
            pytest.fail("map/case permission granted evaluator authority")


def test_new_map_confirmation_cannot_replace_same_admitted_benchmark(joined):
    h, session, cap, maps, _ = joined
    session.propose(FiveFamilies())
    current = maps.preview(session.actor)
    changed = maps.save(
        session.actor,
        expected_revision=current.review_revision,
        operation_ref="new-map-settings",
        changes=(),
        settings=MapSettingsEdit("New trigger", "MANUAL", "Changed workflow"),
    )
    sign_case(
        h.path.parent,
        cap
        | {"map_confirmation_ref": changed.confirmed_ref, "map_settings_ref": changed.settings_ref},
    )
    provider = FiveFamilies()
    with pytest.raises(IntakeError, match="starter_benchmark_successor_required"):
        session.propose(provider)
    assert provider.calls == 0
    with closing(sqlite3.connect(session.core.path)) as db:
        assert db.execute("SELECT count(*) FROM packs").fetchone()[0] == 1


def test_entry_reads_no_policy_or_reserved_targets_and_changes_no_files(joined, monkeypatch):
    h, session, cap, _, _ = joined
    session.propose(FiveFamilies())
    before = {p.name: p.read_bytes() for p in h.path.parent.iterdir() if p.is_file()}
    monkeypatch.setattr(map_review, "_read_policy", lambda *_: pytest.fail("Home read policy"))
    monkeypatch.setattr(session.core, "_load", lambda *_: pytest.fail("Home read case targets"))
    entry = session.entry()
    assert entry.workflow_ref == cap["workflow_ref"] and entry.run_ref == cap["run_ref"]
    assert set(asdict(entry)) == {
        "workflow_ref",
        "run_ref",
        "revision",
        "can_propose",
        "can_adjudicate",
        "can_freeze",
    }
    assert {p.name: p.read_bytes() for p in h.path.parent.iterdir() if p.is_file()} == before
    h.config["starter_setup"]["slack_owner"] = OTHER
    h.save()
    assert session.entry() is None


def test_concurrent_completed_proposal_has_one_provider_call(joined):
    _, session, _, _, _ = joined
    provider = FiveFamilies()
    with ThreadPoolExecutor(max_workers=2) as executor:
        refs = list(executor.map(lambda _: session.propose(provider), range(2)))
    assert refs[0] == refs[1] and provider.calls == 1


@pytest.mark.parametrize(
    "field", ["baseline_ref", "comparator_ref", "scorer_ref", "abstention_ref", "version"]
)
def test_missing_frozen_plan_term_denies_before_authoring(joined, field):
    h, session, cap, _, _ = joined
    sign_case(h.path.parent, cap | {"frozen_plan": cap["frozen_plan"] | {field: None}})
    provider = FiveFamilies()
    with pytest.raises(ValueError):
        session.propose(provider)
    assert provider.calls == 0 and not session.core.path.exists()


@pytest.mark.parametrize("omit_review", [False, True])
def test_material_coverage_is_separate_from_permitted_owner_corrections(joined, omit_review):
    _, session, _, _, _ = joined
    session.propose(FiveFamilies())
    adjudicate(session)
    split = {
        f"family-{i}": "excluded" if omit_review and i in (0, 1) else "reserved" for i in range(5)
    }
    frozen = session.freeze(splits=split, expected_revision=5)
    review_branch = session.review()["cases"][0]["branch_id"]
    assert frozen.uncovered_critical == ((review_branch,) if omit_review else ())
    assert len(frozen.coverage) == 3
    assert {action: count for _, action, count in frozen.coverage} == {
        "REVIEW": 0 if omit_review else 2,
        "APPROVE": 1,
        "MANUAL": 2,
    }
