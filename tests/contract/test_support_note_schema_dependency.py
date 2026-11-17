"""Canonical support-note scope dependency; shape validation grants no support authority."""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from tools import schema_validate

URI = "urn:opentour:support-case:1.0"
SCOPE = URI + "#/$defs/SupportNoteGrantScope"
SOURCE = "packages/contracts/common/support-case.schema.json"
SOURCE_SHA = "e393eec1cb6eb9592db2bbaef665d544cec0ba4a56b3ff4aa3ab3b94986aad08"
READ: dict[str, Any] = {
    "resource_type": "support_note_read",
    "case_id": "generated-case",
    "note_ref": {
        "environment_id": "generated-environment",
        "note_id": "generated-note",
        "revision": "1",
    },
}
WRITE: dict[str, Any] = {
    "resource_type": "support_note_write",
    "case_id": "generated-case",
    "environment_id": "generated-environment",
    "write_intent_id": "generated-write-intent",
    "max_utf8_bytes": 16384,
}


def test_exact_source_bytes_identity_and_local_reference_closure() -> None:
    source = schema_validate.ROOT / SOURCE
    assert hashlib.sha256(source.read_bytes()).hexdigest() == SOURCE_SHA
    document = schema_validate.read_json(source)
    assert document["$id"] == URI
    # The original file is a definition library. Consumers select the specific definition,
    # rather than treating its unconstrained root as validation of a grant or support request.
    assert set(document) == {"$schema", "$id", "title", "$defs"}
    registry = schema_validate.schema_registry()
    assert registry.resolver().lookup(SCOPE).contents == document["$defs"]["SupportNoteGrantScope"]
    assert (
        registry.resolver().lookup(URI + "#/$defs/SupportNoteRef").contents
        == document["$defs"]["SupportNoteRef"]
    )


@pytest.mark.parametrize("value", [READ, WRITE])
def test_canonical_read_and_write_scope_branches_resolve_offline(value: dict[str, Any]) -> None:
    assert schema_validate.valid_instance(SCOPE, value, schema_validate.schema_registry())


@pytest.mark.parametrize(
    "value,field",
    [(WRITE, field) for field in WRITE] + [(READ, field) for field in READ],
    ids=["write-" + field for field in WRITE] + ["read-" + field for field in READ],
)
def test_every_required_scope_dimension_remains_required(value: dict[str, Any], field: str) -> None:
    changed = copy.deepcopy(value)
    del changed[field]
    assert not schema_validate.valid_instance(SCOPE, changed, schema_validate.schema_registry())


@pytest.mark.parametrize("revision", ["1", "9223372036854775807"])
def test_nested_note_revision_accepts_canonical_positive_boundaries(revision: str) -> None:
    changed = copy.deepcopy(READ)
    changed["note_ref"]["revision"] = revision
    assert schema_validate.valid_instance(SCOPE, changed, schema_validate.schema_registry())


@pytest.mark.parametrize(
    "revision",
    [
        0,
        1,
        True,
        None,
        "0",
        "01",
        "+1",
        "1.0",
        " 1",
        "1\n",
        "9223372036854775808",
        "9999999999999999999",
    ],
    ids=[
        "zero-number",
        "number",
        "boolean",
        "null",
        "zero",
        "leading-zero",
        "plus",
        "decimal",
        "space",
        "newline",
        "overflow",
        "large-overflow",
    ],
)
def test_nested_note_revision_does_not_coerce_or_overflow(revision: Any) -> None:
    changed = copy.deepcopy(READ)
    changed["note_ref"]["revision"] = revision
    assert not schema_validate.valid_instance(SCOPE, changed, schema_validate.schema_registry())


@pytest.mark.parametrize("limit", [0, 16383, 16385, "16384", True, None])
def test_write_scope_preserves_exact_fixed_byte_ceiling(limit: Any) -> None:
    assert not schema_validate.valid_instance(
        SCOPE, {**WRITE, "max_utf8_bytes": limit}, schema_validate.schema_registry()
    )


@pytest.mark.parametrize(
    "value",
    [
        {**WRITE, "note_ref": READ["note_ref"]},
        {**READ, "environment_id": "generated-override"},
        {**READ, "resource_type": "artifact"},
        {**READ, "resource_type": "support_note_write"},
        {**READ, "note_ref": {**READ["note_ref"], "url": "https://generated.invalid/note"}},
        {**READ, "note_ref": {"id": "generated-artifact", "sha256": "a" * 64}},
        {**WRITE, "organization_id": "generated-caller-tenant"},
    ],
    ids=[
        "write-read-mixture",
        "read-environment-override",
        "artifact-kind",
        "wrong-branch",
        "note-url",
        "artifact-ref",
        "caller-organization",
    ],
)
def test_scope_union_is_closed_and_cannot_substitute_artifact_or_url(value: dict[str, Any]) -> None:
    assert not schema_validate.valid_instance(SCOPE, value, schema_validate.schema_registry())


@pytest.mark.parametrize("change", ["source-bytes", "missing-common", "remote-ref"])
def test_missing_tampered_or_remote_schema_dependency_fails_closed(
    tmp_path: Path, change: str
) -> None:
    shutil.copytree(schema_validate.ROOT / "packages/contracts", tmp_path / "packages/contracts")
    registry_path = tmp_path / "packages/contracts/common/schema-registry.json"
    registry = json.loads(registry_path.read_text())
    if change == "source-bytes":
        with (tmp_path / SOURCE).open("a") as file:
            file.write("\n")
    elif change == "missing-common":
        registry["schemas"] = [
            item for item in registry["schemas"] if item["id"] != "urn:opentour:common-contract:1.0"
        ]
    else:
        path = tmp_path / SOURCE
        document = json.loads(path.read_text())
        document["$defs"]["SupportNoteRef"]["$ref"] = "https://generated.invalid/schema.json"
        path.write_text(json.dumps(document))
        next(item for item in registry["schemas"] if item["id"] == URI)["sha256"] = hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
    registry_path.write_text(json.dumps(registry))
    with pytest.raises(schema_validate.SchemaError):
        schema_validate.schema_registry(tmp_path)
