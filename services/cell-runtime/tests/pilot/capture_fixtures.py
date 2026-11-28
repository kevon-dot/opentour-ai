"""Generated fixtures for decision-capture adapter and original-record tests.

Every value is a generated sentinel or an opaque reference. Nothing here is customer data,
and no fixture contacts a provider.
"""

from __future__ import annotations

import base64
import hmac
import json
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

from opentour_cell.pilot.capture_streams import CaptureGrant, CaptureStreams
from opentour_cell.pilot.intake_mcp import IntakeError, IntakePrincipal
from opentour_cell.pilot.live_evidence import FeedGrant, LiveEvidence, LiveScope
from opentour_cell.pilot.local_capture_authority import PURPOSE, SCHEMA, LocalCaptureAuthority
from opentour_cell.pilot.operating_point import canonical
from opentour_cell.pilot.training import LocalTrainingStore
from test_intake_mcp import _write_private

WORKFLOW = "invoice-workflow"
KEY = bytes(range(32))
NOW = 1_000
GATEWAY = "gateway-log"
JOURNAL = "decision-journal"
HISTORY = "historical-export"
SOURCES = (
    ("application_request", GATEWAY),
    ("provider_response", GATEWAY),
    ("application_commit", JOURNAL),
    ("historical_export", HISTORY),
)
RECORDERS = {
    "application_request": "gateway-collector",
    "provider_response": "gateway-collector",
    "application_commit": "journal-collector",
    "historical_export": "history-collector",
}
INSPECTORS = {
    "owner": tuple(boundary for boundary, _ in SOURCES),
    "response-inspector": ("provider_response",),
    "history-inspector": ("historical_export",),
}
REQUEST_SENTINEL = "GENERATED_REQUEST_SENTINEL_7Q4K2W"
RESPONSE_SENTINEL = "GENERATED_RESPONSE_SENTINEL_9X3M8P"
CREDENTIAL_SENTINEL = "GENERATED-CREDENTIAL-SENTINEL-5T1Q8"
CUSTOMER_SENTINEL = "generated.customer.sentinel@example.invalid"
RESTRICTED = (REQUEST_SENTINEL, RESPONSE_SENTINEL, CREDENTIAL_SENTINEL, CUSTOMER_SENTINEL)


def projection_bytes(value: dict[str, Any]) -> bytes:
    """The declared canonical-json-v1 serializer."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


REQUEST_PROJECTION = projection_bytes(
    {
        "attachments": [{"ref": "PO-v1", "transform": "ocr-v2"}],
        "case": {"description": "carrier charge " + REQUEST_SENTINEL, "missing": None},
        "instructions": {"template": "P7"},
        "tools": [],
    }
)
COMPONENTS = ("attachments", "case", "instructions", "tools")
# Credentials and unrelated source fields the allowlist must drop before persistence.
UNRELATED = {
    "authorization": CREDENTIAL_SENTINEL,
    "requester_email": CUSTOMER_SENTINEL,
    "debug": {"nested": [REQUEST_SENTINEL, {"deeper": RESPONSE_SENTINEL}]},
}
STREAM = (b'{"label":"SUP', b'PLIES","note":"', RESPONSE_SENTINEL.encode() + b'"}')


class Authority:
    """Generated grant resolver: one recorder per boundary and scoped inspectors."""

    def __init__(self, store: LocalTrainingStore, mode: str = "metadata_only") -> None:
        self.base = CaptureGrant(
            "unused",
            store.organization,
            store.engagement_id,
            WORKFLOW,
            "capture-grant-v1",
            "policy-v7",
            2 * NOW,
            mode,  # type: ignore[arg-type]
            SOURCES,
            KEY,
        )
        self.calls = 0

    def resolve(self, actor, workflow, action, boundary):
        self.calls += 1
        if workflow != WORKFLOW:
            return None
        sources = dict(self.base.sources)
        if action == "record" and RECORDERS.get(boundary) == actor and boundary in sources:
            return replace(self.base, actor=actor, sources=((boundary, sources[boundary]),))
        if action == "inspect" and boundary is None and actor in INSPECTORS:
            allowed = tuple((b, s) for b, s in self.base.sources if b in INSPECTORS[actor])
            return replace(self.base, actor=actor, sources=allowed)
        return None


def ledger(path: Path, tenant: str = "org-a", mode: str = "metadata_only", clock=lambda: NOW):
    path.mkdir(mode=0o700, parents=True)
    store = LocalTrainingStore(path / "training.sqlite3", tenant)
    authority = Authority(store, mode)
    return CaptureStreams(store, authority, clock), authority


def reopen(capture: CaptureStreams, clock=lambda: NOW) -> CaptureStreams:
    store = LocalTrainingStore(capture.store.path, capture.store.organization, existing_only=True)
    return CaptureStreams(store, capture.authority, clock)


def install_local(capture: CaptureStreams, **changes: Any) -> LocalCaptureAuthority:
    """Seal a real local capture grant for the same scope and return its resolver."""
    store = capture.store
    directory = store.path.parent
    authority = LocalCaptureAuthority(directory, store.organization, WORKFLOW)
    _write_private(directory / "capture-authority-key.json", {"key": KEY.hex()})
    grant = {
        "schema_version": SCHEMA,
        "purpose": PURPOSE,
        "tenant": store.organization,
        "engagement": store.engagement_id,
        "workflow": WORKFLOW,
        "grant_ref": "generated-capture-permission",
        "policy_revision": "policy-v7",
        "mode": "metadata_only",
        "expires_at": 2 * NOW,
        "recorders": {
            boundary: {"actor": RECORDERS[boundary], "source": source}
            for boundary, source in SOURCES
        },
        "inspectors": {actor: list(boundaries) for actor, boundaries in INSPECTORS.items()},
        **changes,
    }
    seal = hmac.digest(KEY, canonical(grant).encode(), "sha256").hex()
    _write_private(authority.path, {"grant": grant, "seal_sha256": seal})
    return authority


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def jsonl(records: list[dict[str, Any]]) -> bytes:
    return b"".join(json.dumps(record, sort_keys=True).encode() + b"\n" for record in records)


def identity(
    attempt: str,
    *,
    operation: str = "op-d1",
    case: str = "case-d1",
    family: str = "family-d1",
    decision: str = "D1",
    revision: int = 1,
) -> dict[str, Any]:
    return {
        "operation_id": operation,
        "attempt_id": attempt,
        "case_id": case,
        "family_id": family,
        "decision_id": decision,
        "decision_revision": revision,
    }


def _envelope(kind: str, event: str, observed_at: int, ids: dict[str, Any]) -> dict[str, Any]:
    schema = (
        "opentour.capture.decision-journal.v1"
        if kind == "commit"
        else "opentour.capture.gateway-log.v1"
    )
    return {
        "schema": schema,
        "record": kind,
        "event_id": event,
        "event_revision": 1,
        "observed_at": observed_at,
        **ids,
    }


def request(attempt: str, event: str, observed_at: int, *, parent=None, **ids):
    return {
        **_envelope("request", event, observed_at, identity(attempt, **ids)),
        "parent_attempt_id": parent,
        "requested_model": "invoice-alias",
        "projection_revision": "projection-v1",
        "template_revision": "P7",
        "request_projection_b64": b64(REQUEST_PROJECTION),
        **UNRELATED,
    }


def chunk(attempt: str, event: str, observed_at: int, index: int, data: bytes, **ids):
    return {
        **_envelope("chunk", event, observed_at, identity(attempt, **ids)),
        "index": index,
        "data_b64": b64(data),
        "provider_headers": {"x-trace": CREDENTIAL_SENTINEL},
    }


def terminal(attempt: str, event: str, observed_at: int, state: str, expected, **fields):
    ids = {key: fields.pop(key) for key in list(fields) if key in _IDS}
    record = {
        **_envelope("terminal", event, observed_at, identity(attempt, **ids)),
        "state": state,
        "expected_chunks": expected,
    }
    return record | fields


def final_body(attempt: str, event: str, observed_at: int, chunks, **ids):
    body = b"".join(chunks)
    return {
        **_envelope("final_body", event, observed_at, identity(attempt, **ids)),
        "body_b64": b64(body),
        "chunk_lengths": [len(part) for part in chunks],
    }


def commit(attempt: str, event: str, observed_at: int, payload: str, ref: str = "commit-d1", **ids):
    return {
        **_envelope("commit", event, observed_at, identity(attempt, **ids)),
        "commit_ref": ref,
        "decision_payload_ref": payload,
        "operator_note": CUSTOMER_SENTINEL,
    }


_IDS = {"operation", "case", "family", "decision", "revision"}


def fixture_w_gateway() -> list[dict[str, Any]]:
    """Worked acceptance 1: a1 streams then times out; the app's own retry a2 finishes.

    Chunks arrive out of order, one is delivered twice, and chunk 1 arrives last.
    """
    return [
        request("a1", "request-a1", 100),
        chunk("a1", "chunk-a1-0", 101, 0, b'{"label":"SUP'),
        terminal("a1", "terminal-a1", 105, "timeout", None),
        request("a2", "request-a2", 110, parent="a1"),
        chunk("a2", "chunk-a2-2", 112, 2, STREAM[2]),
        chunk("a2", "chunk-a2-0", 111, 0, STREAM[0]),
        chunk("a2", "chunk-a2-2", 112, 2, STREAM[2]),
        terminal(
            "a2",
            "terminal-a2",
            115,
            "finished",
            3,
            provider_receipt="receipt-a2",
            parsed_output="SUPPLIES",
            parser_revision="parser-v1",
        ),
        chunk("a2", "chunk-a2-1", 113, 1, STREAM[1]),
    ]


def fixture_w_journal(payload: str = "payload-supplies") -> list[dict[str, Any]]:
    return [commit("a2", "commit-d1", 120, payload)]


# OPE-514 live reducer fixtures: the authoritative source of committed decisions and truth.
LIVE_ACTOR = IntakePrincipal("org-a", "custodian", "intake")
DECISIONS = FeedGrant(
    "decisions", "decision", "grant-v1", "source-v1", "invoice-app", 10, "lease-a"
)
TRUTH = FeedGrant(
    "truth",
    "independent_outcome",
    "truth-grant-v1",
    "truth-v1",
    "invoice-app",
    20,
    "lease-a",
    "independent-verifier",
)


class LiveAuthority:
    def __init__(self) -> None:
        self.scope = LiveScope(
            "org-a",
            WORKFLOW,
            "epoch-a",
            "dataset-a",
            "dataset-v1",
            (DECISIONS, TRUTH),
            True,
            True,
            True,
            "custodian",
            2**40,
        )

    @contextmanager
    def guard(self, actor, workflow):
        if actor.tenant != self.scope.tenant or workflow != self.scope.workflow_ref:
            raise IntakeError("live_evidence_unavailable")
        yield self.scope


class LiveCase:
    """A generated OPE-514 reducer with one decision feed and one independent outcome feed."""

    def __init__(self, path: Path) -> None:
        path.mkdir(mode=0o700, parents=True)
        self.store = LiveEvidence(path, LiveAuthority())
        self.store.initialize()
        self.decisions = self.store.save_binding(LIVE_ACTOR, WORKFLOW, "decisions")
        self.outcomes = self.store.save_binding(LIVE_ACTOR, WORKFLOW, "truth")
        self.page = 0

    def ingest(self, *events, outcome: bool = False) -> None:
        self.page += 1
        self.store.ingest_page(
            LIVE_ACTOR,
            self.outcomes if outcome else self.decisions,
            page_ref=f"page-{self.page}",
            source_revision="truth-v1" if outcome else "source-v1",
            events=events,
            source_positions=tuple(30 + self.page for _ in events),
            event_epochs=tuple("epoch-a" for _ in events),
            checkpoint=30 + self.page,
        )

    def snapshot(self):
        return self.store.snapshot(LIVE_ACTOR, WORKFLOW)


def live_decision(decision: str = "D1", action: str = "SUPPLIES", **changes: Any) -> dict:
    return {
        "kind": "DecisionRecorded",
        "event_id": "delivery-" + decision,
        "event_revision": 1,
        "decision_id": decision,
        "decision_revision": 1,
        "case_id": "case-" + decision.lower(),
        "family_id": "family-d1",
        "decision_at": 120,
        "action_ref": action,
        "policy_ref": "P7",
        "input_ref": None,
        "input_fidelity": "unavailable",
        "source_provenance": "application-commit",
        "actor_ref": None,
        "model_ref": None,
        **changes,
    }


def live_outcome(decision: str = "D1", verified: str = "REVIEW", **changes: Any) -> dict:
    return {
        "kind": "OutcomeVerified",
        "event_id": "truth-" + decision,
        "event_revision": 1,
        "decision_id": decision,
        "decision_revision": 1,
        "outcome_id": "O-" + decision,
        "outcome_revision": 1,
        "effective_at": 130,
        "recorded_at": 131,
        "finality": "final",
        "verified_action_ref": verified,
        "verifier": "independent-verifier",
        **changes,
    }
