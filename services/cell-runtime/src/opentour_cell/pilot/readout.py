"""Describe a frozen set of local decisions with independently supplied reference labels."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

from opentour_cell.pilot.brief import decision_brief

INPUT_VERSION = "opentour.pilot.case-manifest.v1"
OUTPUT_VERSION = "opentour.pilot.readout.v1"
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_CASES = 50_000


class ManifestError(ValueError):
    """An input error whose message can be displayed without exposing case content."""


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ManifestError("Duplicate JSON member.")
    return result


def _reject_constant(_: str) -> NoReturn:
    raise ManifestError("Invalid JSON number.")


def parse_manifest(payload: bytes) -> dict[str, Any]:
    """Decode bounded UTF-8 JSON. evaluate_manifest validates its case contract."""
    if not isinstance(payload, bytes) or len(payload) > MAX_MANIFEST_BYTES:
        raise ManifestError("Expected at most 4 MiB of UTF-8 JSON.")
    try:
        manifest = json.loads(
            payload.decode("utf-8"), object_pairs_hook=_unique_keys, parse_constant=_reject_constant
        )
    except ManifestError:
        raise
    except (ValueError, RecursionError):
        raise ManifestError("Invalid UTF-8 JSON document.") from None
    if not isinstance(manifest, dict):
        raise ManifestError("Expected a manifest object.")
    return manifest


def _token(value: object) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 256
        or value.strip() != value
        or not value.isprintable()
    ):
        raise ManifestError("Expected a nonempty, unpadded printable identifier or label.")
    return value


def _decision(value: object) -> tuple[str, str | None]:
    if isinstance(value, dict):
        status = value.get("status")
        if status == "automatic" and value.keys() == {"status", "label"}:
            return "automatic", _token(value["label"])
        if status in ("review", "missing") and value.keys() == {"status"}:
            return str(status), None
    raise ManifestError("Expected an automatic decision, explicit review, or missing output.")


def _rate(numerator: int, denominator: int) -> dict[str, int | float | None]:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "value": numerator / denominator if denominator else None,
    }


@dataclass
class _Arm:
    automatic: int = 0
    review: int = 0
    missing: int = 0
    unknown_automatic: int = 0
    incorrect_automatic: int = 0

    def observe(self, status: str, label: str | None, reference: str | None) -> None:
        if status == "review":
            self.review += 1
        elif status == "missing":
            self.missing += 1
        else:
            self.automatic += 1
            if reference is None:
                self.unknown_automatic += 1
            elif label != reference:
                self.incorrect_automatic += 1

    def summary(self, size: int, ceiling_bps: int) -> dict[str, Any]:
        verified = self.automatic - self.unknown_automatic
        ceiling_met = None
        if verified and not self.unknown_automatic:
            ceiling_met = self.incorrect_automatic * 10_000 <= ceiling_bps * verified
        return {
            "automatic": self.automatic,
            "review": self.review,
            "missing": self.missing,
            "unknown_reference_automatic": self.unknown_automatic,
            "incorrect_automatic": self.incorrect_automatic,
            "coverage": _rate(self.automatic, size),
            "observed_error_rate": _rate(self.incorrect_automatic, verified),
            "observed_ceiling_met": ceiling_met,
        }


def evaluate_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """Compare both arms on the provided cohort. No I/O and no release decision."""
    if (
        not isinstance(manifest, dict)
        or manifest.keys() != {"schema_version", "error_ceiling_bps", "cases"}
        or manifest.get("schema_version") != INPUT_VERSION
    ):
        raise ManifestError("Unsupported manifest version or fields.")
    ceiling_bps = manifest["error_ceiling_bps"]
    if type(ceiling_bps) is not int or not 0 <= ceiling_bps <= 10_000:
        raise ManifestError("Expected an integer error ceiling from 0 to 10000 basis points.")
    cases = manifest["cases"]
    if not isinstance(cases, list) or len(cases) > MAX_CASES:
        raise ManifestError("Expected an array of at most 50000 cases.")

    current, repair = _Arm(), _Arm()
    identifiers: set[str] = set()
    known = current_only = repair_only = 0
    fixes = regressions = ties_correct = ties_incorrect = 0
    for case in cases:
        if not isinstance(case, dict) or case.keys() != {
            "case_id",
            "reference",
            "current",
            "repair",
        }:
            raise ManifestError("Expected a case ID, reference and decisions from both methods.")
        case_id = _token(case["case_id"])
        if case_id in identifiers:
            raise ManifestError("Duplicate case ID.")
        identifiers.add(case_id)

        reference = None if case["reference"] is None else _token(case["reference"])
        known += reference is not None
        current_status, current_label = _decision(case["current"])
        repair_status, repair_label = _decision(case["repair"])
        current.observe(current_status, current_label, reference)
        repair.observe(repair_status, repair_label, reference)

        current_only += current_status == "automatic" and repair_status != "automatic"
        repair_only += repair_status == "automatic" and current_status != "automatic"
        if reference is None or current_status != "automatic" or repair_status != "automatic":
            continue
        if current_label == reference:
            if repair_label == reference:
                ties_correct += 1
            else:
                regressions += 1
        elif repair_label == reference:
            fixes += 1
        else:
            ties_incorrect += 1

    size = len(cases)
    comparable = fixes + regressions + ties_correct + ties_incorrect
    if (
        not comparable
        or current.missing
        or repair.missing
        or current.unknown_automatic
        or repair.unknown_automatic
    ):
        comparison = "insufficient_evidence"
    elif current_only or repair_only:
        comparison = "different_automatic_cases"
    elif fixes > regressions:
        comparison = "improved"
    elif fixes < regressions:
        comparison = "regressed"
    else:
        comparison = "equivalent"

    return {
        "schema_version": OUTPUT_VERSION,
        "cohort_size": size,
        "known_references": known,
        "unknown_references": size - known,
        "error_ceiling_bps": ceiling_bps,
        "current": current.summary(size, ceiling_bps),
        "repair": repair.summary(size, ceiling_bps),
        "paired": {
            "comparable": comparable,
            "excluded": size - comparable,
            "fixes": fixes,
            "regressions": regressions,
            "ties_correct": ties_correct,
            "ties_incorrect": ties_incorrect,
        },
        "changes": {
            "current_only_automatic": current_only,
            "repair_only_automatic": repair_only,
            "same_automatic_cases": current_only == repair_only == 0,
            "coverage_change": _rate(repair.automatic - current.automatic, size),
            "review_delta": repair.review - current.review,
            "missing_delta": repair.missing - current.missing,
        },
        "observed_comparison": comparison,
    }


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        self.exit(2, "Invalid command arguments. Use --help for usage.\n")


def main() -> int:
    parser = _Parser(description=__doc__)
    parser.add_argument("manifest", metavar="FILE", help="local case manifest, or - for stdin")
    parser.add_argument(
        "--report-dir",
        type=Path,
        metavar="NEW_DIRECTORY",
        help="write brief.md and aggregate.json together to a new local directory",
    )
    args = parser.parse_args()
    try:
        if args.manifest == "-":
            payload = sys.stdin.buffer.read(MAX_MANIFEST_BYTES + 1)
        else:
            with Path(args.manifest).open("rb") as source:
                payload = source.read(MAX_MANIFEST_BYTES + 1)
        report = evaluate_manifest(parse_manifest(payload))
    except ManifestError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except OSError:
        print("Unable to read local manifest.", file=sys.stderr)
        return 2
    aggregate = json.dumps(report, sort_keys=True, allow_nan=False)
    if args.report_dir is None:
        print(aggregate)
        return 0
    try:
        target: Path = args.report_dir
        with tempfile.TemporaryDirectory(dir=target.parent, prefix=".opentour-") as scratch:
            staged = Path(scratch) / "readout"
            staged.mkdir(mode=0o700)
            (staged / "aggregate.json").write_bytes((aggregate + "\n").encode("utf-8"))
            (staged / "brief.md").write_bytes(decision_brief(report).encode("utf-8"))
            if os.name == "nt":
                # Windows rename refuses any existing destination.
                staged.rename(target)
            else:
                # Reserve the name exclusively before replacing our own empty directory.
                target.mkdir(mode=0o700)
                reservation = target.stat(follow_symlinks=False)
                try:
                    staged.rename(target)
                except OSError:
                    try:
                        if os.path.samestat(reservation, target.stat(follow_symlinks=False)):
                            target.rmdir()
                    except FileNotFoundError:
                        pass
                    raise
    except OSError:
        print(
            "Unable to publish report. Choose a new directory under a writable parent.",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
