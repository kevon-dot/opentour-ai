import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from decimal import Decimal

import pytest
from opentour_cell.pilot import guided_policy
from opentour_cell.pilot.guided_decision_map import FieldKind, GuidedMapService, guided_draft
from opentour_cell.pilot.intake_admission import DurableIntake
from opentour_cell.pilot.intake_local import LocalStarterAuthority
from opentour_cell.pilot.intake_mcp import IntakeError, Starter
from test_intake_mcp import _sealed_starter, _starter_only, _write_private

POLICY = "# Facturation\r\nRévision de référence.\r\n\r\n## Achat\r\nIf PO is missing, REVIEW.\r\n"
POLICY_BYTES = POLICY.encode()
PURCHASE_POLICY = b"""# Ordering
Apply the first matching rule.
If po is missing, REVIEW.
If total < 250, APPROVE.
If total >= 250, MANUAL.
"""
FIELDS = {"po": FieldKind.REFERENCE, "total": FieldKind.NUMBER}
ACTIONS = frozenset({"REVIEW", "APPROVE", "MANUAL"})


@pytest.fixture
def starter(tmp_path):
    def staged(raw=POLICY_BYTES, filename="policy.md", *, admit=True):
        sha = hashlib.sha256(raw).hexdigest()
        owner = tmp_path.resolve() / ("owner-" + sha[:12])
        selection_path, selection = _starter_only(owner)
        grant = _sealed_starter(selection, source_revision=sha)
        grant_path = owner / "starter-intake-grant-registered-starter.json"
        _write_private(grant_path, grant)
        path = owner / filename
        path.write_bytes(raw)
        path.chmod(0o600)
        authority = LocalStarterAuthority(selection_path)
        body = grant["grant"]
        request = Starter(
            policy_resource_ref=body["policy_resource_ref"],
            source_revision=sha,
            setup_policy_revision=body["setup_policy_revision"],
            idempotency_key="generated-intent",
            creation_ref=body["creation_ref"],
            workflow_ref=None,
        )
        intake = DurableIntake(owner, authority)
        workflow = "workflow-" + "0" * 32
        if admit:
            receipt = intake.submit_intake(authority.principal, request)
            workflow = intake.get_run(authority.principal, receipt.run_ref).workflow_ref
        return guided_policy.GuidedPolicySource(authority, path), workflow, request, intake

    return staged


def test_admitted_policy_citations_bind_normalized_multibyte_spans_across_restart(starter):
    source, workflow, request, intake = starter()
    actor = source.authority.principal
    snapshot = source.read(actor, workflow)
    span = snapshot.cite(5, 5)
    assert span.section_ref == "section-4"
    assert (span.line_start, span.line_end) == (5, 5)
    normalized = POLICY.replace("\r\n", "\n").encode()
    selected = normalized[span.byte_start : span.byte_end]
    assert selected.decode() == "If PO is missing, REVIEW.\n"
    assert hashlib.sha256(selected).hexdigest() == span.passage_sha256
    assert snapshot.normalized_sha256 == hashlib.sha256(normalized).hexdigest()
    assert snapshot.passage(span) == selected.decode()
    with pytest.raises(guided_policy.PolicyError, match="policy_span_crosses_sections"):
        snapshot.cite(3, 5)

    next_request = replace(request, idempotency_key="generated-retry")
    again = intake.submit_intake(actor, next_request)
    assert intake.get_run(actor, again.run_ref).workflow_ref == workflow
    reopened = guided_policy.GuidedPolicySource(
        LocalStarterAuthority(source.authority.path), source.path
    )
    assert reopened.read(actor, workflow).passage(span) == selected.decode()
    with pytest.raises(guided_policy.PolicyError, match="policy_span_revision_changed"):
        snapshot.passage(replace(span, byte_start=span.byte_start + 1))


def test_foreign_grant_and_unadmitted_workflow_never_open_policy(starter, monkeypatch):
    source, workflow, _, _ = starter(admit=False)
    actor = source.authority.principal

    def unexpected(*_args):
        pytest.fail("a policy read occurred without matching scope")

    monkeypatch.setattr(guided_policy, "_read_policy", unexpected)
    with pytest.raises(IntakeError, match="intake_unavailable"):
        source.read(replace(actor, tenant="foreign"), workflow)
    with pytest.raises(guided_policy.PolicyError, match="policy_workflow_unavailable"):
        source.read(actor, workflow)


@pytest.mark.parametrize("filename, raw", [("policy.pdf", b"fixture"), ("policy.txt", b"\xff")])
def test_unsupported_format_returns_fixed_diagnostic(starter, filename, raw):
    source, workflow, _, _ = starter(raw, filename)
    with pytest.raises(guided_policy.PolicyError, match="^policy_format_unsupported$"):
        source.read(source.authority.principal, workflow)


def test_changed_policy_and_withdrawal_hold_instead_of_refreshing_under_old_revision(starter):
    source, workflow, _, _ = starter()
    actor = source.authority.principal
    prior = source.read(actor, workflow).cite(5, 5)
    source.path.write_text("# Generated successor\nDo not return this under a previous grant.\n")
    with pytest.raises(guided_policy.PolicyError, match="policy_source_revision_changed"):
        source.read(actor, workflow)
    assert prior.source_revision != hashlib.sha256(source.path.read_bytes()).hexdigest()

    _write_private(source.path.parent / "starter-intake-withdrawn-registered-starter.json", {})
    with pytest.raises(IntakeError, match="intake_unavailable"):
        source.read(actor, workflow)


def test_current_policy_successor_admitted_by_existing_workflow_is_readable(starter, monkeypatch):
    initial, workflow, request, _ = starter(PURCHASE_POLICY)
    actor = initial.authority.principal
    original = initial.read(actor, workflow)
    revised = PURCHASE_POLICY.replace(b"250", b"300")
    revision = hashlib.sha256(revised).hexdigest()
    initial.path.write_bytes(revised)
    authority_path = initial.authority.path
    source_directory = authority_path.parent
    settings = json.loads(authority_path.read_bytes())
    _write_private(
        source_directory / "starter-intake-grant-registered-starter.json",
        _sealed_starter(settings, source_revision=revision),
    )
    authority = LocalStarterAuthority(authority_path)
    reader = guided_policy.GuidedPolicySource(authority, initial.path)
    read = guided_policy._read_policy
    calls = []

    def observe(*args):
        calls.append(args[0])
        return read(*args)

    monkeypatch.setattr(guided_policy, "_read_policy", observe)
    with pytest.raises(guided_policy.PolicyError, match="policy_workflow_unavailable"):
        reader.read(actor, workflow)
    assert calls == []
    admission = DurableIntake(source_directory, authority)
    result = admission.submit_intake(
        actor,
        replace(
            request,
            idempotency_key="existing-workflow-successor",
            creation_ref=None,
            workflow_ref=workflow,
            source_revision=revision,
        ),
    )
    assert admission.get_run(actor, result.run_ref).workflow_ref == workflow
    restarted = guided_policy.GuidedPolicySource(
        LocalStarterAuthority(authority_path), initial.path
    )
    current = restarted.read(actor, workflow)
    assert current.source_revision == revision and len(calls) == 1
    assert "300" in current.passage(current.cite(4, 4))
    assert "250" in original.passage(original.cite(4, 4))
    with closing(sqlite3.connect(admission.path)) as db, db:
        (raw_request,) = db.execute(
            "SELECT request FROM intake_runs WHERE run_ref=?", (result.run_ref,)
        ).fetchone()
        altered = json.loads(raw_request) | {"workflow_ref": "workflow-foreign"}
        db.execute(
            "UPDATE intake_runs SET request=? WHERE run_ref=?",
            (json.dumps(altered, sort_keys=True, separators=(",", ":")), result.run_ref),
        )
    with pytest.raises(guided_policy.PolicyError, match="policy_workflow_unavailable"):
        restarted.read(actor, workflow)
    assert len(calls) == 1


def test_private_policy_file_cannot_be_redirected(starter, tmp_path):
    source, workflow, _, _ = starter()
    foreign = tmp_path / "foreign.md"
    foreign.write_bytes(POLICY.encode())
    source.path.unlink()
    source.path.symlink_to(foreign)
    with pytest.raises(guided_policy.PolicyError, match="policy_source_unavailable"):
        source.read(source.authority.principal, workflow)


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        (PURCHASE_POLICY, ["REVIEW", "REVIEW", "APPROVE", "MANUAL", "MANUAL"]),
        (
            PURCHASE_POLICY.replace(b"total < 250", b"total <= 250"),
            ["REVIEW", "REVIEW", "APPROVE", "APPROVE", "MANUAL"],
        ),
        (
            PURCHASE_POLICY.replace(b"If po is missing, REVIEW.\n", b"")
            + b"If po is missing, REVIEW.\n",
            ["APPROVE", "MANUAL", "APPROVE", "MANUAL", "MANUAL"],
        ),
    ],
)
def test_guided_policy_supports_hidden_boundaries_and_fails_mutated_semantics(starter, raw, want):
    source, workflow, _, _ = starter(raw)
    snapshot = source.read(source.authority.principal, workflow)
    actual = guided_draft(snapshot, FIELDS, ACTIONS)
    hidden_cases = [(None, "50"), (None, "250"), ("po", "249.99"), ("po", "250"), ("po", "250.01")]
    decisions = [
        actual.draft_action({"po": purchase_order, "total": Decimal(total)})
        for purchase_order, total in hidden_cases
    ]
    assert [action for action, _ in decisions] == want
    assert all(action in snapshot.passage(spans[0]) for action, spans in decisions)
    assert len({branch.branch_ref for branch in actual.branches}) == 3
    assert guided_draft(snapshot, FIELDS, ACTIONS) == actual
    expected = ["REVIEW", "REVIEW", "APPROVE", "MANUAL", "MANUAL"]
    assert (want == expected) is (raw == PURCHASE_POLICY)


def test_guided_conflict_remains_unresolved_and_future_fields_never_enter_predicates(starter):
    source, workflow, _, _ = starter(
        b"# Policy\nIf total < 250, APPROVE.\nIf total < 250, REVIEW.\n"
    )
    snapshot = source.read(source.authority.principal, workflow)
    conflicting = guided_draft(snapshot, FIELDS, ACTIONS)
    decision, citations = conflicting.draft_action({"total": Decimal("50")})
    assert decision is None
    assert [snapshot.passage(citation) for citation in citations] == [
        "If total < 250, APPROVE.\n",
        "If total < 250, REVIEW.\n",
    ]
    future_source, future_workflow, _, _ = starter(
        b"If later_accountant_label is present, MANUAL.\n"
    )
    future = future_source.read(future_source.authority.principal, future_workflow)
    unsupported = guided_draft(future, FIELDS, ACTIONS)
    assert unsupported.branches == ()
    assert len(unsupported.unresolved) == 1


def test_worker_saves_one_exact_draft_and_rejects_stale_revision_after_restart(starter):
    source, workflow, _, _ = starter(PURCHASE_POLICY)
    actor = source.authority.principal
    service = GuidedMapService(source, FIELDS, ACTIONS)
    candidate = service.preview(actor, workflow)
    assert not list(source.path.parent.glob("guided-map-*.json"))
    assert service.save_draft(actor, workflow, candidate.revision) == candidate
    records = list(source.path.parent.glob("guided-map-*.json"))
    assert len(records) == 1
    retained = records[0].read_bytes()

    restarted_source = guided_policy.GuidedPolicySource(
        LocalStarterAuthority(source.authority.path), source.path
    )
    restarted = GuidedMapService(restarted_source, FIELDS, ACTIONS)
    assert restarted.save_draft(actor, workflow, candidate.revision) == candidate
    with pytest.raises(guided_policy.PolicyError, match="guided_map_stale_revision"):
        restarted.save_draft(actor, workflow, "0" * 64)
    assert list(source.path.parent.glob("guided-map-*.json")) == records
    assert records[0].read_bytes() == retained


@pytest.mark.parametrize(
    "example",
    [
        b"~~~text\n## Illustration\nIf total < 250, APPROVE.\n~~~\n",
        b"<!--\n## Editorial comment\nIf total < 250, APPROVE.\n-->\n",
        b"    If total < 250, APPROVE.\n",
    ],
)
def test_examples_and_hidden_headings_cannot_supply_policy_rules_or_sections(starter, example):
    source, workflow, _, _ = starter(
        b"# Policy\nApply the first matching rule.\n" + example + b"If total < 250, REVIEW.\n"
    )
    policy = source.read(source.authority.principal, workflow)
    map_draft = guided_draft(policy, FIELDS, ACTIONS)
    action, citations = map_draft.draft_action({"total": Decimal("12")})
    assert action == "REVIEW"
    assert len(map_draft.branches) == 1
    assert len(citations) == 1 and citations[0].section_ref == "section-1"
    assert policy.passage(citations[0]) == "If total < 250, REVIEW.\n"


@pytest.mark.parametrize(
    "clause",
    [b"Except when po is missing: MANUAL.\n", b"Exception: when po is missing, MANUAL.\n"],
)
def test_unparsed_exception_cannot_be_silently_ignored(starter, clause):
    source, workflow, _, _ = starter(b"If total < 250, APPROVE.\n" + clause)
    policy = source.read(source.authority.principal, workflow)
    draft = guided_draft(policy, FIELDS, ACTIONS)
    action, missing = draft.draft_action({"po": None, "total": Decimal("12")})
    assert action is None
    assert tuple(policy.passage(citation) for citation in missing) == (clause.decode(),)


def test_numbered_markdown_rule_retains_its_citation_and_precedence(starter):
    source, workflow, _, _ = starter(
        b"# Policy\nApply the first matching rule.\n"
        b"1. If po is missing, MANUAL.\n2. If total < 250, APPROVE.\n"
    )
    snapshot = source.read(source.authority.principal, workflow)
    draft = guided_draft(snapshot, FIELDS, ACTIONS)
    action, citations = draft.draft_action({"po": None, "total": Decimal("12")})
    assert action == "MANUAL"
    assert snapshot.passage(citations[0]) == "1. If po is missing, MANUAL.\n"


def test_numbered_markdown_continuation_remains_operative(starter):
    source, workflow, _, _ = starter(
        b"# Policy\n1. If total < 250, APPROVE.\n    Except when the purchase order is missing.\n"
    )
    snapshot = source.read(source.authority.principal, workflow)
    draft = guided_draft(snapshot, FIELDS, ACTIONS)
    action, citations = draft.draft_action({"po": None, "total": Decimal("12")})
    assert action is None
    assert snapshot.passage(citations[0]).strip() == "Except when the purchase order is missing."


def test_fenced_example_inside_numbered_list_never_becomes_an_operative_branch(starter):
    source, workflow, _, _ = starter(
        b"# Policy\n1. ~~~text\n   If total < 250, APPROVE.\n   ~~~\n2. If total < 250, REVIEW.\n"
    )
    snapshot = source.read(source.authority.principal, workflow)
    draft = guided_draft(snapshot, FIELDS, ACTIONS)
    assert len(draft.branches) == 1
    assert draft.draft_action({"total": Decimal("12")})[0] == "REVIEW"


def test_wrong_canonical_admission_intent_denies_before_policy_read(starter, monkeypatch):
    source, workflow, _, intake = starter()
    with closing(sqlite3.connect(intake.path)) as db, db:
        db.execute("UPDATE intake_runs SET intent=?", ("0" * 64,))

    def unexpected(*_args):
        pytest.fail("a restored, substituted intake record authorized a policy read")

    monkeypatch.setattr(guided_policy, "_read_policy", unexpected)
    with pytest.raises(guided_policy.PolicyError, match="policy_workflow_unavailable"):
        source.read(source.authority.principal, workflow)
