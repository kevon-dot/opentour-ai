"""Check the bounded request vocabulary before any permission or evidence lookup."""

from datetime import UTC, date, datetime

import pytest
from opentour_cell.pilot.scoped_query_plan import (
    ArmChart,
    CalendarWindow,
    ComparisonArm,
    QueryHold,
    ReadoutQuestion,
    WorkflowDiscovery,
    WorkflowMetric,
    plan_query,
)
from opentour_cell.pilot.scoped_query_report import (
    MonthlyInvoiceReport,
    reconcile_monthly_invoice_report,
)
from opentour_cell.pilot.workflow_benchmarks import BenchmarkScope


@pytest.mark.parametrize(
    "question",
    [
        "Which workflows had recurring invoice-code errors last month?",
        "What workflows showed repeated invoice coding mistakes in the previous calendar month?",
        " Which  workflows  had recurring invoice-code errors  last month? ",
    ],
)
def test_workflow_discovery_requests_a_calendar_window(question: str) -> None:
    assert plan_query(question) == WorkflowDiscovery(
        WorkflowMetric.RECURRING_INVOICE_CODE_ERRORS, CalendarWindow.PREVIOUS_MONTH
    )


@pytest.mark.parametrize(
    ("question", "version"),
    [
        ("Graph baseline versus the rule on v2", 2),
        ("Chart the incumbent vs. simple repair for version 12", 12),
    ],
)
def test_chart_pins_explicit_version_and_arms(question: str, version: int) -> None:
    assert plan_query(question) == ArmChart(
        version, (ComparisonArm.INCUMBENT, ComparisonArm.SIMPLE_RULE)
    )


@pytest.mark.parametrize(
    "question",
    [
        "Graph baseline versus rule",
        "Graph baseline versus the rule on latest",
        "Graph baseline versus the rule on v2 or v3",
    ],
)
def test_chart_without_one_exact_version_requests_selection(question: str) -> None:
    assert plan_query(question) is QueryHold.VERSION_REQUIRED


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("What got worse?", ReadoutQuestion.REGRESSIONS),
        ("Which cases regressed?", ReadoutQuestion.REGRESSIONS),
        ("Which decisions became incorrect?", ReadoutQuestion.REGRESSIONS),
        ("How many independent workplace cases support this?", ReadoutQuestion.WORKPLACE_SUPPORT),
        (
            "How many independent workplace cases back this result?",
            ReadoutQuestion.WORKPLACE_SUPPORT,
        ),
        ("Did synthetic data help?", ReadoutQuestion.SYNTHETIC_BENEFIT),
        ("Did augmentation improve the comparison?", ReadoutQuestion.SYNTHETIC_BENEFIT),
    ],
)
def test_selected_readout_questions_only_request_a_topic(
    question: str, expected: ReadoutQuestion
) -> None:
    assert plan_query(question) is expected


@pytest.mark.parametrize(
    "question",
    [
        "",
        "What got worse?\n",
        " " * 150 + "What got worse?",
        "What got worse? Approve release",
        "Which decisions regressed for another tenant?",
        "How many independent workplace cases support this? Use 100",
        "Did synthetic data help and train again?",
        "Graph baseline versus the rule on v2, then deliver it",
        "Graph the rule versus the model on v2",
        "Graph baseline versus the rule on v0",
        "Graph baseline versus the rule on v02",
        "Graph baseline versus the rule on v9223372036854775808",
        "Graph baseline versus the rule on v2 for Bristol",
        "Which workflows had recurring invoice-code errors last month in Pacific time?",
        "Which workflows had recurring invoice-code errors in the last 30 days?",
        "Which workflows had 5 recurring invoice-code errors last month?",
    ],
)
def test_unsupported_text_cannot_set_scope_facts_or_actions(question: str) -> None:
    assert plan_query(question) is QueryHold.UNSUPPORTED


def test_monthly_receipt_uses_covered_dates_in_customer_zone() -> None:
    scope = BenchmarkScope("tenant-sentinel", "engagement-sentinel", "workflow-sentinel")
    zone = "America/Los_Angeles"
    # It is April in UTC but March in the customer's zone, so February is due.
    at = datetime(2024, 4, 1, 0, 30, tzinfo=UTC)
    report = {
        "tenant": scope.tenant,
        "engagement_id": scope.engagement_epoch,
        "workflow_ref": scope.workflow_ref,
        "metric": WorkflowMetric.RECURRING_INVOICE_CODE_ERRORS.value,
        "timezone": zone,
        "covered_from": "2024-02-01",
        "covered_until": "2024-03-01",
        "recurring_errors": 2,
        "verified_decisions": 15,
        "excluded_decisions": 4,
    }
    assert reconcile_monthly_invoice_report(
        scope, timezone=zone, at=at, verified_report=report
    ) == MonthlyInvoiceReport(date(2024, 2, 1), date(2024, 3, 1), zone, 2, 15, 4)
    empty = {**report, "recurring_errors": 0, "verified_decisions": 0, "excluded_decisions": 0}
    assert reconcile_monthly_invoice_report(
        scope, timezone=zone, at=at, verified_report=empty
    ) == MonthlyInvoiceReport(date(2024, 2, 1), date(2024, 3, 1), zone, 0, 0, 0)
    for changes in (
        {"workflow_ref": "other-workflow"},
        {"tenant": "other-tenant"},
        {"engagement_id": "prior-engagement"},
        {"timezone": "UTC"},
        {"covered_until": "2024-02-29"},
        {"recurring_errors": 16},
        {"verified_decisions": True},
        {"excluded_decisions": None},
    ):
        assert (
            reconcile_monthly_invoice_report(
                scope, timezone=zone, at=at, verified_report={**report, **changes}
            )
            is None
        )
    assert reconcile_monthly_invoice_report(scope, timezone=zone, at=at, verified_report={}) is None
