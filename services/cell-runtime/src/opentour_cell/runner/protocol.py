"""Decode the shared cell command context before signed command admission."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated, Literal, NoReturn, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

_MAX_REVISION = 9_223_372_036_854_775_807
_MAX_EPOCH = 9_007_199_254_740_991
_MAX_CLOCK_UNCERTAINTY = timedelta(seconds=5)
_DECIMAL = re.compile(r"[1-9][0-9]{0,18}")
_UTC_TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?Z")

_Id = Annotated[str, StringConstraints(min_length=1, max_length=256)]
_Nonempty = Annotated[str, StringConstraints(min_length=1)]
_Sha256 = Annotated[str, StringConstraints(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")]
_Epoch = Annotated[int, Field(ge=0, le=_MAX_EPOCH)]
_DeploymentMode = Literal["opentour_managed", "customer_controlled"]


class CommandContextError(ValueError):
    """A redacted preflight failure without command contents."""


def _require_revision(value: str) -> str:
    if not _DECIMAL.fullmatch(value) or int(value) > _MAX_REVISION:
        raise ValueError("Invalid resource revision")
    return value


def _require_timestamp(value: str) -> str:
    if not _UTC_TIMESTAMP.fullmatch(value):
        raise ValueError("Invalid UTC timestamp")
    datetime.fromisoformat(value)
    return value


def _freeze_manifest_array(value: object) -> tuple[object, ...]:
    if not isinstance(value, list):
        raise ValueError("Expected an array of artifact references")
    return tuple(value)


_Revision = Annotated[str, AfterValidator(_require_revision)]
_Timestamp = Annotated[str, AfterValidator(_require_timestamp)]


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class _Actor(_ClosedModel):
    kind: Literal["human", "workload"]
    subject: _Id
    issuer: _Nonempty
    delegated_grant_id: str | None = None


class _ArtifactRef(_ClosedModel):
    artifact_id: _Id
    kind: _Nonempty
    revision: _Id
    sha256: _Sha256
    manifest_id: _Id
    environment_id: _Id
    classification: Literal[
        "control_metadata", "customer_confidential", "protected_evaluation", "model_artifact"
    ]
    purpose_grant_id: _Id
    expires_at: _Timestamp | None = None


class CellCommandContext(_ClosedModel):
    schema_version: Literal["1.0"]
    request_id: _Id
    request_nonce: _Id = Field(repr=False)
    idempotency_key: _Id = Field(repr=False)
    organization_id: _Id
    workspace_id: _Id
    project_id: _Id
    environment_id: _Id
    cell_id: _Id
    deployment_mode: _DeploymentMode
    actor: _Actor = Field(repr=False)
    authority_revision: _Id
    revocation_epoch: _Epoch
    created_at: _Timestamp
    expires_at: _Timestamp
    manifest_refs: Annotated[tuple[_ArtifactRef, ...], BeforeValidator(_freeze_manifest_array)]
    expected_revision: _Revision | None

    @model_validator(mode="after")
    def check_interval(self) -> Self:
        if datetime.fromisoformat(self.expires_at) <= datetime.fromisoformat(self.created_at):
            raise ValueError("Invalid command lifetime")
        return self


def _unique_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("Duplicate JSON member")
        result[name] = value
    return result


def _reject_constant(_value: str) -> NoReturn:
    raise ValueError("Invalid JSON constant")


def parse_cell_command_context(body: bytes) -> CellCommandContext:
    """Read the exact context while preserving the original wire values."""
    try:
        if not isinstance(body, bytes):
            raise ValueError("Expected UTF-8 bytes")
        document = json.loads(
            body.decode("utf-8"), object_pairs_hook=_unique_keys, parse_constant=_reject_constant
        )
        json.dumps(document, ensure_ascii=False).encode("utf-8")
        return CellCommandContext.model_validate(document)
    except (ValidationError, ValueError, RecursionError):
        pass
    raise CommandContextError("COMMAND_CONTEXT_INVALID") from None


@dataclass(frozen=True)
class CellCommandScope:
    """Expected scope and current authority supplied by trusted local state."""

    organization_id: str
    workspace_id: str
    project_id: str
    environment_id: str
    cell_id: str
    deployment_mode: _DeploymentMode
    authority_revision: str
    revocation_epoch: int


def require_current_command_context(
    context: CellCommandContext,
    scope: CellCommandScope,
    *,
    now: datetime,
    uncertainty: timedelta,
) -> None:
    """Check a new command against the trusted scope and clock before admission."""
    command_scope = CellCommandScope(
        context.organization_id,
        context.workspace_id,
        context.project_id,
        context.environment_id,
        context.cell_id,
        context.deployment_mode,
        context.authority_revision,
        context.revocation_epoch,
    )
    if type(scope.revocation_epoch) is not int or command_scope != scope:
        raise CommandContextError("COMMAND_CONTEXT_MISMATCH")
    if now.utcoffset() is None or not timedelta(0) <= uncertainty <= _MAX_CLOCK_UNCERTAINTY:
        raise CommandContextError("CLOCK_SKEW")
    if datetime.fromisoformat(context.expires_at) - now <= uncertainty:
        raise CommandContextError("COMMAND_CONTEXT_EXPIRED")
