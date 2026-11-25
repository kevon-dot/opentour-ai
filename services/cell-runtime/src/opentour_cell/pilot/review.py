"""Prepare exceptions from a frozen case manifest for the operator's local review process."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import TypedDict

from opentour_cell.pilot.readout import (
    MAX_MANIFEST_BYTES,
    ManifestError,
    evaluate_manifest,
    parse_manifest,
)


class ReviewItem(TypedDict):
    case_id: str
    review_reasons: list[str]
    missing_predictions: list[str]


class ReviewWorklist(TypedDict):
    schema_version: str
    manifest_sha256: str
    exceptions: list[ReviewItem]
    paired_regression_case_ids: list[str]


def build_worklist(payload: bytes) -> ReviewWorklist:
    """Index exceptions by the original local case ID and exact manifest bytes."""
    manifest = parse_manifest(payload)
    # The readout enforces the full case contract before any row can be handed off.
    evaluate_manifest(manifest)
    exceptions: list[ReviewItem] = []
    paired_regressions: list[str] = []
    for case in manifest["cases"]:
        reasons: list[str] = []
        missing: list[str] = []
        reference = case["reference"]
        if reference is None:
            reasons.append("missing_independent_reference")
        for arm in ("current", "repair"):
            status = case[arm]["status"]
            if status == "review":
                reasons.append(f"{arm}_requested_review")
            elif status == "missing":
                missing.append(arm)
        if reasons or missing:
            exceptions.append(
                {
                    "case_id": case["case_id"],
                    "review_reasons": reasons,
                    "missing_predictions": missing,
                }
            )
        elif case["current"]["label"] == reference and case["repair"]["label"] != reference:
            # Both arms automated and the reference is known. This is an operator diagnostic.
            paired_regressions.append(case["case_id"])
    exceptions.sort(key=lambda item: item["case_id"])
    return {
        "schema_version": "opentour.pilot.review-worklist.v1",
        "manifest_sha256": hashlib.sha256(payload).hexdigest(),
        "exceptions": exceptions,
        "paired_regression_case_ids": sorted(paired_regressions),
    }


def slack_preview(worklist: ReviewWorklist) -> dict[str, str] | None:
    """Build a message locally. Only the presence of a human review item affects it."""
    if any(item["review_reasons"] for item in worklist["exceptions"]):
        return {"text": "Open the approved review system to check the pending workflow exceptions."}
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", metavar="FILE", help="local case manifest, or - for stdin")
    parser.add_argument(
        "--slack-preview",
        action="store_true",
        help="print only the local Slack message preview when a case needs human review",
    )
    args = parser.parse_args()
    try:
        if args.manifest == "-":
            payload = sys.stdin.buffer.read(MAX_MANIFEST_BYTES + 1)
        else:
            with Path(args.manifest).open("rb") as source:
                payload = source.read(MAX_MANIFEST_BYTES + 1)
        worklist = build_worklist(payload)
    except ManifestError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except OSError:
        print("Unable to read local manifest.", file=sys.stderr)
        return 2
    result = slack_preview(worklist) if args.slack_preview else worklist
    if result is not None:
        print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
