"""Bounded, contiguous replay. A verified frontier is a point in time, never a lease."""

from __future__ import annotations

import math
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from opentour_journal.crypto import (
    FRONTIER_PURPOSE,
    JournalVerifier,
    canonical,
    genesis_digest,
    model_digest,
    sha256,
    transcript,
)
from opentour_journal.models import (
    MAX_MANIFEST_BYTES,
    MAX_REPLAY_ENTRIES,
    AppendIntent,
    AppendOutcome,
    ExactEntryResponse,
    HeadState,
    JournalError,
    JournalTelemetry,
    LiveHeadEnvelope,
    MetadataRef,
    MutationKind,
    ReplayCheckpoint,
    ReplayManifest,
    ReplayObservation,
    VerifiedFrontierProof,
    emit_metric,
)


class JournalReader(Protocol):
    """Authenticated, bounded reads from the independently configured live service."""

    def read_live_head(self, organization_id: str, nonce: str) -> LiveHeadEnvelope: ...

    def read_entry(self, organization_id: str, sequence: int) -> ExactEntryResponse: ...


class CheckpointAuthority(Protocol):
    """Independent retained checkpoint authority; never the restored control database.

    The production composition must authenticate its immutable record and approval
    before returning it. Caller request fields cannot supply or override this port.
    """

    def load(self, organization_id: str) -> ReplayCheckpoint: ...


class EvidenceWriter(Protocol):
    """Persist exact immutable control-metadata bytes, returning their actual reference."""

    def write(self, organization_id: str, kind: str, data: bytes) -> MetadataRef: ...


@dataclass(frozen=True)
class RestrictiveInclusion:
    operation_id: str
    intent_sequence: int
    intent_chain_sha256: str
    outcome_sequence: int
    outcome_chain_sha256: str


_VERIFIED = object()


@dataclass(frozen=True, init=False)
class VerifiedAuthorityFrontier:
    """Only replay constructs this object; persisted proof bytes must be replayed again.

    Consumers still serialize issuance against this head and the durable current
    operation. No field here proves cell enforcement, generation recovery or grants.
    """

    head: HeadState
    proof_ref: MetadataRef
    unresolved_kind: MutationKind | None
    observations: tuple[ReplayObservation, ...]

    def __init__(
        self,
        seal: object,
        head: HeadState,
        proof_ref: MetadataRef,
        unresolved_kind: MutationKind | None,
        observations: tuple[ReplayObservation, ...],
    ) -> None:
        if seal is not _VERIFIED:
            raise JournalError("RECOVERY_HOLD")
        object.__setattr__(self, "head", head)
        object.__setattr__(self, "proof_ref", proof_ref)
        object.__setattr__(self, "unresolved_kind", unresolved_kind)
        object.__setattr__(self, "observations", observations)

    def require_resolved(self) -> None:
        if self.head.unresolved_operation_id is not None:
            raise JournalError("AUTHORITY_OPERATION_PENDING")

    def restrictive_inclusion(
        self,
        operation_id: str,
        intent_sequence: int,
        intent_digest: str,
    ) -> RestrictiveInclusion:
        """Exact historical inclusion only; sealed cell effect proofs remain mandatory."""
        intent: ReplayObservation | None = None
        for observation in self.observations:
            body, entry = observation.body, observation.entry
            if isinstance(body, AppendIntent) and (
                body.operation_id == operation_id
                and entry.sequence == intent_sequence
                and entry.chain_sha256 == intent_digest
                and body.mutation_kind in {"restrict", "tombstone"}
            ):
                intent = observation
            elif isinstance(body, AppendOutcome) and (
                intent is not None
                and body.operation_id == operation_id
                and body.intent_sequence == intent_sequence
                and body.result == "committed"
            ):
                return RestrictiveInclusion(
                    operation_id,
                    intent_sequence,
                    intent_digest,
                    entry.sequence,
                    entry.chain_sha256,
                )
        raise JournalError("JOURNAL_INCLUSION_UNPROVEN")


def _write_checked(
    writer: EvidenceWriter,
    organization_id: str,
    kind: str,
    data: bytes,
) -> MetadataRef:
    ref = writer.write(organization_id, kind, data)
    if ref.sha256 != sha256(data) or ref.kind != kind or ref.expires_at is not None:
        raise JournalError("RECOVERY_HOLD")
    return ref


class FrontierVerifier:
    def __init__(
        self,
        verifier: JournalVerifier,
        checkpoints: CheckpointAuthority,
        telemetry: JournalTelemetry,
        *,
        max_entries: int = MAX_REPLAY_ENTRIES,
        max_manifest_bytes: int = MAX_MANIFEST_BYTES,
        max_passes: int = 2,
        max_seconds: float = 60.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if (
            type(max_entries) is not int
            or not 1 <= max_entries <= MAX_REPLAY_ENTRIES
            or type(max_manifest_bytes) is not int
            or not 1 <= max_manifest_bytes <= MAX_MANIFEST_BYTES
            or type(max_passes) is not int
            or not 1 <= max_passes <= 3
            or not math.isfinite(max_seconds)
            or not 0 < max_seconds <= 300
        ):
            raise JournalError("JOURNAL_CONFIGURATION_INVALID", 503)
        self.verifier = verifier
        self.checkpoints = checkpoints
        self.telemetry = telemetry
        self.max_entries = max_entries
        self.max_manifest_bytes = max_manifest_bytes
        self.max_passes = max_passes
        self.max_seconds = max_seconds
        self.monotonic = monotonic

    def verify(
        self,
        organization_id: str,
        reader: JournalReader,
        writer: EvidenceWriter,
    ) -> VerifiedAuthorityFrontier:
        start = previous = self.monotonic()

        def budget() -> None:
            nonlocal previous
            now = self.monotonic()
            if (
                not math.isfinite(start)
                or not math.isfinite(now)
                or now < previous
                or now - start >= self.max_seconds
            ):
                raise JournalError("RECOVERY_HOLD")
            previous = now

        checkpoint = self.checkpoints.load(organization_id)
        budget()
        if (
            checkpoint.organization_id != organization_id
            or checkpoint.journal != self.verifier.trust.journal
            or checkpoint.evidence_ref.expires_at is not None
        ):
            raise JournalError("RECOVERY_HOLD")
        for _ in range(self.max_passes):
            nonce = secrets.token_hex(32)
            first = reader.read_live_head(organization_id, nonce)
            budget()
            head = self.verifier.live_head(first, organization_id, nonce)
            observations, pending = self._replay(checkpoint, head, reader, budget)
            nonce = secrets.token_hex(32)
            second = reader.read_live_head(organization_id, nonce)
            budget()
            final_head = self.verifier.live_head(second, organization_id, nonce)
            if final_head != head:
                if final_head.sequence <= head.sequence:
                    raise JournalError("RECOVERY_HOLD")
                continue
            manifest = ReplayManifest(
                profile_version="1.0",
                journal=checkpoint.journal,
                organization_id=organization_id,
                checkpoint_sequence=checkpoint.sequence,
                head_sequence=head.sequence,
                observations=observations,
            )
            manifest_bytes = canonical(manifest.wire())
            if len(manifest_bytes) > self.max_manifest_bytes:
                raise JournalError("RECOVERY_HOLD")
            # No verified frontier may reference evidence our retained parser rejects.
            ReplayManifest.parse(manifest_bytes)
            budget()
            manifest_ref = _write_checked(
                writer,
                organization_id,
                "journal_replay_manifest",
                manifest_bytes,
            )
            budget()
            proof = VerifiedFrontierProof(
                profile_version="1.0",
                journal=checkpoint.journal,
                organization_id=organization_id,
                checkpoint=checkpoint,
                live_head_envelope=second,
                replay_manifest_ref=manifest_ref,
                verified_entry_count=len(observations),
                unresolved_operation_id=head.unresolved_operation_id,
                latest_committed_epoch=head.latest_epoch,
                latest_committed_authority_revision=head.latest_authority_revision,
            )
            proof_ref = _write_checked(
                writer,
                organization_id,
                "journal_verified_frontier",
                transcript(FRONTIER_PURPOSE, proof.wire()),
            )
            budget()
            return VerifiedAuthorityFrontier(
                _VERIFIED,
                head,
                proof_ref,
                pending.mutation_kind if pending else None,
                observations,
            )
        raise JournalError("RECOVERY_HOLD")

    def _replay(
        self,
        checkpoint: ReplayCheckpoint,
        head: HeadState,
        reader: JournalReader,
        budget: Callable[[], None],
    ) -> tuple[tuple[ReplayObservation, ...], AppendIntent | None]:
        count = head.sequence - checkpoint.sequence
        if not 0 <= count <= self.max_entries or head.latest_epoch < checkpoint.latest_epoch:
            raise JournalError("RECOVERY_HOLD")
        chain, epoch, revision = (
            checkpoint.chain_sha256,
            checkpoint.latest_epoch,
            checkpoint.latest_authority_revision,
        )
        if checkpoint.sequence == 0:
            initial = HeadState(
                organization_id=checkpoint.organization_id,
                sequence=0,
                chain_sha256=chain,
                latest_epoch=epoch,
                latest_authority_revision=revision,
                unresolved_operation_id=None,
                revision="1",
            )
            if chain != genesis_digest(checkpoint.journal, initial):
                raise JournalError("RECOVERY_HOLD")
        pending: AppendIntent | None = None
        pending_sequence: int | None = None
        prior_head_revision = checkpoint.sequence + 1
        operations: set[str] = set()
        observations: list[ReplayObservation] = []
        observation_bytes = 0
        for sequence in range(checkpoint.sequence + 1, head.sequence + 1):
            budget()
            try:
                response = reader.read_entry(checkpoint.organization_id, sequence)
            except JournalError as error:
                if error.code == "JOURNAL_SEQUENCE_MISSING":
                    emit_metric(self.telemetry, "missing_sequence_count", 1)
                raise
            budget()
            entry, body = response.entry, response.body
            self.verifier.entry(entry, checkpoint.organization_id, sequence)
            phase = "intent" if isinstance(body, AppendIntent) else "outcome"
            if (
                body.organization_id != checkpoint.organization_id
                or body.operation_id != entry.operation_id
                or phase != entry.phase
                or body.expected_head_sequence != sequence - 1
                or body.expected_head_digest != chain
                or entry.previous_chain_sha256 != chain
                or model_digest(body) != entry.content_sha256
                or int(entry.head_revision) != prior_head_revision + 1
            ):
                raise JournalError("RECOVERY_HOLD")
            if isinstance(body, AppendIntent):
                if (
                    pending is not None
                    or body.operation_id in operations
                    or body.proposed_epoch < epoch
                    or (
                        body.mutation_kind in {"restrict", "tombstone"}
                        and body.proposed_epoch <= epoch
                    )
                    or body.proposed_authority_revision == revision
                ):
                    raise JournalError("RECOVERY_HOLD")
                pending, pending_sequence = body, sequence
                operations.add(body.operation_id)
            else:
                if (
                    pending is None
                    or body.operation_id != pending.operation_id
                    or body.intent_sequence != pending_sequence
                ):
                    raise JournalError("RECOVERY_HOLD")
                expected = (
                    (pending.proposed_epoch, pending.proposed_authority_revision)
                    if body.result == "committed"
                    else (epoch, revision)
                )
                if (body.resulting_epoch, body.resulting_authority_revision) != expected:
                    raise JournalError("RECOVERY_HOLD")
                epoch, revision = expected
                pending, pending_sequence = None, None
            chain, prior_head_revision = entry.chain_sha256, int(entry.head_revision)
            observation = ReplayObservation(entry=entry, body=body)
            observation_bytes += len(canonical(observation.wire())) + bool(observations)
            if observation_bytes > self.max_manifest_bytes:
                raise JournalError("RECOVERY_HOLD")
            observations.append(observation)
        if (
            chain != head.chain_sha256
            or epoch != head.latest_epoch
            or revision != head.latest_authority_revision
            or (pending.operation_id if pending else None) != head.unresolved_operation_id
            or prior_head_revision != int(head.revision)
        ):
            raise JournalError("RECOVERY_HOLD")
        return tuple(observations), pending
