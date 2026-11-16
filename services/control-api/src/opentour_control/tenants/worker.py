"""Recover the same persisted setup intent; never infer no effect from missing evidence."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Literal, Protocol

from opentour_control.auth.repository import lock_membership_observation

from .models import Operation, SetupError, advance_revision, server_time
from .provider import (
    MembershipObservation,
    OrganizationObservation,
    ProviderRejected,
    ProviderUnknown,
    ProvisioningProvider,
)
from .repository import Connection, ConnectionFactory, PostgresOrganizationRepository


class ProvisioningStore(Protocol):
    def load(self, operation_id: str) -> Operation: ...
    def claim(
        self, operation_id: str, stage: Literal["organization", "membership"]
    ) -> Operation | None: ...
    def unknown(self, operation_id: str, stage: Literal["organization", "membership"]) -> None: ...
    def rejected(self, operation_id: str, stage: Literal["organization", "membership"]) -> None: ...
    def bind(self, operation_id: str, observed: OrganizationObservation) -> None: ...
    def activate(self, operation_id: str, observed: MembershipObservation) -> None: ...


class PostgresProvisioningStore:
    """A distinct workload role, never an end-user connection or supplied scope flag."""

    def __init__(
        self, connect: ConnectionFactory, clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    ) -> None:
        self.connect, self.clock = connect, clock

    @contextmanager
    def _connection(self) -> Iterator[Connection]:
        with self.connect() as conn:
            role = conn.execute(
                "SELECT rolsuper,rolbypassrls,pg_has_role(current_user,"
                "'opentour_provisioner','MEMBER') AS allowed FROM pg_rol"
                "es WHERE rolname=current_user"
            ).fetchone()
            if role is None or role["rolsuper"] or role["rolbypassrls"] or not role["allowed"]:
                raise SetupError("DATABASE_ROLE_INVALID", 503)
            yield conn

    def load(self, operation_id: str) -> Operation:
        with self._connection() as conn:
            return PostgresOrganizationRepository._load(conn, operation_id)

    def metrics(self) -> dict[str, int | bool]:
        """Aggregate operational facts only; the alert never changes setup state."""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT count(*) FILTER (WHERE o.state NOT IN "
                "('active','cancelled','action_required')) AS pending_count, "
                "count(*) FILTER (WHERE p.stage_outcome IN ('dispatched','unknown')) "
                "AS unknown_count, min(p.created_at) FILTER (WHERE o.state NOT IN "
                "('active','cancelled','action_required')) AS pending_oldest, "
                "min(p.created_at) FILTER (WHERE p.stage_outcome IN ('dispatched','unknown')) "
                "AS unknown_oldest, max(p.created_at) AS newest "
                "FROM opentour_tenants.provisioning_operations p "
                "JOIN opentour_tenants.organizations o ON o.id=p.organization_id "
                "WHERE o.state NOT IN ('active','cancelled','action_required') "
                "OR p.stage_outcome IN ('dispatched','unknown')"
            ).fetchone()
        now = server_time(self.clock())
        if row is None:
            raise SetupError("DATABASE_UNAVAILABLE", 503)
        if row["newest"] is not None and row["newest"] > now:
            raise SetupError("CLOCK_UNAVAILABLE", 503)
        pending_age = (
            0
            if row["pending_oldest"] is None
            else int((now - row["pending_oldest"]).total_seconds())
        )
        unknown_age = (
            0
            if row["unknown_oldest"] is None
            else int((now - row["unknown_oldest"]).total_seconds())
        )
        return {
            "pending_count": row["pending_count"],
            "unknown_count": row["unknown_count"],
            "pending_max_age_seconds": pending_age,
            "unknown_max_age_seconds": unknown_age,
            "attention_required": max(pending_age, unknown_age) >= 900,
        }

    def _advance(
        self, conn: Connection, op: Operation, state: str, kind: str
    ) -> tuple[int, datetime]:
        rev, now = advance_revision(op.revision), server_time(self.clock())
        if now < op.updated_at:
            raise SetupError("CLOCK_UNAVAILABLE", 503)
        conn.execute(
            "UPDATE opentour_tenants.organizations SET state=%s,revi"
            "sion=%s,updated_at=%s WHERE id=%s",
            (state, rev, now, op.organization_id),
        )
        conn.execute(
            "UPDATE opentour_tenants.provisioning_operations SET updated_at=%s WHERE id=%s",
            (now, op.operation_id),
        )
        PostgresOrganizationRepository._event(
            conn, op.operation_id, op.organization_id, rev, kind, now
        )
        return rev, now

    def claim(
        self, operation_id: str, stage: Literal["organization", "membership"]
    ) -> Operation | None:
        with self._connection() as conn:
            op = PostgresOrganizationRepository._load(conn, operation_id, lock=True)
            if (
                op.state in {"active", "cancelled", "action_required"}
                or op.stage != stage
                or op.stage_outcome != "not_dispatched"
            ):
                return None
            _, now = self._advance(conn, op, op.state, "organization.progressed")
            conn.execute(
                "UPDATE opentour_tenants.provisioning_operations SET sta"
                "ge_outcome='dispatched',dispatch_started_at=%s,last_err"
                "or=NULL WHERE id=%s",
                (now, operation_id),
            )
            return PostgresOrganizationRepository._load(conn, operation_id)

    def _outcome(self, operation_id: str, stage: str, *, rejected: bool) -> None:
        with self._connection() as conn:
            op = PostgresOrganizationRepository._load(conn, operation_id, lock=True)
            if (
                op.stage != stage
                or op.stage_outcome not in {"dispatched", "unknown"}
                or op.state == "active"
            ):
                return  # A stale response cannot overwrite a newer observed stage.
            outcome = "rejected" if rejected else "unknown"
            state = "action_required" if rejected else "provider_outcome_unknown"
            if op.state == "cancelled":
                state = "cancelled"
            if op.stage_outcome == outcome and op.state == state:
                return
            self._advance(conn, op, state, "organization.progressed")
            conn.execute(
                "UPDATE opentour_tenants.provisioning_operations SET sta"
                "ge_outcome=%s,last_error=%s WHERE id=%s",
                (
                    outcome,
                    "PROVIDER_REJECTED" if rejected else "PROVIDER_OUTCOME_UNKNOWN",
                    operation_id,
                ),
            )

    def unknown(self, operation_id: str, stage: Literal["organization", "membership"]) -> None:
        self._outcome(operation_id, stage, rejected=False)

    def rejected(self, operation_id: str, stage: Literal["organization", "membership"]) -> None:
        self._outcome(operation_id, stage, rejected=True)

    def bind(self, operation_id: str, observed: OrganizationObservation) -> None:
        with self._connection() as conn:
            op = PostgresOrganizationRepository._load(conn, operation_id, lock=True)
            if (observed.correlation, observed.environment) != (
                op.correlation,
                op.provider_environment,
            ):
                raise SetupError("PROVIDER_IDENTITY_CONFLICT", 409)
            if op.provider_org_id is not None:
                if op.provider_org_id != observed.organization_id:
                    raise SetupError("PROVIDER_IDENTITY_CONFLICT", 409)
                return
            if op.stage != "organization" or op.stage_outcome not in {"dispatched", "unknown"}:
                raise SetupError("STATE_CONFLICT", 409)
            state = "cancelled" if op.state == "cancelled" else "identity_bound"
            self._advance(conn, op, state, "organization.progressed")
            conn.execute(
                "UPDATE opentour_tenants.organizations SET identity_org_id=%s WHERE id=%s",
                (observed.organization_id, op.organization_id),
            )
            conn.execute(
                "UPDATE opentour_tenants.provisioning_operations SET pro"
                "vider_org_id=%s,stage='membership',stage_outcome='not_d"
                "ispatched',last_error=NULL,dispatch_started_at=NULL WHE"
                "RE id=%s",
                (observed.organization_id, operation_id),
            )

    def activate(self, operation_id: str, observed: MembershipObservation) -> None:
        with self._connection() as conn:
            op = PostgresOrganizationRepository._load(conn, operation_id, lock=True)
            if (observed.organization_id, observed.subject) != (
                op.provider_org_id,
                op.creator_subject,
            ):
                raise SetupError("PROVIDER_IDENTITY_CONFLICT", 409)
            now = server_time(self.clock())
            if now < observed.checked_at or (now - observed.checked_at).total_seconds() > 30:
                raise SetupError("PROVIDER_OBSERVATION_STALE", 503, retryable=True)
            member = conn.execute(
                "SELECT * FROM opentour_tenants.memberships WHERE organi"
                "zation_id=%s AND issuer=%s AND subject=%s FOR UPDATE",
                (op.organization_id, op.creator_issuer, op.creator_subject),
            ).fetchone()
            if member is None:
                raise SetupError("MEMBERSHIP_STATE_UNAVAILABLE", 503)
            if op.state == "active":
                if (
                    op.provider_membership_id != observed.membership_id
                    or member["provider_membership_id"] != observed.membership_id
                ):
                    raise SetupError("PROVIDER_IDENTITY_CONFLICT", 409)
                return
            if op.stage != "membership" or op.stage_outcome not in {
                "not_dispatched",
                "dispatched",
                "unknown",
            }:
                raise SetupError("STATE_CONFLICT", 409)
            # Current provider denial wins over a late positive RPC. Lock ordering
            # remains organization -> membership -> identity observation.
            lock_membership_observation(conn, op.creator_issuer, observed.membership_id)
            denied = conn.execute(
                "SELECT status,locally_denied FROM opentour_identity.mem"
                "bership_observations WHERE issuer=%s AND membership_id="
                "%s",
                (op.creator_issuer, observed.membership_id),
            ).fetchone()
            removed = member["status"] == "removed" or (
                denied is not None and (denied["locally_denied"] or denied["status"] == "removed")
            )
            now = server_time(self.clock())
            if now < observed.checked_at or (now - observed.checked_at).total_seconds() > 30:
                raise SetupError("PROVIDER_OBSERVATION_STALE", 503, retryable=True)
            cancelled = op.state == "cancelled" or removed
            active = not cancelled and observed.status == "active"
            state = "cancelled" if cancelled else "active" if active else "action_required"
            _, now = self._advance(
                conn,
                op,
                state,
                "organization.cancelled"
                if cancelled
                else "organization.active"
                if active
                else "organization.progressed",
            )
            conn.execute(
                "UPDATE opentour_tenants.provisioning_operations SET pro"
                "vider_membership_id=%s,stage='activation',stage_outcome"
                "='observed',last_error=%s WHERE id=%s",
                (
                    observed.membership_id,
                    "SETUP_CANCELLED"
                    if cancelled
                    else None
                    if active
                    else "PROVIDER_MEMBERSHIP_NOT_ACTIVE",
                    operation_id,
                ),
            )
            conn.execute(
                "UPDATE opentour_tenants.memberships SET status=%s,provi"
                "der_membership_id=%s,updated_at=%s WHERE organization_i"
                "d=%s AND issuer=%s AND subject=%s",
                (
                    "removed" if cancelled else "active" if active else "provisional",
                    observed.membership_id,
                    now,
                    op.organization_id,
                    op.creator_issuer,
                    op.creator_subject,
                ),
            )


class ProvisioningWorker:
    """One bounded stage per invocation; queue206 owns delivery/retry scheduling.

    A claimed but unobserved effect is reconciled read-only forever until an exact
    result is established. A missing result does not authorize a second mutation.
    """

    def __init__(self, store: ProvisioningStore, provider: ProvisioningProvider) -> None:
        self.store, self.provider = store, provider

    def step(self, operation_id: str) -> Operation:
        op = self.store.load(operation_id)
        if op.provider_environment != self.provider.environment:
            raise SetupError("PROVIDER_CONFIGURATION_MISMATCH", 503)
        if op.state in {"active", "action_required"} or op.stage == "activation":
            return op
        cancelled = op.state == "cancelled"
        if cancelled and op.stage_outcome not in {"dispatched", "unknown"}:
            return op
        if op.stage == "organization":
            if op.stage_outcome in {"dispatched", "unknown"}:
                try:
                    found = self.provider.find_organization(op)
                    if found is not None:
                        self.store.bind(operation_id, found)
                    else:
                        self.store.unknown(operation_id, "organization")
                except ProviderUnknown:
                    self.store.unknown(operation_id, "organization")
            elif not cancelled:
                claimed = self.store.claim(operation_id, "organization")
                if claimed is not None:
                    try:
                        self.store.bind(operation_id, self.provider.create_organization(claimed))
                    except ProviderRejected:
                        self.store.rejected(operation_id, "organization")
                    except ProviderUnknown:
                        self.store.unknown(operation_id, "organization")
        elif op.stage == "membership":
            try:
                found_member = self.provider.find_membership(op)
            except ProviderUnknown:
                self.store.unknown(operation_id, "membership")
                return self.store.load(operation_id)
            if found_member is not None:
                # Do not reactivate a removed/inactive membership by issuing create.
                self.store.activate(operation_id, found_member)
            elif op.stage_outcome in {"dispatched", "unknown"}:
                self.store.unknown(operation_id, "membership")
            elif not cancelled:
                claimed = self.store.claim(operation_id, "membership")
                if claimed is not None:
                    try:
                        self.store.activate(operation_id, self.provider.create_membership(claimed))
                    except ProviderRejected:
                        self.store.rejected(operation_id, "membership")
                    except ProviderUnknown:
                        self.store.unknown(operation_id, "membership")
        return self.store.load(operation_id)
