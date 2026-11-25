"""Validate and freeze a small owner-enrolled training and evaluation dataset."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from opentour_cell.pilot.readout import INPUT_VERSION, ManifestError, evaluate_manifest
from opentour_cell.pilot.text_classifier import (
    MAX_HELDOUT_ROWS,
    MAX_TRAIN_ROWS,
    TrainingError,
    feature_signature,
)

DATA_VERSION = "opentour.pilot.training-dataset.v1"
EVALUATION_VERSION = "opentour.pilot.evaluation-dataset.v1"
EVALUATION_FIELDS = frozenset({"case_id", "current", "repair", "reference"})
FIELD_NAMES = frozenset({"case_id", "family_id", "text", "label", "incumbent"})
PREVIEW_NAMES = frozenset({"case_id", "text", "label"})


def canonical(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def identifier(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", value):
        raise TrainingError("invalid_identifier")
    return value


def _text(value: Any, maximum: int) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= maximum or not value.isprintable():
        raise TrainingError("input_outside_profile")
    return value


def _source_excerpt(excerpt: str, source: str) -> bool:
    if excerpt in source:
        return True
    parts = excerpt.split("[redacted]")
    if len(parts) < 2 or not source.startswith(parts[0]) or not source.endswith(parts[-1]):
        return False
    cursor = len(parts[0])
    end = len(source) - len(parts[-1])
    for literal in parts[1:-1]:
        position = source.find(literal, cursor + 1, end)
        if position < 0:
            return False
        cursor = position + len(literal)
    return cursor < end


def _fields(value: Any, allowed: frozenset[str]) -> list[str]:
    if not isinstance(value, list) or len(value) > len(allowed):
        raise TrainingError("invalid_visibility")
    if any(not isinstance(v, str) or v not in allowed for v in value) or len(set(value)) != len(
        value
    ):
        raise TrainingError("invalid_visibility")
    return sorted(value)


@dataclass
class Dataset:
    name: str
    revision: str
    reviewers: list[str]
    fields: list[str]
    preview_fields: list[str]
    previews: list[dict[str, str]]
    train: list[tuple[str, str]]
    heldout: list[dict[str, Any]]
    keys: dict[str, str]
    ceiling_bps: int
    expires_at: int | None
    mode: str = "training"
    destination: str = "owner_local_store"
    retention: dict[str, str] = field(default_factory=lambda: {"disposition": "unrecorded"})

    @property
    def purpose(self) -> str:
        return "evaluation_only" if self.mode == "evaluation_only" else "local_text_classification"

    @property
    def actions(self) -> list[str]:
        return permitted_actions(self.mode, bool(self.previews))


def permitted_actions(mode: str, has_previews: bool) -> list[str]:
    actions = ["inspect_metadata", "score_frozen"]
    if mode == "training":
        actions.append("train_cpu_text")
        if has_previews:
            actions.append("preview_training_examples")
    return actions


def _custody(data: dict[str, Any], *, required: bool) -> tuple[str, dict[str, str]]:
    destination = data.get("destination", None if required else "owner_local_store")
    if not isinstance(destination, str) or (
        destination != "owner_local_store"
        and not re.fullmatch(r"dest-[0-9a-f]{16,64}", destination)
    ):
        raise TrainingError("invalid_destination")
    if not required and "retention" not in data:
        return destination, {"disposition": "unrecorded"}
    retention = data.get("retention")
    if not isinstance(retention, dict):
        raise TrainingError("invalid_retention")
    if (
        retention.keys() == {"terms_ref"}
        and isinstance(retention["terms_ref"], str)
        and re.fullmatch(r"terms-[0-9a-f]{16,64}", retention["terms_ref"])
    ):
        return destination, dict(retention)
    if (
        retention.keys() == {"disposition", "date"}
        and retention["disposition"] in ("delete", "return", "retain")
        and isinstance(retention["date"], str)
    ):
        try:
            if date.fromisoformat(retention["date"]).isoformat() == retention["date"]:
                return destination, dict(retention)
        except ValueError:
            pass
    raise TrainingError("invalid_retention")


def _expiry(data: dict[str, Any]) -> int | None:
    expires = data.get("expires_at")
    if expires is not None and (type(expires) is not int or not 0 < expires < 2**63):
        raise TrainingError("invalid_grant")
    return expires


def _prepare_evaluation(data: dict[str, Any], organization: str) -> Dataset:
    required = {
        "schema_version",
        "organization",
        "dataset_id",
        "evaluation_approved",
        "visible_fields",
        "destination",
        "retention",
        "manifest",
    }
    if not required <= data.keys() <= required | {"expires_at"}:
        raise TrainingError("invalid_evaluation_dataset")
    if data["organization"] != organization:
        raise TrainingError("scope_mismatch")
    if data["evaluation_approved"] is not True:
        raise TrainingError("permission_required")
    manifest = data["manifest"]
    try:
        evaluate_manifest(manifest)
    except ManifestError:
        raise TrainingError("invalid_evaluation_manifest") from None
    destination, retention = _custody(data, required=True)
    fields = _fields(data["visible_fields"], EVALUATION_FIELDS)
    cases = manifest["cases"]
    return Dataset(
        identifier(data["dataset_id"]),
        digest(data),
        [],
        fields,
        [],
        [],
        [],
        cases,
        {digest(["case", r["case_id"]]): "heldout" for r in cases},
        manifest["error_ceiling_bps"],
        _expiry(data),
        "evaluation_only",
        destination,
        retention,
    )


def prepare(data: dict[str, Any], organization: str) -> Dataset:
    if isinstance(data, dict) and data.get("schema_version") == EVALUATION_VERSION:
        return _prepare_evaluation(data, organization)
    required = {
        "schema_version",
        "organization",
        "dataset_id",
        "reviewers",
        "train_approved",
        "evaluation_approved",
        "visible_fields",
        "error_ceiling_bps",
        "train",
        "heldout",
    }
    if not isinstance(data, dict) or not required <= data.keys() <= required | {
        "preview_fields",
        "expires_at",
        "destination",
        "retention",
    }:
        raise TrainingError("invalid_dataset")
    if data["schema_version"] != DATA_VERSION or data["organization"] != organization:
        raise TrainingError("scope_mismatch")
    if data["train_approved"] is not True or data["evaluation_approved"] is not True:
        raise TrainingError("permission_required")
    expires_at = _expiry(data)
    destination, retention = _custody(data, required=False)
    name = identifier(data["dataset_id"])
    reviewers = data["reviewers"]
    if not isinstance(reviewers, list) or not 1 <= len(reviewers) <= 16:
        raise TrainingError("reviewer_required")
    reviewers = sorted({identifier(r) for r in reviewers})
    fields = _fields(data["visible_fields"], FIELD_NAMES)
    preview_fields = _fields(data.get("preview_fields", []), PREVIEW_NAMES & frozenset(fields))
    ceiling = data["error_ceiling_bps"]
    if type(ceiling) is not int or not 0 <= ceiling <= 10_000:
        raise TrainingError("input_outside_profile")
    train, heldout = data["train"], data["heldout"]
    if not isinstance(train, list) or not 2 <= len(train) <= MAX_TRAIN_ROWS:
        raise TrainingError("training_input_required")
    if not isinstance(heldout, list) or not 1 <= len(heldout) <= MAX_HELDOUT_ROWS:
        raise TrainingError("heldout_input_required")
    inputs: list[tuple[str, str, str]] = []
    evaluation: list[dict[str, Any]] = []
    previews: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    keys: dict[str, str] = {}
    for split, rows in (("train", train), ("heldout", heldout)):
        for row in rows:
            base = {"case_id", "family_id", "organization", "text", "label"}
            if not isinstance(row, dict):
                raise TrainingError("invalid_dataset")
            allowed = base | ({"preview"} if split == "train" else {"incumbent"})
            if not base <= row.keys() <= allowed or (split == "heldout" and "incumbent" not in row):
                raise TrainingError("invalid_dataset")
            if row["organization"] != organization:
                raise TrainingError("scope_mismatch")
            case_id = identifier(row["case_id"])
            if case_id in seen_ids:
                raise TrainingError("duplicate_case")
            seen_ids.add(case_id)
            text = _text(row["text"], 512)
            # Bag-of-words equality and declared family links remain protected across aliases.
            fingerprints = (
                digest(["case", case_id]),
                digest(["family", identifier(row["family_id"])]),
                digest(["features", feature_signature(text)]),
            )
            for key in fingerprints:
                if keys.setdefault(key, split) != split:
                    raise TrainingError("split_overlap")
            if split == "train":
                label = identifier(row["label"])
                inputs.append((case_id, text, label))
                if "preview" in row:
                    preview = row["preview"]
                    if (
                        not isinstance(preview, dict)
                        or not preview
                        or not preview.keys() <= set(preview_fields)
                    ):
                        raise TrainingError("invalid_visibility")
                    for field, value in preview.items():
                        _text(value, 180)
                        if field == "text" and not _source_excerpt(value, text):
                            raise TrainingError("invalid_visibility")
                        if field in ("case_id", "label") and value != row[field]:
                            raise TrainingError("invalid_visibility")
                    previews.append(dict(preview))
            else:
                reference = None if row["label"] is None else identifier(row["label"])
                decision = row["incumbent"]
                if isinstance(decision, dict) and "label" in decision:
                    identifier(decision["label"])
                evaluation.append(
                    {"case_id": case_id, "text": text, "reference": reference, "current": decision}
                )
    if not 2 <= len({label for _, _, label in inputs}) <= 8:
        raise TrainingError("training_input_required")
    if len(previews) > 3:
        raise TrainingError("invalid_visibility")
    # Use the real evaluation contract to validate the immutable owner-supplied incumbent.
    baseline = [
        {
            "case_id": r["case_id"],
            "reference": r["reference"],
            "current": r["current"],
            "repair": {"status": "missing"},
        }
        for r in evaluation
    ]
    try:
        evaluate_manifest(
            {"schema_version": INPUT_VERSION, "error_ceiling_bps": ceiling, "cases": baseline}
        )
    except ManifestError:
        raise TrainingError("invalid_heldout_incumbent") from None
    return Dataset(
        name,
        digest(data),
        reviewers,
        fields,
        preview_fields,
        previews,
        [(text, label) for _, text, label in sorted(inputs)],
        evaluation,
        keys,
        ceiling,
        expires_at,
        "training",
        destination,
        retention,
    )


def generated_dataset(organization: str, reviewer: str) -> dict[str, Any]:
    """Public synthetic routing fixture for an operator's rehearsal."""
    train = []
    for i, (text, label) in enumerate(
        (
            ("invoice billing statement", "billing"),
            ("parcel tracking delivery", "shipping"),
            ("billing fee invoice", "billing"),
            ("shipping parcel transit", "shipping"),
            ("refund charge billing", "billing"),
            ("delivery parcel shipment", "shipping"),
        )
    ):
        row: dict[str, Any] = {
            "case_id": f"train-{i}",
            "family_id": f"train-family-{i}",
            "organization": organization,
            "text": text,
            "label": label,
        }
        if i < 2:
            row["preview"] = {"case_id": row["case_id"], "text": text, "label": label}
        train.append(row)
    heldout = [
        {
            "case_id": f"test-{i}",
            "family_id": f"test-family-{i}",
            "organization": organization,
            "text": text,
            "label": label,
            "incumbent": {"status": "automatic", "label": "billing"},
        }
        for i, (text, label) in enumerate(
            (
                ("billing invoice requested", "billing"),
                ("parcel delivery delayed", "shipping"),
                ("shipping transit update", "shipping"),
                ("fee invoice correction", "billing"),
            )
        )
    ]
    return {
        "schema_version": DATA_VERSION,
        "destination": "owner_local_store",
        "retention": {"terms_ref": "terms-0123456789abcdef"},
        "organization": organization,
        "dataset_id": "generated-intake",
        "reviewers": [reviewer],
        "train_approved": True,
        "evaluation_approved": True,
        "visible_fields": sorted(FIELD_NAMES),
        "preview_fields": ["case_id", "label", "text"],
        "error_ceiling_bps": 1000,
        "train": train,
        "heldout": heldout,
    }


def generated_evaluation_dataset(organization: str) -> dict[str, Any]:
    """Generated frozen outputs with independent references and zero training authority."""
    automatic = {"status": "automatic", "label": "route-a"}
    other = {"status": "automatic", "label": "route-b"}
    cases = [
        {"case_id": "supplied-1", "reference": "route-a", "current": other, "repair": automatic},
        {"case_id": "supplied-2", "reference": "route-b", "current": other, "repair": other},
        {
            "case_id": "supplied-3",
            "reference": "route-b",
            "current": automatic,
            "repair": automatic,
        },
    ]
    return {
        "schema_version": EVALUATION_VERSION,
        "organization": organization,
        "dataset_id": "generated-supplied",
        "evaluation_approved": True,
        "visible_fields": sorted(EVALUATION_FIELDS),
        "destination": "owner_local_store",
        "retention": {"terms_ref": "terms-0123456789abcdef"},
        "manifest": {"schema_version": INPUT_VERSION, "error_ceiling_bps": 1000, "cases": cases},
    }
