"""Enroll one operator-selected dataset through a separate local MCP server."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sqlite3
import stat
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from opentour_cell.pilot.mcp import serve_tools, tool_error, tool_result
from opentour_cell.pilot.readout import MAX_MANIFEST_BYTES, ManifestError, parse_manifest
from opentour_cell.pilot.text_classifier import TrainingError
from opentour_cell.pilot.training import (
    LocalTrainingStore,
    onboarding_store_available,
    registration_state,
)
from opentour_cell.pilot.training_data import canonical, identifier, prepare

PREVIEW = "preview_dataset_onboarding"
CONFIRM = "confirm_dataset_onboarding"
SCOPE = "register_local_dataset"
GRANT_VERSION = "opentour.pilot.onboarding-authorization.v2"
TOOLS = [
    {
        "name": PREVIEW,
        "description": "Inspect permitted use, fields, destination, retention and status.",
        "inputSchema": {"type": "object", "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
    },
    {
        "name": CONFIRM,
        "description": "Register the previewed revision with an existing local operator grant.",
        "inputSchema": {
            "type": "object",
            "properties": {"dataset_revision": {"type": "string", "pattern": "^[0-9a-f]{64}$"}},
            "required": ["dataset_revision"],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
]


def _private_parent(path: Path) -> None:
    item = path.parent.lstat()
    if not stat.S_ISDIR(item.st_mode) or item.st_uid != os.getuid() or item.st_mode & 0o077:
        raise TrainingError("private_directory_required")


def _read_private(path: Path, limit: int) -> dict[str, Any]:
    _private_parent(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        item = os.fstat(source.fileno())
        if (
            not stat.S_ISREG(item.st_mode)
            or item.st_uid != os.getuid()
            or item.st_mode & 0o077
            or item.st_nlink != 1
            or item.st_size > limit
        ):
            raise TrainingError("private_file_required")
        data = source.read(limit + 1)
    if len(data) > limit:
        raise TrainingError("private_file_required")
    return parse_manifest(data)


class Selection:
    def __init__(
        self,
        organization: str,
        store: Path,
        manifest: Path,
        authorization: Path,
        custodian_attestation: str,
    ):
        self.organization = identifier(organization)
        if not re.fullmatch(r"att-[0-9a-f]{16,64}", custodian_attestation):
            raise TrainingError("invalid_custodian_attestation")
        self.custodian_attestation = custodian_attestation
        self.store = store
        self.manifest = manifest
        self.authorization = authorization
        paths = (store, manifest, authorization)
        if any(not path.is_absolute() for path in paths) or len(set(paths)) != len(paths):
            raise TrainingError("distinct_absolute_files_required")
        for path in paths:
            _private_parent(path)
        data = prepare(_read_private(manifest, MAX_MANIFEST_BYTES), self.organization)
        self.name = data.name
        self.revision = data.revision
        self.training_rows = len(data.train)
        self.heldout_rows = len(data.heldout)
        self.dataset_expires = data.expires_at
        self.scope: dict[str, Any] = {
            "mode": data.mode,
            "purpose": data.purpose,
            "permitted_actions": data.actions,
            "permitted_fields": data.fields,
            "destination": data.destination,
            "retention": data.retention,
        }
        self.previewed = False
        self.previewed_grant_expiry: int | None = None

    def binding(self) -> dict[str, Any]:
        return {
            "schema_version": GRANT_VERSION,
            "scope": SCOPE,
            "organization": self.organization,
            "dataset_id": self.name,
            "dataset_revision": self.revision,
            "store_binding": hashlib.sha256(os.fsencode(self.store.resolve())).hexdigest(),
            "custodian_attestation": self.custodian_attestation,
            **self.scope,
        }

    def _current_manifest(self) -> dict[str, Any]:
        value = _read_private(self.manifest, MAX_MANIFEST_BYTES)
        if prepare(value, self.organization).revision != self.revision:
            raise TrainingError("selection_changed_restart")
        return value

    def registration(self) -> str:
        if self.dataset_expires is not None and self.dataset_expires <= time.time():
            return "unavailable"
        return registration_state(self.store, self.organization, self.name, self.revision)

    def _check_grant(self) -> int:
        if self.scope["retention"] == {"disposition": "unrecorded"}:
            raise TrainingError("retention_terms_required")
        try:
            grant = _read_private(self.authorization, 4096)
        except FileNotFoundError:
            raise TrainingError("authorization_required") from None
        except (OSError, ManifestError, TrainingError):
            raise TrainingError("authorization_invalid") from None
        expires = grant.pop("expires_at", None)
        if type(expires) is not int or not 0 < expires < 2**63 or grant != self.binding():
            raise TrainingError("authorization_scope_mismatch")
        now = time.time()
        if expires <= now or (self.dataset_expires is not None and self.dataset_expires <= now):
            raise TrainingError("authorization_expired")
        return expires

    def _status(self) -> dict[str, Any]:
        expires = None
        try:
            expires = self._check_grant()
            authority = "authorized"
        except TrainingError as error:
            authority = str(error)
        current = self.registration()
        if current == "unavailable":
            return {"registration": current}
        return {
            "registration": current,
            "authorization": authority,
            "grant_expires_at": expires,
            **self.scope,
            "dataset_revision": self.revision,
            "training_rows": self.training_rows,
            "heldout_rows": self.heldout_rows,
        }

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            if name == PREVIEW:
                if arguments:
                    return tool_error("preview_accepts_no_arguments")
                self.previewed = False
                self.previewed_grant_expiry = None
                if self.registration() == "unavailable":
                    return tool_result({"registration": "unavailable"})
                self._current_manifest()
                status = self._status()
                if status["registration"] != "unavailable":
                    self.previewed = True
                    self.previewed_grant_expiry = status["grant_expires_at"]
                return tool_result(status)
            if name != CONFIRM or set(arguments) != {"dataset_revision"}:
                return tool_error("expected_dataset_revision_only")
            if not self.previewed:
                return tool_error("preview_required")
            if self.registration() == "unavailable":
                return tool_error("private_selection_unavailable")
            if arguments["dataset_revision"] != self.revision:
                return tool_error("revision_mismatch")
            expires = self._check_grant()
            if self.previewed_grant_expiry is not None and self.previewed_grant_expiry != expires:
                return tool_error("authorization_changed_preview_again")
            manifest = self._current_manifest()

            def authorize_commit() -> None:
                self._current_manifest()
                if self._check_grant() != expires:
                    raise TrainingError("authorization_changed_preview_again")

            store = LocalTrainingStore(self.store, self.organization)
            stored = store.register(manifest, check_authorization=authorize_commit)
            return tool_result(
                {"registration": "registered", "dataset_revision": stored["dataset_revision"]}
            )
        except TrainingError as error:
            return tool_error(str(error))
        except (OSError, ManifestError, sqlite3.Error):
            return tool_error("private_selection_unavailable")


def _arguments(*, owner: bool = False) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--organization", required=True)
    parser.add_argument("--store", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--authorization", required=True, type=Path)
    parser.add_argument("--custodian-attestation", required=True, help="opaque private receipt ID")
    if owner:
        parser.add_argument("--dataset-revision", required=True)
        parser.add_argument("--expires-at", required=True, type=int, help="UTC Unix seconds")
    return parser.parse_args()


def _open_selection(args: argparse.Namespace) -> Selection | None:
    if not onboarding_store_available(args.store, args.organization):
        return None
    return Selection(
        args.organization, args.store, args.manifest, args.authorization, args.custodian_attestation
    )


def _unavailable(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == PREVIEW and not arguments:
        return tool_result({"registration": "unavailable"})
    return tool_error("private_selection_unavailable")


def authorize_main() -> int:
    """Operator-only local command; this authority is never advertised as an MCP tool."""
    args = _arguments(owner=True)
    try:
        selected = _open_selection(args)
        if selected is None or selected.registration() == "unavailable":
            raise TrainingError("private_selection_unavailable")
        if (
            re.fullmatch(r"[0-9a-f]{64}", args.dataset_revision) is None
            or args.dataset_revision != selected.revision
        ):
            raise TrainingError("revision_mismatch")
        if not time.time() < args.expires_at < 2**63:
            raise TrainingError("authorization_expired")
        if selected.dataset_expires is not None and selected.dataset_expires <= time.time():
            raise TrainingError("dataset_expired")
        if selected.scope["retention"] == {"disposition": "unrecorded"}:
            raise TrainingError("retention_terms_required")
        grant = selected.binding() | {"expires_at": args.expires_at}
        # Link a complete mode-0600 file without replacing a prior permission or following a link.
        with tempfile.NamedTemporaryFile(dir=selected.authorization.parent) as staging:
            staging.write((canonical(grant) + "\n").encode())
            staging.flush()
            os.fsync(staging.fileno())
            os.link(staging.name, selected.authorization)
        print("Enrollment authorization recorded for the selected revision.")
        return 0
    except FileExistsError:
        print("authorization_already_exists", file=sys.stderr)
    except TrainingError as error:
        print(str(error), file=sys.stderr)
    except (OSError, ManifestError):
        print("private_selection_unavailable", file=sys.stderr)
    return 2


def main() -> int:
    args = _arguments()
    try:
        selected = _open_selection(args)
        return serve_tools(
            "opentour-dataset-onboarding",
            TOOLS,
            selected.call if selected is not None else _unavailable,
            sys.stdin.buffer,
            sys.stdout.buffer,
        )
    except BrokenPipeError:
        return 0
    except (OSError, ManifestError, TrainingError):
        print("Unable to open the selected private onboarding files.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
