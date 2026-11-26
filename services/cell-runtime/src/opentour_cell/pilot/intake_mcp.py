"""Versioned, intake-only MCP surface over an authorized local stdio connection."""

from __future__ import annotations

import re
import sqlite3
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic
from typing import Any, BinaryIO, Literal, Protocol

from opentour_cell.pilot.mcp import serve_tools, tool_error, tool_result

DISCOVER = "discover_sources"
SUBMIT = "submit_intake"
GET_RUN = "get_run"
REF_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"
REF = {"type": "string", "minLength": 1, "maxLength": 128, "pattern": REF_PATTERN}
MAX_PAGE = 50
CALLS_PER_MINUTE = 120


class IntakeError(ValueError):
    """An expected failure whose code is safe to display to the authenticated caller."""


@dataclass(frozen=True)
class IntakePrincipal:
    tenant: str
    actor: str
    role: str


@dataclass(frozen=True)
class HistoricalPair:
    pair_ref: str
    source_revision: str
    correction_revision: str
    policy_revision: str
    idempotency_key: str


@dataclass(frozen=True)
class Starter:
    policy_resource_ref: str
    source_revision: str
    setup_policy_revision: str
    idempotency_key: str
    creation_ref: str | None
    workflow_ref: str | None


@dataclass(frozen=True)
class LiveRefresh:
    feed_binding_ref: str
    source_revision: str
    idempotency_key: str


IntakeRequest = HistoricalPair | Starter | LiveRefresh
FeedKind = Literal["decision", "independent_outcome"]
BindingState = Literal["current", "superseded", "revoked", "unavailable"]


@dataclass(frozen=True)
class LiveBinding:
    tenant: str
    workflow_ref: str
    engagement_epoch: str
    binding_id: str
    binding_version: str
    feed_kind: FeedKind
    resource: str
    grant_revision: str
    state: BindingState
    source_revision: str
    successor_ref: str | None = None


class LiveBindingResolver(Protocol):
    def resolve(
        self, feed_binding_ref: str, authenticated_actor: IntakePrincipal
    ) -> LiveBinding | None: ...


@dataclass(frozen=True)
class CatalogSource:
    source_kind: Literal["historical", "policy"]
    source_ref: str
    source_revision: str


@dataclass(frozen=True)
class CatalogFeed:
    feed_binding_ref: str
    binding_version: str
    feed_kind: FeedKind
    source_revision: str


@dataclass(frozen=True)
class CatalogPage:
    sources: tuple[CatalogSource | CatalogFeed, ...]
    next_cursor: str | None = None


@dataclass(frozen=True)
class AdmissionReceipt:
    run_ref: str
    state: str
    correlation_ref: str


@dataclass(frozen=True)
class AdmissionHold:
    reason_code: str
    successor_ref: str | None = None


@dataclass(frozen=True)
class BenchmarkView:
    benchmark_ref: str
    version: str | int
    benchmark_state: str


@dataclass(frozen=True)
class RunStatus:
    run_ref: str
    state: str
    next_action: str
    reason_code: str | None
    workflow_ref: str
    benchmark: BenchmarkView | None = None


class IntakeAdmission(Protocol):
    """The same scoped admission service is used by stdio and the HTTPS adapter."""

    def discover_sources(
        self, actor: IntakePrincipal, *, limit: int, cursor: str | None
    ) -> CatalogPage: ...

    def submit_intake(
        self, actor: IntakePrincipal, request: IntakeRequest
    ) -> AdmissionReceipt | AdmissionHold: ...

    def get_run(self, actor: IntakePrincipal, run_ref: str) -> RunStatus: ...


_HISTORICAL = tuple(HistoricalPair.__dataclass_fields__)
_STARTER = ("policy_resource_ref", "source_revision", "setup_policy_revision", "idempotency_key")
_LIVE = tuple(LiveRefresh.__dataclass_fields__)


def _shape(required: tuple[str, ...], **extra: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {**dict.fromkeys(required, REF), **extra},
        "required": list(required),
    }


def _mode_shape(mode: str, fields: tuple[str, ...]) -> dict[str, Any]:
    return _shape(("mode", *fields), mode={"type": "string", "const": mode})


def _tool(
    name: str, description: str, schema: dict[str, Any], *, read_only: bool
) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "$id": f"urn:opentour:pilot:intake:v1:{name}",
            **schema,
        },
        "annotations": {
            "readOnlyHint": read_only,
            "idempotentHint": True,
            "destructiveHint": False,
            "openWorldHint": False,
        },
    }


TOOLS = [
    _tool(
        DISCOVER,
        "List opaque metadata for sources authorized by the current intake connection.",
        _shape((), limit={"type": "integer", "minimum": 1, "maximum": MAX_PAGE}, cursor=REF),
        read_only=True,
    ),
    _tool(
        SUBMIT,
        "Admit one authorized historical pair, policy starter or bound live feed revision.",
        {
            "type": "object",
            "oneOf": [
                _shape(_HISTORICAL),
                _mode_shape("historical", _HISTORICAL),
                _mode_shape("starter", (*_STARTER, "creation_ref")),
                _mode_shape("starter", (*_STARTER, "workflow_ref")),
                _mode_shape("live", _LIVE),
            ],
        },
        read_only=False,
    ),
    _tool(
        GET_RUN,
        "Read the current state of an authorized intake run.",
        _shape(("run_ref",)),
        read_only=True,
    ),
]


def require_ref(value: object) -> str:
    if not isinstance(value, str) or len(value) > 128 or re.fullmatch(REF_PATTERN, value) is None:
        raise IntakeError("invalid_arguments")
    return value


def parse_intake(arguments: dict[str, Any]) -> IntakeRequest:
    mode = arguments.get("mode", "historical")
    keys = set(arguments)
    keys.discard("mode")
    if mode == "historical" and keys == set(_HISTORICAL):
        return HistoricalPair(*(require_ref(arguments[field]) for field in _HISTORICAL))
    if mode == "live" and keys == set(_LIVE):
        return LiveRefresh(*(require_ref(arguments[field]) for field in _LIVE))
    if mode == "starter" and keys in (
        {*_STARTER, "creation_ref"},
        {*_STARTER, "workflow_ref"},
    ):
        return Starter(
            policy_resource_ref=require_ref(arguments["policy_resource_ref"]),
            source_revision=require_ref(arguments["source_revision"]),
            setup_policy_revision=require_ref(arguments["setup_policy_revision"]),
            idempotency_key=require_ref(arguments["idempotency_key"]),
            creation_ref=require_ref(arguments["creation_ref"]) if "creation_ref" in keys else None,
            workflow_ref=require_ref(arguments["workflow_ref"]) if "workflow_ref" in keys else None,
        )
    raise IntakeError("invalid_arguments")


def _catalog(page: CatalogPage, limit: int) -> dict[str, Any]:
    if len(page.sources) > limit:
        raise IntakeError("intake_unavailable")
    items: list[dict[str, str]] = []
    for source in page.sources:
        if isinstance(source, CatalogFeed):
            items.append(
                {
                    "feed_binding_ref": require_ref(source.feed_binding_ref),
                    "binding_version": require_ref(source.binding_version),
                    "feed_kind": require_ref(source.feed_kind),
                    "source_revision": require_ref(source.source_revision),
                }
            )
        else:
            items.append(
                {
                    "source_kind": require_ref(source.source_kind),
                    "source_ref": require_ref(source.source_ref),
                    "source_revision": require_ref(source.source_revision),
                }
            )
    return {
        "sources": items,
        "next_cursor": require_ref(page.next_cursor) if page.next_cursor is not None else None,
    }


def _receipt(receipt: AdmissionReceipt | AdmissionHold) -> dict[str, Any]:
    if isinstance(receipt, AdmissionHold):
        held = {"state": "held", "reason_code": require_ref(receipt.reason_code)}
        if receipt.successor_ref is not None:
            held["successor_feed_binding_ref"] = require_ref(receipt.successor_ref)
        return held
    return {
        "run_ref": require_ref(receipt.run_ref),
        "state": require_ref(receipt.state),
        "correlation_ref": require_ref(receipt.correlation_ref),
    }


def _status(status: RunStatus) -> dict[str, Any]:
    value: dict[str, Any] = {
        "run_ref": require_ref(status.run_ref),
        "state": require_ref(status.state),
        "next_action": require_ref(status.next_action),
        "reason_code": require_ref(status.reason_code) if status.reason_code is not None else None,
        "workflow_ref": require_ref(status.workflow_ref),
    }
    if status.benchmark is not None:
        value.update(
            benchmark_ref=require_ref(status.benchmark.benchmark_ref),
            version=(
                status.benchmark.version
                if type(status.benchmark.version) is int
                else require_ref(status.benchmark.version)
            ),
            benchmark_state=require_ref(status.benchmark.benchmark_state),
        )
    return value


def intake_dispatch(
    principal: IntakePrincipal, admission: IntakeAdmission
) -> Callable[[str, dict[str, Any]], dict[str, Any]]:
    """Serve the sole role selected by the trusted launcher before JSON-RPC negotiation."""
    if principal.role != "intake":
        raise IntakeError("intake_role_required")
    require_ref(principal.tenant)
    require_ref(principal.actor)
    calls: deque[float] = deque()

    def call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        now = monotonic()
        while calls and calls[0] <= now - 60:
            calls.popleft()
        if len(calls) >= CALLS_PER_MINUTE:
            return tool_error("intake_rate_limited")
        calls.append(now)
        try:
            if name == DISCOVER and arguments.keys() <= {"limit", "cursor"}:
                limit = arguments.get("limit", MAX_PAGE)
                if type(limit) is not int or not 1 <= limit <= MAX_PAGE:
                    raise IntakeError("invalid_arguments")
                cursor = require_ref(arguments["cursor"]) if "cursor" in arguments else None
                return tool_result(
                    _catalog(
                        admission.discover_sources(principal, limit=limit, cursor=cursor), limit
                    )
                )
            if name == SUBMIT:
                return tool_result(
                    _receipt(admission.submit_intake(principal, parse_intake(arguments)))
                )
            if name == GET_RUN and arguments.keys() == {"run_ref"}:
                return tool_result(
                    _status(admission.get_run(principal, require_ref(arguments["run_ref"])))
                )
            return tool_error("invalid_arguments")
        except IntakeError as error:
            return tool_error(str(error))
        except (OSError, sqlite3.Error):
            return tool_error("intake_unavailable")

    return call


def serve_intake(
    principal: IntakePrincipal,
    admission: IntakeAdmission,
    source: BinaryIO,
    destination: BinaryIO,
) -> int:
    call = intake_dispatch(principal, admission)
    try:
        return serve_tools("opentour-intake-v1", TOOLS, call, source, destination)
    except BrokenPipeError:
        return 0
