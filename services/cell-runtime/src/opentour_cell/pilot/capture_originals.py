"""Verify original decision-capture evidence independently of the product's counters.

A separate evaluator runs this module over an export directory and a separately retained,
sealed fixture. It imports only the standard library and never the capture ledger or its
adapters. It recomputes attempts, terminal state, stream gaps, committed decisions and
field provenance from the original gateway/provider log and application decision journal,
binds every file to the digests the fixture pinned, and scans every emitted surface
recursively for restricted values, including unexpected keys, nested objects, encoded
forms, unkeyed digests and textual fallbacks.

A passing projected receipt is never enough: every counter the product declares must equal
the value recomputed from originals, and both must equal the sealed fixture's expectation.
Generated fixtures cannot qualify an installation, so the installed witness is always
reported unavailable. Integrity is at most ``locally_keyed``; no issuer is authenticated.

Run: ``python -I capture_originals.py FIXTURE.json EXPORT_DIR``
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import stat
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

FIXTURE_SCHEMA = "opentour.capture.original-fixture.v1"
MANIFEST_SCHEMA = "opentour.capture.original-manifest.v1"
# Source formats are restated here on purpose: the evaluator does not import the product.
GATEWAY_SCHEMA = "opentour.capture.gateway-log.v1"
JOURNAL_SCHEMA = "opentour.capture.decision-journal.v1"
ORIGINALS = ("gateway_log", "decision_journal")
SURFACE_FORMATS = frozenset({"jsonl", "json", "text", "binary"})
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_CHUNKS = 1024
MAX_CHUNK_BYTES = 64 * 1024
_IDENTITY = ("operation_id", "attempt_id", "case_id", "family_id", "decision_id")
_KINDS = {
    "gateway_log": (GATEWAY_SCHEMA, frozenset({"request", "chunk", "terminal", "final_body"})),
    "decision_journal": (JOURNAL_SCHEMA, frozenset({"commit"})),
}
_FIXTURE_REQUIRED = frozenset(
    {
        "schema",
        "fixture_kind",
        "collector",
        "run",
        "tenant",
        "engagement",
        "workflow",
        "grant_revision",
        "policy_revision",
        "source_cursor",
        "coverage_window",
        "redaction_revision",
        "operations",
        "originals",
        "surfaces",
        "restricted",
        "expected",
    }
)
_FIXTURE_OPTIONAL = frozenset({"approved_keys", "receipt_key"})
_MANIFEST_KEYS = frozenset(
    {
        "schema",
        "collector",
        "run",
        "tenant",
        "engagement",
        "workflow",
        "grant_revision",
        "policy_revision",
        "source_cursor",
        "coverage_window",
        "redaction_revision",
        "operations",
        "attempts",
        "commits",
        "files",
        "declared",
    }
)
_BOUND = (
    "collector",
    "run",
    "tenant",
    "engagement",
    "workflow",
    "grant_revision",
    "policy_revision",
    "source_cursor",
    "coverage_window",
    "redaction_revision",
    "operations",
)
_SCOPE_COLUMNS = {
    "tenant": "tenant",
    "epoch": "engagement",
    "workflow": "workflow",
    "grant_revision": "grant_revision",
    "policy_revision": "policy_revision",
}
_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_HEX64 = re.compile(r"[0-9a-f]{64}")


class _Invalid(Exception):
    """An input that cannot be read as evidence."""


def _strict(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise _Invalid
    return dict(pairs)


def _constant(_: str) -> None:
    raise _Invalid


def _json(data: bytes) -> Any:
    value: Any = None
    failed = False
    try:
        value = json.loads(
            data.decode("utf-8"), object_pairs_hook=_strict, parse_constant=_constant
        )
    except (_Invalid, ValueError, RecursionError):
        failed = True
    if failed:
        raise _Invalid
    return value


def _lines(data: bytes) -> list[bytes]:
    return [line for line in data.split(b"\n") if line.strip()]


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_ref(value: object) -> bool:
    return isinstance(value, str) and _REF.fullmatch(value) is not None


def _is_int(value: object, low: int = 0, high: int = 2**31) -> bool:
    return type(value) is int and low <= value < high


def _b64(value: object, limit: int) -> bytes | None:
    if not isinstance(value, str):
        return None
    decoded: bytes | None = None
    try:
        decoded = base64.b64decode(value.encode("ascii"), validate=True)
    except (ValueError, binascii.Error):
        decoded = None
    return decoded if decoded is not None and len(decoded) <= limit else None


def _read(directory: Path, name: str) -> bytes:
    """A regular file directly inside the export; links and devices are not evidence."""
    path = directory / name
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
        raise _Invalid
    return path.read_bytes()


def _needles(restricted: list[str]) -> list[list[tuple[str, bytes]]]:
    """Each restricted value with encoded and dictionary-checkable digest variants."""
    result: list[list[tuple[str, bytes]]] = []
    for value in restricted:
        raw = value.encode("utf-8")
        forms: list[tuple[str, bytes]] = [
            ("plain", raw),
            ("utf16", value.encode("utf-16-le")),
            ("hex", raw.hex().encode()),
            ("json_escape", "".join(f"\\u{ord(c):04x}" for c in value).encode()),
        ]
        for pad in range(3):
            # The characters that depend only on the value wherever it sits in a larger blob.
            remainder = (pad + len(raw)) % 3
            for encoder in (base64.b64encode, base64.urlsafe_b64encode):
                encoded = encoder(b"\x00" * pad + raw).rstrip(b"=")
                start, stop = (0, 2, 3)[pad], len(encoded) - (1 if remainder else 0)
                if stop - start >= 8:
                    forms.append(("base64", encoded[start:stop]))
        for algorithm in ("md5", "sha1", "sha256", "sha512", "blake2b", "sha3_256"):
            forms.append(("digest", hashlib.new(algorithm, raw).hexdigest().encode()))
        result.append(forms)
    return result


def _hits(data: bytes, needles: list[list[tuple[str, bytes]]]) -> set[tuple[int, str]]:
    """Restricted values found, as (index, form); never the matched text itself."""
    lowered = data.lower()
    found: set[tuple[int, str]] = set()
    for index, forms in enumerate(needles):
        for kind, needle in forms:
            if (needle in data) if kind == "base64" else (needle.lower() in lowered):
                found.add((index, kind))
    return found


def _leaves(value: Any) -> Iterator[str]:
    """Every key and scalar at any depth, as text."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _leaves(item)
    elif isinstance(value, list):
        for item in value:
            yield from _leaves(item)
    elif not (isinstance(value, bool) or value is None):
        yield str(value)


def _keys(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _keys(item)


def _valid(record: Any, source: str) -> bool:
    """Whether the documented adapter rules admit this record; otherwise it is quarantined."""
    schema, kinds = _KINDS[source]
    if not isinstance(record, dict) or record.get("schema") != schema:
        return False
    kind = record.get("record")
    if kind not in kinds:
        return False
    if not (
        all(_is_ref(record.get(name)) for name in (*_IDENTITY, "event_id"))
        and _is_int(record.get("decision_revision"), 1)
        and _is_int(record.get("event_revision"), 1)
        and _is_int(record.get("observed_at"), 0, 2**63)
    ):
        return False
    if kind == "request":
        model, projection = record.get("requested_model"), record.get("projection_revision")
        template, parent = record.get("template_revision"), record.get("parent_attempt_id")
        if parent is not None and (not _is_ref(parent) or parent == record["attempt_id"]):
            return False
        if (model, projection, template) != (None, None, None) and not (
            _is_ref(model) and _is_ref(projection) and (template is None or _is_ref(template))
        ):
            return False
        return "request_projection_b64" not in record or (
            _b64(record["request_projection_b64"], 256 * 1024) is not None
        )
    if kind == "chunk":
        return _is_int(record.get("index"), 0, MAX_CHUNKS) and (
            _b64(record.get("data_b64"), MAX_CHUNK_BYTES) is not None
        )
    if kind == "terminal":
        state, expected = record.get("state"), record.get("expected_chunks")
        refs = [record.get(name) for name in ("served_model", "parsed_output", "parser_revision")]
        receipt = record.get("provider_receipt")
        return (
            (
                (state == "finished" and _is_int(expected, 0, MAX_CHUNKS + 1))
                or (state in {"timeout", "failed"} and expected is None)
            )
            and all(ref is None or _is_ref(ref) for ref in [receipt, *refs])
            and (refs[1] is None) == (refs[2] is None)
            and (refs[1] is None or state == "finished")
            and (receipt is not None or refs[:2] == [None, None])
        )
    if kind == "final_body":
        lengths, body = record.get("chunk_lengths"), _b64(record.get("body_b64"), 8 * 1024 * 1024)
        return (
            isinstance(lengths, list)
            and len(lengths) <= MAX_CHUNKS
            and all(_is_int(n, 0, MAX_CHUNK_BYTES + 1) for n in lengths)
            and body is not None
            and len(body) == sum(lengths)
        )
    return _is_ref(record.get("commit_ref")) and _is_ref(record.get("decision_payload_ref"))


class _Recompute:
    """Attempt and decision state recomputed from the original source records."""

    def __init__(self, operations: set[str]) -> None:
        self.operations = operations
        self.failures: set[str] = set()
        self.records = {
            name: {"admitted": 0, "quarantined": 0, "dropped_fields": 0} for name in ORIGINALS
        }
        self.events: dict[tuple[str, str, int], set[str]] = {}
        self.event_operation: dict[tuple[str, str, int], str] = {}
        self.event_attempts: dict[tuple[str, str, int], set[tuple[str, str]]] = {}
        self.identities: dict[tuple[str, str], set[str]] = {}
        self.gateway: set[tuple[str, str]] = set()
        self.starts: dict[tuple[str, str], set[str]] = {}
        self.chunks: dict[tuple[str, str], dict[int, set[str]]] = {}
        self.terminals: dict[tuple[str, str], set[str]] = {}
        self.bodies: dict[tuple[str, str], set[tuple[str, ...]]] = {}
        self.fields: dict[tuple[str, str], dict[str, str]] = {}
        self.commits: dict[tuple[str, int], set[str]] = {}
        self.commit_attempts: dict[tuple[str, int], set[tuple[str, str]]] = {}
        self.commit_refs: set[str] = set()

    def read(self, source: str, data: bytes) -> None:
        counts = self.records[source]
        for line in _lines(data):
            try:
                record = _json(line)
            except _Invalid:
                counts["quarantined"] += 1
                continue
            if not _valid(record, source):
                counts["quarantined"] += 1
                continue
            counts["dropped_fields"] += sum(1 for key in record if key not in _allowed(record))
            if record["operation_id"] not in self.operations:
                self.failures.add("foreign_record:" + source)
                continue
            event = (source, record["event_id"], record["event_revision"])
            # The ledger refuses an event reference that another operation already used.
            if (
                self.event_operation.setdefault(event, record["operation_id"])
                != record["operation_id"]
            ):
                counts["quarantined"] += 1
                continue
            counts["admitted"] += 1
            self._record(source, event, record)

    def _record(self, source: str, event: tuple[str, str, int], record: dict[str, Any]) -> None:
        key = (record["operation_id"], record["attempt_id"])
        identity = _canonical([record[name] for name in _IDENTITY] + [record["decision_revision"]])
        projected: dict[str, Any] = {}
        for name in sorted(_allowed(record) & set(record)):
            value = record[name]
            if name.endswith("_b64"):
                value = _sha256(base64.b64decode(value, validate=True))
            projected[name] = value
        # A redelivery must carry the same projection; any other change conflicts.
        self.events.setdefault(event, set()).add(_canonical(projected))
        self.event_attempts.setdefault(event, set()).add(key)
        self.identities.setdefault(key, set()).add(identity)
        kind = record["record"]
        if source == "gateway_log":
            self.gateway.add(key)
        if kind == "request":
            digest = projected.get("request_projection_b64")
            sealed = digest is not None or record.get("requested_model") is not None
            start = [record.get("parent_attempt_id"), record.get("requested_model")]
            start += [record.get("projection_revision"), record.get("template_revision"), digest]
            # A sealed observation is distinguished by its time; an unsealed one is not.
            self.starts.setdefault(key, set()).add(
                _canonical([record["observed_at"] if sealed else None, *start])
            )
            self.fields.setdefault(key, {}).update(
                requested_model="observed" if record.get("requested_model") else "unavailable",
                request_projection="fingerprinted" if digest else "not_recorded",
            )
        elif kind == "chunk":
            found = self.chunks.setdefault(key, {})
            found.setdefault(record["index"], set()).add(projected["data_b64"])
        elif kind == "terminal":
            names = ("state", "expected_chunks", "provider_receipt", "served_model")
            terminal = [record.get(name) for name in (*names, "parsed_output", "parser_revision")]
            sealed = record.get("provider_receipt") is not None
            self.terminals.setdefault(key, set()).add(
                _canonical([record["observed_at"] if sealed else None, *terminal])
            )
            self.fields.setdefault(key, {}).update(
                served_model="observed" if record.get("served_model") else "unavailable",
                parsed_output="observed" if record.get("parsed_output") else "unavailable",
            )
        elif kind == "final_body":
            body = base64.b64decode(record["body_b64"], validate=True)
            slices: list[str] = []
            offset = 0
            for length in record["chunk_lengths"]:
                slices.append(_sha256(body[offset : offset + length]))
                offset += length
            self.bodies.setdefault(key, set()).add(tuple(slices))
        else:
            decision = (record["decision_id"], record["decision_revision"])
            self.commits.setdefault(decision, set()).add(record["decision_payload_ref"])
            self.commit_attempts.setdefault(decision, set()).add(key)
            self.commit_refs.add(record["commit_ref"])

    def result(self) -> dict[str, Any]:
        conflicted = {
            attempt
            for event, versions in self.events.items()
            if len(versions) > 1
            for attempt in self.event_attempts[event]
        }
        attempts = {key[1]: self._attempt(key, conflicted) for key in sorted(self.identities)}
        # Commits are disputed by a conflicting commit delivery or an attempt identity conflict.
        commit_conflicts = {
            attempt
            for event, versions in self.events.items()
            if event[0] == "decision_journal" and len(versions) > 1
            for attempt in self.event_attempts[event]
        } | {key for key, ids in self.identities.items() if len(ids) > 1}
        current: dict[str, int] = {}
        for decision, revision in self.commits:
            current[decision] = max(revision, current.get(decision, revision))
        disputed = unjoinable = 0
        for decision, revision in current.items():
            joined = self.commit_attempts[(decision, revision)]
            unjoinable += sum(1 for key in joined if key not in self.gateway)
            disputed += len(self.commits[(decision, revision)]) > 1 or bool(
                joined & commit_conflicts
            )
        states = [item["state"] for item in attempts.values()]
        return {
            "attempts": len(attempts),
            "finished_attempts": states.count("finished"),
            "incomplete_attempts": states.count("incomplete"),
            "disputed_attempts": states.count("disputed"),
            "committed_decisions": len(current) - disputed,
            "disputed_decisions": disputed,
            "unjoinable_commits": unjoinable,
            "records": self.records,
            "per_attempt": attempts,
        }

    def _attempt(self, key: tuple[str, str], conflicted: set[tuple[str, str]]) -> dict[str, Any]:
        chunks = self.chunks.get(key, {})
        terminals = self.terminals.get(key, set())
        bodies = self.bodies.get(key, set())
        reported = json.loads(next(iter(terminals))) if len(terminals) == 1 else None
        state, expected = (reported[1], reported[2]) if reported else (None, None)
        body = next(iter(bodies)) if len(bodies) == 1 else None
        body_conflict = len(bodies) > 1 or (
            body is not None
            and (
                (reported is not None and (state, expected) != ("finished", len(body)))
                or any(
                    index >= len(body) or found != {body[index]} for index, found in chunks.items()
                )
            )
        )
        invalid = (
            key in conflicted
            or len(self.identities[key]) > 1
            or len(self.starts.get(key, set())) > 1
            or len(terminals) > 1
            or any(len(found) > 1 for found in chunks.values())
            or (expected is not None and any(index >= expected for index in chunks))
            or body_conflict
        )
        gap = sorted(set(range(expected)) - set(chunks)) if expected is not None else []
        exact = not invalid and state == "finished" and (not gap or body is not None)
        fields = self.fields.get(key, {})
        if invalid:
            status = "disputed"
        else:
            status = "finished" if self.starts.get(key) and exact else "incomplete"
        return {
            "state": status,
            "terminal_state": state,
            "received_indices": sorted(chunks),
            "missing_indices": gap,
            "exact_stream": exact,
            "requested_model": fields.get("requested_model", "unavailable"),
            "served_model": fields.get("served_model", "unavailable"),
            "parsed_output": fields.get("parsed_output", "unavailable"),
            "request_projection": fields.get("request_projection", "not_recorded"),
            "exact_replay": "unavailable",
        }


_COMMON = frozenset(
    {"schema", "record", "event_id", "event_revision", "observed_at", *_IDENTITY}
    | {"decision_revision"}
)
_FIELDS = {
    "request": {
        "parent_attempt_id",
        "requested_model",
        "projection_revision",
        "template_revision",
        "request_projection_b64",
    },
    "chunk": {"index", "data_b64"},
    "terminal": {
        "state",
        "expected_chunks",
        "provider_receipt",
        "served_model",
        "parsed_output",
        "parser_revision",
    },
    "final_body": {"body_b64", "chunk_lengths"},
    "commit": {"commit_ref", "decision_payload_ref"},
}


def _allowed(record: Mapping[str, Any]) -> frozenset[str]:
    return _COMMON | _FIELDS[record["record"]]


def _receipt_ref(fixture: Mapping[str, Any], source: str, cursor: str, data: bytes) -> str:
    """The product's keyed page reference, restated from its documented binding."""
    binding = [
        fixture["tenant"],
        fixture["engagement"],
        fixture["workflow"],
        fixture["grant_revision"],
        fixture["policy_revision"],
        [source, cursor],
        "capture-page",
    ]
    key = bytes.fromhex(fixture["receipt_key"])
    return hmac.digest(key, _canonical(binding).encode() + b"\x00" + data, "sha256").hex()


def _store_rows(fixture: Mapping[str, Any], records: list[Any]) -> set[str]:
    failures: set[str] = set()
    for record in records:
        row = record.get("row") if isinstance(record, dict) else None
        if not isinstance(row, dict) or set(record) != {"table", "row"}:
            failures.add("unparseable_surface:metadata_store")
            continue
        for column, bound in _SCOPE_COLUMNS.items():
            if column in row and row[column] != fixture[bound]:
                failures.add("foreign_row:metadata_store")
        if "operation_ref" in row and row["operation_ref"] not in fixture["operations"]:
            failures.add("foreign_row:metadata_store")
    return failures


def _compare(prefix: str, expected: Any, actual: Any, failures: set[str], path: str = "") -> None:
    if isinstance(expected, dict) and isinstance(actual, dict):
        for key in sorted(set(expected) | set(actual)):
            if key not in actual or key not in expected:
                failures.add(f"{prefix}:{path}{key}")
            else:
                _compare(prefix, expected[key], actual[key], failures, f"{path}{key}.")
    elif _canonical(expected) != _canonical(actual):
        failures.add(f"{prefix}:{path.rstrip('.')}")


def _select(recomputed: Mapping[str, Any], shape: Any) -> Any:
    """The recomputed values a declaration or expectation names, with every attempt."""
    if not isinstance(shape, dict):
        return dict(recomputed)
    selected: dict[str, Any] = {}
    for key, item in shape.items():
        if key == "per_attempt" and isinstance(item, dict):
            fields = {
                field for value in item.values() if isinstance(value, dict) for field in value
            }
            selected[key] = {
                name: {field: value.get(field) for field in sorted(fields)}
                for name, value in recomputed["per_attempt"].items()
            }
        elif key in recomputed:
            selected[key] = recomputed[key]
    return selected


def _check_fixture(fixture: Any) -> None:
    if not isinstance(fixture, dict):
        raise _Invalid
    keys = set(fixture)
    if not _FIXTURE_REQUIRED <= keys <= _FIXTURE_REQUIRED | _FIXTURE_OPTIONAL:
        raise _Invalid
    if fixture["schema"] != FIXTURE_SCHEMA or not isinstance(fixture["expected"], dict):
        raise _Invalid
    if set(fixture["originals"]) != set(ORIGINALS) or not all(
        isinstance(value, str) and _HEX64.fullmatch(value)
        for value in fixture["originals"].values()
    ):
        raise _Invalid
    if set(fixture["source_cursor"]) != set(ORIGINALS) or not isinstance(
        fixture["operations"], list
    ):
        raise _Invalid
    for name, surface in fixture["surfaces"].items():
        if name in ORIGINALS or name == "manifest.json" or not isinstance(surface, dict):
            raise _Invalid
        if surface.get("status") == "unavailable":
            if set(surface) != {"status", "reason"} or not isinstance(surface["reason"], str):
                raise _Invalid
        elif surface != {"status": "required", "format": surface.get("format")}:
            raise _Invalid
        elif surface["format"] not in SURFACE_FORMATS:
            raise _Invalid
    restricted = fixture["restricted"]
    if not isinstance(restricted, list) or not restricted:
        raise _Invalid
    if not all(isinstance(value, str) and len(value) >= 8 for value in restricted):
        raise _Invalid
    key = fixture.get("receipt_key")
    if key is not None and not (isinstance(key, str) and _HEX64.fullmatch(key)):
        raise _Invalid


def _surfaces(
    fixture: Mapping[str, Any], directory: Path, files: Mapping[str, Any], failures: set[str]
) -> tuple[dict[str, Any], dict[str, list[Any]]]:
    needles = _needles(fixture["restricted"])
    report: dict[str, Any] = {}
    parsed: dict[str, list[Any]] = {}
    for name, declared in sorted(fixture["surfaces"].items()):
        if declared["status"] == "unavailable":
            report[name] = {"status": "unavailable", "reason": declared["reason"]}
            if name in files:
                failures.add("unexpected_file:" + name)
            continue
        if name not in files:
            failures.add("missing_surface:" + name)
            report[name] = {"status": "missing"}
            continue
        try:
            data = _read(directory, name)
        except (_Invalid, OSError):
            failures.add("missing_surface:" + name)
            report[name] = {"status": "missing"}
            continue
        if not hmac.compare_digest(_sha256(data), str(files[name])):
            failures.add("digest_mismatch:" + name)
        texts = [data]
        records: list[Any] = []
        if declared["format"] in {"jsonl", "json"}:
            try:
                lines = _lines(data) if declared["format"] == "jsonl" else [data]
                records = [_json(line) for line in lines]
            except _Invalid:
                failures.add("unparseable_surface:" + name)
            texts += [leaf.encode("utf-8") for leaf in _leaves(records)]
            approved = fixture.get("approved_keys", {}).get(name)
            if approved is not None and any(key not in approved for key in _keys(records)):
                failures.add("unapproved_field:" + name)
        hits = {hit for text in texts for hit in _hits(text, needles)}
        if hits:
            failures.add("restricted_value:" + name)
        parsed[name] = records
        report[name] = {
            "status": "scanned",
            "bytes": len(data),
            "records": len(records) if declared["format"] in {"jsonl", "json"} else None,
            "hits": len(hits),
        }
    return report, parsed


def _receipts(
    fixture: Mapping[str, Any],
    declared: Mapping[str, Any],
    originals: Mapping[str, bytes],
    failures: set[str],
) -> None:
    receipts = declared.get("page_receipts")
    if not isinstance(receipts, dict) or set(receipts) != set(ORIGINALS):
        failures.add("receipt_mismatch:missing")
        return
    for name in ORIGINALS:
        receipt, cursor = receipts[name], fixture["source_cursor"][name]
        if name not in originals or not isinstance(receipt, dict):
            failures.add("receipt_mismatch:" + name)
            continue
        source = receipt.get("source_ref")
        expected = _receipt_ref(fixture, str(source), cursor, originals[name])
        if receipt.get("cursor") != cursor or not hmac.compare_digest(
            str(receipt.get("page_ref")), expected
        ):
            failures.add("receipt_mismatch:" + name)


def verify_originals(fixture: Mapping[str, Any], directory: Path) -> dict[str, Any]:
    """Recompute and compare; never trust a counter that originals do not reproduce."""
    failures: set[str] = set()
    result: dict[str, Any] = {
        "status": "fail",
        "failures": [],
        "recomputed": None,
        "surfaces": {},
        "sentinel_scan": "not_run",
        "fixture_kind": None,
        "installed_witness": "unavailable",
        "integrity": "digest_self_consistent_only",
        "issuer_authenticated": False,
    }
    try:
        _check_fixture(fixture)
    except (_Invalid, KeyError, TypeError, AttributeError):
        result["failures"] = ["fixture_invalid"]
        return result
    result["fixture_kind"] = fixture["fixture_kind"]
    try:
        manifest = _json(_read(directory, "manifest.json"))
        if not isinstance(manifest, dict) or set(manifest) != _MANIFEST_KEYS:
            raise _Invalid
        if manifest["schema"] != MANIFEST_SCHEMA or not isinstance(manifest["files"], dict):
            raise _Invalid
    except (_Invalid, OSError):
        result["failures"] = ["manifest_invalid"]
        return result
    for name in _BOUND:
        if _canonical(manifest[name]) != _canonical(fixture[name]):
            failures.add("manifest_mismatch:" + name)
    files = manifest["files"]
    for name in sorted(set(files) - set(ORIGINALS) - set(fixture["surfaces"])):
        failures.add("unexpected_file:" + name)
    originals: dict[str, bytes] = {}
    for name in ORIGINALS:
        try:
            data = _read(directory, name)
        except (_Invalid, OSError):
            failures.add("missing_original:" + name)
            continue
        if name not in files or not hmac.compare_digest(_sha256(data), str(files[name])):
            failures.add("digest_mismatch:" + name)
        if not hmac.compare_digest(_sha256(data), fixture["originals"][name]):
            failures.add("original_differs:" + name)
        originals[name] = data
    recompute = _Recompute(set(fixture["operations"]))
    recomputed: dict[str, Any] | None = None
    if set(originals) == set(ORIGINALS):
        for name in ORIGINALS:
            recompute.read(name, originals[name])
        recomputed = recompute.result()
        failures |= recompute.failures
    result["recomputed"] = recomputed
    surfaces, parsed = _surfaces(fixture, directory, files, failures)
    result["surfaces"] = surfaces
    statuses = {item["status"] for item in surfaces.values()}
    scanned = "scanned" in statuses
    result["sentinel_scan"] = (
        "complete" if statuses == {"scanned"} else "partial" if scanned else "not_run"
    )
    if "metadata_store" in parsed:
        failures |= _store_rows(fixture, parsed["metadata_store"])
    declared = manifest["declared"]
    if not isinstance(declared, dict):
        failures.add("declared_mismatch:declared")
    elif recomputed is not None:
        expected = fixture["expected"]
        _compare("expected_mismatch", expected, _select(recomputed, expected), failures)
        claims = {key: value for key, value in declared.items() if key != "page_receipts"}
        _compare("declared_mismatch", _select(recomputed, claims), claims, failures)
        if sorted(recomputed["per_attempt"]) != sorted(manifest["attempts"]):
            failures.add("declared_mismatch:attempts_list")
        if sorted(recompute.commit_refs) != sorted(manifest["commits"]):
            failures.add("declared_mismatch:commits_list")
        if fixture.get("receipt_key") is not None:
            result["integrity"] = "locally_keyed"
            _receipts(fixture, declared, originals, failures)
    result["failures"] = sorted(failures)
    result["status"] = "pass" if not failures else "fail"
    return result


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: capture_originals.py FIXTURE.json EXPORT_DIR", file=sys.stderr)
        return 2
    try:
        fixture = _json(Path(argv[0]).read_bytes())
    except (_Invalid, OSError):
        print(json.dumps({"status": "fail", "failures": ["fixture_invalid"]}))
        return 1
    result = verify_originals(fixture, Path(argv[1]))
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
