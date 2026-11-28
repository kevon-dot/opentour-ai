"""Reconcile the prior-month query with a current custodian-verified aggregate."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from opentour_cell.pilot.operating_point import digest
from opentour_cell.pilot.repair_bundle import _read
from opentour_cell.pilot.repair_workflow import _name
from opentour_cell.pilot.scoped_query_plan import WorkflowMetric
from opentour_cell.pilot.workflow_benchmarks import BenchmarkScope

REPORT_PURPOSE = "opentour.pilot.monthly-invoice-report.v1"
REPORT_ROLE = "workflow_data_custodian"
_REPORT_FIELDS = {
    "profile_version",
    "tenant",
    "engagement_id",
    "workflow_ref",
    "metric",
    "timezone",
    "covered_from",
    "covered_until",
    "recurring_errors",
    "verified_decisions",
    "excluded_decisions",
    "issued_at",
    "not_after",
}


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


def open_signed_monthly_invoice_report(
    directory: Path,
    publication_file: str,
    scope: BenchmarkScope,
    *,
    timezone: str,
    at: datetime,
    custodian_key_id: str,
) -> tuple[MonthlyInvoiceReport, str] | None:
    """Read an issuer-signed report after the caller checks separate viewer access.

    The launch pins the issuer key for this workflow. The root-signed pilot registry
    verifies the purpose, current role, key and time independently of that pin.
    """
    try:
        from opentour_journal.crypto import canonical, sha256
        from opentour_journal.models import JournalError, strict_json

        from opentour_cell.pilot.signed_records import (
            MAX_CONFIG_BYTES,
            MonotonicRecord,
            PilotRecordRefused,
            PilotVerifier,
            TrustStateStore,
        )
    except ImportError:
        return None

    try:
        registry_path = directory / "pilot-issuer-registry.json"
        report_path = directory / _name(publication_file)
        registry = _read(registry_path, MAX_CONFIG_BYTES)
        raw_report = _read(report_path, MAX_CONFIG_BYTES)
        payload = PilotVerifier(registry).verify(raw_report, REPORT_PURPOSE)
        if payload.keys() != _REPORT_FIELDS or payload["profile_version"] != "1.0":
            return None
        # The verifier authenticated the only allowed issuer; its key must also
        # be the one pinned to the exact workflow by the private launch.
        envelope = strict_json(raw_report)
        if not isinstance(envelope, dict):
            return None
        signer = envelope["signatures"][0]
        if signer["signer_role"] != REPORT_ROLE or signer["key_id"] != custodian_key_id:
            return None
        report = reconcile_monthly_invoice_report(
            scope, timezone=timezone, at=at, verified_report=payload
        )
        issued_at = payload["issued_at"]
        if report is None or not isinstance(issued_at, str):
            return None
        issue_time = datetime.fromisoformat(issued_at)
        if issue_time.astimezone(ZoneInfo(timezone)).date() < report.end:
            return None
        revision = sha256(canonical(payload))
        sequence = (issue_time - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(milliseconds=1)
        floor = MonotonicRecord(
            "monthly_invoice_report:"
            + digest(
                [scope.tenant, scope.engagement_epoch, scope.workflow_ref, report.start.isoformat()]
            ),
            sequence,
            {"payload_sha256": revision},
        )
        trust = TrustStateStore()
        with trust.locked():
            if (
                _read(registry_path, MAX_CONFIG_BYTES) != registry
                or _read(report_path, MAX_CONFIG_BYTES) != raw_report
            ):
                return None
            trust.advance(floor, lambda _previous, _proposed: None)
        return report, revision
    except (OSError, KeyError, ValueError, JournalError, PilotRecordRefused):
        return None
