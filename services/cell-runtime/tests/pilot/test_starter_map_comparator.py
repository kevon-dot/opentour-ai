"""Generated typed rules and a real frozen scorer exercise the local comparator port."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace

import pytest
from opentour_cell.pilot.guided_decision_map import Condition
from opentour_cell.pilot.intake_mcp import IntakeError
from opentour_cell.pilot.starter_map_comparator import (
    StarterMapComparator,
    _action,
    initialize_journal,
)
from test_starter_cases import WORK
from test_starter_scoring import EVALUATOR, run, scored_setup  # noqa: F401


@dataclass(frozen=True)
class Rule:
    conditions: tuple
    action: str
    precedence: int | None


@dataclass(frozen=True)
class Context:
    source_revision: str
    source_digest: str
    comparator_ref: str
    comparator_revision: str
    confirmed_map_ref: str = "generated-confirmed"
    field_kinds: tuple = (("category", "reference"),)
    branches: tuple = (Rule((Condition("category", "present"),), "SOFTWARE", 1),)
    fallback_action: str | None = "REVIEW"


@pytest.fixture
def joined(scored_setup, tmp_path):  # noqa: F811
    scorer, authority, *_ = scored_setup
    frozen = scorer.cases.evaluation_inputs(EVALUATOR, WORK)
    context = Context(
        WORK.revisions[0],
        frozen.source_digest,
        frozen.plan.comparator_ref,
        authority.permission.comparator_revision,
    )
    authority.comparator_context = lambda actor, work, permission: context
    port = StarterMapComparator(
        tmp_path,
        EVALUATOR,
        WORK,
        scorer.cases,
        authority,
        authority.permission,
        context.comparator_ref,
        b"k" * 32,
    )
    scorer.comparator = port
    return port, context, scored_setup


def replay_args(joined):
    port, _, setup = joined
    result = run(setup)
    from opentour_cell.pilot.starter_scoring import PredictionRequest

    request = PredictionRequest(**result["predictions"]["comparator"]["request"])
    inputs = setup[0].baseline.calls[0][1]
    return request, inputs


def test_normal_scoring_durable_retry_concurrent_and_restart(joined):
    port, _, setup = joined
    initialize_journal(port.path.parent)
    request, inputs = replay_args(joined)
    receipt = port.lookup(request)
    assert set(receipt.outputs) == set(inputs)
    assert all(v == {"status": "automatic", "label": "SOFTWARE"} for v in receipt.outputs.values())
    with ThreadPoolExecutor(2) as pool:
        assert list(pool.map(lambda _: port.execute(request, inputs), range(2))) == [
            receipt,
            receipt,
        ]
    reopened = StarterMapComparator(
        port.path.parent,
        EVALUATOR,
        WORK,
        setup[0].cases,
        setup[1],
        setup[1].permission,
        port.reference,
        b"k" * 32,
    )
    assert reopened.lookup(request) == receipt
    with sqlite3.connect(port.path) as db:
        assert db.execute("SELECT count(*) FROM receipts").fetchone() == (1,)
        assert "decision_inputs" not in db.execute("SELECT body FROM receipts").fetchone()[0]


def test_missing_journal_and_orphaned_sidecar_hold(joined):
    port, _, setup = joined
    with pytest.raises(IntakeError):
        run(setup)
    assert not port.path.exists()
    orphan = port.path.with_suffix(".sqlite3-wal")
    orphan.write_bytes(b"generated-orphan")
    with pytest.raises(IntakeError):
        initialize_journal(port.path.parent)
    assert orphan.read_bytes() == b"generated-orphan" and not port.path.exists()


def test_request_scope_tamper_receipt_tamper_and_input_fidelity(joined):
    port, context, setup = joined
    initialize_journal(port.path.parent)
    request, inputs = replay_args(joined)
    with pytest.raises(IntakeError):
        port.execute(request, {**inputs, "unfrozen": {}})
    with pytest.raises(IntakeError):
        port.lookup(replace(request, operation_ref="foreign-operation"))
    setup[1].comparator_context = lambda *args: replace(context, source_digest="changed")
    with pytest.raises(IntakeError):
        port.lookup(request)
    setup[1].comparator_context = lambda *args: context
    with sqlite3.connect(port.path) as db:
        db.execute("UPDATE receipts SET body='{}'")
    with pytest.raises(IntakeError):
        port.lookup(request)


def test_withdrawal_during_rule_execution_rolls_back(joined, monkeypatch):
    from opentour_cell.pilot import starter_map_comparator as module

    port, _, setup = joined
    initialize_journal(port.path.parent)
    original = module._action

    def withdraw(*args):
        setup[1].allowed = False
        return original(*args)

    monkeypatch.setattr(module, "_action", withdraw)
    with pytest.raises(IntakeError):
        run(setup)
    with sqlite3.connect(port.path) as db:
        assert db.execute("SELECT count(*) FROM receipts").fetchone() == (0,)


def test_typed_numbers_missing_ambiguity_and_priority(joined):
    _, ctx, _ = joined
    low = Rule((Condition("amount", "<", "10"),), "APPROVE", 2)
    even_lower = Rule((Condition("amount", "<=", "5"),), "REVIEW", 1)
    ctx = replace(
        ctx, field_kinds=(("amount", "number"),), branches=(low, even_lower), fallback_action=None
    )
    assert _action(ctx, {"amount": "5.00"}) == {"status": "review"}
    assert _action(ctx, {"amount": 7}) == {"status": "automatic", "label": "APPROVE"}
    for invalid in ("generated-invalid", True, None, 20):
        assert _action(ctx, {"amount": invalid}) == {"status": "missing"}
    ambiguous = replace(
        ctx, branches=(replace(low, precedence=None), replace(even_lower, precedence=None))
    )
    assert _action(ambiguous, {"amount": 3}) == {"status": "missing"}


def test_concurrent_first_execution_lost_ack_and_reconciliation(joined, monkeypatch):
    port, _, setup = joined
    initialize_journal(port.path.parent)
    original = port.execute
    calls = []

    def deliver_twice(request, inputs):
        with ThreadPoolExecutor(2) as pool:
            receipts = list(pool.map(lambda _: original(request, inputs), range(2)))
        assert receipts[0] == receipts[1]
        calls.append(receipts[0].receipt_ref)
        raise OSError("generated lost acknowledgement")

    monkeypatch.setattr(port, "execute", deliver_twice)
    first = run(setup)
    assert first["state"] == "scored" and len(calls) == 1
    assert run(setup) == first and len(calls) == 1
    with sqlite3.connect(port.path) as db:
        assert db.execute("SELECT count(*) FROM receipts").fetchone() == (1,)
