"""Reconcile the prior-month query with a current custodian-verified aggregate."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from opentour_cell.pilot.scoped_query_plan import WorkflowMetric
from opentour_cell.pilot.workflow_benchmarks import BenchmarkScope


@dataclass(frozen=True, slots=True)
class MonthlyInvoiceReport:
    start: date
    end: date
    timezone: str
    recurring_errors: int
    verified_decisions: int
    excluded_decisions: int


def reconcile_monthly_invoice_report(
    scope: BenchmarkScope,
    *,
    timezone: str,
    at: datetime,
    verified_report: Mapping[str, object],
) -> MonthlyInvoiceReport | None:
    """Validate exact coverage after the caller verifies current grant and report custody.

    Dates denote the complete interval in the tenant's configured reporting zone.
    An absent or incomplete report does not establish zero cases.
    """
    if at.utcoffset() is None:
        return None
    try:
        end = at.astimezone(ZoneInfo(timezone)).date().replace(day=1)
        start = (end - timedelta(days=1)).replace(day=1)
    except (ZoneInfoNotFoundError, ValueError, OverflowError):
        return None
    expected = {
        "tenant": scope.tenant,
        "engagement_id": scope.engagement_epoch,
        "workflow_ref": scope.workflow_ref,
        "metric": WorkflowMetric.RECURRING_INVOICE_CODE_ERRORS.value,
        "timezone": timezone,
        "covered_from": start.isoformat(),
        "covered_until": end.isoformat(),
    }
    if any(verified_report.get(field) != value for field, value in expected.items()):
        return None
    errors = verified_report.get("recurring_errors")
    verified = verified_report.get("verified_decisions")
    excluded = verified_report.get("excluded_decisions")
    if (
        type(errors) is not int
        or type(verified) is not int
        or type(excluded) is not int
        or not 0 <= errors <= verified < 2**31
        or not 0 <= excluded < 2**31
    ):
        return None
    return MonthlyInvoiceReport(start, end, timezone, errors, verified, excluded)
