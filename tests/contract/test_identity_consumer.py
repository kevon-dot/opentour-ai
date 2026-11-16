"""Owned identity schema and actual quality-command integration, not auth authority."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from tools import schema_validate
from tools import test_runner as runner

SCHEMA = "packages/contracts/platform/auth-context.schema.json"
URI = "urn:opentour:auth-context:1.0"
REQUIRED = (
    "apps/web/lib/server/tsconfig.json",
    "apps/web/lib/server/authkit-adapter.ts",
    "apps/web/app/auth/signin/route.ts",
    "apps/web/app/auth/callback/route.ts",
)


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    shutil.copytree(runner.ROOT / "packages/contracts", tmp_path / "packages/contracts")
    return tmp_path


def test_owned_auth_schema_resolves_common_refs_without_duplicate_source(checkout: Path) -> None:
    value = {
        "state": "active",
        "organization_id": "org-generated",
        "membership_id": "member-generated",
        "session_generation": 1,
        "request_id": "request-generated",
    }
    registry = schema_validate.schema_registry(checkout)
    assert schema_validate.valid_instance(URI, value, registry)
    for field, wrong in (
        ("organization_id", None),
        ("session_generation", 9007199254740992),
        ("session_generation", True),
        ("session_generation", 1.5),
        ("state", "forged-authorized"),
        ("request_id", ""),
    ):
        assert not schema_validate.valid_instance(URI, {**value, field: wrong}, registry)
    assert not schema_validate.valid_instance(URI, {**value, "access_token": "generated"}, registry)
    assert not (checkout / "packages/contracts/common/auth-context.schema.json").exists()


@pytest.mark.parametrize("mutation", ["hash", "escape", "remote"])
def test_owned_schema_registry_rejects_hash_escape_and_remote_resolution(
    checkout: Path, mutation: str
) -> None:
    manifest_path = checkout / "packages/contracts/common/schema-registry.json"
    manifest = json.loads(manifest_path.read_bytes())
    entry = next(row for row in manifest["schemas"] if row["id"] == URI)
    if mutation == "hash":
        entry["sha256"] = "f" * 64
    elif mutation == "escape":
        entry["file"] = "packages/contracts/../../generated-outside.json"
    else:
        path = checkout / SCHEMA
        document = json.loads(path.read_bytes())
        document["$ref"] = "https://generated.invalid/schema.json"
        path.write_text(json.dumps(document))
        entry["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(schema_validate.SchemaError):
        schema_validate.schema_registry(checkout)


@pytest.mark.parametrize("command", ["lint", "typecheck"])
def test_quality_dispatch_requires_real_owned_files(checkout: Path, command: str) -> None:
    assert runner.identity_quality_commands(checkout, command) == []
    first = checkout / REQUIRED[0]
    first.parent.mkdir(parents=True)
    first.write_text("{}")
    with pytest.raises(runner.CommandError):
        runner.identity_quality_commands(checkout, command)
    for name in REQUIRED:
        path = checkout / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}" if name.endswith(".json") else "export {};\n")
    argv = runner.identity_quality_commands(checkout, command)
    if command == "typecheck":
        assert argv == [("pnpm", "exec", "tsc", "--project", REQUIRED[0])]
    else:
        assert argv == [
            ("pnpm", "exec", "oxlint", "--deny-warnings", "apps/web/lib/server", *REQUIRED[2:])
        ]


def test_quality_dispatch_does_not_ignore_broken_source_symlink(checkout: Path) -> None:
    path = checkout / REQUIRED[0]
    path.parent.mkdir(parents=True)
    path.symlink_to(checkout / "missing-config")
    with pytest.raises(runner.CommandError):
        runner.identity_quality_commands(checkout, "typecheck")
