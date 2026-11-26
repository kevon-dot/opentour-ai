"""Join two permissioned, owner-local workflow exports into a revisioned pilot intake."""

from __future__ import annotations

import argparse
import csv
import hashlib
import hmac
import io
import os
import re
import secrets
import sqlite3
import stat
import sys
import tempfile
import time
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from opentour_cell.pilot.operating_point import canonical, digest
from opentour_cell.pilot.readout import (
    INPUT_VERSION,
    MAX_MANIFEST_BYTES,
    ManifestError,
    parse_manifest,
)
from opentour_cell.pilot.text_classifier import TrainingError
from opentour_cell.pilot.training import LocalTrainingStore
from opentour_cell.pilot.training_data import (
    EVALUATION_FIELDS,
    EVALUATION_VERSION,
    identifier,
    prepare,
)

Json = dict[str, Any]
INTAKE_VERSION = "opentour.pilot.source-intake.v1"
GRANT_VERSION = "opentour.pilot.source-grant.v1"
TRACE = {
    "event_id",
    "source",
    "organization",
    "workflow",
    "field",
    "policy_version",
    "source_schema_version",
    "customer_id",
    "group_id",
    "decision_at",
    "features_available_at",
    "features",
    "incumbent",
}
OUTCOME = {
    "event_id",
    "source",
    "organization",
    "workflow",
    "field",
    "policy_version",
    "source_schema_version",
    "outcome_id",
    "adjudicator_id",
    "facts_as_of",
    "outcome_at",
    "verified_at",
    "reference",
}
SCOPE = {
    "schema_version",
    "organization",
    "engagement",
    "dataset_id",
    "workflow",
    "field",
    "policy_version",
    "source_schema_version",
    "trace_source",
    "correction_source",
    "trace_sha256",
    "correction_sha256",
    "owner_id",
    "owner_attestation",
    "adjudicators",
    "permitted_features",
    "permitted_slices",
    "evaluation_allowed",
    "as_of",
    "expires_at",
    "split",
    "retention_terms",
}
MAX_ROWS = 5000


def require(ok: object, code: str) -> None:
    if not ok:
        raise TrainingError(code)


def _attest(value: object) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"att-[0-9a-f]{16,64}", value))


def _time(value: object) -> int:
    if type(value) is not int or not 0 <= value < 2**63:
        raise TrainingError("invalid_source_time")
    return value


def _ids(values: object, *, empty: bool = False) -> list[str]:
    require(isinstance(values, list), "invalid_source_identifiers")
    assert isinstance(values, list)
    valid = [identifier(v) for v in values]
    require(
        len(valid) <= 128 and (empty or valid) and len(set(valid)) == len(valid),
        "invalid_source_identifiers",
    )
    return sorted(valid)


def _parent(path: Path) -> None:
    require(
        path.is_absolute() and ".." not in path.parts and path.parent.resolve() == path.parent,
        "absolute_private_path_required",
    )
    parent = path.parent.lstat()
    require(
        stat.S_ISDIR(parent.st_mode)
        and parent.st_uid == os.getuid()
        and not parent.st_mode & 0o077,
        "private_directory_required",
    )


def _read(path: Path, limit: int = MAX_MANIFEST_BYTES) -> bytes:
    _parent(path)
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as f:
        rec = os.fstat(f.fileno())
        require(
            stat.S_ISREG(rec.st_mode)
            and rec.st_uid == os.getuid()
            and rec.st_nlink == 1
            and not rec.st_mode & 0o077
            and rec.st_size <= limit,
            "private_file_required",
        )
        contents = f.read(limit + 1)
    require(len(contents) <= limit, "private_file_required")
    return contents


def _publish(path: Path, data: Json) -> None:
    _parent(path)
    contents = (canonical(data) + "\n").encode()
    require(len(contents) <= MAX_MANIFEST_BYTES, "intake_exceeds_limit")
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".intake-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(contents)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.link(tmp, path, follow_symlinks=False)
        except FileExistsError:
            require(_read(path) == contents, "intake_destination_conflict")
        directory = os.open(path.parent, os.O_DIRECTORY | os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        os.unlink(tmp)


@dataclass(frozen=True)
class _BoundEngagement:
    path: Path
    organization: str
    engagement_id: str


def _marker_seal(
    store: LocalTrainingStore | _BoundEngagement, marker: Json, *, create: bool = False
) -> str:
    key_path = store.path.parent / "source-intake-key.json"
    if create and not os.path.lexists(key_path):
        _publish(key_path, {"key": secrets.token_hex(32)})
    key = parse_manifest(_read(key_path, 256)).get("key")
    require(isinstance(key, str) and re.fullmatch(r"[0-9a-f]{64}", key), "source_seal_unavailable")
    assert isinstance(key, str)
    return hmac.digest(bytes.fromhex(key), canonical(marker).encode(), "sha256").hex()


def _revocation(store: LocalTrainingStore | _BoundEngagement, owner_attestation: str) -> Path:
    require(_attest(owner_attestation), "invalid_owner_attestation")
    return store.path.parent / ("source-withdrawn-" + owner_attestation + ".json")


def _training_current(store: LocalTrainingStore | _BoundEngagement, grant: Json) -> bool:
    attestation = grant.get("training_attestation")
    expires = grant.get("training_expires_at", grant["expires_at"])
    return (
        isinstance(attestation, str)
        and type(expires) is int
        and time.time() < expires
        and not os.path.lexists(_revocation(store, attestation))
    )


def _delegated_run(store: LocalTrainingStore | _BoundEngagement, g: Json) -> tuple[Any, Json]:
    from opentour_cell.pilot.learning_mandate import LearningMandates

    d = g["delegation"]
    require(
        isinstance(d, dict)
        and set(d) == {"authority_kind", "run_id", "delegate"}
        and d["authority_kind"] == "standing_customer_mandate",
        "invalid_source_delegation",
    )
    ledger = LearningMandates(store.path.parent, store.organization, g["engagement"])
    receipt = ledger.check_run(d["run_id"], actor=d["delegate"])
    request, scope = receipt["source_request"], receipt["scope"]
    require(
        receipt["authority_kind"] == d["authority_kind"]
        and receipt["delegate"] == d["delegate"]
        and scope["mode"] == "autonomous"
        and scope["owner_id"] == g["owner_id"]
        and all(
            request[k] == g[k]
            for k in (
                "workflow",
                "field",
                "trace_source",
                "correction_source",
                "trace_sha256",
                "correction_sha256",
            )
        )
        and sorted(request["fields"]) == sorted(g["permitted_features"])
        and g["expires_at"] <= scope["expires_at"]
        and "evaluate" in scope["uses"],
        "source_delegation_scope_mismatch",
    )
    if "training_attestation" in g:
        require(
            "train" in scope["uses"]
            and receipt.get("training_attestation") == g["training_attestation"],
            "source_training_not_authorized",
        )
    return ledger, receipt


def _scope(store: LocalTrainingStore | _BoundEngagement, path: Path) -> Json:
    require(path.parent == store.path.parent and path != store.path, "local_source_grant_required")
    g = parse_manifest(_read(path, 64 * 1024))
    require(
        SCOPE <= g.keys() <= SCOPE | {"training_attestation", "training_expires_at", "delegation"},
        "invalid_source_grant",
    )
    require(
        g["schema_version"] == GRANT_VERSION
        and g["organization"] == store.organization
        and g["engagement"] == store.engagement_id
        and g["evaluation_allowed"] is True,
        "source_grant_scope_mismatch",
    )
    for key in (
        "dataset_id",
        "workflow",
        "field",
        "policy_version",
        "source_schema_version",
        "trace_source",
        "correction_source",
        "owner_id",
    ):
        identifier(g[key])
    require(
        g["trace_source"] != g["correction_source"] and _attest(g["owner_attestation"]),
        "independent_authority_required",
    )
    require(
        isinstance(g["retention_terms"], str)
        and re.fullmatch(r"terms-[0-9a-f]{16,64}", g["retention_terms"]),
        "retention_terms_required",
    )
    if "training_attestation" in g:
        require(
            _attest(g["training_attestation"])
            and g["training_attestation"] != g["owner_attestation"],
            "distinct_training_authority_required",
        )
    if "training_expires_at" in g:
        require(
            "training_attestation" in g
            and 0 < _time(g["training_expires_at"]) <= _time(g["expires_at"]),
            "invalid_training_expiry",
        )
    for key in ("trace_sha256", "correction_sha256"):
        require(
            isinstance(g[key], str) and re.fullmatch(r"[0-9a-f]{64}", g[key]),
            "invalid_source_digest",
        )
    for key in ("permitted_features", "permitted_slices", "adjudicators"):
        g[key] = _ids(g[key], empty=key == "permitted_slices")
    require(
        _time(g["as_of"]) <= time.time() < _time(g["expires_at"]), "source_grant_outside_validity"
    )
    split = g["split"]
    require(isinstance(split, dict), "invalid_source_split")
    if split.get("mode") == "chronological":
        require(
            set(split) == {"mode", "cutoff"} and 0 < _time(split["cutoff"]) <= g["as_of"],
            "invalid_source_split",
        )
    elif split.get("mode") == "heldout_customer":
        require(set(split) == {"mode", "customers"}, "invalid_source_split")
        split["customers"] = _ids(split["customers"])
    else:
        raise TrainingError("invalid_source_split")
    require(
        not os.path.lexists(_revocation(store, g["owner_attestation"])), "source_grant_withdrawn"
    )
    if "delegation" in g:
        _delegated_run(store, g)
    return g


def _csv_row(row: dict[str, Any]) -> Json:
    require(
        None not in row and all(isinstance(val, str) for val in row.values()), "invalid_source_csv"
    )
    for key in ("features", "incumbent", "slices"):
        if key in row:
            # JSON objects embedded in CSV follow the same duplicate-key rules as JSONL.
            row[key] = parse_manifest(('{"value":' + row[key] + "}").encode())["value"]
    for key in ("decision_at", "features_available_at", "facts_as_of", "outcome_at", "verified_at"):
        if key in row:
            require(
                isinstance(row[key], str) and re.fullmatch(r"0|[1-9][0-9]{0,18}", row[key]),
                "invalid_source_time",
            )
            row[key] = int(row[key])
    if row.get("reference") == "":
        row["reference"] = None
    return row


def _export(path: Path, approved: str, *, trace: bool) -> tuple[list[Json], str, int]:
    raw = _read(path)
    require(hashlib.sha256(raw).hexdigest() == approved, "source_export_digest_mismatch")
    records: list[Json] = []
    if path.suffix.lower() == ".jsonl":
        for line in raw.splitlines():
            if line.strip():
                records.append(parse_manifest(line))
    elif path.suffix.lower() == ".csv":
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline=""), strict=True)
        cols = reader.fieldnames or []
        require(bool(cols) and len(cols) == len(set(cols)), "invalid_source_csv")
        records = [_csv_row(row) for row in reader]
    else:
        raise TrainingError("unsupported_export_format")
    require(len(records) <= MAX_ROWS, "source_row_limit")
    required = TRACE if trace else OUTCOME
    encoded: dict[str, Json] = {}
    for row in records:
        require(
            required <= row.keys() <= (required | ({"slices"} if trace else set())),
            "invalid_export_fields",
        )
        for key in required - {
            "decision_at",
            "features_available_at",
            "facts_as_of",
            "outcome_at",
            "verified_at",
            "features",
            "incumbent",
            "reference",
        }:
            identifier(row[key])
        for key in (
            {"decision_at", "features_available_at"}
            if trace
            else {"facts_as_of", "outcome_at", "verified_at"}
        ):
            _time(row[key])
        if trace:
            row["slices"] = _ids(row.get("slices", []), empty=True)
            value = row["incumbent"]
            require(isinstance(value, dict), "invalid_incumbent")
            if value.get("status") == "automatic":
                require(set(value) == {"status", "label"}, "invalid_incumbent")
                identifier(value["label"])
            else:
                require(
                    set(value) == {"status"} and value["status"] in ("review", "missing"),
                    "invalid_incumbent",
                )
            fields = row["features"]
            require(
                isinstance(fields, dict) and 1 <= len(fields) <= 32, "invalid_decision_features"
            )
            for name, field in fields.items():
                identifier(name)
                require(
                    isinstance(field, str)
                    and 0 < len(field) <= 1024
                    and field.isprintable()
                    and field.strip(),
                    "invalid_decision_features",
                )
        elif row["reference"] is not None:
            identifier(row["reference"])
        encoded[canonical(row)] = row
    ordered = [encoded[key] for key in sorted(encoded)]
    return ordered, digest(ordered), len(records) - len(ordered)


def _reason(g: Json, trace: Json, outcomes: list[Json], reused: set[str]) -> str:
    if (
        trace["policy_version"] != g["policy_version"]
        or trace["source_schema_version"] != g["source_schema_version"]
    ):
        return "changed_policy_or_schema"
    if not trace["features"].keys() <= set(g["permitted_features"]) or not set(
        trace["slices"]
    ) <= set(g["permitted_slices"]):
        return "fields_outside_grant"
    if trace["decision_at"] > g["as_of"] or trace["features_available_at"] > trace["decision_at"]:
        return "future_decision_features"
    if not outcomes:
        return "missing_outcome"
    if len(outcomes) != 1:
        return (
            "contradictory_outcomes"
            if len({o["reference"] for o in outcomes}) > 1
            else "ambiguous_outcomes"
        )
    outcome = outcomes[0]
    if outcome["outcome_id"] in reused:
        return "reused_outcome"
    if (
        outcome["policy_version"] != trace["policy_version"]
        or outcome["source_schema_version"] != trace["source_schema_version"]
    ):
        return "changed_policy_or_schema"
    if outcome["adjudicator_id"] not in g["adjudicators"]:
        return "unapproved_adjudicator"
    if outcome["reference"] is None:
        return "outcome_unavailable"
    if outcome["facts_as_of"] > trace["decision_at"]:
        return "facts_unavailable_at_decision"
    if outcome["verified_at"] < max(outcome["outcome_at"], outcome["facts_as_of"]):
        return "inconsistent_outcome_time"
    if outcome["verified_at"] > g["as_of"]:
        return "verification_after_cutoff"
    return ""


def _partition(
    rows: list[Json], split: Json, excluded: dict[str, str]
) -> tuple[list[Json], list[Json]]:
    # A declared family and a normalized equivalent input can connect previously separate groups.
    parents: dict[str, str] = {}

    def root(key: str) -> str:
        parents.setdefault(key, key)
        while parents[key] != key:
            parents[key] = parents[parents[key]]
            key = parents[key]
        return key

    for row in rows:
        normalized = {
            key: sorted(re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", val).casefold()))
            or [val]
            for key, val in row["features"].items()
        }
        a, b = root("family:" + row["group_id"]), root("features:" + digest(normalized))
        parents[max(a, b)] = min(a, b)
    groups: dict[str, list[Json]] = defaultdict(list)
    for row in rows:
        groups[root("family:" + row["group_id"])].append(row)
    development: list[Json] = []
    protected: list[Json] = []
    for family in groups.values():
        identity = "grp-" + digest(sorted({r["group_id"] for r in family}))[:32]
        if split["mode"] == "chronological":
            sides = {r["decision_at"] >= split["cutoff"] for r in family}
        else:
            sides = {r["customer_id"] in split["customers"] for r in family}
        if len(sides) != 1:
            excluded.update((r["case_id"], "group_crosses_split") for r in family)
            continue
        testing = sides.pop()
        for row in family:
            if (
                not testing
                and split["mode"] == "chronological"
                and row["verified_at"] >= split["cutoff"]
            ):
                excluded[row["case_id"]] = "development_label_after_split"
                continue
            row["group_id"] = identity
            (protected if testing else development).append(row)
    return sorted(development, key=lambda r: r["case_id"]), sorted(
        protected, key=lambda r: r["case_id"]
    )


def _build(g: Json, traces_path: Path, corrections_path: Path) -> Json:
    traces, trace_digest, d1 = _export(traces_path, g["trace_sha256"], trace=True)
    corrections, outcome_digest, d2 = _export(corrections_path, g["correction_sha256"], trace=False)
    trace_by_id: dict[str, list[Json]] = defaultdict(list)
    outcome_by_event: dict[str, list[Json]] = defaultdict(list)
    outcome_links: dict[str, set[str]] = defaultdict(set)
    for rows, expected, destination in (
        (traces, g["trace_source"], trace_by_id),
        (corrections, g["correction_source"], outcome_by_event),
    ):
        for row in rows:
            require(
                all(row[k] == g[k] for k in ("organization", "workflow", "field"))
                and row["source"] == expected,
                "source_export_scope_mismatch",
            )
            destination[row["event_id"]].append(row)
    for row in corrections:
        outcome_links[row["outcome_id"]].add(row["event_id"])
    reused = {key for key, ids in outcome_links.items() if len(ids) != 1}
    included: list[Json] = []
    excluded: dict[str, str] = {}
    for event_id, events in trace_by_id.items():
        outcomes = outcome_by_event.get(event_id, [])
        reason = "ambiguous_trace" if len(events) != 1 else _reason(g, events[0], outcomes, reused)
        if reason:
            excluded[event_id] = reason
            continue
        trace, outcome = events[0], outcomes[0]
        included.append(
            {
                "case_id": event_id,
                **{
                    k: trace[k]
                    for k in (
                        "group_id",
                        "customer_id",
                        "features",
                        "incumbent",
                        "slices",
                        "decision_at",
                        "features_available_at",
                    )
                },
                **{
                    k: outcome[k]
                    for k in (
                        "reference",
                        "outcome_id",
                        "adjudicator_id",
                        "facts_as_of",
                        "outcome_at",
                        "verified_at",
                    )
                },
            }
        )
    dev, test = _partition(included, g["split"], excluded)
    mistakes: dict[tuple[str, str], list[str]] = defaultdict(list)
    for row in dev:
        current = row["incumbent"]
        if current.get("status") == "automatic" and current["label"] != row["reference"]:
            mistakes[current["label"], row["reference"]].append(row["group_id"])
    classes = [
        {
            "class_id": "class-" + digest([a, b])[:24],
            "from_label": a,
            "to_label": b,
            "count": len(groups),
            "groups": len(set(groups)),
        }
        for (a, b), groups in sorted(mistakes.items())
        if len(set(groups)) >= 2
    ]
    return {
        "schema_version": INTAKE_VERSION,
        **{
            k: g[k]
            for k in (
                "organization",
                "engagement",
                "dataset_id",
                "workflow",
                "field",
                "policy_version",
                "source_schema_version",
                "split",
                "as_of",
                "retention_terms",
            )
        },
        "authority_revision": digest(g),
        "expires_at": g["expires_at"],
        "training_allowed": "training_attestation" in g,
        "training_expires_at": g.get("training_expires_at", g["expires_at"])
        if "training_attestation" in g
        else None,
        "allowed_features": g["permitted_features"],
        "allowed_slices": g["permitted_slices"],
        "sources": {
            "trace": {
                "source_id": g["trace_source"],
                "sha256": g["trace_sha256"],
                "records_digest": trace_digest,
                "duplicate_rows": d1,
            },
            "correction": {
                "source_id": g["correction_source"],
                "sha256": g["correction_sha256"],
                "records_digest": outcome_digest,
                "duplicate_rows": d2,
            },
        },
        "development": dev,
        "protected": test,
        "protected_membership_digest": digest(sorted(r["case_id"] for r in test)),
        "excluded": dict(sorted(excluded.items())),
        "excluded_counts": dict(sorted(Counter(excluded.values()).items())),
        "unmatched_outcome_ids": sorted(
            {o["outcome_id"] for o in corrections if o["event_id"] not in trace_by_id}
        ),
        "suspected_error_classes": classes,
    }


def _discovery(result: Json) -> Json:
    return {
        "discovery_revision": digest(result),
        "development_count": len(result["development"]),
        "protected_count": len(result["protected"]),
        "excluded_counts": result["excluded_counts"],
        "suspected_error_classes": result["suspected_error_classes"],
    }


def discover_intake(
    store: LocalTrainingStore, grant: Path, traces: Path, corrections: Path
) -> Json:
    """Return development-only error candidates for a human owner to review."""
    with store.engagement_guard():
        g = _scope(store, grant)
        require(
            len({store.path, grant, traces, corrections}) == 4, "distinct_source_files_required"
        )
        data = _build(g, traces, corrections)
        data["training_allowed"] = _training_current(store, g)
        return _discovery(data)


def freeze_intake(
    store: LocalTrainingStore,
    grant: Path,
    traces: Path,
    corrections: Path,
    output: Path,
    *,
    confirmation: Json | None = None,
) -> Json:
    """Create the immutable private evaluation input; no data is transmitted."""
    with store.engagement_guard():
        g = _scope(store, grant)
        require(
            len({store.path, grant, traces, corrections, output}) == 5,
            "distinct_source_files_required",
        )
        data = _build(g, traces, corrections)
        data["training_allowed"] = _training_current(store, g)
        proposal = _discovery(data)
        error_class = None
        if confirmation is not None:
            if "delegation" not in g:
                require(
                    confirmation.keys()
                    == {"discovery_revision", "class_id", "owner_id", "attestation"}
                    and confirmation["owner_id"] == g["owner_id"]
                    and _attest(confirmation["attestation"])
                    and confirmation["attestation"]
                    not in {g["owner_attestation"], g.get("training_attestation")}
                    and confirmation["discovery_revision"] == proposal["discovery_revision"],
                    "stale_or_unconfirmed_error_class",
                )
            match = next(
                (
                    c
                    for c in data["suspected_error_classes"]
                    if c["class_id"] == confirmation["class_id"]
                ),
                None,
            )
            require(match is not None, "unverified_recurring_error")
            if "delegation" in g:
                ledger, receipt = _delegated_run(store, g)
                require(
                    confirmation
                    == ledger.confirm_class(
                        receipt["run_id"],
                        actor=receipt["delegate"],
                        discovery_revision=proposal["discovery_revision"],
                        candidate=match,
                        existing_only=True,
                    ),
                    "stale_or_unconfirmed_delegation",
                )
            error_class = {**(match or {}), **confirmation}
        if "delegation" in g:
            data["authority_kind"] = "standing_customer_mandate"
            data["delegation"] = g["delegation"]
        data["error_class"] = error_class
        data["training_case_ids"] = (
            [r["case_id"] for r in data["development"]]
            if data["training_allowed"] and error_class
            else []
        )
        data["intake_revision"] = digest(data)
        # Re-read original authority immediately before durable admission, including withdrawal.
        require(
            digest(_scope(store, grant)) == data["authority_revision"], "source_authority_changed"
        )
        revision = data["intake_revision"]
        require(
            output.parent != store.path.parent
            or not output.name.startswith(
                (
                    "source-intake-",
                    "source-development-",
                    "source-inputs-",
                    "source-file-",
                    "source-withdrawn-",
                )
            ),
            "reserved_source_path",
        )
        blind = [
            {"case_id": row["case_id"], "features": row["features"], "group_id": row["group_id"]}
            for row in data["protected"]
        ]
        marker = {
            "protected_inputs_digest": digest(blind),
            "grant_file": grant.name,
            "intake_revision": revision,
            "authority_revision": data["authority_revision"],
            "protected_membership_digest": data["protected_membership_digest"],
            "development_digest": digest(data["development"]),
            "development_count": len(data["development"]),
            "protected_count": len(data["protected"]),
            "confirmed_recurring_error": error_class is not None,
            "confirmed_error_class_id": error_class["class_id"] if error_class else None,
        }
        _publish(
            store.path.parent / ("source-development-" + revision + ".json"),
            {"rows": data["development"]},
        )
        _publish(store.path.parent / ("source-inputs-" + revision + ".json"), {"rows": blind})
        _publish(output, data)
        _publish(
            store.path.parent / ("source-file-" + digest(str(output)) + ".json"),
            {"intake_revision": revision},
        )
        marker["seal"] = _marker_seal(store, marker, create=True)
        _publish(store.path.parent / ("source-intake-" + revision + ".json"), marker)
        return data


def _verified_marker(
    store: LocalTrainingStore | _BoundEngagement, revision: str
) -> tuple[Json, Json]:
    require(
        isinstance(revision, str) and re.fullmatch(r"[0-9a-f]{64}", revision),
        "invalid_intake_revision",
    )
    marker = parse_manifest(
        _read(store.path.parent / ("source-intake-" + revision + ".json"), 4096)
    )
    seal = marker.pop("seal", None)
    require(
        isinstance(seal, str) and hmac.compare_digest(seal, _marker_seal(store, marker)),
        "unverified_source_authority",
    )
    name = marker.get("grant_file")
    require(
        marker.get("intake_revision") == revision
        and isinstance(name, str)
        and Path(name).name == name,
        "unverified_source_authority",
    )
    assert isinstance(name, str)
    grant = _scope(store, store.path.parent / name)
    require(marker.get("authority_revision") == digest(grant), "unverified_source_authority")
    return marker, grant


def check_current_intake(
    directory: Path,
    organization: str,
    engagement: str,
    revision: str,
    *,
    require_training: bool = False,
) -> Json:
    """Recheck source authority; caller separately verifies and holds the current engagement."""
    store = _BoundEngagement(directory / "training.sqlite3", organization, engagement)
    marker, grant = _verified_marker(store, revision)
    train_expiry = (
        grant.get("training_expires_at", grant["expires_at"])
        if "training_attestation" in grant
        else None
    )
    permitted = _training_current(store, grant)
    if require_training:
        require(permitted and marker["confirmed_recurring_error"], "source_training_not_authorized")
    return {
        **{
            k: marker[k]
            for k in (
                "intake_revision",
                "authority_revision",
                "protected_membership_digest",
                "development_count",
                "protected_count",
                "confirmed_recurring_error",
            )
        },
        **{
            k: grant[k]
            for k in (
                "organization",
                "engagement",
                "dataset_id",
                "workflow",
                "field",
                "expires_at",
            )
        },
        "confirmed_error_class_id": marker.get("confirmed_error_class_id"),
        "training_allowed": permitted,
        "training_authorized": permitted,
        "training_expires_at": train_expiry,
        "allowed_features": grant["permitted_features"],
    }


def inspect_intake(store: LocalTrainingStore, revision: str) -> Json:
    """Check authority and counters without opening either case file or reading answers."""
    engagement = store.engagement_id
    with store.engagement_guard(engagement_id=engagement):
        return check_current_intake(store.path.parent, store.organization, engagement, revision)


def development_rows(
    store: LocalTrainingStore, revision: str, *, for_training: bool = False
) -> list[Json]:
    """Read development data only; model training also needs current authorization."""
    with store.engagement_guard():
        marker, grant = _verified_marker(store, revision)
        if for_training:
            require(
                _training_current(store, grant) and marker["confirmed_recurring_error"],
                "source_training_not_authorized",
            )
        dev = parse_manifest(
            _read(store.path.parent / ("source-development-" + revision + ".json"))
        )["rows"]
        require(
            isinstance(dev, list) and digest(dev) == marker["development_digest"],
            "intake_revision_mismatch",
        )
        return list(dev)


def protected_inputs(
    store: LocalTrainingStore, revision: str, *, include_groups: bool = False
) -> list[Json]:
    """Read frozen features for inference without opening the evaluator's outcomes or incumbent."""
    with store.engagement_guard():
        marker, _ = _verified_marker(store, revision)
        rows = parse_manifest(_read(store.path.parent / ("source-inputs-" + revision + ".json")))[
            "rows"
        ]
        require(
            isinstance(rows, list) and digest(rows) == marker["protected_inputs_digest"],
            "intake_revision_mismatch",
        )
        if include_groups:
            return list(rows)
        return [{"case_id": row["case_id"], "features": row["features"]} for row in rows]


def load_intake(
    store: LocalTrainingStore, path: Path, *, expected_revision: str | None = None
) -> Json:
    """Trusted evaluator only. Check confirmed authority before opening the sealed case package."""
    with store.engagement_guard():
        revision = expected_revision
        if revision is None:
            locator = parse_manifest(
                _read(store.path.parent / ("source-file-" + digest(str(path)) + ".json"), 4096)
            )
            revision = locator.get("intake_revision")
        require(isinstance(revision, str), "invalid_intake_revision")
        assert isinstance(revision, str)
        marker, grant = _verified_marker(store, revision)
        data = parse_manifest(_read(path))
        same = data.pop("intake_revision", None)
        require(
            data.get("schema_version") == INTAKE_VERSION
            and data.get("organization") == grant["organization"]
            and data.get("engagement") == grant["engagement"]
            and same == revision == digest(data)
            and data.get("authority_revision") == marker["authority_revision"],
            "intake_revision_mismatch",
        )
        return {**data, "intake_revision": revision}


def supplied_manifest(
    store: LocalTrainingStore,
    path: Path,
    repair_outputs: dict[str, Json],
    *,
    error_ceiling_bps: int,
) -> Json:
    """Bind supplied predictions to the protected cases for existing MCP enrollment."""
    data = load_intake(store, path)
    ids = {row["case_id"] for row in data["protected"]}
    require(repair_outputs.keys() <= ids, "predictions_outside_protected_cohort")
    manifest = {
        "schema_version": EVALUATION_VERSION,
        "organization": store.organization,
        "dataset_id": data["dataset_id"],
        "evaluation_approved": True,
        "visible_fields": sorted(EVALUATION_FIELDS),
        "destination": "owner_local_store",
        "retention": {"terms_ref": data["retention_terms"]},
        "expires_at": data["expires_at"],
        "manifest": {
            "schema_version": INPUT_VERSION,
            "error_ceiling_bps": error_ceiling_bps,
            "cases": [
                {
                    "case_id": r["case_id"],
                    "current": r["incumbent"],
                    "reference": r["reference"],
                    "repair": repair_outputs.get(r["case_id"], {"status": "missing"}),
                }
                for r in data["protected"]
            ],
        },
    }
    prepare(manifest, store.organization)
    return manifest


def withdraw_intake(store: LocalTrainingStore, grant_attestation: str) -> None:
    """Withdraw this grant even if an old grant or intake file is later restored."""
    with store.engagement_guard():
        _publish(_revocation(store, grant_attestation), {"withdrawn": True})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--organization", required=True)
    actions = parser.add_subparsers(required=True, dest="action")
    for cmd in ("discover", "freeze"):
        p = actions.add_parser(cmd)
        for arg in ("grant", "traces", "corrections"):
            p.add_argument("--" + arg, type=Path, required=True)
        if cmd == "freeze":
            p.add_argument("--output", type=Path, required=True)
            p.add_argument("--confirmation", type=Path)
    actions.add_parser("inspect").add_argument("--intake", type=Path, required=True)
    actions.add_parser("withdraw").add_argument("--grant-attestation", required=True)
    args = parser.parse_args(argv)
    try:
        store = LocalTrainingStore(args.store, args.organization)
        if args.action == "withdraw":
            withdraw_intake(store, args.grant_attestation)
            answer: Json = {"status": "withdrawn"}
        elif args.action == "discover":
            answer = discover_intake(store, args.grant, args.traces, args.corrections)
        else:
            if args.action == "freeze":
                confirm = (
                    parse_manifest(_read(args.confirmation, 4096)) if args.confirmation else None
                )
                result = freeze_intake(
                    store,
                    args.grant,
                    args.traces,
                    args.corrections,
                    args.output,
                    confirmation=confirm,
                )
            else:
                result = load_intake(store, args.intake)
            answer = {
                "intake_revision": result["intake_revision"],
                "protected_membership_digest": result["protected_membership_digest"],
                "development_count": len(result["development"]),
                "protected_count": len(result["protected"]),
                "excluded_counts": result["excluded_counts"],
                "confirmed_recurring_error": result["error_class"] is not None,
            }
        print(canonical(answer))
        return 0
    except TrainingError as error:
        print(str(error), file=sys.stderr)
    except (OSError, sqlite3.Error, csv.Error, ManifestError, UnicodeError, RecursionError):
        print("private_source_unavailable", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
