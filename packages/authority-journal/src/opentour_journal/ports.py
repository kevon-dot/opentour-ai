"""Pure authenticated authority ports shared by control and audit services.

These types carry resolved metadata, not authority by themselves. Concrete
resolvers must authenticate their canonical sources; no provider or HTTP adapter
is imported here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from opentour_journal.models import AppendIntent, AppendOutcome, ImmutableMetadataRef, MutationKind


@dataclass(frozen=True)
class ServicePrincipal:
    service_id: str
    authentication_ref: ImmutableMetadataRef

@dataclass(frozen=True)
class AuthorizedScope:
    service_id: str
    organization_id: str
    capability: Literal["append", "read", "enroll"]

@dataclass(frozen=True)
class ResolvedIntent:
    organization_id: str
    operation_id: str
    mutation_kind: MutationKind
    proposed_epoch: int
    proposed_authority_revision: str
    canonical_intent_digest: str
    scoped_intent_ref: ImmutableMetadataRef
    actor_ref: ImmutableMetadataRef

@dataclass(frozen=True)
class ReconciledTransaction:
    organization_id: str
    operation_id: str
    intent_content_sha256: str
    control_transaction_ref: ImmutableMetadataRef
    result: Literal["committed", "aborted"]
    resulting_epoch: int
    resulting_authority_revision: str

@dataclass(frozen=True)
class ApprovedRegistration:
    organization_id: str
    initial_epoch: int
    initial_authority_revision: str
    enrollment_ref: ImmutableMetadataRef

class AuthorityResolver(Protocol):
    """Authenticate source records, not caller booleans or raw provider claims.

    authorize checks current registered workload service/capability and organization.
    resolve_intent binds the durable reserved operation, actor and canonical intent.
    reconcile_transaction authenticates the actual SQL result/ref, including proven
    abort; timeout, missing row or unconfirmed commit must raise unavailable/pending.
    register resolves approved enrollment, refuses retired/tombstoned organizations.
    """

    def authorize(
        self,
        principal: ServicePrincipal,
        organization_id: str,
        capability: Literal["append", "read", "enroll"],
    ) -> AuthorizedScope: ...

    def resolve_intent(self, principal: ServicePrincipal, body: AppendIntent) -> ResolvedIntent: ...

    def reconcile_transaction(
        self,
        principal: ServicePrincipal,
        body: AppendOutcome,
        intent: AppendIntent,
    ) -> ReconciledTransaction: ...

    def registration(
        self,
        principal: ServicePrincipal,
        organization_id: str,
    ) -> ApprovedRegistration: ...
