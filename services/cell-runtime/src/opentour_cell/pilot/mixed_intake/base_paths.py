"""Base starter paths that must never depend on the mixed-source adapter route.

The one-policy-only and structured-only paths read the owner's bytes directly with the
deterministic readers. They import no adapter, open no connector and so cannot be blocked when
the mixed-source route is unavailable. Simulated/not proven: no provider or live OPE-505 worker.
"""

from __future__ import annotations

from opentour_cell.pilot.mixed_intake import readers
from opentour_cell.pilot.mixed_intake.model import (
    Deadline,
    ExtractionState,
    Json,
    Limits,
    ReadResult,
    Role,
    digest,
    sha256,
)
from opentour_cell.pilot.mixed_intake.service import DraftSink, IntakeError


def _eligible(data: bytes, name: str, limits: Limits) -> tuple[str, ReadResult]:
    fmt, result = readers.read_object(data, name, limits, Deadline(limits.max_seconds))
    if result.state != ExtractionState.ELIGIBLE:
        raise IntakeError("SOURCE_NOT_USABLE:" + result.state.value.upper())
    return fmt, result


def freeze_single_policy_starter(
    markdown: bytes,
    *,
    tenant: str,
    workflow_ref: str,
    sink: DraftSink | None = None,
    limits: Limits | None = None,
) -> Json:
    """A founder supplies one Markdown policy directly. One draft, one job, no adapter."""
    fmt, result = _eligible(markdown, "policy.md", limits or Limits())
    if fmt not in ("markdown", "text"):
        raise IntakeError("POLICY_MUST_BE_MARKDOWN")
    content = sha256(markdown)
    key = digest(["ope505-draft", tenant, workflow_ref, "single-policy", content])
    payload: Json = {
        "scope": "scoped_starter",
        "route": "single_policy",
        "included": [{"role": Role.POLICY.value, "content_digest": content}],
        "spans": len(result.spans),
        "exclusions": [],
    }
    if sink is not None:
        sink.create_draft(key, payload)
        sink.enqueue_job(digest(["ope505-job", key]), key)
    return {"draft_key": key, **payload}


def structured_only_intake(
    data: bytes,
    *,
    name: str,
    tenant: str,
    workflow_ref: str,
    limits: Limits | None = None,
) -> Json:
    """Structured event-pair rows with header/path + row lineage and no adapter."""
    fmt, result = _eligible(data, name, limits or Limits())
    if fmt not in ("csv", "jsonl"):
        raise IntakeError("STRUCTURED_SOURCE_REQUIRED")
    return {
        "route": "structured_only",
        "tenant": tenant,
        "workflow_ref": workflow_ref,
        "format": fmt,
        "rows": len(result.spans),
        "content_digest": sha256(data),
        "lineage": [s.locator for s in result.spans[:3]],
    }
