"""Journal shape registration only; schema validity never establishes authority."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from tools import schema_validate

URI = "urn:opentour:authority-journal:1.0"
SCHEMA = "packages/contracts/authority-journal.schema.json"


def head() -> dict[str, object]:
    return {
        "organization_id": "generated-org",
        "sequence": 0,
        "chain_sha256": "a" * 64,
        "latest_epoch": 0,
        "latest_authority_revision": "generated-authority",
        "unresolved_operation_id": None,
        "revision": "9223372036854775807",
    }


def test_registered_journal_head_resolves_exact_common_primitives() -> None:
    registry = schema_validate.schema_registry()
    assert schema_validate.valid_instance(f"{URI}#/$defs/HeadState", head(), registry)
    duplicate = schema_validate.ROOT / "packages/contracts/common/authority-journal.schema.json"
    assert not duplicate.exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("revision", "9223372036854775808"),
        ("revision", "01"),
        ("revision", 1),
        ("sequence", 9007199254740992),
        ("sequence", True),
        ("latest_epoch", -1),
        ("organization_id", ""),
        ("raw_workflow_content", "generated-sentinel"),
    ],
)
def test_journal_registration_retains_closed_shape_and_numeric_bounds(
    field: str, value: object
) -> None:
    registry = schema_validate.schema_registry()
    assert not schema_validate.valid_instance(
        f"{URI}#/$defs/HeadState", {**head(), field: value}, registry
    )


@pytest.mark.parametrize("mutation", ["hash", "remote", "escape"])
def test_journal_registry_rejects_changed_source_or_network_refs(
    tmp_path: Path, mutation: str
) -> None:
    shutil.copytree(schema_validate.ROOT / "packages/contracts", tmp_path / "packages/contracts")
    path = tmp_path / "packages/contracts/common/schema-registry.json"
    manifest = json.loads(path.read_bytes())
    entry = next(row for row in manifest["schemas"] if row["id"] == URI)
    if mutation == "hash":
        entry["sha256"] = "f" * 64
    elif mutation == "escape":
        entry["file"] = "packages/contracts/../../generated-outside.json"
    else:
        schema_path = tmp_path / SCHEMA
        document = json.loads(schema_path.read_bytes())
        document["$defs"]["JournalIdentity"]["$ref"] = "https://generated.invalid/schema.json"
        schema_path.write_text(json.dumps(document))
        entry["sha256"] = hashlib.sha256(schema_path.read_bytes()).hexdigest()
    path.write_text(json.dumps(manifest))
    with pytest.raises(schema_validate.SchemaError):
        schema_validate.schema_registry(tmp_path)
