"""Authoring-inference proposals over generated sentinel policies and a scripted stand-in profile.

Proves software behavior only. No real inference, provider, grant issuer or customer is involved.
"""

import sqlite3
import threading
from contextlib import closing
from dataclasses import replace
from decimal import Decimal

import pytest
from opentour_cell.pilot import authoring_inference as ai
from opentour_cell.pilot.guided_decision_map import (
    Condition,
    FieldKind,
    GuidedMapService,
    guided_draft,
)
from opentour_cell.pilot.guided_policy import PolicyError
from test_guided_policy import ACTIONS, FIELDS, PURCHASE_POLICY

pytest_plugins = ("test_guided_policy",)

SENTINEL_KEY = "generated-held-out-answer-key-sentinel"
PROFILE = "generated-authoring-profile"
SCHEMA = "generated-authoring-schema-v1"


def cite(start, end=None):
    return {"line_start": start, "line_end": end or start}


def branch(conditions, action, line):
    return {"conditions": conditions, "action": action, "citation": cite(line)}


MISSING = {"field": "po", "operator": "missing"}
BELOW = {"field": "total", "operator": "<", "threshold": "250"}
ABOVE = {"field": "total", "operator": ">=", "threshold": "250"}
GOOD = [
    branch([MISSING], "REVIEW", 3),
    branch([BELOW], "APPROVE", 4),
    branch([ABOVE], "MANUAL", 5),
]


def reply(branches, schema=SCHEMA):
    return {"schema_id": schema, "branches": branches}


class Stub:
    """Generated deterministic profile. It replays a script and records what it was given."""

    schema_id = SCHEMA

    def __init__(self, script, cost=7, profile_id=PROFILE, stand_in=True):
        self.profile_id = profile_id
        self.stand_in = stand_in
        self.script = list(script)
        self.cost = cost
        self.requests = []
        self.tools = None

    def infer(self, request, tools):
        self.requests.append(request)
        self.tools = tools
        step = self.script.pop(0)
        if isinstance(step, BaseException):
            raise step
        if callable(step):
            return step(request, tools)
        return ai.ProfileReply(step, self.cost)


class Grants:
    def __init__(self, grant):
        self.grant = grant
        self.calls = 0
        self.revoke_after = None

    def current(self, actor, workflow_ref):
        self.calls += 1
        if self.revoke_after is not None and self.calls > self.revoke_after:
            return None
        grant = self.grant
        if grant is None or grant.tenant != actor.tenant:
            return None
        return grant


class Crash(BaseException):
    pass


def make(starter, tmp_path, script, *, grant="default", quota=10, limits=None, raw=PURCHASE_POLICY):
    source, workflow, _, _ = starter(raw)
    actor = source.authority.principal
    if grant == "default":
        grant = ai.AuthoringGrant(
            actor.tenant, workflow, PROFILE, SCHEMA, "grant-rev-1", frozenset(FIELDS), quota
        )
    grants = Grants(grant)
    profile = Stub(script)
    ledger = source.path.parent / "authoring-ledger.sqlite3"
    kwargs = {} if limits is None else {"limits": limits}
    service = ai.AuthoringService(source, grants, profile, FIELDS, ACTIONS, ledger, **kwargs)
    return service, source, actor, workflow, grants, profile, ledger


def counts(ledger):
    with closing(sqlite3.connect(ledger)) as db:
        return tuple(
            db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("attempts", "proposals")
        )


def test_grounded_proposal_matches_guided_branches_and_is_attributed_to_the_profile(
    starter, tmp_path
):
    service, source, actor, workflow, _, profile, _ = make(starter, tmp_path, [reply(GOOD)])
    proposal = service.propose(actor, workflow, "generated-key-1")
    assert proposal.state == "needs_owner_review"
    assert proposal.applied is False
    assert proposal.proposed_by == "authoring-inference:" + PROFILE
    assert proposal.requested_by == actor.actor != proposal.proposed_by
    assert proposal.evidence_tier == "generated"
    assert (proposal.attempts, proposal.cost_micro, proposal.issues) == (1, 7, ())
    guided = guided_draft(source.read(actor, workflow), FIELDS, ACTIONS)
    assert {b.branch_ref for b in proposal.branches} == {b.branch_ref for b in guided.branches}
    assert all(b.citation.passage_sha256 for b in proposal.branches)
    assert len(profile.requests) == 1


def test_request_discloses_only_cited_spans_and_schema_vocabulary(starter, tmp_path):
    service, _, actor, workflow, _, profile, _ = make(starter, tmp_path, [reply(GOOD)])
    service.propose(actor, workflow, "generated-key-1")
    request = profile.requests[0]
    assert [span.text.strip() for span in request.spans] == [
        "Apply the first matching rule.",
        "If po is missing, REVIEW.",
        "If total < 250, APPROVE.",
        "If total >= 250, MANUAL.",
    ]
    text = repr(request)
    assert actor.tenant not in text and "grant-rev-1" not in text and SENTINEL_KEY not in text


def test_scorer_counts_grounded_matches_attempts_and_cost_with_withheld_key(starter, tmp_path):
    service, _, actor, workflow, _, _, _ = make(starter, tmp_path, [reply(GOOD[:2])])
    proposal = service.propose(actor, workflow, "generated-key-1")
    key = frozenset(
        {
            ((Condition("po", "missing"),), "REVIEW"),
            ((Condition("total", "<", "250"),), "APPROVE"),
            ((Condition("total", ">=", "250"),), "MANUAL"),
        }
    )
    score = ai.score_proposal(proposal, key)
    assert (score.proposed, score.grounded, score.expected, score.matched) == (2, 2, 3, 2)
    assert (score.attempts, score.cost_micro) == (1, 7)


def test_unsupported_answer_retries_then_succeeds_with_issue_codes_only(starter, tmp_path):
    wrong = [branch([BELOW], "MANUAL", 4)]  # MANUAL is not stated on line 4
    service, _, actor, workflow, _, profile, _ = make(
        starter, tmp_path, [reply(wrong), reply(GOOD)]
    )
    proposal = service.propose(actor, workflow, "generated-key-1")
    assert (proposal.attempts, proposal.cost_micro, len(proposal.branches)) == (2, 14, 3)
    assert profile.requests[1].prior_issue_codes == ("answer_unsupported",)


@pytest.mark.parametrize(
    "bad, code",
    [
        (branch([BELOW], "APPROVE", 99), "citation_invalid"),
        (branch([BELOW], "APPROVE", 1), "citation_invalid"),  # a heading is not a rule span
        (branch([BELOW], "SHIP", 4), "action_unsupported"),
        (
            branch([{"field": "total", "operator": "~", "threshold": "250"}], "APPROVE", 4),
            "predicate_unsupported",
        ),
        (
            branch([{"field": "secret", "operator": "missing"}], "APPROVE", 4),
            "predicate_unsupported",
        ),
        (
            branch([{"field": "po", "operator": "<", "threshold": "250"}], "APPROVE", 4),
            "predicate_unsupported",
        ),
        (
            branch([{"field": "total", "operator": "<", "threshold": "999"}], "APPROVE", 4),
            "answer_unsupported",
        ),
        ({"conditions": [], "action": "APPROVE", "citation": cite(4)}, "branch_schema_invalid"),
        ({"action": "APPROVE"}, "branch_schema_invalid"),
    ],
)
def test_invalid_branches_exhaust_bounded_retries_into_an_actionable_editable_draft(
    starter, tmp_path, bad, code
):
    service, _, actor, workflow, _, profile, ledger = make(starter, tmp_path, [reply([bad])] * 5)
    proposal = service.propose(actor, workflow, "generated-key-1")
    assert proposal.attempts == 3 == len(profile.requests)  # bounded by configured retries
    assert proposal.branches == () and proposal.applied is False
    assert proposal.state == "needs_owner_review"
    assert {issue.code for issue in proposal.issues} == {code}
    assert counts(ledger) == (3, 1)


def test_schema_mismatch_and_malformed_reply_are_rejected(starter, tmp_path):
    service, _, actor, workflow, _, _, _ = make(
        starter, tmp_path, [reply(GOOD, schema="other"), {"branches": GOOD}, "text", reply(GOOD)]
    )
    proposal = service.propose(actor, workflow, "generated-key-1")
    assert [i.code for i in proposal.issues] == ["reply_schema_invalid"]
    assert proposal.branches == () and proposal.attempts == 3


def test_model_text_cannot_widen_fields_beyond_the_grant(starter, tmp_path):
    grant = ai.AuthoringGrant("t", "w", PROFILE, SCHEMA, "r", frozenset({"po"}), 5)
    service, _, actor, workflow, _, _, _ = make(starter, tmp_path, [reply(GOOD)] * 3)
    service.grants.grant = replace(grant, tenant=actor.tenant, workflow_ref=workflow)
    proposal = service.propose(actor, workflow, "generated-key-1")
    assert {i.code for i in proposal.issues} == {"field_not_permitted"}
    assert [b.action for b in proposal.branches] == ["REVIEW"]


def test_conflicting_branches_stay_unresolved_for_the_owner(starter, tmp_path):
    raw = b"# Ordering\nIf total < 250, APPROVE or MANUAL.\n"
    clash = [branch([BELOW], "APPROVE", 2), branch([BELOW], "MANUAL", 2)]
    service, _, actor, workflow, _, _, _ = make(starter, tmp_path, [reply(clash)] * 3, raw=raw)
    proposal = service.propose(actor, workflow, "generated-key-1")
    assert "conflict_unresolved" in {i.code for i in proposal.issues}
    assert proposal.applied is False


def _refused(service, actor, workflow, code="authoring_unavailable", key="generated-key-1"):
    with pytest.raises(ai.AuthoringError, match=f"^{code}$"):
        service.propose(actor, workflow, key)


def test_no_grant_refuses_cleanly_with_zero_reads_calls_and_effects(starter, tmp_path, monkeypatch):
    service, source, actor, workflow, _, profile, ledger = make(
        starter, tmp_path, [reply(GOOD)], grant=None
    )
    monkeypatch.setattr(type(source), "read", lambda *_: pytest.fail("policy was read"))
    _refused(service, actor, workflow)
    assert profile.requests == [] and counts(ledger) == (0, 0)


def test_foreign_tenant_is_indistinguishable_from_no_grant(starter, tmp_path, monkeypatch):
    service, source, actor, workflow, _, profile, ledger = make(starter, tmp_path, [reply(GOOD)])
    monkeypatch.setattr(type(source), "read", lambda *_: pytest.fail("policy was read"))
    _refused(service, replace(actor, tenant="generated-foreign"), workflow)
    assert profile.requests == [] and counts(ledger) == (0, 0)


def test_workflow_access_alone_grants_nothing(starter, tmp_path):
    service, source, actor, workflow, _, profile, _ = make(
        starter, tmp_path, [reply(GOOD)], grant=None
    )
    assert source.read(actor, workflow).lines  # the owner may read the admitted policy
    _refused(service, actor, workflow)
    assert profile.requests == []


def test_grant_for_another_workflow_is_refused(starter, tmp_path):
    service, _, actor, workflow, grants, profile, _ = make(starter, tmp_path, [reply(GOOD)])
    grants.grant = replace(grants.grant, workflow_ref="workflow-" + "1" * 32)
    _refused(service, actor, workflow)
    assert profile.requests == []


def test_unsupported_profile_makes_zero_calls(starter, tmp_path):
    service, _, actor, workflow, grants, profile, ledger = make(starter, tmp_path, [reply(GOOD)])
    grants.grant = replace(grants.grant, profile_id="generated-unlisted-profile")
    _refused(service, actor, workflow, "authoring_profile_unsupported")
    grants.grant = replace(grants.grant, profile_id=PROFILE, schema_id="generated-other-schema")
    _refused(service, actor, workflow, "authoring_profile_unsupported")
    assert profile.requests == [] and counts(ledger) == (0, 0)


def test_exhausted_quota_makes_zero_calls(starter, tmp_path):
    service, _, actor, workflow, _, profile, ledger = make(
        starter, tmp_path, [reply(GOOD)], quota=0
    )
    _refused(service, actor, workflow, "authoring_quota_exhausted")
    assert profile.requests == [] and counts(ledger) == (0, 0)


def test_quota_is_shared_across_proposals_and_caps_retries(starter, tmp_path):
    bad = reply([branch([BELOW], "APPROVE", 99)])
    service, _, actor, workflow, _, profile, ledger = make(starter, tmp_path, [bad] * 2, quota=2)
    proposal = service.propose(actor, workflow, "generated-key-1")
    assert proposal.attempts == 2 and len(profile.requests) == 2  # stopped by quota, not retries
    _refused(service, actor, workflow, "authoring_quota_exhausted", key="generated-key-2")
    assert len(profile.requests) == 2 and counts(ledger) == (2, 1)


def test_revocation_before_first_call_and_between_attempts_refuses_without_partial_effect(
    starter, tmp_path
):
    bad = reply([branch([BELOW], "APPROVE", 99)])
    service, _, actor, workflow, grants, profile, ledger = make(starter, tmp_path, [bad] * 3)
    grants.revoke_after = 2  # initial check and first attempt check pass; the next one fails
    _refused(service, actor, workflow)
    assert len(profile.requests) == 1
    assert counts(ledger)[1] == 0  # no proposal stored


def test_revocation_between_inference_and_commit_stores_nothing(starter, tmp_path):
    service, _, actor, workflow, grants, profile, ledger = make(starter, tmp_path, [reply(GOOD)])
    grants.revoke_after = 2
    _refused(service, actor, workflow)
    assert len(profile.requests) == 1 and counts(ledger)[1] == 0


def test_grant_revision_change_mid_run_is_stale_and_refused(starter, tmp_path):
    service, _, actor, workflow, grants, _, ledger = make(
        starter,
        tmp_path,
        [
            lambda *_: (
                setattr(grants, "grant", replace(grants.grant, revision="grant-rev-2"))
                or ai.ProfileReply(reply(GOOD), 1)
            )
        ],
    )
    _refused(service, actor, workflow)
    assert counts(ledger)[1] == 0


@pytest.mark.parametrize("resource", sorted(ai.FORBIDDEN_RESOURCES) + ["anything-else"])
def test_authoring_tools_refuse_withheld_resources(starter, tmp_path, resource):
    def attempt(request, tools):
        tools.retrieve(resource)
        return ai.ProfileReply(reply(GOOD), 1)

    service, _, actor, workflow, _, _, ledger = make(starter, tmp_path, [attempt])
    _refused(service, actor, workflow, "authoring_resource_forbidden")
    assert counts(ledger)[1] == 0


def test_tools_return_only_exactly_cited_policy_spans(starter, tmp_path):
    seen = []

    def attempt(request, tools):
        seen.append(tools.retrieve(ai.SPAN_RESOURCE, request.spans[1].citation))
        with pytest.raises(ai.AuthoringError):
            tools.retrieve(ai.SPAN_RESOURCE, replace(request.spans[1].citation, byte_start=0))
        return ai.ProfileReply(reply(GOOD), 1)

    service, _, actor, workflow, _, _, _ = make(starter, tmp_path, [attempt])
    service.propose(actor, workflow, "generated-key-1")
    assert seen == ["If po is missing, REVIEW.\n"]


def test_proposal_is_never_applied_or_saved_as_a_guided_map(starter, tmp_path):
    service, source, actor, workflow, _, _, _ = make(starter, tmp_path, [reply(GOOD)])
    service.propose(actor, workflow, "generated-key-1")
    directory = source.path.parent
    assert not list(directory.glob("guided-map-*.json"))
    assert not (directory / "map-reviews.sqlite3").exists()
    assert not hasattr(service, "apply") and not hasattr(service, "save_draft")


def test_replay_returns_same_proposal_without_new_calls_after_restart(starter, tmp_path):
    service, source, actor, workflow, grants, profile, ledger = make(
        starter, tmp_path, [reply(GOOD)]
    )
    first = service.propose(actor, workflow, "generated-key-1")
    reopened = ai.AuthoringService(source, grants, Stub([]), FIELDS, ACTIONS, ledger)
    assert reopened.propose(actor, workflow, "generated-key-1") == first
    assert len(profile.requests) == 1 and counts(ledger) == (1, 1)


def test_replay_after_revocation_discloses_nothing(starter, tmp_path):
    service, _, actor, workflow, grants, _, _ = make(starter, tmp_path, [reply(GOOD)])
    service.propose(actor, workflow, "generated-key-1")
    grants.grant = None
    _refused(service, actor, workflow)


def test_changed_policy_source_refuses_replay_instead_of_returning_old_proposal(starter, tmp_path):
    service, source, actor, workflow, _, _, _ = make(starter, tmp_path, [reply(GOOD)])
    service.propose(actor, workflow, "generated-key-1")
    source.path.write_bytes(b"# Generated successor\n")
    with pytest.raises(PolicyError, match="policy_source_revision_changed"):
        service.source.read(actor, workflow)
    _refused(service, actor, workflow)  # stale policy source also refuses cleanly


def test_same_key_with_a_different_profile_intent_is_an_idempotency_conflict(starter, tmp_path):
    service, source, actor, workflow, grants, _, ledger = make(starter, tmp_path, [reply(GOOD)])
    service.propose(actor, workflow, "generated-key-1")
    other = Stub([reply(GOOD)], profile_id="generated-authoring-profile-b")
    grants.grant = replace(grants.grant, profile_id=other.profile_id)
    changed = ai.AuthoringService(source, grants, other, FIELDS, ACTIONS, ledger)
    _refused(changed, actor, workflow, "authoring_idempotency_conflict")
    assert other.requests == [] and counts(ledger) == (1, 1)


def test_crash_after_dispatch_counts_the_attempt_and_resumes_under_the_same_key(starter, tmp_path):
    bad = reply([branch([BELOW], "APPROVE", 99)])
    service, source, actor, workflow, grants, profile, ledger = make(
        starter, tmp_path, [Crash()], quota=4
    )
    with pytest.raises(Crash):
        service.propose(actor, workflow, "generated-key-1")
    assert counts(ledger) == (1, 0)  # the attempt was persisted before dispatch
    resumed = Stub([bad, reply(GOOD)])
    again = ai.AuthoringService(source, grants, resumed, FIELDS, ACTIONS, ledger)
    proposal = again.propose(actor, workflow, "generated-key-1")
    assert proposal.attempts == 3 and len(proposal.branches) == 3
    assert counts(ledger) == (3, 1)  # never more than the configured bound in total


def test_invalid_cost_report_fails_closed(starter, tmp_path):
    service, _, actor, workflow, _, _, ledger = make(
        starter, tmp_path, [lambda *_: ai.ProfileReply(reply(GOOD), -1)]
    )
    _refused(service, actor, workflow, "authoring_cost_invalid")
    assert counts(ledger)[1] == 0


def test_guided_route_continues_with_the_authoring_profile_unavailable(starter, tmp_path):
    service, source, actor, workflow, _, profile, _ = make(
        starter, tmp_path, [reply(GOOD)], grant=None
    )
    _refused(service, actor, workflow)
    guided = GuidedMapService(source, FIELDS, ACTIONS)
    preview = guided.preview(actor, workflow)
    assert [b.action for b in preview.branches] == ["REVIEW", "APPROVE", "MANUAL"]
    saved = guided.save_draft(actor, workflow, preview.revision)
    assert saved.revision == preview.revision
    assert preview.draft_action({"po": "x", "total": Decimal("10")}) == (
        "APPROVE",
        (preview.branches[1].citation,),
    )
    assert profile.requests == []
    assert FieldKind.NUMBER in FIELDS.values()


def test_reversed_comparison_direction_is_not_grounded_and_retries(starter, tmp_path):
    reversed_ = branch([{"field": "total", "operator": ">", "threshold": "250"}], "APPROVE", 4)
    wrong_edge = branch([{"field": "total", "operator": "<=", "threshold": "250"}], "APPROVE", 4)
    service, _, actor, workflow, _, profile, _ = make(
        starter, tmp_path, [reply([reversed_]), reply([wrong_edge]), reply(GOOD)]
    )
    proposal = service.propose(actor, workflow, "generated-key-1")
    assert profile.requests[1].prior_issue_codes == ("answer_unsupported",)
    assert profile.requests[2].prior_issue_codes == ("answer_unsupported",)
    assert proposal.attempts == 3 and len(proposal.branches) == 3 and proposal.issues == ()


def test_reversed_presence_is_not_grounded_and_word_operators_are(starter, tmp_path):
    raw = b"# Ordering\nIf po is missing, REVIEW.\nWhen total below 250, APPROVE.\n"
    flipped = branch([{"field": "po", "operator": "present"}], "REVIEW", 2)
    below = branch([BELOW], "APPROVE", 3)
    service, _, actor, workflow, _, _, _ = make(
        starter, tmp_path, [reply([flipped, below])] * 3, raw=raw
    )
    proposal = service.propose(actor, workflow, "generated-key-1")
    assert [b.action for b in proposal.branches] == ["APPROVE"]
    assert {i.code for i in proposal.issues} == {"answer_unsupported"}


def test_reentrant_proposal_under_another_key_cannot_exceed_the_grant_quota(starter, tmp_path):
    bad = reply([branch([BELOW], "APPROVE", 99)])
    holder = {}

    def nested(request, tools):
        # A second proposal runs completely inside the first one's profile call.
        holder["inner"] = holder["service"].propose(
            holder["actor"], holder["workflow"], "generated-key-B"
        )
        return ai.ProfileReply(bad, 1)

    service, _, actor, workflow, _, _, ledger = make(
        starter, tmp_path, [nested, bad, bad, bad, bad], quota=2
    )
    holder.update(service=service, actor=actor, workflow=workflow)
    proposal = service.propose(actor, workflow, "generated-key-A")
    assert counts(ledger)[0] <= 2  # attempts never exceed the grant quota
    assert holder["inner"].attempts == 1 and proposal.attempts == 1


def test_concurrent_reservations_never_exceed_quota_or_per_key_bound(starter, tmp_path):
    service, _, actor, workflow, _, _, ledger = make(starter, tmp_path, [], quota=3)
    results, lock = [], threading.Lock()

    def reserve(key):
        scope = (actor.tenant, "grant-rev-1", workflow, key)
        got = service._reserve(scope, 3)
        with lock:
            results.append(got)

    threads = [
        threading.Thread(target=reserve, args=(f"generated-key-{i % 4}",)) for i in range(12)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(isinstance(item, int) for item in results) == 3
    assert counts(ledger)[0] == 3
    assert {r for r in results if isinstance(r, str)} == {"quota"}


def test_ledger_failure_maps_to_a_fixed_code_not_a_raw_sqlite_error(starter, tmp_path):
    service, _, actor, workflow, _, profile, ledger = make(starter, tmp_path, [reply(GOOD)])
    with closing(sqlite3.connect(ledger)) as db:
        db.execute("DROP TABLE attempts")
    _refused(service, actor, workflow, "authoring_ledger_unavailable")
    assert profile.requests == []


def test_crash_during_dispatch_is_recorded_as_outcome_unknown_not_reserved(starter, tmp_path):
    service, _, actor, workflow, _, _, ledger = make(starter, tmp_path, [Crash()])
    with pytest.raises(Crash):
        service.propose(actor, workflow, "generated-key-1")
    with closing(sqlite3.connect(ledger)) as db:
        assert db.execute("SELECT outcome FROM attempts").fetchall() == [("dispatched",)]


def test_evidence_marker_is_derived_from_the_profile_not_a_constant(starter, tmp_path):
    service, source, actor, workflow, grants, _, ledger = make(starter, tmp_path, [reply(GOOD)])
    stand_in = service.propose(actor, workflow, "generated-key-1")
    assert (stand_in.stand_in, stand_in.evidence_tier) == (True, "generated")
    assert stand_in.external_gate == "real_inference_receipt_unsatisfied"
    real = Stub([reply(GOOD)], profile_id="generated-authoring-profile-c", stand_in=False)
    grants.grant = replace(grants.grant, profile_id=real.profile_id)
    other = ai.AuthoringService(source, grants, real, FIELDS, ACTIONS, ledger)
    proposal = other.propose(actor, workflow, "generated-key-2")
    assert (proposal.stand_in, proposal.evidence_tier) == (False, "unattested")
    assert proposal.external_gate == "real_inference_receipt_unsatisfied"
