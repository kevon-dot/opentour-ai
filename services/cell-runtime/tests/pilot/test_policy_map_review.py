"""Private Slack views for inspecting and confirming cited map branches."""

from opentour_cell.pilot import slack_views as views


def actions(view):
    return [
        (element["action_id"], element["value"])
        for block in view["blocks"]
        if block["type"] == "actions"
        for element in block["elements"]
    ]


def test_full_citation_paging_preserves_normalized_operative_lines():
    source = "# Rules\n" + "A clause with a continuation.\n" * 90 + "  - Hold\n"
    first, second = [
        views.map_clause("ctx", "policy-a", "revision-a", "rules", "1–92", source, index)
        for index in (0, 1)
    ]
    assert first["blocks"][2]["text"]["text"] + second["blocks"][2]["text"]["text"] == source
    assert ("ot_map_clause_page", "next") in actions(first)
    assert ("ot_map_clause_page", "previous") in actions(second)
    assert ("ot_map_clause_page", "next") not in actions(second)


def test_read_only_branch_table_has_citations_and_no_edit_controls():
    rows = [
        views.MapReviewRow(
            f"branch-{i}",
            "Purchase submitted",
            "total",
            "total < 250",
            "First match",
            "APPROVE",
            "No supported action",
            "Policy clause",
            "Proposed",
        )
        for i in range(6)
    ]
    first = views.decision_map(
        "ctx",
        "workflow-a",
        "map-a",
        rows,
        [],
        0,
        False,
        version=0,
        source_revision="source-a",
        draft_revision="draft-a",
    )
    last = views.decision_map(
        "ctx",
        "workflow-a",
        "map-a",
        rows,
        [],
        1,
        True,
        version=1,
        source_revision="source-a",
        draft_revision="draft-a",
    )
    assert [v for a, v in actions(first) if a == "ot_map_citation"] == [
        r.branch_ref for r in rows[:5]
    ]
    assert not any(a == "ot_map_edit" for a, _ in actions(first))
    assert ("ot_map_edit", "branch-5") in actions(last)
    assert ("ot_map_page", "previous") in actions(last)


def branch_values(
    view,
    *,
    disposition="accepted",
    reason="Checked the cited policy.",
    action=None,
    conditions=None,
):
    result = {}
    for block in view["blocks"]:
        if block.get("type") != "input":
            continue
        key = block["block_id"]
        element = block["element"]
        if key in {"action", "disposition"}:
            selected = (
                disposition
                if key == "disposition"
                else (action or element["initial_option"]["value"])
            )
            result[key] = {key: {"type": "static_select", "selected_option": {"value": selected}}}
        else:
            value = (
                reason
                if key == "reason"
                else (conditions if conditions is not None else element.get("initial_value"))
            )
            result[key] = {key: {"type": "plain_text_input", "value": value}}
    return result


def enter_map(h, monkeypatch, actor):
    from test_app_home import click, publish
    from test_reviewer_workflow import screen

    home = publish(h, monkeypatch, actor)
    assert any(v for a, v in actions(home) if a == "ot_app_map")
    click(h, home, "ot_app_map", actor)
    view = list(h.slack.screens)[-1]
    assert screen(h, view)["callback_id"] == "ot_map_review"
    return view


def prepare_map(h, monkeypatch, policy=None):
    import json
    from hashlib import sha256

    from test_guided_policy import PURCHASE_POLICY
    from test_intake_mcp import _sealed_starter, _write_private
    from test_reviewer_workflow import buttons, screen
    from test_slack_adapter import ACTOR, OTHER
    from test_starter_setup import open_starter

    policy = PURCHASE_POLICY if policy is None else policy
    directory = h.path.parent
    source = directory / "purchase.md"
    source.write_bytes(policy)
    source.chmod(0o600)
    selection = json.loads((directory / "selected.json").read_bytes())
    grant_path = directory / "starter-intake-grant-policy-scope.json"
    prior = json.loads(grant_path.read_bytes())
    original = _sealed_starter(
        selection,
        source_revision=sha256(policy).hexdigest(),
        setup_policy_revision=prior["grant"]["setup_policy_revision"],
    )
    _write_private(grant_path, original)
    view = open_starter(h, monkeypatch)
    h.action(view, "ot_starter_save", buttons(screen(h, view))["ot_starter_save"])
    draft = h.app._starter_setup(ACTOR).preview().draft
    assert draft
    workflow = draft["workflow_ref"]
    from opentour_cell.pilot.guided_policy import GuidedPolicySource
    from opentour_cell.pilot.intake_local import LocalStarterAuthority
    from test_map_review import stage_map_review

    stage_map_review(
        GuidedPolicySource(LocalStarterAuthority(directory / "selected.json"), source),
        workflow,
        grant_ref="owner-review-v1",
        editors={ACTOR: selection["owner_id"], OTHER: "second-owner"},
    )
    return workflow


pytest_plugins = ("test_starter_setup",)


def test_owner_reaches_all_three_cited_branches_and_confirms_via_emitted_forms(
    founder, monkeypatch
):
    import json

    from test_reviewer_workflow import screen
    from test_slack_adapter import ACTOR
    from test_starter_setup import effects

    h = founder
    prepare_map(h, monkeypatch)
    benchmark = h.path.parent / "workflow-benchmarks.sqlite3"
    original_benchmark = benchmark.read_bytes()
    view = enter_map(h, monkeypatch, ACTOR)
    branch_refs = [v for a, v in actions(screen(h, view)) if a == "ot_map_edit"]
    assert len(branch_refs) == 3
    priority = next(v for a, v in actions(screen(h, view)) if a == "ot_map_priority")
    h.action(view, "ot_map_priority", priority)
    assert "Apply the first matching rule." in screen(h, view)["blocks"][2]["text"]["text"]
    h.action(view, "ot_map_back", "back")
    clauses = []
    for ref in branch_refs:
        h.action(view, "ot_map_citation", ref)
        clause_view = screen(h, view)
        assert clause_view["callback_id"] == "ot_map_clause"
        clauses.append(clause_view["blocks"][2]["text"]["text"])
        h.action(view, "ot_map_back", "back")
        h.action(view, "ot_map_edit", ref)
        form = screen(h, view)
        assert form["callback_id"] == "ot_map_branch_save"
        h.form(view, branch_values(form))
        assert screen(h, view)["callback_id"] == "ot_map_review"
    assert [s.split(", ")[1].strip().rstrip(".") for s in clauses] == [
        "REVIEW",
        "APPROVE",
        "MANUAL",
    ]
    assert "Confirmed map" in json.dumps(screen(h, view))
    from decimal import Decimal

    service, principal = h.app._map_service(ACTOR)
    confirmed = service.preview(principal)
    families = [(None, "50"), (None, "250"), ("po", "249.99"), ("po", "250"), ("po", "250.01")]

    def actual(snapshot):
        results = [
            snapshot.expected_action({"po": po, "total": Decimal(total)}) for po, total in families
        ]
        assert all(support for _, support in results)
        return [choice for choice, _ in results]

    expected = ["REVIEW", "REVIEW", "APPROVE", "MANUAL", "MANUAL"]
    assert actual(confirmed) == expected
    for (po, total), decision in zip(families, expected, strict=True):
        _, refs = confirmed.expected_action({"po": po, "total": Decimal(total)})
        assert len(refs) == 1
        assert (
            service.citation(
                principal, expected_revision=confirmed.review_revision, citation_ref=refs[0]
            )
            .strip()
            .endswith(f", {decision}.")
        )
    h.action(view, "ot_map_edit", branch_refs[1])
    wrong = screen(h, view)
    h.form(
        view,
        branch_values(
            wrong,
            disposition="corrected",
            conditions="total < 350",
            reason="Generated wrong-threshold control.",
        ),
    )
    changed = service.preview(principal)
    assert changed.review_revision != confirmed.review_revision
    assert actual(changed) == ["REVIEW", "REVIEW", "APPROVE", "APPROVE", "APPROVE"]
    assert benchmark.read_bytes() == original_benchmark
    assert effects(h) == (0,) * 7


def test_stale_owner_gets_diff_then_explicit_reapply(founder, monkeypatch):
    import json

    from test_reviewer_workflow import screen
    from test_slack_adapter import ACTOR, OTHER
    from test_starter_setup import effects

    h = founder
    workflow = prepare_map(h, monkeypatch)
    view_a = enter_map(h, monkeypatch, ACTOR)
    view_b = enter_map(h, monkeypatch, OTHER)
    refs = [v for a, v in actions(screen(h, view_a)) if a == "ot_map_edit"]
    ref = refs[1]
    h.action(view_a, "ot_map_edit", ref, ACTOR)
    h.action(view_b, "ot_map_edit", ref, OTHER)
    b_input = branch_values(
        screen(h, view_b),
        disposition="corrected",
        conditions="total < 350",
        reason="Owner B proposes a 350 threshold.",
    )
    h.form(view_a, branch_values(screen(h, view_a)), ACTOR)
    service, a = h.app._map_service(ACTOR, workflow)
    after_a = service.preview(a)
    service_b, b = h.app._map_service(OTHER, workflow)
    expected_b_revision = service_b.preview(b).review_revision
    h.form(view_b, b_input, OTHER)
    stale = screen(h, view_b)
    assert stale["submit"]["text"] == "Reapply my edit"
    assert expected_b_revision in json.dumps(stale)
    assert service.preview(a).review_revision == after_a.review_revision
    h.form(view_b, b_input, OTHER)
    applied = service.preview(a)
    assert applied.review_revision != after_a.review_revision
    branch = next(b for b in applied.branches if b.branch_ref == ref)
    assert (branch.conditions[0].threshold, branch.disposition) == ("350", "corrected")
    assert effects(h) == (0,) * 7


def test_read_only_and_revoked_actor_cannot_edit_or_disclose_clause(founder, monkeypatch):
    import json

    from test_app_home import publish
    from test_intake_mcp import _write_private
    from test_reviewer_workflow import screen
    from test_slack_adapter import ACTOR
    from test_starter_setup import effects

    h = founder
    workflow = prepare_map(h, monkeypatch)
    reader = "UREADER"
    h.config["users"][reader] = dict(h.config["users"][ACTOR])
    h.slack.members.append(reader)
    h.save()
    view = enter_map(h, monkeypatch, reader)
    assert not any(a in {"ot_map_edit", "ot_map_assertions"} for a, _ in actions(screen(h, view)))
    ref = next(v for a, v in actions(screen(h, view)) if a == "ot_map_citation")
    service, principal = h.app._map_service(ACTOR, workflow)
    initial = service.preview(principal)
    h.action(view, "ot_map_edit", ref, reader)
    assert "ot_map_branch_save" not in json.dumps(screen(h, view))
    assert service.preview(principal).review_revision == initial.review_revision
    view = enter_map(h, monkeypatch, ACTOR)
    original_render = h.app.views.map_clause

    def withdraw(*args, **kwargs):
        layout = original_render(*args, **kwargs)
        _write_private(h.path.parent / "map-review-withdrawn-owner-review-v1.json", {})
        return layout

    monkeypatch.setattr(h.app.views, "map_clause", withdraw)
    h.action(view, "ot_map_citation", ref, ACTOR)
    assert "If po is missing" not in json.dumps(screen(h, view))
    assert not any(a == "ot_app_map" for a, _ in actions(publish(h, monkeypatch, ACTOR)))
    assert effects(h) == (0,) * 7


def test_equal_priority_question_has_one_explicit_order_form(founder, monkeypatch):
    from test_reviewer_workflow import screen
    from test_slack_adapter import ACTOR
    from test_starter_setup import effects

    h = founder
    preparation = b"# Exceptions\nIf total < 250, APPROVE.\nIf total < 250, REVIEW.\n"
    workflow = prepare_map(h, monkeypatch, preparation)
    view = enter_map(h, monkeypatch, ACTOR)
    for action, ref in list(actions(screen(h, view))):
        if action == "ot_map_edit":
            h.action(view, action, ref)
            h.form(view, branch_values(screen(h, view)))
    pending = screen(h, view)
    options = [v for a, v in actions(pending) if a == "ot_map_precedence"]
    assert len(options) == 1
    svc, principal = h.app._map_service(ACTOR, workflow)
    prior = svc.preview(principal)
    assert prior.can_set_precedence
    from decimal import Decimal

    assert prior.expected_action({"total": Decimal(50)})[0] is None
    h.action(view, "ot_map_precedence", options[0])
    form = screen(h, view)
    assert form["callback_id"] == "ot_map_precedence_save"
    refs = [r.branch_ref for r in reversed(prior.branches)]
    values = {
        f"rank_{i}": {f"rank_{i}": {"type": "static_select", "selected_option": {"value": ref}}}
        for i, ref in enumerate(refs)
    }
    values["reason"] = {
        "reason": {
            "type": "plain_text_input",
            "value": "Review takes precedence under the current owner policy.",
        }
    }
    h.form(view, values)
    saved = svc.preview(principal)
    assert saved.review_revision != prior.review_revision
    assert "Review takes precedence" in str(screen(h, view))
    assert not saved.can_set_precedence and saved.confirmed_ref
    assert saved.expected_action({"total": Decimal(50)})[0] == "REVIEW"
    assert effects(h) == (0,) * 7


def test_changed_source_requires_successor_and_flags_only_affected_branch(founder, monkeypatch):
    import json

    from test_guided_policy import PURCHASE_POLICY
    from test_reviewer_workflow import screen
    from test_slack_adapter import ACTOR, OTHER

    h = founder
    workflow = prepare_map(h, monkeypatch)
    first = enter_map(h, monkeypatch, ACTOR)
    for kind, ref in list(actions(screen(h, first))):
        if kind == "ot_map_edit":
            h.action(first, kind, ref)
            h.form(first, branch_values(screen(h, first)))
    old_service, owner = h.app._map_service(ACTOR, workflow)
    before = old_service.preview(owner)
    stale = enter_map(h, monkeypatch, OTHER)
    old_changed = next(r for r in before.branches if r.action == "APPROVE")
    h.action(stale, "ot_map_edit", old_changed.branch_ref, OTHER)
    old_edit = branch_values(screen(h, stale), disposition="corrected", conditions="total < 351")
    assert (
        prepare_map(h, monkeypatch, PURCHASE_POLICY.replace(b"total < 250", b"total < 100"))
        == workflow
    )
    new_service, owner = h.app._map_service(ACTOR, workflow)
    after = new_service.preview(owner)
    assert after.source_revision != before.source_revision
    assert before.confirmed_ref and after.confirmed_ref is None
    changed = next(r for r in after.branches if r.action == "APPROVE")
    assert changed.branch_ref in after.changed_branch_refs and not changed.confirmed
    assert all(
        r.confirmed and r.branch_ref not in after.changed_branch_refs
        for r in after.branches
        if r.action != "APPROVE"
    )
    current = enter_map(h, monkeypatch, ACTOR)
    assert json.dumps(screen(h, current)).count("Source changed") == 1
    h.form(stale, old_edit, OTHER)
    assert "Reapply my edit" not in json.dumps(screen(h, stale))
    assert new_service.preview(owner).review_revision == after.review_revision


def test_rebound_editor_cannot_reapply_previous_identitys_form(founder, monkeypatch):
    import json

    from test_reviewer_workflow import screen
    from test_slack_adapter import OTHER

    h = founder
    workflow = prepare_map(h, monkeypatch)
    old = enter_map(h, monkeypatch, OTHER)
    ref = next(v for k, v in actions(screen(h, old)) if k == "ot_map_edit")
    h.action(old, "ot_map_edit", ref, OTHER)
    values = branch_values(screen(h, old))
    grant_file = h.path.parent / "map-review-grant.json"
    grant = json.loads(grant_file.read_bytes())["grant"]
    grant["editors"][OTHER] = "replacement-owner"
    from test_map_review import sign_review

    sign_review(h.path.parent, grant)
    service, principal = h.app._map_service(OTHER, workflow)
    before = service.preview(principal)
    h.form(old, values, OTHER)
    assert "Reapply my edit" not in json.dumps(screen(h, old))
    assert service.preview(principal) == before
    assert enter_map(h, monkeypatch, OTHER)


def test_missing_or_expired_map_capability_denies_before_policy_read(founder, monkeypatch):
    import json

    from opentour_cell.pilot import map_review
    from test_app_home import publish
    from test_slack_adapter import ACTOR, OTHER

    h = founder
    prepare_map(h, monkeypatch)
    raw_reads = []
    reader = map_review._read_policy

    def watched(*args, **kwargs):
        raw_reads.append(True)
        return reader(*args, **kwargs)

    monkeypatch.setattr(map_review, "_read_policy", watched)
    assert ("ot_app_map", h.app._map_entry(ACTOR)) in actions(publish(h, monkeypatch, ACTOR))
    assert raw_reads == []
    grant_file = h.path.parent / "map-review-grant.json"
    grant = json.loads(grant_file.read_bytes())["grant"]
    for changes in (
        {"tenant": "foreign-tenant"},
        {"expires_at": 1},
        {"editors": {ACTOR: grant["owner_ref"]}},
    ):
        denied = dict(grant, **changes)
        from test_map_review import sign_review

        sign_review(h.path.parent, denied)
        assert not any(a == "ot_app_map" for a, _ in actions(publish(h, monkeypatch, OTHER)))
    assert not (h.path.parent / "map-reviews.sqlite3").exists()
    assert raw_reads == []


def test_unknown_clause_stays_unresolved_until_named_owner_supplies_rule(founder, monkeypatch):
    from decimal import Decimal

    from test_reviewer_workflow import screen
    from test_slack_adapter import ACTOR

    h = founder
    workflow = prepare_map(
        h, monkeypatch, b"# Ordering\nHandle unusual totals according to the local threshold.\n"
    )
    view = enter_map(h, monkeypatch, ACTOR)
    service, owner = h.app._map_service(ACTOR, workflow)
    original = service.preview(owner)
    assert original.expected_action({"total": Decimal(500)})[0] is None
    assert not any(a == "ot_map_precedence" for a, _ in actions(screen(h, view)))
    ref = next(v for a, v in actions(screen(h, view)) if a == "ot_map_edit")
    h.action(view, "ot_map_edit", ref)
    form = screen(h, view)
    options = next(
        b["element"]["options"] for b in form["blocks"] if b.get("block_id") == "disposition"
    )
    assert "accepted" not in {o["value"] for o in options}
    h.form(
        view,
        branch_values(
            form,
            disposition="corrected",
            action="MANUAL",
            conditions="total >= 450",
            reason="The named owner requires manual review for totals of 450 or more.",
        ),
    )
    result = service.preview(owner)
    assert result.expected_action({"total": Decimal(500)})[0] == "MANUAL"
    assert (
        result.branches[0].actor_ref == ACTOR
        and result.branches[0].source_support == "owner_assertion"
    )
    h.action(view, "ot_map_citation", ref)
    combined = " ".join(b.get("text", {}).get("text", "") for b in screen(h, view)["blocks"])
    assert "Handle unusual totals" in combined and "named owner requires manual review" in combined


def test_duplicate_confirm_payload_after_restart_creates_no_second_version(founder, monkeypatch):
    from test_reviewer_workflow import restart, screen
    from test_slack_adapter import ACTOR
    from test_starter_setup import effects

    h = founder
    workflow = prepare_map(h, monkeypatch)
    view = enter_map(h, monkeypatch, ACTOR)
    ref = next(v for a, v in actions(screen(h, view)) if a == "ot_map_edit")
    h.action(view, "ot_map_edit", ref)
    original = h.form(view, branch_values(screen(h, view)))
    service, principal = h.app._map_service(ACTOR, workflow)
    saved = service.preview(principal)
    restart(h)
    h.send("interactive", original)
    fresh, principal = h.app._map_service(ACTOR, workflow)
    assert fresh.preview(principal) == saved and saved.version == 1
    assert effects(h) == (0,) * 7


def open_assertions(h, monkeypatch, actor):
    from test_reviewer_workflow import screen

    view = enter_map(h, monkeypatch, actor)
    choice = next(v for key, v in actions(screen(h, view)) if key == "ot_map_assertions")
    h.action(view, "ot_map_assertions", choice, actor)
    assert screen(h, view)["callback_id"] == "ot_map_assertions_save"
    return view


def assertion_values(trigger, fallback, reason="The named owner described the purchase review."):
    return {
        "trigger": {"trigger": {"type": "plain_text_input", "value": trigger}},
        "fallback": {"fallback": {"type": "static_select", "selected_option": {"value": fallback}}},
        "reason": {"reason": {"type": "plain_text_input", "value": reason}},
    }


def test_owner_explicitly_sets_and_clears_trigger_and_fallback_without_dispatch(
    founder, monkeypatch
):
    from test_reviewer_workflow import restart, screen
    from test_slack_adapter import ACTOR
    from test_starter_setup import effects

    h = founder
    workflow = prepare_map(h, monkeypatch)
    service, owner = h.app._map_service(ACTOR, workflow)
    initial = service.preview(owner)
    assert initial.settings_trigger is None and initial.fallback_action is None
    view = open_assertions(h, monkeypatch, ACTOR)
    assert service.preview(owner) == initial
    benchmark = h.path.parent / "workflow-benchmarks.sqlite3"
    held = benchmark.read_bytes()
    h.form(view, assertion_values("A purchase request is submitted.", "MANUAL"))
    saved = service.preview(owner)
    assert (saved.settings_trigger, saved.fallback_action, saved.settings_actor_ref) == (
        "A purchase request is submitted.",
        "MANUAL",
        ACTOR,
    )
    assert saved.settings_support == "owner_assertion" and saved.settings_reason
    assert (
        saved.confirmed_ref is None
        and saved.expected_action({"po": "po", "total": None})[0] is None
    )
    restart(h)
    view = enter_map(h, monkeypatch, ACTOR)
    assert "A purchase request is submitted." in str(screen(h, view))
    assert "MANUAL" in str(screen(h, view)) and saved.settings_editor_ref in str(screen(h, view))
    for action, ref in list(actions(screen(h, view))):
        if action == "ot_map_edit":
            h.action(view, action, ref)
            h.form(view, branch_values(screen(h, view)))
    approved = service.preview(owner)
    assert approved.expected_action({"po": "po", "total": None}) == (
        "MANUAL",
        (approved.settings_ref,),
    )
    clear = open_assertions(h, monkeypatch, ACTOR)
    h.form(clear, assertion_values(None, "unresolved", "Unset the previously declared assertions."))
    cleared = service.preview(owner)
    assert cleared.settings_trigger is None and cleared.fallback_action is None
    assert cleared.expected_action({"po": "po", "total": None})[0] is None
    assert benchmark.read_bytes() == held and effects(h) == (0,) * 7


def test_owner_assertion_stale_diff_requires_reapply_and_foreign_choice_has_zero_effect(
    founder, monkeypatch
):
    from test_reviewer_workflow import screen
    from test_slack_adapter import ACTOR, OTHER

    h = founder
    workflow = prepare_map(h, monkeypatch)
    a = open_assertions(h, monkeypatch, ACTOR)
    b = open_assertions(h, monkeypatch, OTHER)
    h.form(a, assertion_values("Owner A trigger", "REVIEW"), ACTOR)
    service, owner = h.app._map_service(ACTOR, workflow)
    a_saved = service.preview(owner)
    proposal = assertion_values("Owner B trigger", "MANUAL")
    h.form(b, proposal, OTHER)
    diff = screen(h, b)
    assert diff["submit"]["text"] == "Reapply my edit"
    assert "Owner A trigger" in str(diff) and "Owner B trigger" in str(diff)
    assert service.preview(owner) == a_saved
    h.form(b, proposal, OTHER)
    b_saved = service.preview(owner)
    assert b_saved.settings_trigger == "Owner B trigger" and b_saved.settings_actor_ref == OTHER
    forged = open_assertions(h, monkeypatch, ACTOR)
    h.form(forged, assertion_values("Forged", "EXTERNAL_ACTION"))
    assert service.preview(owner) == b_saved


def test_trigger_fallback_assertion_requires_current_source_on_save(founder, monkeypatch):
    from test_guided_policy import PURCHASE_POLICY
    from test_reviewer_workflow import screen
    from test_slack_adapter import ACTOR

    h = founder
    workflow = prepare_map(h, monkeypatch)
    first = open_assertions(h, monkeypatch, ACTOR)
    h.form(first, assertion_values("Purchase request accepted", "MANUAL"))
    service, owner = h.app._map_service(ACTOR, workflow)
    old = service.preview(owner)
    assert old.settings_ref
    pending = open_assertions(h, monkeypatch, ACTOR)
    assert prepare_map(h, monkeypatch, PURCHASE_POLICY.replace(b"250", b"200")) == workflow
    service, owner = h.app._map_service(ACTOR, workflow)
    current = service.preview(owner)
    assert current.source_revision != old.source_revision
    assert (
        current.settings_trigger is None
        and current.fallback_action is None
        and not current.settings_ref
    )
    h.form(pending, assertion_values("Old source trigger", "APPROVE"))
    assert "Reapply my edit" not in str(screen(h, pending))
    assert service.preview(owner) == current
    current_view = enter_map(h, monkeypatch, ACTOR)
    assert "Purchase request accepted" not in str(screen(h, current_view))
