"""Generated local case manifests for the offline exception handoff."""

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from opentour_cell.pilot.readout import INPUT_VERSION, MAX_MANIFEST_BYTES, evaluate_manifest
from opentour_cell.pilot.review import build_worklist

LABEL = "synthetic-private-label"
AUTO = {"status": "automatic", "label": LABEL}
REVIEW = {"status": "review"}
MISSING = {"status": "missing"}


def case(
    case_id: str,
    reference: str | None = LABEL,
    current: dict[str, str] = AUTO,
    repair: dict[str, str] = AUTO,
) -> dict[str, object]:
    return {"case_id": case_id, "reference": reference, "current": current, "repair": repair}


def manifest(*cases: dict[str, object]) -> bytes:
    return json.dumps(
        {"schema_version": INPUT_VERSION, "error_ceiling_bps": 100, "cases": cases}
    ).encode()


def run_cli(*args: str, payload: bytes = b"") -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, "-m", "opentour_cell.pilot.review", *args],
        input=payload,
        capture_output=True,
        timeout=10,
        check=False,
    )


class OfflineReviewTests(unittest.TestCase):
    def test_mixed_worklist_keeps_review_and_missing_output_distinct(self) -> None:
        payload = manifest(
            case("case-z", current=MISSING, repair=REVIEW),
            case("case-routine"),
            case("case-known-error", reference="other-verified-label"),
            case("case-a", reference=None, current=REVIEW, repair=REVIEW),
            case("case-machine", repair=MISSING),
        )
        with tempfile.TemporaryDirectory() as dirname:
            path = Path(dirname) / "manifest.json"
            path.write_bytes(payload)
            result = run_cli(str(path))
            self.assertEqual(path.read_bytes(), payload)
            self.assertEqual(list(path.parent.iterdir()), [path])

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, b"")
        self.assertEqual(result.stdout, run_cli("-", payload=payload).stdout)
        original = json.loads(result.stdout)
        self.assertEqual(
            original,
            {
                "schema_version": "opentour.pilot.review-worklist.v1",
                "manifest_sha256": hashlib.sha256(payload).hexdigest(),
                "paired_regression_case_ids": [],
                "exceptions": [
                    {
                        "case_id": "case-a",
                        "review_reasons": [
                            "missing_independent_reference",
                            "current_requested_review",
                            "repair_requested_review",
                        ],
                        "missing_predictions": [],
                    },
                    {
                        "case_id": "case-machine",
                        "review_reasons": [],
                        "missing_predictions": ["repair"],
                    },
                    {
                        "case_id": "case-z",
                        "review_reasons": ["repair_requested_review"],
                        "missing_predictions": ["current"],
                    },
                ],
            },
        )
        self.assertNotIn(LABEL.encode(), result.stdout)
        revised = build_worklist(payload + b"\n")
        self.assertNotEqual(original["manifest_sha256"], revised["manifest_sha256"])
        self.assertEqual(original["exceptions"], revised["exceptions"])

    def test_actionable_preview_is_a_fixed_message_independent_of_manifest_contents(self) -> None:
        message = "Open the approved review system to check the pending workflow exceptions."
        payloads = [
            manifest(case("local-case-identifier", reference=None)),
            manifest(case("other-local-identifier", repair=REVIEW), case("extra", repair=REVIEW)),
        ]
        for payload in payloads:
            with self.subTest(payload_length=len(payload)):
                result = run_cli("-", "--slack-preview", payload=payload)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, b"")
                self.assertEqual(json.loads(result.stdout), {"text": message})

    def test_local_diagnostics_reconcile_to_paired_regressions_without_notifying(self) -> None:
        wrong = {"status": "automatic", "label": "alternative-private-label"}
        regressed = manifest(case("case-z", repair=wrong), case("case-a", repair=wrong))
        preview = run_cli("-", "--slack-preview", payload=regressed)
        self.assertEqual((preview.returncode, preview.stdout, preview.stderr), (0, b"", b""))
        self.assertEqual(build_worklist(regressed)["exceptions"], [])

        mixed = manifest(
            case("case-z", repair=wrong),
            case("case-a", repair=wrong),
            case("case-fixed", current=wrong),
            case("case-tie", current=wrong, repair=wrong),
            case("case-review", repair=REVIEW),
            case("case-missing", repair=MISSING),
            case("case-unknown", reference=None, repair=wrong),
        )
        result = run_cli("-", payload=mixed)
        self.assertEqual(result.returncode, 0, result.stderr)
        diagnostic_ids = json.loads(result.stdout)["paired_regression_case_ids"]
        self.assertEqual(diagnostic_ids, ["case-a", "case-z"])
        aggregate = evaluate_manifest(json.loads(mixed))
        self.assertEqual(len(diagnostic_ids), aggregate["paired"]["regressions"])
        self.assertNotIn(wrong["label"].encode(), result.stdout)
        self.assertNotIn(LABEL.encode(), result.stdout)

    def test_no_notification_for_empty_routine_or_machine_only_cases(self) -> None:
        for payload in (
            manifest(),
            manifest(case("case-routine")),
            manifest(case("case-missing", current=MISSING, repair=MISSING)),
        ):
            with self.subTest(payload_length=len(payload)):
                result = run_cli("-", "--slack-preview", payload=payload)
                self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"", b""))

    def test_invalid_input_fails_without_partial_worklist_or_preview(self) -> None:
        for payload in (
            manifest(case("private-sentinel", reference=None), case("private-sentinel")),
            manifest(case("private-sentinel", repair={"status": "unexpected-private-sentinel"})),
            b'{"private-sentinel":',
            b" " * (MAX_MANIFEST_BYTES + 1),
        ):
            for flags in ([], ["--slack-preview"]):
                with self.subTest(payload_length=len(payload), flags=flags):
                    result = run_cli("-", *flags, payload=payload)
                    self.assertEqual((result.returncode, result.stdout), (2, b""))
                    self.assertTrue(result.stderr)
                    self.assertNotIn(b"private-sentinel", result.stderr)
                    self.assertNotIn(b"Traceback", result.stderr)

    def test_unreadable_manifest_does_not_echo_filename(self) -> None:
        with tempfile.TemporaryDirectory() as dirname:
            result = run_cli(str(Path(dirname) / "private-path-sentinel"))
        self.assertEqual((result.returncode, result.stdout), (2, b""))
        self.assertEqual(result.stderr, b"Unable to read local manifest.\n")
