import hashlib
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
