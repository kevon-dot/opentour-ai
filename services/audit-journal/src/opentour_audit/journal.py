"""Independent audit service: scoped metadata, immutable S3 versions and DynamoDB CAS.

The three-item transaction is the append linearization point. S3 and signing
precede it; neither an object nor a candidate signature grants authority. Service
composition must supply authenticated control/enrollment and reconciliation ports.
"""

from __future__ import annotations

import base64
import hashlib
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal, Protocol

from boto3.dynamodb.conditions import Attr, ConditionExpressionBuilder  # type: ignore[import-untyped]
from botocore.config import Config  # type: ignore[import-untyped]
from botocore.exceptions import BotoCoreError, ClientError  # type: ignore[import-untyped]
from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from opentour_journal.crypto import (
    JournalVerifier,
    TrustedKey,
    TrustPolicy,
    canonical,
    chain_digest,
    content_key,
    create_signature,
    genesis_digest,
    model_digest,
    sha256,
)
from opentour_journal.models import (
    APPEND_PURPOSE,
    HEAD_PURPOSE,
    MAX_COUNTER,
    MAX_REVISION,
    MAX_WIRE_BYTES,
    AppendIntent,
    AppendOutcome,
    AppendReceiptEnvelope,
    AppendReceiptPayload,
    AppendResponse,
    ExactEntryResponse,
    ExactOperationResponse,
    HeadState,
    ImmutableMetadataRef,
    JournalEntry,
    JournalError,
    JournalTelemetry,
    LiveHeadEnvelope,
    LiveHeadPayload,
    MutationKind,
    Phase,
    emit_metric,
)
from pydantic import ValidationError


@dataclass(frozen=True)
class ServicePrincipal:
    service_id: str
    authentication_ref: ImmutableMetadataRef


class ServiceAuthenticator(Protocol):
    async def __call__(self, request: Request) -> ServicePrincipal: ...


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


@dataclass(frozen=True)
class JournalConfiguration:
    trust: TrustPolicy
    table_name: str
    encryption_key_arn: str
    append_role_arn: str
    signing_key: TrustedKey
    retention_mode: Literal["COMPLIANCE", "GOVERNANCE"]
    retention_days: int
    maximum_backup_age_days: int
    replay_horizon_days: int
    tombstone_retention_days: int

    def validate(self) -> None:
        self.trust.validate()
        identity = self.trust.journal
        prefix = f"arn:aws:kms:{identity.region}:{identity.audit_account_id}:key/"
        days = (
            self.retention_days,
            self.maximum_backup_age_days,
            self.replay_horizon_days,
            self.tombstone_retention_days,
        )
        if (
            not re.fullmatch(r"[A-Za-z0-9_.-]{3,255}", self.table_name)
            or not self.encryption_key_arn.startswith(prefix)
            or self.encryption_key_arn == self.signing_key.key_id
            or not self.append_role_arn.startswith(
                f"arn:aws:iam::{identity.audit_account_id}:role/"
            )
            or self.signing_key not in self.trust.keys
            or not self.signing_key.live_head_eligible
            or self.signing_key.purposes != {APPEND_PURPOSE, HEAD_PURPOSE}
            or self.retention_mode not in {"COMPLIANCE", "GOVERNANCE"}
            or any(type(v) is not int or not 1 <= v <= 36500 for v in days)
            or self.retention_days < self.maximum_backup_age_days + self.replay_horizon_days
            or self.retention_days < self.tombstone_retention_days
        ):
            raise JournalError("JOURNAL_CONFIGURATION_INVALID", 503)


def _condition(condition: Any) -> dict[str, Any]:
    built = ConditionExpressionBuilder().build_expression(condition)
    result = {
        "ConditionExpression": built.condition_expression,
        "ExpressionAttributeNames": built.attribute_name_placeholders,
    }
    if built.attribute_value_placeholders:
        result["ExpressionAttributeValues"] = built.attribute_value_placeholders
    return result


def _native(value: Any) -> Any:
    # DynamoDB resource clients return exact Decimal numbers, not JSON integers.
    if isinstance(value, Decimal):
        if value != value.to_integral_value():
            raise JournalError("JOURNAL_STATE_INVALID", 503)
        return int(value)
    if isinstance(value, dict):
        return {k: _native(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_native(v) for v in value]
    return value


class AWSJournalStore:
    """SDK clients are constructed once with approved account/region credentials.

    DynamoDB is the resource's auto-marshalling meta.client. Request deadlines are
    finite connect/read timeouts with one attempt; they are not a claim that DNS or
    an in-flight AWS operation can be cancelled at a global wall-clock deadline.
    A timed-out append remains unknown until its exact durable OP is observed.
    """

    def __init__(self, config: JournalConfiguration, dynamodb: Any, s3: Any, kms: Any) -> None:
        config.validate()
        self.config, self.ddb, self.s3, self.kms = config, dynamodb, s3, kms

    @classmethod
    def from_session(cls, config: JournalConfiguration, session: Any) -> AWSJournalStore:
        """Explicit activation, never called at import or by local setup."""
        config.validate()
        sdk = Config(
            connect_timeout=2,
            read_timeout=5,
            max_pool_connections=8,
            retries={"mode": "standard", "total_max_attempts": 1},
            signature_version="v4",
            ignore_configured_endpoint_urls=True,
        )
        region = config.trust.journal.region
        sts = session.client("sts", region_name=region, config=sdk)
        try:
            identity = sts.get_caller_identity()
            role_name = config.append_role_arn.rsplit("/", 1)[-1]
            expected = (
                f"arn:aws:sts::{config.trust.journal.audit_account_id}:assumed-role/{role_name}/"
            )
            if identity.get("Account") != config.trust.journal.audit_account_id or not str(
                identity.get("Arn", "")
            ).startswith(expected):
                raise JournalError("JOURNAL_TRUST_INVALID", 503)
        except (ClientError, BotoCoreError):
            raise JournalError("JOURNAL_UNAVAILABLE", 503) from None
        finally:
            sts.close()
        store = cls(
            config,
            session.resource("dynamodb", region_name=region, config=sdk).meta.client,
            session.client("s3", region_name=region, config=sdk),
            session.client("kms", region_name=region, config=sdk),
        )
        try:
            store.verify_storage_configuration()
        except Exception:
            store.close()
            raise JournalError("JOURNAL_CONFIGURATION_INVALID", 503) from None
        return store

    def verify_storage_configuration(self) -> None:
        """Read actual immutable destination settings before production activation."""
        bucket = {
            "Bucket": self.config.trust.content_bucket,
            "ExpectedBucketOwner": self.config.trust.journal.audit_account_id,
        }
        lock = self.s3.get_object_lock_configuration(**bucket)["ObjectLockConfiguration"]
        versioning = self.s3.get_bucket_versioning(**bucket)
        encryption = self.s3.get_bucket_encryption(**bucket)["ServerSideEncryptionConfiguration"]
        table = self.ddb.describe_table(TableName=self.config.table_name)["Table"]
        pitr = self.ddb.describe_continuous_backups(TableName=self.config.table_name)[
            "ContinuousBackupsDescription"
        ]["PointInTimeRecoveryDescription"]
        ttl = self.ddb.describe_time_to_live(TableName=self.config.table_name)[
            "TimeToLiveDescription"
        ]
        public = self.kms.get_public_key(KeyId=self.config.signing_key.key_id)
        expected_table = (
            f"arn:aws:dynamodb:{self.config.trust.journal.region}:"
            f"{self.config.trust.journal.audit_account_id}:table/{self.config.table_name}"
        )
        encryption_defaults = [
            r.get("ApplyServerSideEncryptionByDefault") for r in encryption.get("Rules", [])
        ]
        if (
            lock.get("ObjectLockEnabled") != "Enabled"
            or lock.get("Rule", {}).get("DefaultRetention")
            != {"Mode": self.config.retention_mode, "Days": self.config.retention_days}
            or versioning.get("Status") != "Enabled"
            or encryption_defaults
            != [{"SSEAlgorithm": "aws:kms", "KMSMasterKeyID": self.config.encryption_key_arn}]
            or table.get("TableArn") != expected_table
            or table.get("TableStatus") != "ACTIVE"
            or table.get("SSEDescription", {}).get("KMSMasterKeyArn")
            != self.config.encryption_key_arn
            or table.get("SSEDescription", {}).get("Status") != "ENABLED"
            or pitr.get("PointInTimeRecoveryStatus") != "ENABLED"
            or ttl.get("TimeToLiveStatus") != "DISABLED"
            or public.get("KeyId") != self.config.signing_key.key_id
            or public.get("KeySpec") != "ECC_NIST_P256"
            or public.get("KeyUsage") != "SIGN_VERIFY"
            or "ECDSA_SHA_256" not in public.get("SigningAlgorithms", [])
            or sha256(bytes(public.get("PublicKey", b"")))
            != self.config.signing_key.public_key_sha256
        ):
            raise JournalError("JOURNAL_CONFIGURATION_INVALID", 503)

    def close(self) -> None:
        for client in (self.ddb, self.s3, self.kms):
            client.close()

    def read_item(self, organization_id: str, sort_key: str) -> dict[str, Any] | None:
        result = self.ddb.get_item(
            TableName=self.config.table_name,
            Key={"PK": f"ORG#{organization_id}", "SK": sort_key},
            ConsistentRead=True,
        ).get("Item")
        return _native(result) if result is not None else None

    def head(self, organization_id: str) -> HeadState:
        row = self.read_item(organization_id, "HEAD")
        if row is None:
            raise JournalError("JOURNAL_NOT_FOUND", 404)
        return HeadState.model_validate(row["head"])

    def operation(
        self, organization_id: str, operation_id: str, phase: Phase
    ) -> AppendResponse | None:
        row = self.read_item(organization_id, f"OP#{operation_id}#{phase}")
        return AppendResponse.parse(row["response"].encode()) if row is not None else None

    def entry(self, organization_id: str, sequence: int) -> JournalEntry:
        row = self.read_item(organization_id, f"SEQ#{sequence:020d}")
        if row is None:
            raise JournalError("JOURNAL_SEQUENCE_MISSING")
        return JournalEntry.parse(row["entry"].encode())

    def content(self, entry: JournalEntry) -> bytes:
        response = self.s3.get_object(
            Bucket=self.config.trust.content_bucket,
            Key=entry.content_key,
            VersionId=entry.content_version_id,
            ExpectedBucketOwner=self.config.trust.journal.audit_account_id,
            ChecksumMode="ENABLED",
        )
        body = response["Body"]
        try:
            data = body.read(MAX_WIRE_BYTES + 1)
        finally:
            body.close()
        if (
            len(data) > MAX_WIRE_BYTES
            or response.get("VersionId") != entry.content_version_id
            or response.get("ServerSideEncryption") != "aws:kms"
            or response.get("SSEKMSKeyId") != self.config.encryption_key_arn
            or sha256(data) != entry.content_sha256
        ):
            raise JournalError("RECOVERY_HOLD")
        return bytes(data)

    def put_content(self, body: AppendIntent | AppendOutcome, now: datetime) -> tuple[str, str]:
        phase = "intent" if isinstance(body, AppendIntent) else "outcome"
        data = canonical(body.wire())
        if len(data) > MAX_WIRE_BYTES:
            raise JournalError("SCHEMA_INVALID", 422)
        digest = sha256(data)
        key = content_key(body.organization_id, body.operation_id, phase, digest)
        response = self.s3.put_object(
            Bucket=self.config.trust.content_bucket,
            Key=key,
            Body=data,
            ContentType="application/json",
            ChecksumSHA256=base64.b64encode(hashlib.sha256(data).digest()).decode(),
            ServerSideEncryption="aws:kms",
            SSEKMSKeyId=self.config.encryption_key_arn,
            ExpectedBucketOwner=self.config.trust.journal.audit_account_id,
        )
        # Qualified bucket default applies retention at creation. This role cannot
        # change retention on old versions or bypass governance retention.
        version = response.get("VersionId")
        if type(version) is not str or not version or version == "null" or len(version) > 256:
            raise JournalError("JOURNAL_UNAVAILABLE", 503)
        return key, version

    def sign(self, digest: bytes) -> bytes:
        response = self.kms.sign(
            KeyId=self.config.signing_key.key_id,
            Message=digest,
            MessageType="DIGEST",
            SigningAlgorithm="ECDSA_SHA_256",
        )
        if (
            response.get("KeyId") != self.config.signing_key.key_id
            or response.get("SigningAlgorithm") != "ECDSA_SHA_256"
        ):
            raise JournalError("JOURNAL_SIGNATURE_INVALID")
        return bytes(response["Signature"])

    def register(self, head: HeadState, registration_digest: str) -> None:
        self.ddb.put_item(
            TableName=self.config.table_name,
            Item={
                "PK": f"ORG#{head.organization_id}",
                "SK": "HEAD",
                "head": head.wire(),
                "registration_digest": registration_digest,
            },
            **_condition(Attr("PK").not_exists()),
        )

    def commit(
        self, before: HeadState, after: HeadState, entry: JournalEntry, response: AppendResponse
    ) -> None:
        partition = f"ORG#{before.organization_id}"
        condition = (
            Attr("head.sequence").eq(before.sequence)
            & Attr("head.chain_sha256").eq(before.chain_sha256)
            & Attr("head.revision").eq(before.revision)
            & Attr("head.unresolved_operation_id").eq(before.unresolved_operation_id)
        )
        expected = _condition(condition)
        names = {**expected["ExpressionAttributeNames"], "#new_head": "head"}
        values = {**expected["ExpressionAttributeValues"], ":new_head": after.wire()}
        self.ddb.transact_write_items(
            TransactItems=[
                {
                    "Update": {
                        "TableName": self.config.table_name,
                        "Key": {"PK": partition, "SK": "HEAD"},
                        "UpdateExpression": "SET #new_head = :new_head",
                        "ConditionExpression": expected["ConditionExpression"],
                        "ExpressionAttributeNames": names,
                        "ExpressionAttributeValues": values,
                    }
                },
                {
                    "Put": {
                        "TableName": self.config.table_name,
                        "Item": {
                            "PK": partition,
                            "SK": f"SEQ#{entry.sequence:020d}",
                            "entry": canonical(entry.wire()).decode(),
                        },
                        **_condition(Attr("PK").not_exists()),
                    }
                },
                {
                    "Put": {
                        "TableName": self.config.table_name,
                        "Item": {
                            "PK": partition,
                            "SK": f"OP#{entry.operation_id}#{entry.phase}",
                            "response": canonical(response.wire()).decode(),
                        },
                        **_condition(Attr("PK").not_exists()),
                    }
                },
            ]
        )


class JournalService:
    def __init__(
        self,
        store: AWSJournalStore,
        authority: AuthorityResolver,
        telemetry: JournalTelemetry,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.store, self.authority, self.clock = store, authority, clock
        self.telemetry, self.monotonic = telemetry, monotonic
        self.verifier = JournalVerifier(store.config.trust)
        self.capacity = threading.BoundedSemaphore(8)

    def _scope(
        self, principal: ServicePrincipal, org: str, capability: Literal["append", "read", "enroll"]
    ) -> None:
        scope = self.authority.authorize(principal, org, capability)
        if scope != AuthorizedScope(principal.service_id, org, capability):
            raise JournalError("JOURNAL_FORBIDDEN", 403)

    def execute(self, action: Callable[[], Any]) -> Any:
        """Finite admission; sanitize all SDK failures at the service boundary."""
        if not self.capacity.acquire(blocking=False):
            raise JournalError("JOURNAL_UNAVAILABLE", 503)
        try:
            return action()
        except (ClientError, BotoCoreError, OSError):
            raise JournalError("JOURNAL_UNAVAILABLE", 503) from None
        except (ValidationError, KeyError, TypeError, ValueError):
            raise JournalError("JOURNAL_STATE_INVALID", 503) from None
        finally:
            self.capacity.release()

    def register(self, principal: ServicePrincipal, organization_id: str) -> HeadState:
        self._scope(principal, organization_id, "enroll")
        approved = self.authority.registration(principal, organization_id)
        if approved.organization_id != organization_id:
            raise JournalError("JOURNAL_NOT_FOUND", 404)
        head = HeadState(
            organization_id=organization_id,
            sequence=0,
            chain_sha256="0" * 64,
            latest_epoch=approved.initial_epoch,
            latest_authority_revision=approved.initial_authority_revision,
            unresolved_operation_id=None,
            revision="1",
        )
        head = head.model_copy(
            update={"chain_sha256": genesis_digest(self.verifier.trust.journal, head)}
        )
        registration_digest = sha256(
            canonical(
                {
                    "head": head.wire(),
                    "enrollment_ref": approved.enrollment_ref.wire(),
                }
            )
        )
        try:
            self.store.register(head, registration_digest)
        except self.store.ddb.exceptions.ConditionalCheckFailedException:
            # Registration never rewrites or replays an existing/tombstoned head.
            raise JournalError("JOURNAL_REGISTRATION_CONFLICT") from None
        return head

    def read_live_head(
        self,
        principal: ServicePrincipal,
        organization_id: str,
        nonce: str,
    ) -> LiveHeadEnvelope:
        self._scope(principal, organization_id, "read")
        head = self.store.head(organization_id)
        if int(head.revision) != head.sequence + 1:
            raise JournalError("JOURNAL_STATE_INVALID", 503)
        payload = LiveHeadPayload(
            profile_version="1.0",
            journal=self.verifier.trust.journal,
            request_nonce=nonce,
            observed_at=self._now().isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            head=head,
        )
        return LiveHeadEnvelope(
            payload=payload,
            signature=create_signature(
                payload,
                HEAD_PURPOSE,
                self.store.config.signing_key,
                self.store.sign,
            ),
        )

    def read_operation(
        self,
        principal: ServicePrincipal,
        organization_id: str,
        operation_id: str,
        phase: Phase,
    ) -> ExactOperationResponse:
        self._scope(principal, organization_id, "read")
        result = self.store.operation(organization_id, operation_id, phase)
        if result is not None:
            self.verifier.receipt(result.signed_receipt, organization_id, operation_id, phase)
        return ExactOperationResponse(
            organization_id=organization_id,
            operation_id=operation_id,
            phase=phase,
            signed_response=result,
        )

    def read_entry(
        self,
        principal: ServicePrincipal,
        organization_id: str,
        sequence: int,
    ) -> ExactEntryResponse:
        self._scope(principal, organization_id, "read")
        entry = self.store.entry(organization_id, sequence)
        self.verifier.entry(entry, organization_id, sequence)
        body_type = AppendIntent if entry.phase == "intent" else AppendOutcome
        body = body_type.parse(self.store.content(entry))
        if model_digest(body) != entry.content_sha256:
            raise JournalError("RECOVERY_HOLD")
        return ExactEntryResponse(entry=entry, body=body)

    def append(
        self,
        principal: ServicePrincipal,
        body: AppendIntent | AppendOutcome,
    ) -> tuple[int, AppendResponse]:
        started = self.monotonic()
        try:
            return self._append(principal, body)
        finally:
            # A delivery failure after commit is an unknown response, resolved by exact OP.
            emit_metric(
                self.telemetry, "journal_append_latency_seconds", self.monotonic() - started
            )

    def _append(
        self,
        principal: ServicePrincipal,
        body: AppendIntent | AppendOutcome,
    ) -> tuple[int, AppendResponse]:
        self._scope(principal, body.organization_id, "append")
        phase: Phase = "intent" if isinstance(body, AppendIntent) else "outcome"
        prior = self.store.operation(body.organization_id, body.operation_id, phase)
        if prior is not None:
            return 200, self._identical(prior, body)
        head = self.store.head(body.organization_id)
        if isinstance(body, AppendIntent) and head.unresolved_operation_id is not None:
            raise JournalError("AUTHORITY_OPERATION_PENDING")
        if (body.expected_head_sequence, body.expected_head_digest) != (
            head.sequence,
            head.chain_sha256,
        ):
            raise JournalError("JOURNAL_HEAD_CONFLICT")
        if head.sequence >= MAX_COUNTER or int(head.revision) >= MAX_REVISION:
            raise JournalError("REVISION_EXHAUSTED")
        if int(head.revision) != head.sequence + 1:
            raise JournalError("JOURNAL_STATE_INVALID", 503)
        if isinstance(body, AppendIntent):
            resolved = self.authority.resolve_intent(principal, body)
            if resolved != ResolvedIntent(
                **{name: getattr(body, name) for name in ResolvedIntent.__dataclass_fields__}
            ):
                raise JournalError("JOURNAL_NOT_FOUND", 404)
            if (
                body.proposed_epoch < head.latest_epoch
                or body.proposed_authority_revision == head.latest_authority_revision
                or (
                    body.mutation_kind in {"restrict", "tombstone"}
                    and body.proposed_epoch <= head.latest_epoch
                )
            ):
                raise JournalError("JOURNAL_AUTHORITY_CONFLICT")
            epoch, revision, unresolved = (
                head.latest_epoch,
                head.latest_authority_revision,
                body.operation_id,
            )
        else:
            if head.unresolved_operation_id != body.operation_id:
                raise JournalError("JOURNAL_OPERATION_CONFLICT")
            entry = self.store.entry(body.organization_id, body.intent_sequence)
            self.verifier.entry(entry, body.organization_id, body.intent_sequence)
            if entry.phase != "intent" or entry.operation_id != body.operation_id:
                raise JournalError("JOURNAL_OPERATION_CONFLICT")
            intent = AppendIntent.parse(self.store.content(entry))
            reconciled = self.authority.reconcile_transaction(principal, body, intent)
            expected = ReconciledTransaction(
                body.organization_id,
                body.operation_id,
                entry.content_sha256,
                body.control_transaction_ref,
                body.result,
                body.resulting_epoch,
                body.resulting_authority_revision,
            )
            result_tuple = (
                (intent.proposed_epoch, intent.proposed_authority_revision)
                if body.result == "committed"
                else (head.latest_epoch, head.latest_authority_revision)
            )
            if (
                reconciled != expected
                or (body.resulting_epoch, body.resulting_authority_revision) != result_tuple
            ):
                raise JournalError("JOURNAL_RECONCILIATION_REQUIRED")
            epoch, revision = result_tuple
            unresolved = None
        now = self._now()
        key, version = self.store.put_content(body, now)
        payload = AppendReceiptPayload(
            profile_version="1.0",
            journal=self.verifier.trust.journal,
            organization_id=body.organization_id,
            operation_id=body.operation_id,
            phase=phase,
            sequence=head.sequence + 1,
            previous_chain_sha256=head.chain_sha256,
            chain_sha256="0" * 64,
            content_sha256=model_digest(body),
            content_bucket_ref=self.verifier.trust.content_bucket,
            content_key=key,
            content_version_id=version,
            head_revision=str(int(head.revision) + 1),
        )
        payload = payload.model_copy(update={"chain_sha256": chain_digest(payload)})
        try:
            signed = AppendReceiptEnvelope(
                payload=payload,
                signature=create_signature(
                    payload,
                    APPEND_PURPOSE,
                    self.store.config.signing_key,
                    self.store.sign,
                ),
            )
        except Exception:
            emit_metric(self.telemetry, "orphan_bytes", len(canonical(body.wire())))
            raise
        response = AppendResponse(
            **{
                name: getattr(payload, name)
                for name in AppendResponse.model_fields
                if name != "signed_receipt"
            },
            signed_receipt=signed,
        )
        entry = JournalEntry.model_validate(
            {
                **payload.wire(),
                "signed_receipt": signed,
                "created_at": now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                "signer_id": self.store.config.signing_key.key_id,
            }
        )
        after = HeadState(
            organization_id=head.organization_id,
            sequence=payload.sequence,
            chain_sha256=payload.chain_sha256,
            latest_epoch=epoch,
            latest_authority_revision=revision,
            unresolved_operation_id=unresolved,
            revision=payload.head_revision,
        )
        try:
            self.store.commit(head, after, entry, response)
        except self.store.ddb.exceptions.TransactionCanceledException:
            # Transaction cancellation proves this candidate version was not committed.
            emit_metric(self.telemetry, "orphan_bytes", len(canonical(body.wire())))
            committed = self.store.operation(body.organization_id, body.operation_id, phase)
            if committed is not None:
                return 200, self._identical(committed, body)
            current = self.store.head(body.organization_id)
            if current != head:
                if isinstance(body, AppendIntent) and current.unresolved_operation_id is not None:
                    raise JournalError("AUTHORITY_OPERATION_PENDING") from None
                raise JournalError("JOURNAL_HEAD_CONFLICT") from None
            raise JournalError("JOURNAL_UNAVAILABLE", 503) from None
        except (ClientError, BotoCoreError, OSError):
            # A strong absence is not proof a timed-out write cannot still commit.
            committed = self.store.operation(body.organization_id, body.operation_id, phase)
            if committed is not None:
                return 200, self._identical(committed, body)
            raise JournalError("JOURNAL_UNAVAILABLE", 503) from None
        return 201, response

    def observe_unresolved(
        self, principal: ServicePrincipal, organizations: tuple[str, ...]
    ) -> None:
        """Poll a bounded authorized batch; the runtime schedules complete coverage.

        Aggregate age using max across batches. Alert routing is a required sink concern.
        Service-created entry time is used; caller-supplied intent time is not trusted.
        This method never resolves an operation or changes any authority state.
        """
        if not 1 <= len(organizations) <= 64 or len(set(organizations)) != len(organizations):
            raise JournalError("SCHEMA_INVALID", 422)
        oldest = 0.0
        for organization in organizations:
            self._scope(principal, organization, "read")
            head = self.store.head(organization)
            if head.unresolved_operation_id is None:
                continue
            entry = self.read_entry(principal, organization, head.sequence).entry
            if entry.operation_id != head.unresolved_operation_id or entry.phase != "intent":
                raise JournalError("JOURNAL_STATE_INVALID", 503)
            age = (self._now() - datetime.fromisoformat(entry.created_at)).total_seconds()
            if age < 0:
                raise JournalError("JOURNAL_TIME_UNAVAILABLE", 503)
            oldest = max(oldest, age)
        emit_metric(self.telemetry, "oldest_unresolved_operation_seconds", oldest)
        if oldest >= 600:
            emit_metric(self.telemetry, "unresolved_intent_over_10_minutes", oldest)

    def _identical(
        self, response: AppendResponse, body: AppendIntent | AppendOutcome
    ) -> AppendResponse:
        if response.content_sha256 != model_digest(body):
            raise JournalError("JOURNAL_OPERATION_CONFLICT")
        self.verifier.response(response, body)
        return response

    def _now(self) -> datetime:
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() != timedelta(0):
            raise JournalError("JOURNAL_TIME_UNAVAILABLE", 503)
        return now


def create_router(service: JournalService, authenticate: ServiceAuthenticator) -> APIRouter:
    """Compose behind a qualified registered-workload authenticator, never raw headers.

    No unauthenticated default, browser-session admission or enrollment endpoint.
    OPE-145 owns workload-identity qualification; OPE-213 owns the approved ingress.
    """
    from uuid import uuid4

    from opentour_journal.models import ExactEntryRequest, ExactOperationRequest, LiveHeadRequest

    router = APIRouter(prefix="/v1/authority-journal")

    async def dispatch(request: Request, operation: str) -> JSONResponse:
        try:
            principal = await authenticate(request)
            if not isinstance(principal, ServicePrincipal):
                raise JournalError("JOURNAL_FORBIDDEN", 403)
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > MAX_WIRE_BYTES:
                    raise JournalError("SCHEMA_INVALID", 422)
            status = 200
            match operation:
                case "intent" | "outcome":
                    model = AppendIntent if operation == "intent" else AppendOutcome
                    parsed = model.parse(bytes(body))
                    status, result = await run_in_threadpool(
                        service.execute,
                        lambda: service.append(principal, parsed),
                    )
                case "head":
                    head_request = LiveHeadRequest.parse(bytes(body))
                    result = await run_in_threadpool(
                        service.execute,
                        lambda: service.read_live_head(
                            principal,
                            head_request.organization_id,
                            head_request.request_nonce,
                        ),
                    )
                case "operation":
                    op_request = ExactOperationRequest.parse(bytes(body))
                    result = await run_in_threadpool(
                        service.execute,
                        lambda: service.read_operation(
                            principal,
                            op_request.organization_id,
                            op_request.operation_id,
                            op_request.phase,
                        ),
                    )
                case "entry":
                    entry_request = ExactEntryRequest.parse(bytes(body))
                    result = await run_in_threadpool(
                        service.execute,
                        lambda: service.read_entry(
                            principal,
                            entry_request.organization_id,
                            entry_request.sequence,
                        ),
                    )
                case _:
                    raise JournalError("JOURNAL_NOT_FOUND", 404)
            return JSONResponse(
                result.wire(), status_code=status, headers={"Cache-Control": "no-store"}
            )
        except Exception as error:
            code, status = (
                (error.code, error.status)
                if isinstance(error, JournalError)
                else ("JOURNAL_UNAVAILABLE", 503)
            )
            return JSONResponse(
                {
                    "code": code,
                    "message": "Journal operation could not be completed.",
                    "request_id": uuid4().hex,
                    "retryable": status == 503,
                    "field_errors": [],
                    "current_revision": None,
                },
                status_code=status,
                headers={"Cache-Control": "no-store"},
            )

    @router.post("/intents")
    async def intents(request: Request) -> JSONResponse:
        return await dispatch(request, "intent")

    @router.post("/outcomes")
    async def outcomes(request: Request) -> JSONResponse:
        return await dispatch(request, "outcome")

    @router.post("/heads/read")
    async def heads(request: Request) -> JSONResponse:
        return await dispatch(request, "head")

    @router.post("/operations/read")
    async def operations(request: Request) -> JSONResponse:
        return await dispatch(request, "operation")

    @router.post("/entries/read")
    async def entries(request: Request) -> JSONResponse:
        return await dispatch(request, "entry")

    return router
