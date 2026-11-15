"""Validate exact approved schemas and generated examples without remote resolution."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker  # type: ignore[import-untyped]
from referencing import Registry, Resource
from referencing.exceptions import NoSuchResource

ROOT = Path(__file__).resolve().parents[1]
COMMON = "packages/contracts/common"


class SchemaError(ValueError):
    """Safe configuration/shape failure; never include submitted data in its message."""


def read_json(path: Path) -> Any:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise SchemaError("Duplicate JSON property")
            value[key] = item
        return value

    def reject_constant(value: str) -> None:
        raise SchemaError("Non-finite JSON number")

    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=unique,
            parse_constant=reject_constant,
        )
    except (OSError, UnicodeError, ValueError) as exc:
        raise SchemaError("Missing or invalid local JSON document") from exc


def local_file(root: Path, relative: str) -> Path:
    if not isinstance(relative, str):
        raise SchemaError("Local schema path must be a string")
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise SchemaError("Schema paths must remain inside the repository")
    candidate = root / path
    try:
        if not candidate.resolve().is_relative_to(root.resolve()) or not candidate.is_file():
            raise SchemaError("Missing or escaping local schema file")
    except (OSError, RuntimeError) as exc:
        raise SchemaError("Local schema path cannot be resolved safely") from exc
    return candidate


def deny_retrieval(uri: str) -> Resource[Any]:
    raise NoSuchResource(uri)


def format_checker() -> FormatChecker:
    """The pinned minimal jsonschema install has no optional RFC3339 dependency."""
    checker = FormatChecker()

    @checker.checks("date-time", raises=(ValueError, TypeError))  # type: ignore[untyped-decorator]
    def timestamp(value: Any) -> bool:
        if not isinstance(value, str):
            return True  # The schema's type keyword rejects non-strings.
        if not re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?Z",
            value,
        ):
            return False
        datetime.fromisoformat(value)
        return True

    return checker


def schema_registry(root: Path = ROOT) -> Registry[Any]:
    manifest = read_json(local_file(root, f"{COMMON}/schema-registry.json"))
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != "opentour.local-schema-registry.v1"
        or not isinstance(manifest.get("schemas"), list)
        or not manifest["schemas"]
    ):
        raise SchemaError("Invalid local schema registry manifest")
    registry: Registry[Any] = Registry(retrieve=deny_retrieval)  # type: ignore[call-arg]
    documents: dict[str, Any] = {}
    for entry in manifest["schemas"]:
        if (
            not isinstance(entry, dict)
            or set(entry) != {"id", "file", "sha256", "source_path"}
            or any(not isinstance(value, str) for value in entry.values())
            or not re.fullmatch(r"[a-f0-9]{64}", entry["sha256"])
        ):
            raise SchemaError("Invalid local schema registry entry")
        path = local_file(root, f"{COMMON}/{entry['file']}")
        if hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
            raise SchemaError("Canonical schema source hash mismatch")
        document = read_json(path)
        if (
            not isinstance(document, dict)
            or document.get("$id") != entry["id"]
            or entry["id"] in documents
        ):
            raise SchemaError("Duplicate or mismatched schema identity")
        try:
            Draft202012Validator.check_schema(document)
            registry = registry.with_resource(entry["id"], Resource.from_contents(document))
        except Exception as exc:
            raise SchemaError("Invalid canonical schema") from exc
        documents[entry["id"]] = document

    def check_refs(value: Any, base: str) -> None:
        if isinstance(value, dict):
            if "$ref" in value:
                try:
                    registry.resolver(base).lookup(value["$ref"])
                except Exception as exc:
                    raise SchemaError("Unregistered or unresolved local schema reference") from exc
            for item in value.values():
                check_refs(item, base)
        elif isinstance(value, list):
            for item in value:
                check_refs(item, base)

    for schema_id, document in documents.items():
        check_refs(document, schema_id)
    return registry


def valid_instance(reference: str, value: Any, registry: Registry[Any]) -> bool:
    if not isinstance(reference, str) or not reference.startswith("urn:opentour:"):
        raise SchemaError("Only registered local schema identities are accepted")
    try:
        registry.resolver().lookup(reference)
        validator = Draft202012Validator(
            {"$ref": reference}, registry=registry, format_checker=format_checker()
        )
        return not any(validator.iter_errors(value))
    except Exception as exc:
        raise SchemaError("Unregistered or invalid local schema reference") from exc


def valid_bound_document(root: Path, binding_path: str, value: Any) -> bool:
    """Consume an owning harness's exact local schema without copying or remote resolution."""
    binding = read_json(local_file(root, binding_path))
    if (
        not isinstance(binding, dict)
        or set(binding) != {"schema_version", "id", "file", "sha256"}
        or any(not isinstance(item, str) for item in binding.values())
        or binding["schema_version"] != "opentour.owned-schema-binding.v1"
        or not re.fullmatch(r"[a-f0-9]{64}", binding["sha256"])
        or not binding["id"].startswith("urn:opentour:")
    ):
        raise SchemaError("Invalid owning-harness schema binding")
    path = local_file(root, binding["file"])
    if hashlib.sha256(path.read_bytes()).hexdigest() != binding["sha256"]:
        raise SchemaError("Owning-harness schema hash mismatch")
    document = read_json(path)
    if not isinstance(document, dict) or document.get("$id") != binding["id"]:
        raise SchemaError("Invalid owning-harness schema identity")
    try:
        Draft202012Validator.check_schema(document)
        registry = schema_registry(root).with_resource(
            binding["id"], Resource.from_contents(document)
        )
    except Exception as exc:
        raise SchemaError("Invalid owning-harness schema") from exc
    return valid_instance(binding["id"], value, registry)


def validate_examples(root: Path = ROOT) -> dict[str, Any]:
    registry = schema_registry(root)
    examples = read_json(local_file(root, "packages/contracts/examples/common-examples.json"))
    if not isinstance(examples, list) or not examples:
        raise SchemaError("No schema examples were collected")
    outcomes = []
    ids: set[str] = set()
    for example in examples:
        if (
            not isinstance(example, dict)
            or set(example) != {"id", "schema_ref", "value", "valid"}
            or type(example["valid"]) is not bool
            or not isinstance(example["id"], str)
            or example["id"] in ids
        ):
            raise SchemaError("Malformed or duplicate schema example")
        ids.add(example["id"])
        actual = valid_instance(example["schema_ref"], example["value"], registry)
        outcomes.append({"id": example["id"], "passed": actual == example["valid"]})
    failed = sum(not result["passed"] for result in outcomes)
    return {
        "kind": "simulated",
        "fixture_id": "common-schema-shapes",
        "fixture_version": "1",
        "collected": len(outcomes),
        "passed": len(outcomes) - failed,
        "failed": failed,
        "status": "FAIL" if failed else "PASS",
        "exit_code": 1 if failed else 0,
        "tests": outcomes,
        "product_acceptance": "NOT_RUN",
        "limitations": ["Shape validation only; no signature, authorization or provider proof"],
    }


def main() -> int:
    try:
        result = validate_examples()
    except SchemaError as exc:
        result = {"status": "FAIL", "exit_code": 2, "message": str(exc)}
    print(json.dumps(result, sort_keys=True))
    return int(result["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
