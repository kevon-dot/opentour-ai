"""Generated issuer keys exercise the actual ES256 monthly report reader."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from opentour_cell.pilot import signed_records as sr
from opentour_cell.pilot.scoped_query_report import (
    REPORT_PURPOSE,
    REPORT_ROLE,
    open_signed_monthly_invoice_report,
)
from opentour_cell.pilot.workflow_benchmarks import BenchmarkScope
from test_signed_records import ENGAGEMENT, Kit, Signer, sign, stamp
from test_signed_records import kit as signer_kit  # noqa: F401

SCOPE = BenchmarkScope("tenant-sentinel", ENGAGEMENT, "workflow-sentinel")
REPORT_FILE = "invoice-report.json"


def _write(path: Path, record: bytes) -> None:
    path.write_bytes(record)
    path.chmod(0o600)


def test_signed_monthly_report_binds_issuer_window_and_corrections(
    signer_kit: Kit,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The serialized verifier writer owns the compiled role registration. This
    # fixture exercises the existing canonical verifier with the approved policy.
    if REPORT_PURPOSE not in sr.RECORD_PURPOSES:
        monkeypatch.setattr(sr, "RECORD_PURPOSES", sr.RECORD_PURPOSES | {REPORT_PURPOSE})
        monkeypatch.setitem(sr.REQUIRED_ROLES, REPORT_PURPOSE, frozenset({REPORT_ROLE}))
        monkeypatch.setitem(sr.REGISTRY_ROLES, REPORT_ROLE, frozenset({REPORT_PURPOSE}))
    issuer = Signer(REPORT_ROLE, [REPORT_PURPOSE])
    different_issuer = Signer(REPORT_ROLE, [REPORT_PURPOSE])
    directory = signer_kit.worker
    _write(
        directory / "pilot-issuer-registry.json",
        signer_kit.registry(keys=[*signer_kit.issuers(), issuer.entry(), different_issuer.entry()]),
    )
    at = datetime.now(UTC) - timedelta(minutes=1)
    end = at.date().replace(day=1)
    start = (end - timedelta(days=1)).replace(day=1)
    base = {
        "profile_version": "1.0",
        "tenant": SCOPE.tenant,
        "engagement_id": SCOPE.engagement_epoch,
        "workflow_ref": SCOPE.workflow_ref,
        "metric": "recurring_invoice_code_errors",
        "timezone": "UTC",
        "covered_from": start.isoformat(),
        "covered_until": end.isoformat(),
        "recurring_errors": 2,
        "verified_decisions": 12,
        "excluded_decisions": 3,
        "issued_at": stamp(-15),
        "not_after": stamp(3600),
    }

    def read(scope: BenchmarkScope = SCOPE, zone: str = "UTC"):
        return open_signed_monthly_invoice_report(
            directory, REPORT_FILE, scope, timezone=zone, at=at, custodian_key_id=issuer.key_id
        )

    path = directory / REPORT_FILE
    _write(path, sign(base, REPORT_PURPOSE, different_issuer))
    assert read() is None
    _write(path, sign(base, REPORT_PURPOSE, issuer))
    valid = read()
    assert valid is not None and valid[0].recurring_errors == 2
    assert valid[0].verified_decisions == 12 and valid[0].excluded_decisions == 3
    assert read(replace(SCOPE, workflow_ref="different-workflow")) is None
    assert read(replace(SCOPE, tenant="different-tenant")) is None
    assert read(replace(SCOPE, engagement_epoch="different-engagement")) is None
    assert read(zone="America/Los_Angeles") is None
    for invalid in (
        {"covered_until": start.isoformat()},
        {"verified_decisions": 1},
        {"issued_at": stamp(-300), "not_after": stamp(-60)},
        {"proposal_access": "allow"},
    ):
        _write(path, sign({**base, **invalid}, REPORT_PURPOSE, issuer))
        assert read() is None

    replacement = {**base, "issued_at": stamp(-5), "recurring_errors": 1}
    _write(path, sign(replacement, REPORT_PURPOSE, issuer))
    corrected = read()
    assert corrected is not None and corrected[0].recurring_errors == 1 and corrected[1] != valid[1]
    _write(path, sign(base, REPORT_PURPOSE, issuer))
    assert read() is None
    _write(path, sign(replacement, REPORT_PURPOSE, issuer))
    assert read() == corrected
    path.chmod(0o644)
    assert read() is None
