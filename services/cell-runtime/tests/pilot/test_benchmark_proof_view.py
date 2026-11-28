"""Generated managed run through the private Slack evidence controls."""

from test_managed_slack import setup_workflow
from test_reviewer_workflow import buttons, screen, text
from test_status_questions import counts


def test_proof_calculation_audit_and_return_are_scoped_and_read_only(tmp_path):
    h, _, _, _ = setup_workflow(tmp_path, "rule")
    view = h.launch()

    def click(action):
        h.action(view, action, buttons(screen(h, view))[action])

    click("ot_evidence")
    before = counts(h)
    click("ot_bench_proof")
    assert screen(h, view)["callback_id"] == "ot_bench_proof_view"
    assert "Benchmark approval unavailable" in text(h, view)
    click("ot_bench_calc")
    assert "Incumbent · routed to review" in text(h, view)
    assert "Candidate · routed to review" in text(h, view)
    click("ot_bench_audit")
    assert "Evidence digest" in text(h, view)
    click("ot_evidence")
    assert screen(h, view)["callback_id"] == "ot_evidence_view"
    assert counts(h) == before


def test_revoked_reviewer_cannot_reuse_current_calculation_button(tmp_path):
    from test_slack_adapter import ACTOR

    h, _, _, _ = setup_workflow(tmp_path, "rule")
    view = h.launch()
    for action in ("ot_evidence", "ot_bench_proof"):
        h.action(view, action, buttons(screen(h, view))[action])
    revision = buttons(screen(h, view))["ot_bench_calc"]
    before = counts(h)
    h.config["users"][ACTOR]["roles"] = ["inspect"]
    h.save()
    h.action(view, "ot_bench_calc", revision)
    assert "Incumbent · routed to review" not in text(h, view)
    assert screen(h, view)["callback_id"] != "ot_bench_proof_view"
    assert counts(h) == before


def test_aggregate_member_gets_limitation_and_same_scope_return(tmp_path):
    from test_slack_adapter import OTHER

    h, _, _, _ = setup_workflow(tmp_path, "rule")
    h.config["users"][OTHER].update(datasets=["generated-repair"], roles=["inspect"])
    h.save()
    view = h.launch(actor=OTHER)
    for action in ("ot_evidence", "ot_bench_proof"):
        h.action(view, action, buttons(screen(h, view))[action], actor=OTHER)
    assert "No benchmark calculation is available" in text(h, view)
    assert set(buttons(screen(h, view))) == {"ot_evidence", "ot_home"}
