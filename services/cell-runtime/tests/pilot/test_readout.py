"""Generated classification cases for the customer-local descriptive readout."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from opentour_cell.pilot import readout
from opentour_cell.pilot.brief import decision_brief
from opentour_cell.pilot.readout import (
    INPUT_VERSION,
    MAX_MANIFEST_BYTES,
    OUTPUT_VERSION,
    ManifestError,
    evaluate_manifest,
    parse_manifest,
)


def automatic(label: str) -> dict[str, str]:
    return {"status": "automatic", "label": label}


def case(
    identifier: str,
    reference: str | None,
    current: dict[str, str],
    repair: dict[str, str],
) -> dict[str, Any]:
    return {"case_id": identifier, "reference": reference, "current": current, "repair": repair}


def manifest(*rows: dict[str, Any], ceiling: int = 1000) -> dict[str, Any]:
    return {"schema_version": INPUT_VERSION, "error_ceiling_bps": ceiling, "cases": list(rows)}


GOOD = automatic("generated-expected")
BAD = automatic("generated-other")
REVIEW = {"status": "review"}
MISSING = {"status": "missing"}
LABEL = GOOD["label"]


def test_qualified_pairs_count_fixes_regressions_and_both_kinds_of_tie():
    data = manifest(
        case("generated-1", LABEL, BAD, GOOD),
        case("generated-2", LABEL, GOOD, BAD),
        case("generated-3", LABEL, GOOD, GOOD),
        case("generated-4", LABEL, BAD, automatic("generated-third")),
        case("generated-5", None, REVIEW, REVIEW),
        ceiling=5000,
    )
    report = evaluate_manifest(data)
    assert report["schema_version"] == OUTPUT_VERSION
    assert report["cohort_size"] == 5
    assert report["known_references"] == 4
    assert report["unknown_references"] == 1
    assert report["observed_comparison"] == "equivalent"
    assert (
        report["current"]
        == report["repair"]
        == {
            "automatic": 4,
            "review": 1,
            "missing": 0,
            "unknown_reference_automatic": 0,
            "incorrect_automatic": 2,
            "coverage": {"numerator": 4, "denominator": 5, "value": 0.8},
            "observed_error_rate": {"numerator": 2, "denominator": 4, "value": 0.5},
            "observed_ceiling_met": True,
        }
    )
    assert report["paired"] == {
        "comparable": 4,
        "excluded": 1,
        "fixes": 1,
        "regressions": 1,
        "ties_correct": 1,
        "ties_incorrect": 1,
    }
    assert report["changes"]["same_automatic_cases"] is True
    assert "generated-" not in json.dumps(report)


@pytest.mark.parametrize(
    ("current", "repair", "result"),
    [(BAD, GOOD, "improved"), (GOOD, BAD, "regressed"), (BAD, BAD, "equivalent")],
)
def test_describes_observed_net_change_at_matched_coverage(current, repair, result):
    report = evaluate_manifest(manifest(case("generated-1", LABEL, current, repair)))
    assert report["observed_comparison"] == result


def test_removing_a_known_bad_output_to_review_is_never_a_fix():
    report = evaluate_manifest(
        manifest(case("generated-1", LABEL, BAD, REVIEW), case("generated-2", LABEL, GOOD, GOOD))
    )
    assert report["current"]["incorrect_automatic"] == 1
    assert report["repair"]["incorrect_automatic"] == 0
    assert report["paired"]["fixes"] == 0
    assert report["paired"]["excluded"] == 1
    assert report["observed_comparison"] == "different_automatic_cases"
    assert report["changes"] == {
        "current_only_automatic": 1,
        "repair_only_automatic": 0,
        "same_automatic_cases": False,
        "coverage_change": {"numerator": -1, "denominator": 2, "value": -0.5},
        "review_delta": 1,
        "missing_delta": 0,
    }


def test_equal_coverage_percentages_cannot_hide_different_automatic_cases():
    report = evaluate_manifest(
        manifest(
            case("generated-1", LABEL, GOOD, REVIEW),
            case("generated-2", LABEL, REVIEW, GOOD),
            case("generated-3", LABEL, BAD, GOOD),
        )
    )
    assert report["changes"]["coverage_change"]["value"] == 0
    assert not report["changes"]["same_automatic_cases"]
    assert report["paired"]["fixes"] == 1
    assert report["paired"]["comparable"] == 1
    assert report["observed_comparison"] == "different_automatic_cases"


def test_missing_output_and_unknown_truth_are_distinct_from_review():
    report = evaluate_manifest(
        manifest(
            case("generated-1", LABEL, GOOD, GOOD),
            case("generated-2", LABEL, MISSING, REVIEW),
            case("generated-3", None, BAD, GOOD),
        )
    )
    assert report["known_references"] == 2
    assert report["unknown_references"] == 1
    assert report["current"]["missing"] == 1
    assert report["current"]["review"] == 0
    assert report["repair"]["review"] == 1
    assert report["changes"]["missing_delta"] == -1
    assert report["current"]["observed_error_rate"]["denominator"] == 1
    assert report["repair"]["unknown_reference_automatic"] == 1
    assert report["repair"]["observed_ceiling_met"] is None
    assert report["paired"]["comparable"] == 1
    assert report["observed_comparison"] == "insufficient_evidence"


def test_empty_cohort_exposes_undefined_rates_and_comparison():
    report = evaluate_manifest(manifest())
    assert report["cohort_size"] == report["paired"]["comparable"] == 0
    assert report["current"]["coverage"] == {"numerator": 0, "denominator": 0, "value": None}
    assert report["repair"]["observed_error_rate"]["value"] is None
    assert report["current"]["observed_ceiling_met"] is None
    assert report["observed_comparison"] == "insufficient_evidence"


@pytest.mark.parametrize(("ceiling", "met"), [(3333, False), (3334, True)])
def test_ceiling_comparison_uses_exact_counts(ceiling, met):
    data = manifest(
        *(case(str(i), LABEL, output, GOOD) for i, output in enumerate([BAD, GOOD, GOOD]))
    )
    data["error_ceiling_bps"] = ceiling
    assert evaluate_manifest(data)["current"]["observed_ceiling_met"] is met


@pytest.mark.parametrize("threshold", [True, None, -1, 10001, 100.0, "100"])
def test_invalid_error_ceiling_is_rejected(threshold):
    with pytest.raises(ManifestError):
        evaluate_manifest(manifest() | {"error_ceiling_bps": threshold})


@pytest.mark.parametrize(
    "bad_field",
    [
        {"case_id": ""},
        {"case_id": " padded"},
        {"reference": {"label": "unknown"}},
        {"repair": {"status": "review", "label": LABEL}},
        {"repair": {"status": "missing", "reason": LABEL}},
        {"current": {"status": "automatic"}},
        {"current": {"status": "automatic", "label": ""}},
        {"current": {"status": "unexpected"}},
        {"current": None},
        {"extra": "generated-field"},
    ],
)
def test_invalid_case_shape_and_decisions_are_rejected(bad_field):
    row = case("generated-1", LABEL, GOOD, GOOD)
    with pytest.raises(ManifestError):
        evaluate_manifest(manifest(row | bad_field))


def test_duplicate_cases_missing_decisions_and_unsupported_version_are_rejected():
    row = case("generated-1", LABEL, GOOD, GOOD)
    with pytest.raises(ManifestError, match="Duplicate case ID"):
        evaluate_manifest(manifest(row, row))
    with pytest.raises(ManifestError):
        evaluate_manifest(manifest({key: value for key, value in row.items() if key != "repair"}))
    with pytest.raises(ManifestError):
        evaluate_manifest(manifest() | {"schema_version": "v0"})


@pytest.mark.parametrize(
    "payload",
    [b"{}{}", b"[]", b"\xff", b'{"x":1,"x":2}', b'{"nested":{"x":1,"x":2}}', b'{"x":NaN}'],
)
def test_invalid_documents_are_rejected(payload):
    with pytest.raises(ManifestError):
        parse_manifest(payload)


def test_file_parse_bounds_apply_before_processing():
    with pytest.raises(ManifestError, match="4 MiB"):
        parse_manifest(b" " * (MAX_MANIFEST_BYTES + 1))


def run_cli(
    argument: str, payload: bytes = b"", *options: str
) -> subprocess.CompletedProcess[bytes]:
    src = Path(__file__).resolve().parents[2] / "src"
    paths = [str(src)]
    if existing := os.environ.get("PYTHONPATH"):
        paths.append(existing)
    return subprocess.run(
        [sys.executable, "-m", "opentour_cell.pilot.readout", *options, argument],
        input=payload,
        capture_output=True,
        env=os.environ | {"PYTHONPATH": os.pathsep.join(paths)},
        check=False,
    )


def test_cli_file_and_stdin_replay_are_read_only(tmp_path):
    rows = [case("generated-1", LABEL, BAD, GOOD), case("generated-2", LABEL, GOOD, GOOD)]
    data = manifest(*rows)
    payload = json.dumps(data).encode()
    source = tmp_path / "manifest.json"
    source.write_bytes(payload)
    result = run_cli(str(source))
    replay = run_cli("-", json.dumps(manifest(*reversed(rows))).encode())
    assert result.returncode == replay.returncode == 0
    assert result.stdout == replay.stdout
    assert json.loads(result.stdout) == evaluate_manifest(data)
    assert result.stderr == replay.stderr == b""
    assert source.read_bytes() == payload
    assert list(tmp_path.iterdir()) == [source]


def test_cli_failure_returns_no_partial_report_and_does_not_echo_values(tmp_path):
    row = case("generated-sensitive-identifier", "generated-sensitive-label", GOOD, BAD)
    failure = run_cli("-", json.dumps(manifest(row, row)).encode())
    unreadable = run_cli(str(tmp_path / "generated-sensitive-path"))
    for result in (failure, unreadable):
        assert result.returncode == 2
        assert result.stdout == b""
        assert result.stderr and b"sensitive" not in result.stderr


def test_report_dir_publishes_replayable_outputs_after_validation(tmp_path):
    data = manifest(
        case("generated-1", LABEL, BAD, GOOD),
        case("generated-2", LABEL, GOOD, BAD),
        case("generated-3", LABEL, GOOD, REVIEW),
        case("generated-4", LABEL, REVIEW, GOOD),
    )
    payload = json.dumps(data).encode()
    target = tmp_path / "new"
    assert run_cli("-", b"{}", "--report-dir", str(target)).returncode == 2
    assert list(tmp_path.iterdir()) == []
    previous = run_cli("-", payload)
    result = run_cli("-", payload, "--report-dir", str(target))
    assert (result.returncode, result.stdout, result.stderr) == (0, b"", b"")
    assert (target / "aggregate.json").read_bytes() == previous.stdout
    brief = (target / "brief.md").read_text()
    assert brief == decision_brief(evaluate_manifest(data))
    for text in (
        "3/4 (75.00%)",
        "(fix) | 1",
        "(regression) | 1",
        "Whole-arm error rates",
        "only by the incumbent total 1",
        "only by the repair total 1",
    ):
        assert text in brief
    assert "generated-" not in brief and "| Decision and next action | |" in brief
    existing = run_cli("-", json.dumps(manifest()).encode(), "--report-dir", str(target))
    assert existing.returncode == 2
    assert (target / "brief.md").read_text() == brief
    assert (target / "aggregate.json").read_bytes() == previous.stdout


def test_empty_and_unverified_briefs_use_true_denominators():
    empty = decision_brief(evaluate_manifest(manifest()))
    assert "0/0 (undefined)" in empty and "Undetermined" in empty
    mixed = manifest(
        case("generated-1", LABEL, BAD, GOOD),
        case("generated-2", None, GOOD, MISSING),
    )
    result = decision_brief(evaluate_manifest(mixed))
    for text in (
        "Insufficient evidence",
        "1/1 (100.00%) | 0/1 (0.00%)",
        "Unknown reference | 1",
        "1/2 (50.00%)",
        "missing outputs by +1",
        "paired comparison total 1",
    ):
        assert text in result


def test_cli_rejects_unknown_arguments_and_existing_empty_output(tmp_path):
    result = run_cli("-", b"{}", "--generated-private-identifier")
    assert result.returncode == 2 and result.stdout == b"" and b"private" not in result.stderr
    vacant = tmp_path / "preexisting"
    vacant.mkdir()
    before = vacant.stat()
    result = run_cli("-", json.dumps(manifest()).encode(), "--report-dir", str(vacant))
    assert result.returncode == 2 and not list(vacant.iterdir())
    assert vacant.stat().st_ino == before.st_ino


def test_report_publication_failure_cleans_empty_reservation(tmp_path, monkeypatch, capsys):
    source = tmp_path / "source.json"
    source.write_text(json.dumps(manifest()))
    target = tmp_path / "report"
    monkeypatch.setattr(sys, "argv", ["readout", "--report-dir", str(target), str(source)])

    def fail_rename(staged, destination):
        assert set(p.name for p in staged.iterdir()) == {"aggregate.json", "brief.md"}
        if os.name == "posix":
            assert destination.is_dir() and not list(destination.iterdir())
            assert destination.stat().st_mode & 0o777 == 0o700
        raise OSError("generated publication failure")

    monkeypatch.setattr(Path, "rename", fail_rename)
    assert readout.main() == 2
    assert capsys.readouterr().out == ""
    assert list(tmp_path.iterdir()) == [source]


@pytest.mark.skipif(os.name != "posix", reason="POSIX uses an exclusive directory reservation")
def test_failed_publication_leaves_a_replaced_reservation_alone(tmp_path, monkeypatch):
    source = tmp_path / "source.json"
    source.write_text(json.dumps(manifest()))
    target = tmp_path / "report"
    monkeypatch.setattr(sys, "argv", ["readout", "--report-dir", str(target), str(source)])

    def replace_and_fail(staged, destination):
        os.rename(destination, tmp_path / "moved-reservation")
        destination.mkdir()
        raise OSError("generated replacement")

    monkeypatch.setattr(Path, "rename", replace_and_fail)
    assert readout.main() == 2
    assert target.is_dir()
