"""Exercise private status questions through the actual picker and frozen worker records."""

import time
from pathlib import Path
from typing import Any

import pytest
from opentour_cell.pilot import zapier_approval
from test_approval_reconciliation import signed_at_clock
from test_fenced_approval_readback import lose_reply, short_request
from test_managed_slack import setup_workflow
from test_reviewer_workflow import buttons, restart, screen, supplied, text
from test_slack_adapter import ACTOR, Harness
from test_zapier_approval import deliver, make_case, private


def inspect(h: Harness) -> tuple[str, str]:
    view = h.launch()
    revision = buttons(screen(h, view))["ot_evidence"]
    h.action(view, "ot_evidence", revision)
    return view, revision


def answer(h: Harness, question: str, revision: str) -> str:
    view = h.launch(prompt=question)
    assert buttons(screen(h, view))["ot_result_open"] == revision
    h.action(view, "ot_result_open", revision)
    assert screen(h, view)["callback_id"] == "ot_status_question"
    assert buttons(screen(h, view))["ot_evidence"] == revision
    return text(h, view)


def counts(h: Harness) -> tuple[int, ...]:
    with h.app.journal.db() as db:
        journal = tuple(
            int(db.execute("SELECT count(*) FROM " + table).fetchone()[0])
            for table in ("decisions", "proposals", "approval_posts")
        )
    with h.store._db() as db:
        jobs = int(db.execute("SELECT count(*) FROM jobs").fetchone()[0])
    return (*journal, jobs, len(h.slack.post))


def test_same_training_question_uses_actual_job_and_role_held_rule(tmp_path: Path) -> None:
    h = Harness(tmp_path / "trained")
    view, _, revision = h.completed()
    h.action(view, "ot_evidence", revision)
    before = counts(h)
    trained = answer(h, "Why didn't it train?", revision)
    assert "CPU training job completed" in trained and "3 training epochs" in trained
    assert counts(h) == before
    h, _, _, outcome = setup_workflow(tmp_path / "held", "unapproved_model")
    assert outcome == "hold"
    _, revision = inspect(h)
    before = counts(h)
    held = answer(h, "Why didn't it train?", revision)
    assert "rule comparison" in held and "Separate model and compute authorization" in held
    assert counts(h) == before


def test_recorded_readout_approval_and_changed_status_at_disclosure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(tmp_path)
    dataset = supplied(h, qualified=True)
    h.config["users"][ACTOR]["datasets"] = [dataset]
    h.config["zapier"] = False
    h.save()
    inspected, revision = inspect(h)
    before = counts(h)
    missing = answer(h, "Did it ship?", revision)
    assert "No verified approval receipt" in missing and "unconfirmed" in missing
    assert counts(h) == before

    # A distinct real reviewer action lands after building the status view, before admission.
    h.action(inspected, "ot_home", "home")
    original = h.app.views.status_question_modal

    def approve_between_read_and_render(*args: Any, **kwargs: Any) -> dict[str, Any]:
        result = original(*args, **kwargs)
        h.action(inspected, "ot_approve", revision)
        return result

    with monkeypatch.context() as m:
        m.setattr(h.app.views, "status_question_modal", approve_between_read_and_render)
        stale = h.launch(prompt="Did it ship?")
        h.action(stale, "ot_result_open", revision)
        assert screen(h, stale).get("callback_id") != "ot_status_question"
        assert revision not in text(h, stale)

    receipt = h.app.journal.decision(revision)["receipt"]
    before = counts(h)
    restart(h)
    for _ in range(2):
        current = answer(h, "Did it ship?", revision)
        assert receipt in current and "Approval recorded" in current and "unconfirmed" in current
    assert counts(h) == before


def test_provider_approval_and_transport_never_attest_artifact_handoff(tmp_path: Path) -> None:
    case = make_case(tmp_path)
    request = case.inspect()
    before = counts(case.h)
    staged = answer(case.h, "Did it ship?", case.revision)
    assert request["request_id"] in staged and "Approval request staged" in staged
    assert counts(case.h) == before
    sent = deliver(case, request)
    accepted = answer(case.h, "Did it ship?", case.revision)
    assert "Approval request transport acknowledged" in accepted and "unconfirmed" in accepted
    assert case.bridge.consume(case.signed(request))["state"] == "approved"
    restart(case.h)
    before = counts(case.h)
    for _ in range(2):
        approved = answer(case.h, "Did it ship?", case.revision)
        assert "Approval recorded" in approved and "Handoff delivery is unconfirmed" in approved
    assert len(sent) == 1 and counts(case.h) == before


def test_expired_provider_readback_stays_scoped_and_does_not_import_vote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = make_case(tmp_path)
    now = int(time.time())
    monkeypatch.setattr(time, "time", lambda: now)
    request = short_request(case)
    event = signed_at_clock(case, request)
    lose_reply(case, request)
    now += 3
    case.bridge.reconcile(poll_interval=1)

    def approval_snapshot() -> dict[str, bytes]:
        return {
            path.name: path.read_bytes()
            for path in case.directory.glob("zapier-*")
            if path.is_file()
        }

    restart(case.h)
    before = counts(case.h)
    snapshot = approval_snapshot()
    unknown = answer(case.h, "Did it ship?", case.revision)
    assert request["request_id"] in unknown and "Approval outcome awaits reconciliation." in unknown
    assert "Delivery remains unconfirmed." in unknown and "Approval recorded" not in unknown
    assert approval_snapshot() == snapshot and counts(case.h) == before

    observed = case.bridge.observe_fenced_status(event)
    assert (
        observed["provider_status"] == "Approved" and observed["release_decision"] == "not_recorded"
    )
    snapshot = approval_snapshot()
    restart(case.h)
    for _ in range(2):
        expiry = answer(case.h, "Did it ship?", case.revision)
        assert request["request_id"] in expiry and "Approval request expired." in expiry
        assert "Delivery remains unconfirmed." in expiry and "Approval recorded" not in expiry
    assert approval_snapshot() == snapshot and counts(case.h) == before
    assert not list(case.directory.glob("managed-outputs/*/release-decision.json"))
    assert not list(case.directory.glob("managed-outputs/*/handoff"))

    case.bridge.prepare_cutover()
    private(case.bridge.config, case.policy | {"zap_version": "version-2"})
    snapshot = approval_snapshot()
    unavailable = answer(case.h, "Did it ship?", case.revision)
    assert (
        "No verified approval receipt" in unavailable and request["request_id"] not in unavailable
    )
    assert approval_snapshot() == snapshot and counts(case.h) == before
    case.bridge.complete_cutover()
    case.view = case.h.launch()
    case.h.action(case.view, "ot_evidence", case.revision)
    successor = case.bridge.issue(**case.binding)
    assert successor["request_id"] != request["request_id"]
    snapshot = approval_snapshot()
    before = counts(case.h)
    current = answer(case.h, "Did it ship?", case.revision)
    assert successor["request_id"] in current and "Approval request staged" in current
    assert request["request_id"] not in current
    assert approval_snapshot() == snapshot and counts(case.h) == before
    assert not list(case.directory.glob("managed-outputs/*/release-decision.json"))


def test_verified_approval_remains_immutable_after_request_expiry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = make_case(tmp_path)
    now = int(time.time())
    monkeypatch.setattr(time, "time", lambda: now)
    request = short_request(case)
    sent = deliver(case, request)
    event = signed_at_clock(case, request)
    assert case.bridge.consume(event)["state"] == "approved"
    (receipt,) = case.directory.glob("managed-outputs/*/release-decision.json")
    original = receipt.read_bytes()

    now += 3
    restart(case.h)
    with pytest.raises(ValueError, match="completed_request_observation_not_required"):
        case.bridge.observe_fenced_status(event)
    before = counts(case.h)
    approved = answer(case.h, "Did it ship?", case.revision)
    assert request["request_id"] in approved and "Approval recorded" in approved
    assert (
        "Handoff delivery is unconfirmed" in approved and "Approval request expired" not in approved
    )
    assert receipt.read_bytes() == original and len(sent) == 1 and counts(case.h) == before
    assert not list(case.directory.glob("managed-outputs/*/handoff"))


@pytest.mark.parametrize(
    ("provider", "decision", "previous", "finding"),
    [
        ("Approved", "approved", "unknown", "Approval recorded"),
        ("Declined", "changes_requested", "Pending", "Changes requested"),
    ],
)
def test_completed_vote_recovered_after_journal_interruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    decision: str,
    previous: str,
    finding: str,
) -> None:
    case = make_case(tmp_path)
    now = int(time.time())
    monkeypatch.setattr(time, "time", lambda: now)
    request = short_request(case)
    sent = deliver(case, request)
    if previous == "Pending":
        case.bridge.consume(signed_at_clock(case, request, previous))
    event = signed_at_clock(case, request, provider)

    transition = zapier_approval.ReconciliationJournal.transition

    def stop(journal, reference, state, reason, status, **kwargs):
        if state == decision:
            raise OSError("generated stop after publishing the release decision")
        return transition(journal, reference, state, reason, status, **kwargs)

    with monkeypatch.context() as local:
        local.setattr(zapier_approval.ReconciliationJournal, "transition", stop)
        with pytest.raises(OSError, match="after publishing the release decision"):
            case.bridge.consume(event)
    (receipt,) = case.directory.glob("managed-outputs/*/release-decision.json")
    original = receipt.read_bytes()
    now += 3
    restart(case.h)
    pending = answer(case.h, "Did it ship?", case.revision)
    assert "Approval outcome awaits reconciliation." in pending
    assert finding not in pending

    assert case.bridge.reconcile(poll_interval=1)["counts"] == {decision: 1}
    assert case.bridge.pending_summary()["items"][0]["provider_status"] == previous
    before = counts(case.h)
    restart(case.h)
    recovered = answer(case.h, "Did it ship?", case.revision)
    assert request["request_id"] in recovered and finding in recovered
    assert (
        "Handoff delivery is unconfirmed" in recovered
        and "Approval request expired" not in recovered
    )
    assert receipt.read_bytes() == original and len(sent) == 1 and counts(case.h) == before
    assert not list(case.directory.glob("managed-outputs/*/handoff"))


def test_pre_journal_approval_can_be_read_without_creating_reconciliation_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = make_case(tmp_path)
    now = int(time.time())
    monkeypatch.setattr(time, "time", lambda: now)
    request = short_request(case)
    sent = deliver(case, request)
    assert case.bridge.consume(signed_at_clock(case, request))["state"] == "approved"
    (receipt,) = case.directory.glob("managed-outputs/*/release-decision.json")
    prior = receipt.read_bytes()
    # The earlier packaged bridge persisted these files without a reconciliation database.
    old_journal = case.directory / "zapier-reconciliation.sqlite3"
    old_lock = case.directory / "zapier-reconciliation.lock"
    old_journal.unlink()
    old_lock.unlink()

    now += 3
    restart(case.h)
    before = counts(case.h)
    approved = answer(case.h, "Did it ship?", case.revision)
    assert request["request_id"] in approved and "Approval recorded" in approved
    assert "Handoff delivery is unconfirmed" in approved
    assert not old_journal.exists() and not old_lock.exists()
    assert receipt.read_bytes() == prior and len(sent) == 1 and counts(case.h) == before
    assert not list(case.directory.glob("managed-outputs/*/handoff"))


def test_legacy_successor_cannot_rediscover_the_previous_runs_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = make_case(tmp_path)
    now = int(time.time())
    monkeypatch.setattr(time, "time", lambda: now)
    request = short_request(case)
    sent = deliver(case, request)
    assert case.bridge.consume(signed_at_clock(case, request))["state"] == "approved"
    (receipt,) = case.directory.glob("managed-outputs/*/release-decision.json")
    prior = receipt.read_bytes()

    now += 3
    policy, key = case.bridge._policy()
    original = case.bridge._request(request["request_id"], policy, key, fresh=False)
    # Restore a historical V1 nonce that shares the original presentation.
    suffix = "1" * 16 if request["request_id"].endswith("0" * 16) else "0" * 16
    reference = request["request_id"][:-16] + suffix
    legacy = original | {
        "request_id": reference,
        "nonce": "9" * 64,
        "issued_at": now,
        "expires_at": now + 30,
    }
    private(
        case.bridge._path(reference, "request"),
        {"request": legacy, "seal": zapier_approval._sig(key, zapier_approval.REQUEST, legacy)},
    )
    with case.bridge._serialized():
        pass
    assert (
        case.bridge.decision_status(reference, team=policy["team"], actor=ACTOR)["state"]
        == "approved"
    )

    original_glob = Path.glob

    def earlier_first(path: Path, pattern: str):
        found = original_glob(path, pattern)
        if path == case.directory and pattern.endswith("-request.json"):
            return iter(sorted(found, key=lambda item: request["request_id"] not in item.name))
        return found

    monkeypatch.setattr(Path, "glob", earlier_first)
    previous = case.bridge.existing_status(
        run_id=legacy["run_id"],
        run_revision=legacy["run_revision"],
        artifact_sha=legacy["artifact_sha"],
        team=policy["team"],
        actor=ACTOR,
    )
    assert previous and previous["request_id"] == request["request_id"]

    restart(case.h)
    before = counts(case.h)
    pending = answer(case.h, "Did it ship?", case.revision)
    assert reference in pending and "Approval outcome awaits reconciliation." in pending
    assert "Approval recorded" not in pending and request["request_id"] not in pending
    assert receipt.read_bytes() == prior and len(sent) == 1 and counts(case.h) == before

    # Even a matching new provider observation cannot attribute the old run-level
    # decision to either of two sealed nonces that share the exact presentation.
    wire = case.bridge._wire(legacy, key, policy)
    second = deliver(case, wire)
    assert case.bridge.consume(signed_at_clock(case, wire))["state"] == "approved"
    before = counts(case.h)
    ambiguous = answer(case.h, "Did it ship?", case.revision)
    assert reference in ambiguous and "Approval outcome awaits reconciliation." in ambiguous
    assert "Approval recorded" not in ambiguous and "Delivery remains unconfirmed." in ambiguous
    assert receipt.read_bytes() == prior and len(second) == 1 and counts(case.h) == before
    assert not list(case.directory.glob("managed-outputs/*/handoff"))


def test_unexpired_but_fenced_request_cannot_be_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = make_case(tmp_path)
    now = int(time.time())
    monkeypatch.setattr(time, "time", lambda: now)
    request = case.inspect()
    assert now < request["expires_at"]
    assert case.bridge.prepare_cutover()["counts"] == {"superseded": 1}
    restart(case.h)
    before = counts(case.h)
    hidden = answer(case.h, "Did it ship?", case.revision)
    assert request["request_id"] not in hidden and "No verified approval receipt" in hidden
    assert "Approval request staged" not in hidden and counts(case.h) == before
    assert not list(case.directory.glob("managed-outputs/*/release-decision.json"))


def test_removed_destination_is_a_current_hold_and_redacts_prior_status(tmp_path: Path) -> None:
    case = make_case(tmp_path, audience=ACTOR)
    first = case.inspect()
    sent = deliver(case, first)
    assert case.bridge.consume(case.signed(first))["state"] == "approved"
    work = case.directory / "managed-outputs" / case.binding["run_id"]
    receipt = work / "release-decision.json"
    original_receipt = receipt.read_bytes()
    view = case.h.launch(prompt="Did it ship?")
    assert buttons(screen(case.h, view))["ot_result_open"] == case.revision

    del case.h.config["recipient"]
    case.h.save()
    before = counts(case.h)
    case.h.action(view, "ot_result_open", case.revision)
    assert screen(case.h, view).get("callback_id") != "ot_status_question"
    assert first["request_id"] not in text(case.h, view)
    assert counts(case.h) == before

    restart(case.h)
    _, revision = inspect(case.h)
    before = counts(case.h)
    for _ in range(2):
        current = answer(case.h, "Did it ship?", revision)
        assert "No release is authorized by the current configuration." in current
        assert "Earlier delivery is unconfirmed." in current
        assert first["request_id"] not in current and "Intended recipient" not in current
    assert len(sent) == 1 and counts(case.h) == before
    assert receipt.read_bytes() == original_receipt and not (work / "handoff").exists()


def test_declined_local_readout_holds_readout_only(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.config["users"][ACTOR]["datasets"] = [supplied(h, qualified=True)]
    h.save()
    view, revision = inspect(h)
    h.action(view, "ot_home", "home")
    h.action(view, "ot_changes", revision)
    receipt = h.app.journal.decision(revision)["receipt"]
    restart(h)
    before = counts(h)
    for _ in range(2):
        current = answer(h, "Did it ship?", revision)
        assert receipt in current and "Changes requested" in current
        assert "Readout is held pending changes." in current
        assert "Earlier delivery is unconfirmed." in current
    assert counts(h) == before


@pytest.mark.parametrize(
    ("action", "expected", "finding"),
    [
        ("ot_release_approve", "Approval recorded", "Handoff delivery is unconfirmed."),
        ("ot_release_changes", "Changes requested", "Release is held pending changes."),
    ],
)
def test_native_release_status_observes_independent_decision(
    tmp_path: Path, action: str, expected: str, finding: str
) -> None:
    h, _, _, _ = setup_workflow(tmp_path, "rule", release=True)
    inspected, revision = inspect(h)
    before = counts(h)
    missing = answer(h, "Did it ship?", revision)
    assert "No verified approval receipt" in missing
    assert counts(h) == before
    h.action(inspected, action, buttons(screen(h, inspected))[action])
    restart(h)
    before = counts(h)
    for _ in range(2):
        current = answer(h, "Did it ship?", revision)
        assert expected in current and finding in current and "unconfirmed" in current
    assert counts(h) == before


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("Was the candidate any quicker than the baseline?", "fit-speed comparison"),
        ("What prevented our training job from starting?", "evaluation-only"),
        ("What keeps the model from training?", "evaluation-only"),
        ("What caused the fit to be skipped?", "evaluation-only"),
        (
            "Which inputs were given to the model and what output came back?",
            "Decision-time model input",
        ),
        ("Has the intended recipient received the package yet?", "Handoff delivery is unconfirmed"),
        (
            "Have we seen any verified mistakes over the past seven days?",
            "denominator for the requested window",
        ),
        ("How much time did we save?", "fit-speed comparison"),
        ("What's holding back the model fit?", "evaluation-only"),
        ("What did the classifier predict?", "Decision-time model input"),
        ("Did the artifact go through?", "Handoff delivery is unconfirmed"),
        ("Was this week error-free?", "Verified live errors"),
    ],
)
def test_status_question_forms_reach_selected_evidence(
    tmp_path: Path, question: str, expected: str
) -> None:
    h = Harness(tmp_path)
    h.config["users"][ACTOR]["datasets"] = [supplied(h, qualified=True)]
    h.save()
    _, revision = inspect(h)
    before = counts(h)
    assert expected in answer(h, question, revision)
    assert counts(h) == before


@pytest.mark.parametrize(
    "question",
    [
        "Could you send the package?",
        "Please fit it and ship it",
        "Has it shipped and how much faster is it?",
        "Did the package arrive for ForeignTenant?",
        "Were there five errors this week?",
        "Did we train it? Ship it",
    ],
)
def test_status_parser_rejects_actions_mixed_clauses_and_unbound_scope(question: str) -> None:
    from opentour_cell.pilot.status_questions import resolve_status

    assert resolve_status(question) is None
