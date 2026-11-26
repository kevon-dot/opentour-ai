"""Read one owner-authorized starter policy and verify its exact citation spans."""

from __future__ import annotations

import hashlib
import os
import re
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from opentour_cell.pilot.intake_admission import DurableIntake, _Resolved, _serial
from opentour_cell.pilot.intake_local import LocalStarterAuthority, VerifiedStarterGrant
from opentour_cell.pilot.intake_mcp import IntakePrincipal, Starter
from opentour_cell.pilot.onboarding_mcp import _private_parent
from opentour_cell.pilot.receipt_recovery import _open_db

MAX_POLICY_BYTES = 256 * 1024
REVISION = re.compile(r"[0-9a-f]{64}\Z")
HEADING = re.compile(r" {0,3}#{1,6} +\S")
_FENCE = re.compile(r" {0,3}(`{3,}|~{3,})([^\r\n]*)")


class PolicyError(ValueError):
    """Fixed worker diagnostic that never echoes policy text or filenames."""


def operative_lines(lines: tuple[str, ...]) -> Iterator[tuple[int, str]]:
    """Keep original line numbers while excluding Markdown examples and HTML comments."""
    active_fence: str | None = None
    comment = False
    for number, raw in enumerate(lines, start=1):
        if active_fence is not None:
            fence = _FENCE.fullmatch(raw.rstrip("\n"))
            if fence and fence[1].startswith(active_fence) and not fence[2].strip():
                active_fence = None
            continue
        line = raw
        visible = []
        while line:
            if comment:
                _, end, line = line.partition("-->")
                comment = not end
            else:
                prefix, begin, line = line.partition("<!--")
                visible.append(prefix)
                comment = bool(begin)
        text = " ".join(visible).rstrip("\n")
        fence = _FENCE.fullmatch(text)
        if fence:
            active_fence = fence[1]
        elif text.strip() and not text.startswith(("    ", "\t")):
            yield number, text


@dataclass(frozen=True)
class PolicySpan:
    resource_ref: str
    source_revision: str
    section_ref: str
    line_start: int
    line_end: int
    byte_start: int
    byte_end: int
    passage_sha256: str


@dataclass(frozen=True)
class PolicySnapshot:
    resource_ref: str
    source_revision: str
    grant_revision: str
    normalized_sha256: str
    lines: tuple[str, ...]

    def cite(self, line_start: int, line_end: int) -> PolicySpan:
        """Use one-based inclusive lines and zero-based half-open normalized UTF-8 bytes."""
        if not 1 <= line_start <= line_end <= len(self.lines):
            raise PolicyError("policy_span_unavailable")
        start = line_start - 1
        section = 0
        for index, line in operative_lines(self.lines[:line_end]):
            if not HEADING.match(line):
                continue
            if index > line_start:
                raise PolicyError("policy_span_crosses_sections")
            section = index
        before = "".join(self.lines[:start]).encode()
        passage = "".join(self.lines[start:line_end]).encode()
        return PolicySpan(
            self.resource_ref,
            self.source_revision,
            f"section-{section}",
            line_start,
            line_end,
            len(before),
            len(before) + len(passage),
            hashlib.sha256(passage).hexdigest(),
        )

    def passage(self, citation: PolicySpan) -> str:
        if self.cite(citation.line_start, citation.line_end) != citation:
            raise PolicyError("policy_span_revision_changed")
        return "".join(self.lines[citation.line_start - 1 : citation.line_end])


def _read_policy(path: Path, grant: VerifiedStarterGrant) -> PolicySnapshot:
    if path.suffix.lower() not in {".md", ".txt"}:
        raise PolicyError("policy_format_unsupported")
    if not REVISION.fullmatch(grant.source_revision):
        raise PolicyError("policy_content_digest_required")
    try:
        _private_parent(path)
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
                or info.st_nlink != 1
                or info.st_size > MAX_POLICY_BYTES
            ):
                raise PolicyError("policy_source_unavailable")
            raw = stream.read(MAX_POLICY_BYTES + 1)
        if len(raw) > MAX_POLICY_BYTES:
            raise PolicyError("policy_source_unavailable")
        if hashlib.sha256(raw).hexdigest() != grant.source_revision:
            raise PolicyError("policy_source_revision_changed")
        normalized = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    except OSError:
        raise PolicyError("policy_source_unavailable") from None
    except UnicodeError:
        raise PolicyError("policy_format_unsupported") from None
    if not normalized.strip() or any(ch < " " and ch not in "\n\t" for ch in normalized):
        raise PolicyError("policy_format_unsupported")
    return PolicySnapshot(
        grant.policy_resource_ref,
        grant.source_revision,
        grant.revision,
        hashlib.sha256(normalized.encode()).hexdigest(),
        tuple(normalized.splitlines(keepends=True)),
    )


class GuidedPolicySource:
    """The path is selected on the worker. Requests only identify an admitted workflow."""

    def __init__(self, authority: LocalStarterAuthority, policy_file: Path):
        self.authority = authority
        directory = authority.path.parent
        if policy_file.parent != directory or policy_file.name in {"", ".", ".."}:
            raise PolicyError("policy_source_unavailable")
        self.path = policy_file

    def _require_workflow(self, workflow_ref: str, grant: VerifiedStarterGrant) -> None:
        directory = self.authority.path.parent
        selected = self.authority.selected
        initial_request = Starter(
            grant.policy_resource_ref,
            grant.source_revision,
            grant.setup_policy_revision,
            "unused-by-receipt",
            grant.creation_ref,
            None,
        )
        authority = self.authority.starter(self.authority.principal, initial_request)
        if authority is None:
            raise PolicyError("policy_workflow_unavailable")
        resolved = _Resolved(
            authority, (grant.source_revision, grant.setup_policy_revision), "starter"
        )
        intent = DurableIntake._intent(workflow_ref, resolved)
        with _open_db(directory / "intake.sqlite3") as db:
            if db is None:
                raise PolicyError("policy_workflow_unavailable")
            row = db.execute(
                "SELECT 1 FROM intake_runs r JOIN intake_workflows w "
                "ON r.tenant=w.tenant AND r.workflow_ref=w.workflow_ref "
                "WHERE w.workflow_ref=? AND w.tenant=? AND w.epoch=? "
                "AND w.creation_ref=? AND w.resource=? AND r.grant_revision=? "
                "AND r.request=? AND r.intent=? "
                "LIMIT 1",
                (
                    workflow_ref,
                    selected.tenant,
                    selected.engagement_epoch,
                    grant.creation_ref,
                    grant.policy_resource_ref,
                    grant.revision,
                    _serial(initial_request, "starter"),
                    intent,
                ),
            ).fetchone()
        if row is None:
            raise PolicyError("policy_workflow_unavailable")

    def read(self, actor: IntakePrincipal, workflow_ref: str) -> PolicySnapshot:
        with self.authority.setup_grant(actor, read_only=True) as grant:
            self._require_workflow(workflow_ref, grant)
            snapshot = _read_policy(self.path, grant)
        with self.authority.setup_grant(actor, read_only=True) as current:
            if current != grant:
                raise PolicyError("policy_source_revision_changed")
            self._require_workflow(workflow_ref, current)
        return snapshot
