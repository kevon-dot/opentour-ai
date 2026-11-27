"""Generated, independently sealed editor grants against real local starter authority."""

import hashlib
import hmac
import json
import sqlite3
import time
from contextlib import closing, contextmanager
from dataclasses import replace
from decimal import Decimal

import pytest
from opentour_cell.pilot import map_review
from opentour_cell.pilot.guided_decision_map import Condition
from opentour_cell.pilot.intake_admission import DurableIntake
from opentour_cell.pilot.intake_local import LocalStarterAuthority
from opentour_cell.pilot.intake_mcp import IntakeError, IntakePrincipal
from opentour_cell.pilot.map_review import (
    BranchEdit,
    MapSettingsEdit,
    StaleMapError,
    open_map_review,
)
from opentour_cell.pilot.operating_point import canonical
from test_guided_policy import ACTIONS, FIELDS, PURCHASE_POLICY
from test_intake_mcp import _sealed_starter, _write_private

pytest_plugins = ("test_guided_policy",)

OWNER_A = IntakePrincipal("org-a", "UOWNER_A", "ignored-wire-role")
OWNER_B = IntakePrincipal("org-a", "UOWNER_B", "ignored-wire-role")
READER = IntakePrincipal("org-a", "UREADER", "owner")


def sign_review(directory, cap):
    key = json.loads((directory / "starter-intake-key.json").read_text())["key"]
    seal = hmac.digest(bytes.fromhex(key), canonical(cap).encode(), "sha256").hex()
    _write_private(directory / "map-review-grant.json", {"grant": cap, "seal_sha256": seal})


def stage_map_review(source, workflow, **overrides):
    directory = source.path.parent
    starter = source.authority._read_grant()
    selected = source.authority.selected
    cap = dict(
        schema_version=map_review.GRANT,
        grant_ref="generated-map-review",
        tenant=selected.tenant,
        engagement_epoch=selected.engagement_epoch,
        workflow_ref=workflow,
        starter_grant_revision=starter.revision,
        policy_resource_ref=starter.policy_resource_ref,
        source_revision=starter.source_revision,
        owner_ref=selected.owner_id,
        custodian_ref=starter.custodian_id,
        expires_at=int(time.time()) + 600,
        editors={OWNER_A.actor: "person-a", OWNER_B.actor: "person-b"},
        readers=[READER.actor],
        field_kinds={key: value.value for key, value in FIELDS.items()},
        actions=sorted(ACTIONS),
    )
    cap.update(overrides)
    _write_private(
        directory / "map-review-selection.json",
        dict(
            schema_version=map_review.SELECTION,
            workflow_ref=workflow,
            starter_selection_file=source.authority.path.name,
            policy_file=source.path.name,
            grant_file="map-review-grant.json",
        ),
    )
    sign_review(directory, cap)
    return open_map_review(directory, selected.tenant), cap


def accept_all(service, actor=OWNER_A, *, operation="confirm-all"):
    snapshot = service.preview(actor)
    return service.save(
        actor,
        expected_revision=snapshot.review_revision,
        operation_ref=operation,
        changes=tuple(
            BranchEdit(row.branch_ref, "accepted", "Checked cited policy")
            for row in snapshot.branches
        ),
    )


def file_snapshot(directory):
    return {p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()}


def test_entry_is_metadata_only_unknown_trigger_and_read_only(starter, monkeypatch):
    source, workflow, _, _ = starter(PURCHASE_POLICY)
    service, _ = stage_map_review(source, workflow)
    before = file_snapshot(service.directory)
    with monkeypatch.context() as patch:
        patch.setattr(map_review, "_read_policy", lambda *_: pytest.fail("metadata read source"))
        assert service.entry(OWNER_A)
        assert service.entry(READER)
        assert service.entry(replace(OWNER_A, actor="UNGRANTED")) is None
    assert file_snapshot(service.directory) == before
    assert not service.path.exists()
    snap = service.preview(READER)
    assert snap.trigger == "Trigger has not been specified"
    assert not snap.can_edit and snap.confirmed_ref is None
    with pytest.raises(IntakeError, match="map_review_read_only"):
        service.save(
            READER,
            expected_revision=snap.review_revision,
            operation_ref="deny",
            changes=(BranchEdit(snap.branches[0].branch_ref, "accepted", "No grant"),),
        )
    assert not service.path.exists()


def test_five_families_citations_and_wrong_threshold_control(starter):
    source, workflow, _, _ = starter(PURCHASE_POLICY)
    service, _ = stage_map_review(source, workflow)
    saved = accept_all(service)
    assert saved.confirmed_ref and saved.version == 1 and len(saved.branches) == 3
    assert saved.owner_ref == "founder"
    assert {r.actor_ref for r in saved.branches} == {OWNER_A.actor}
    assert {r.editor_ref for r in saved.branches} == {"person-a"}
    cases = [(None, "50"), (None, "250"), ("po", "249.99"), ("po", "250"), ("po", "250.01")]
    actual = [saved.expected_action({"po": po, "total": Decimal(total)}) for po, total in cases]
    assert [action for action, _ in actual] == ["REVIEW", "REVIEW", "APPROVE", "MANUAL", "MANUAL"]
    assert len({refs[0] for _, refs in actual}) == 3
    for action, refs in actual:
        assert action in service.citation(
            OWNER_A, expected_revision=saved.review_revision, citation_ref=refs[0]
        )
    restored = open_map_review(service.directory, "org-a").preview(OWNER_A)
    assert restored == saved
    approve = next(r for r in saved.branches if r.action == "APPROVE")
    wrong = service.save(
        OWNER_A,
        expected_revision=saved.review_revision,
        operation_ref="wrong-threshold",
        changes=(
            BranchEdit(
                approve.branch_ref,
                "corrected",
                "Generated wrong boundary",
                conditions=(Condition("total", "<=", "250"),),
            ),
        ),
    )
    assert wrong.expected_action({"po": "po", "total": Decimal("250")})[0] == "APPROVE"
    assert (
        next(r for r in wrong.branches if r.branch_ref == approve.branch_ref).source_support
        == "owner_assertion"
    )


def test_two_actors_stale_diff_reapply_retry_and_rebind(starter):
    source, workflow, _, _ = starter(PURCHASE_POLICY)
    service, cap = stage_map_review(source, workflow)
    old_b = service.preview(OWNER_B)
    saved_a = accept_all(service)
    edit = BranchEdit(saved_a.branches[1].branch_ref, "unresolved", "Owner B asks for clarity")
    with pytest.raises(StaleMapError) as stale:
        service.save(
            OWNER_B,
            expected_revision=old_b.review_revision,
            operation_ref="b-save",
            changes=(edit,),
        )
    assert stale.value.current.version == 1
    assert any(
        d.before.disposition == "needs_review" and d.after.disposition == "accepted"
        for d in stale.value.diff
    )
    revision = stale.value.current.review_revision
    saved_b = service.save(
        OWNER_B, expected_revision=revision, operation_ref="b-save", changes=(edit,)
    )
    assert saved_b.version == 2
    assert (
        service.save(OWNER_B, expected_revision=revision, operation_ref="b-save", changes=(edit,))
        == saved_b
    )
    with pytest.raises(IntakeError, match="map_review_operation_conflict"):
        service.save(
            OWNER_B,
            expected_revision=revision,
            operation_ref="b-save",
            changes=(replace(edit, reason="Changed intent"),),
        )
    sign_review(
        service.directory,
        cap | {"editors": {OWNER_A.actor: "person-a", OWNER_B.actor: "new-person"}},
    )
    with pytest.raises(StaleMapError):
        service.save(OWNER_B, expected_revision=revision, operation_ref="b-save", changes=(edit,))
    assert service.preview(OWNER_B).version == 2


@pytest.mark.parametrize(
    "change",
    [
        {"expires_at": 1},
        {"tenant": "foreign"},
        {"engagement_epoch": "f" * 32},
        {"starter_grant_revision": "0" * 64},
        {"policy_resource_ref": "foreign-policy"},
        {"owner_ref": "forged-owner"},
        {"custodian_ref": "forged-custodian"},
        {"editors": {"OTHER": "person-a"}, "readers": []},
    ],
)
def test_current_scope_required_before_source_or_writes(starter, monkeypatch, change):
    source, workflow, _, _ = starter(PURCHASE_POLICY)
    service, cap = stage_map_review(source, workflow)
    sign_review(service.directory, cap | change)
    before = file_snapshot(service.directory)
    monkeypatch.setattr(map_review, "_read_policy", lambda *_: pytest.fail("unauthorized source"))
    assert service.entry(OWNER_A) is None
    with pytest.raises(IntakeError):
        service.preview(OWNER_A)
    assert file_snapshot(service.directory) == before


def test_withdrawal_restoration_and_foreign_actor_never_read_source(starter, monkeypatch):
    source, workflow, _, _ = starter(PURCHASE_POLICY)
    service, cap = stage_map_review(source, workflow)
    _write_private(service.directory / f"map-review-withdrawn-{cap['grant_ref']}.json", {})
    sign_review(service.directory, cap)
    monkeypatch.setattr(map_review, "_read_policy", lambda *_: pytest.fail("withdrawn source"))
    assert service.entry(OWNER_A) is None
    assert service.entry(replace(OWNER_A, tenant="foreign")) is None
    with pytest.raises(IntakeError):
        service.preview(OWNER_A)


def test_equal_authority_conflict_needs_explicit_order(starter):
    source, workflow, _, _ = starter(
        b"# Policy\nIf total < 250, APPROVE.\nIf total < 250, REVIEW.\n"
    )
    service, _ = stage_map_review(source, workflow)
    saved = accept_all(service)
    assert saved.question == "Which matching branch takes precedence?"
    assert saved.confirmed_ref is None and saved.can_set_precedence
    assert saved.expected_action({"total": Decimal("10")})[0] is None
    chosen = tuple(r.branch_ref for r in reversed(saved.branches))
    ordered = service.save(
        OWNER_A,
        expected_revision=saved.review_revision,
        operation_ref="order",
        changes=(),
        precedence=chosen,
        precedence_reason="Owner chooses REVIEW",
    )
    assert ordered.question is None and ordered.confirmed_ref
    assert ordered.expected_action({"total": Decimal("10")})[0] == "REVIEW"


def test_unsupported_clause_requires_typed_owner_assertion_or_exclusion(starter):
    source, workflow, _, _ = starter(b"# Policy\nUnless prior authorization, follow escalation.\n")
    service, _ = stage_map_review(source, workflow)
    snap = service.preview(OWNER_A)
    assert len(snap.branches) == 1 and snap.branches[0].action is None
    assert snap.question and not snap.can_set_precedence
    edit = BranchEdit(snap.branches[0].branch_ref, "accepted", "Cannot accept undefined rule")
    with pytest.raises(IntakeError, match="unsupported_map_rule"):
        service.save(
            OWNER_A, expected_revision=snap.review_revision, operation_ref="bad", changes=(edit,)
        )
    with pytest.raises(IntakeError, match="unsupported_map_field"):
        service.save(
            OWNER_A,
            expected_revision=snap.review_revision,
            operation_ref="future",
            changes=(
                replace(
                    edit,
                    disposition="corrected",
                    action="REVIEW",
                    conditions=(Condition("later_accountant_label", "present"),),
                ),
            ),
        )
    fixed = service.save(
        OWNER_A,
        expected_revision=snap.review_revision,
        operation_ref="assert",
        changes=(
            replace(
                edit,
                disposition="corrected",
                reason="Owner assertion",
                action="REVIEW",
                conditions=(Condition("po", "missing"),),
            ),
        ),
    )
    assert fixed.confirmed_ref and fixed.branches[0].source_support == "owner_assertion"
    assert fixed.expected_action({"po": None})[0] == "REVIEW"


def test_disclosure_checks_current_permission_after_database_open(starter, monkeypatch):
    source, workflow, _, _ = starter(PURCHASE_POLICY)
    service, cap = stage_map_review(source, workflow)
    snap = accept_all(service)
    original = service._db

    @contextmanager
    def withdraw_on_open(**kwargs):
        with original(**kwargs) as db:
            _write_private(service.directory / f"map-review-withdrawn-{cap['grant_ref']}.json", {})
            yield db

    monkeypatch.setattr(service, "_db", withdraw_on_open)
    disclosed = []
    with pytest.raises(IntakeError):
        with service.disclosure(OWNER_A, expected_revision=snap.review_revision) as body:
            disclosed.append(body)
    assert disclosed == []


def test_source_withdrawal_before_commit_rolls_back_version(starter, monkeypatch):
    source, workflow, _, _ = starter(PURCHASE_POLICY)
    service, cap = stage_map_review(source, workflow)
    snap = accept_all(service)
    before = service.path.read_bytes()
    original = service._current

    def withdraw(actor, ctx):
        _write_private(service.directory / f"map-review-withdrawn-{cap['grant_ref']}.json", {})
        original(actor, ctx)

    monkeypatch.setattr(service, "_current", withdraw)
    with pytest.raises(IntakeError):
        service.save(
            OWNER_A,
            expected_revision=snap.review_revision,
            operation_ref="will-rollback",
            changes=(BranchEdit(snap.branches[0].branch_ref, "unresolved", "Hold"),),
        )
    assert service.path.read_bytes() == before


@pytest.mark.parametrize("database", ["training.sqlite3", "intake.sqlite3", "map-reviews.sqlite3"])
def test_closed_wal_read_does_not_create_sidecars(starter, database):
    source, workflow, _, _ = starter(PURCHASE_POLICY)
    service, _ = stage_map_review(source, workflow)
    accept_all(service)
    path = service.directory / database
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    assert not (service.directory / (database + "-wal")).exists()
    before = file_snapshot(service.directory)
    if database == "map-reviews.sqlite3":
        with pytest.raises((ValueError, OSError)):
            service.preview(OWNER_A)
    else:
        assert service.entry(OWNER_A) is None
    assert file_snapshot(service.directory) == before


def test_missing_training_database_is_not_initialized(starter):
    source, workflow, _, _ = starter(PURCHASE_POLICY)
    service, _ = stage_map_review(source, workflow)
    (service.directory / "training.sqlite3").unlink()
    before = file_snapshot(service.directory)
    with pytest.raises((ValueError, OSError)):
        open_map_review(service.directory, "org-a")
    assert file_snapshot(service.directory) == before


@pytest.mark.parametrize(
    "mutation",
    ["DELETE FROM heads", "UPDATE maps SET body='{}'", "UPDATE operations SET ref='unknown'"],
)
def test_partial_restored_ledger_does_not_project_unverified_map(starter, mutation):
    source, workflow, _, _ = starter(PURCHASE_POLICY)
    service, _ = stage_map_review(source, workflow)
    accept_all(service)
    with closing(sqlite3.connect(service.path)) as db, db:
        db.execute(mutation)
    with pytest.raises(IntakeError, match="map_review_receipt_invalid"):
        service.preview(OWNER_A)


def test_source_successor_preserves_history_marks_only_changed_branches(starter):
    source, workflow, request, _ = starter(PURCHASE_POLICY)
    service, cap = stage_map_review(source, workflow)
    accepted = accept_all(service)
    old = service.save(
        OWNER_A,
        expected_revision=accepted.review_revision,
        operation_ref="old-settings",
        changes=(),
        settings=MapSettingsEdit("Old policy trigger", "REVIEW", "Old source scope"),
    )
    old_body = service.path.read_bytes()
    successor = PURCHASE_POLICY.replace(b"250", b"300")
    source.path.write_bytes(successor)
    with pytest.raises(ValueError):
        service.preview(OWNER_A)
    selection = json.loads(source.authority.path.read_text())
    grant = _sealed_starter(selection, source_revision=hashlib.sha256(successor).hexdigest())
    _write_private(service.directory / "starter-intake-grant-registered-starter.json", grant)
    authority = LocalStarterAuthority(source.authority.path)
    intake = DurableIntake(service.directory, authority)
    admitted = intake.submit_intake(
        authority.principal,
        replace(
            request,
            source_revision=grant["grant"]["source_revision"],
            idempotency_key="successor",
        ),
    )
    assert intake.get_run(authority.principal, admitted.run_ref).workflow_ref == workflow
    current_grant = authority._read_grant()
    sign_review(
        service.directory,
        cap
        | {
            "source_revision": current_grant.source_revision,
            "starter_grant_revision": current_grant.revision,
        },
    )
    current = open_map_review(service.directory, "org-a")
    preview = current.preview(OWNER_A)
    assert service.path.read_bytes() == old_body
    assert preview.confirmed_ref is None
    assert preview.settings_trigger is None and preview.fallback_action is None
    po_before = next(r for r in old.branches if r.action == "REVIEW")
    po_after = next(r for r in preview.branches if r.action == "REVIEW")
    assert po_after.disposition == "accepted" and po_after.branch_ref == po_before.branch_ref
    assert po_after.branch_ref not in preview.changed_branch_refs
    assert all(r.disposition == "needs_review" for r in preview.branches if r.action != "REVIEW")
    with pytest.raises(StaleMapError) as stale:
        current.save(
            OWNER_A,
            expected_revision=old.review_revision,
            operation_ref="old-reapply",
            changes=(BranchEdit(old.branches[1].branch_ref, "accepted", "Old action"),),
        )
    assert all(item.before is None for item in stale.value.diff)
    assert stale.value.previous is None


def test_duplicate_semantic_branches_hold_before_ambiguous_edit(starter):
    source, workflow, _, _ = starter(
        b"# Policy\nIf total < 250, APPROVE.\nIf total < 250, APPROVE.\n"
    )
    service, _ = stage_map_review(source, workflow)
    with pytest.raises(IntakeError, match="ambiguous_map_branches"):
        service.preview(OWNER_A)
    assert not service.path.exists()


def test_withdrawal_after_admission_lookup_prevents_raw_read(starter, monkeypatch):
    source, workflow, _, _ = starter(PURCHASE_POLICY)
    service, cap = stage_map_review(source, workflow)
    require_workflow = service.source._require_workflow

    def withdraw_after_lookup(*args):
        require_workflow(*args)
        _write_private(service.directory / f"map-review-withdrawn-{cap['grant_ref']}.json", {})

    monkeypatch.setattr(service.source, "_require_workflow", withdraw_after_lookup)
    monkeypatch.setattr(map_review, "_read_policy", lambda *_: pytest.fail("withdrawn raw read"))
    with pytest.raises(IntakeError):
        service.preview(OWNER_A)
    assert not service.path.exists()


def test_denied_edit_does_not_open_closed_wal_training_state(starter):
    source, workflow, _, _ = starter(PURCHASE_POLICY)
    service, _ = stage_map_review(source, workflow)
    snap = service.preview(READER)
    with closing(sqlite3.connect(service.directory / "training.sqlite3")) as db:
        db.execute("PRAGMA journal_mode=WAL")
    before = file_snapshot(service.directory)
    with pytest.raises(IntakeError, match="map_review_read_only"):
        service.save(
            READER,
            expected_revision=snap.review_revision,
            operation_ref="denied-wal",
            changes=(BranchEdit(snap.branches[0].branch_ref, "accepted", "No right"),),
        )
    assert file_snapshot(service.directory) == before


@pytest.mark.parametrize("change", ["reorder", "remove-priority"])
def test_successor_priority_change_invalidates_affected_confirmation(starter, change):
    source, workflow, request, _ = starter(PURCHASE_POLICY)
    service, cap = stage_map_review(source, workflow)
    old = accept_all(service)
    if change == "reorder":
        successor = PURCHASE_POLICY.replace(
            b"If po is missing, REVIEW.\nIf total < 250, APPROVE.",
            b"If total < 250, APPROVE.\nIf po is missing, REVIEW.",
        )
    else:
        successor = PURCHASE_POLICY.replace(b"Apply the first matching rule.\n", b"")
    source.path.write_bytes(successor)
    selected = json.loads(source.authority.path.read_text())
    grant = _sealed_starter(selected, source_revision=hashlib.sha256(successor).hexdigest())
    _write_private(service.directory / "starter-intake-grant-registered-starter.json", grant)
    authority = LocalStarterAuthority(source.authority.path)
    intake = DurableIntake(service.directory, authority)
    intake.submit_intake(
        authority.principal,
        replace(
            request,
            source_revision=grant["grant"]["source_revision"],
            idempotency_key="priority-successor",
        ),
    )
    current_grant = authority._read_grant()
    sign_review(
        service.directory,
        cap
        | {
            "source_revision": current_grant.source_revision,
            "starter_grant_revision": current_grant.revision,
        },
    )
    current = open_map_review(service.directory, "org-a")
    changed = current.preview(OWNER_A)
    assert changed.confirmed_ref is None
    assert changed.expected_action({"po": None, "total": Decimal("50")})[0] is None
    if change == "reorder":
        assert set(changed.changed_branch_refs) == {
            row.branch_ref for row in old.branches if row.action in {"REVIEW", "APPROVE"}
        }
        manual = next(row for row in changed.branches if row.action == "MANUAL")
        assert manual.disposition == "accepted"
        assert changed.precedence_citation is not None
    else:
        assert changed.precedence_citation is None
        assert changed.question == "Which matching branch takes precedence?"


def test_source_priority_citation_and_owner_order_are_attributed_separately(starter):
    source, workflow, _, _ = starter(PURCHASE_POLICY)
    service, _ = stage_map_review(source, workflow)
    original = accept_all(service)
    assert original.precedence_citation is not None
    assert (
        service.citation(
            OWNER_A,
            expected_revision=original.review_revision,
            citation_ref=map_review.citation_ref(original.precedence_citation),
        )
        == "Apply the first matching rule.\n"
    )
    ordered = service.save(
        OWNER_B,
        expected_revision=service.preview(OWNER_B).review_revision,
        operation_ref="explicit-order",
        changes=(),
        precedence=tuple(row.branch_ref for row in reversed(original.branches)),
        precedence_reason="Explicit generated owner priority override",
    )
    assert ordered.precedence_citation is None
    assert ordered.precedence_reason == "Explicit generated owner priority override"
    assert ordered.precedence_actor_ref == OWNER_B.actor


def test_stale_error_does_not_disclose_after_permission_withdrawal(starter, monkeypatch):
    source, workflow, _, _ = starter(PURCHASE_POLICY)
    service, cap = stage_map_review(source, workflow)
    old = service.preview(OWNER_B)
    accept_all(service)
    original = service._db

    @contextmanager
    def withdraw_on_open(**kwargs):
        with original(**kwargs) as db:
            _write_private(service.directory / f"map-review-withdrawn-{cap['grant_ref']}.json", {})
            yield db

    monkeypatch.setattr(service, "_db", withdraw_on_open)
    with pytest.raises(IntakeError) as denied:
        service.save(
            OWNER_B,
            expected_revision=old.review_revision,
            operation_ref="stale-denied",
            changes=(BranchEdit(old.branches[0].branch_ref, "accepted", "Old edit"),),
        )
    assert not isinstance(denied.value, StaleMapError)


def test_trigger_fallback_are_explicit_attributed_and_idempotent(starter):
    source, workflow, _, _ = starter(b"# Policy\nIf total < 250, APPROVE.\n")
    service, _ = stage_map_review(source, workflow)
    before = accept_all(service)
    fields = {"total": Decimal("300")}
    assert before.expected_action(fields)[0] is None
    assertion = MapSettingsEdit("When an invoice arrives", "REVIEW", "Owner confirms escalation")
    saved = service.save(
        OWNER_A,
        expected_revision=before.review_revision,
        operation_ref="settings",
        changes=(),
        settings=assertion,
    )
    assert saved.trigger == saved.settings_trigger == assertion.trigger
    assert saved.fallback_action == "REVIEW" and saved.settings_support == "owner_assertion"
    assert saved.settings_actor_ref == OWNER_A.actor and saved.settings_editor_ref == "person-a"
    assert saved.settings_reason == assertion.reason
    assert saved.expected_action(fields) == ("REVIEW", (saved.settings_ref,))
    assert all(row.fallback == "Owner-asserted fallback: REVIEW" for row in saved.branches)
    assert (
        service.save(
            OWNER_A,
            expected_revision=before.review_revision,
            operation_ref="settings",
            changes=(),
            settings=assertion,
        )
        == saved
    )
    assert open_map_review(service.directory, OWNER_A.tenant).preview(OWNER_A) == saved
    cleared = service.save(
        OWNER_A,
        expected_revision=saved.review_revision,
        operation_ref="clear",
        changes=(),
        settings=MapSettingsEdit(None, None, "Unresolved by owner"),
    )
    assert cleared.settings_trigger is None and cleared.trigger == "Trigger has not been specified"
    assert cleared.fallback_action is None and cleared.expected_action(fields)[0] is None


def test_settings_stale_diff_and_explicit_reapply(starter):
    source, workflow, _, _ = starter(PURCHASE_POLICY)
    service, _ = stage_map_review(source, workflow)
    original = service.preview(OWNER_B)
    a = service.save(
        OWNER_A,
        expected_revision=service.preview(OWNER_A).review_revision,
        operation_ref="a-settings",
        changes=(),
        settings=MapSettingsEdit("A's trigger", "REVIEW", "A checked scope"),
    )
    submitted = MapSettingsEdit("B's trigger", "MANUAL", "B checked scope")
    with pytest.raises(StaleMapError) as stale:
        service.save(
            OWNER_B,
            expected_revision=original.review_revision,
            operation_ref="b-settings",
            changes=(),
            settings=submitted,
        )
    assert stale.value.previous.settings_trigger is None
    assert stale.value.current.settings_trigger == "A's trigger"
    assert stale.value.current.settings_reason == a.settings_reason
    b = service.save(
        OWNER_B,
        expected_revision=stale.value.current.review_revision,
        operation_ref="b-settings",
        changes=(),
        settings=submitted,
    )
    assert b.settings_trigger == "B's trigger" and b.version == 2
    assert b.settings_actor_ref == OWNER_B.actor


@pytest.mark.parametrize(
    "assertion",
    [
        MapSettingsEdit("", "REVIEW", "Reason"),
        MapSettingsEdit("x" * 161, "REVIEW", "Reason"),
        MapSettingsEdit("trigger\nsecond", "REVIEW", "Reason"),
        MapSettingsEdit("Trigger", "UNGRANTED", "Reason"),
        MapSettingsEdit("Trigger", "REVIEW", ""),
    ],
)
def test_invalid_settings_have_no_saved_version(starter, assertion):
    source, workflow, _, _ = starter(PURCHASE_POLICY)
    service, _ = stage_map_review(source, workflow)
    snap = service.preview(OWNER_A)
    with pytest.raises(IntakeError, match="invalid_map_settings"):
        service.save(
            OWNER_A,
            expected_revision=snap.review_revision,
            operation_ref="bad-settings",
            changes=(),
            settings=assertion,
        )
    assert service.preview(OWNER_A).version == 0


def test_unconfirmed_or_unresolved_branch_prevents_fallback_truth(starter):
    source, workflow, _, _ = starter(b"# Policy\nIf total < 250, APPROVE.\n")
    service, _ = stage_map_review(source, workflow)
    snap = service.preview(OWNER_A)
    current = service.save(
        OWNER_A,
        expected_revision=snap.review_revision,
        operation_ref="before-confirm",
        changes=(),
        settings=MapSettingsEdit("Invoice arrival", "REVIEW", "Pending review"),
    )
    assert current.expected_action({"total": Decimal("300")})[0] is None
    unresolved = service.save(
        OWNER_A,
        expected_revision=current.review_revision,
        operation_ref="unresolved",
        changes=(BranchEdit(current.branches[0].branch_ref, "unresolved", "Unclear rule"),),
    )
    assert unresolved.expected_action({"total": Decimal("300")})[0] is None
