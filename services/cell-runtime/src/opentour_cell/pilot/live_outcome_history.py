"""Authorized comparison of an archived verification snapshot with current counts."""

import json
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from opentour_cell.pilot.intake_mcp import IntakeError, IntakePrincipal, require_ref
from opentour_cell.pilot.live_authority import PrivateLiveAuthority
from opentour_cell.pilot.live_evidence import LiveEvidence, LiveSnapshot


def _require(value: object) -> None:
    if not value:
        raise IntakeError("outcome_history_unavailable")


@dataclass(frozen=True)
class HistoryScope:
    tenant: str
    workflow_ref: str
    engagement_epoch: str
    dataset_ref: str
    dataset_revision: str


@dataclass(frozen=True)
class HistoryWindow:
    """Inclusive civil dates in the explicitly selected IANA time zone."""

    first_day: date
    last_day: date
    timezone: str


@dataclass(frozen=True)
class SnapshotCounts:
    sequence: int
    evidence_revision: str
    decisions: int
    verified: int
    pending: int
    last_ingested_at: int | None
    last_ingestion_in_window: bool | None


@dataclass(frozen=True)
class OutcomeHistory:
    scope: HistoryScope
    window: HistoryWindow
    historical: SnapshotCounts
    current: SnapshotCounts
    # Snapshots expose ingestion time, not an independent outcome's effective
    # time, individual actor or finality revision. Do not infer any of these.
    detail_status: str = "outcome_event_detail_unavailable"


def compare_snapshots(
    directory: Path,
    actor: IntakePrincipal,
    scope: HistoryScope,
    window: HistoryWindow,
    *,
    historical_sequence: int,
    historical_evidence_revision: str,
) -> OutcomeHistory:
    """Read only the two identified workflow projections under current inspection rights."""
    _require(actor.tenant == scope.tenant)
    for value in (
        scope.workflow_ref,
        scope.engagement_epoch,
        scope.dataset_ref,
        scope.dataset_revision,
    ):
        require_ref(value)
    _require(type(historical_sequence) is int and historical_sequence > 0)
    _require(
        isinstance(historical_evidence_revision, str)
        and re.fullmatch("[a-f0-9]{64}", historical_evidence_revision)
    )
    _require(
        type(window.first_day) is date
        and type(window.last_day) is date
        and window.first_day <= window.last_day
    )
    try:
        zone = ZoneInfo(window.timezone)
    except (TypeError, ValueError, ZoneInfoNotFoundError) as exc:
        raise IntakeError("outcome_history_unavailable") from exc

    def counts(snap: LiveSnapshot) -> SnapshotCounts:
        _require(
            (
                snap.tenant,
                snap.workflow_ref,
                snap.engagement_epoch,
                snap.dataset_ref,
                snap.dataset_revision,
            )
            == (
                scope.tenant,
                scope.workflow_ref,
                scope.engagement_epoch,
                scope.dataset_ref,
                scope.dataset_revision,
            )
        )
        _require(snap.verifier_bound is True and snap.disputed == 0 and snap.quarantined == 0)
        _require(
            all(
                type(n) is int and n >= 0
                for n in (snap.sequence, snap.decisions, snap.verified, snap.pending)
            )
        )
        _require(snap.verified + snap.pending == snap.decisions)
        _require(
            isinstance(snap.evidence_revision, str)
            and re.fullmatch("[a-f0-9]{64}", snap.evidence_revision)
        )
        when = snap.last_ingested_at
        _require(when is None or (type(when) is int and when >= 0))
        try:
            local_day = None if when is None else datetime.fromtimestamp(when, zone).date()
        except (ValueError, OSError, OverflowError) as exc:
            raise IntakeError("outcome_history_unavailable") from exc
        return SnapshotCounts(
            snap.sequence,
            snap.evidence_revision,
            snap.decisions,
            snap.verified,
            snap.pending,
            when,
            None if local_day is None else window.first_day <= local_day <= window.last_day,
        )

    authority = PrivateLiveAuthority(directory, scope.tenant, scope.workflow_ref)
    live = LiveEvidence(directory, authority)
    with authority.guard(actor, scope.workflow_ref) as bound:
        _require(
            bound.can_inspect
            and (bound.engagement_epoch, bound.dataset_ref, bound.dataset_revision)
            == (scope.engagement_epoch, scope.dataset_ref, scope.dataset_revision)
        )
        _require(any(g.kind == "independent_outcome" for g in bound.grants))
        with live._db() as db:
            row = db.execute(
                "SELECT body FROM live_snapshots WHERE tenant=? AND workflow=? "
                "AND epoch=? AND sequence=?",
                (scope.tenant, scope.workflow_ref, scope.engagement_epoch, historical_sequence),
            ).fetchone()
            _require(row is not None)
            try:
                archived = json.loads(row[0])
            except (TypeError, ValueError, RecursionError) as exc:
                raise IntakeError("outcome_history_unavailable") from exc
            _require(isinstance(archived, dict))
            seal = archived.pop("archive_authentication", None)
            _require(set(archived) == {"grants", "snapshot", "slack_verifiers"})
            _require(isinstance(archived["grants"], list) and bool(archived["grants"]))
            _require(isinstance(archived["snapshot"], dict))
            _require(set(archived["snapshot"]) == set(LiveSnapshot.__dataclass_fields__))
            try:
                authority._verify_snapshot_archive(archived, seal)
            except (TypeError, ValueError, RecursionError) as exc:
                raise IntakeError("outcome_history_unavailable") from exc
            _require(archived["slack_verifiers"] == dict(bound.slack_verifiers))
            _require(
                all(g in [asdict(current) for current in bound.grants] for g in archived["grants"])
            )
            historical = counts(LiveSnapshot(**archived["snapshot"]))
        _require(
            historical.sequence == historical_sequence
            and historical.evidence_revision == historical_evidence_revision
        )
        current = counts(live.snapshot(actor, scope.workflow_ref))
        _require(current.sequence >= historical.sequence)
        return OutcomeHistory(scope, window, historical, current)
