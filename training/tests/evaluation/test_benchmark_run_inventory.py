"""OPE-526 steps 1-4: run inventory, grouping, branch choice and decision record.

Fixture kind: generated. Measurement (SHA, dirty flag, clock) is injected, so no test calls git.
A generated run can never select the "adequate" branch or satisfy a real-population gate.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from audit_fixtures import baseline_records
from ope526_run_inventory import POPULATIONS, build_document, write_document
from opentour_training.evaluation.benchmark_audit.model import Verdict
from opentour_training.evaluation.benchmark_audit.run_inventory import (
    EVIDENCE_LABEL,
    GENERATED_KIND,
    Measurement,
    select_branch,
)

SHA = "a" * 40
MEASURED = Measurement("2026-01-01T00:00:00Z", SHA, False)
MEASUREMENT_ONLY = {"observed_audit_cpu_ms"}


def clock() -> str:
    return "2026-01-01T00:00:01Z"


def doc(**kw: Any) -> dict[str, Any]:
    return build_document(MEASURED, clock, **kw)


def strip(document: dict[str, Any]) -> dict[str, Any]:
    copy = json.loads(json.dumps(document))
    for row in copy["runs"]:
        for key in MEASUREMENT_ONLY:
            row.pop(key)
    return copy


def test_generation_is_reproducible_apart_from_observed_cpu_time() -> None:
    assert strip(doc()) == strip(doc())


def test_every_row_carries_run_id_timestamp_sha_and_dirty_flag() -> None:
    document = doc()
    ids = [r["run_id"] for r in document["runs"]]
    assert len(ids) == len(set(ids)) == len(POPULATIONS)
    for row in document["runs"]:
        assert re.fullmatch(r"ope526-fixture-[a-z0-9_]+-[0-9a-f]{12}", row["run_id"])
        assert row["timestamp"] == clock()
        assert row["measured_git_sha"] == SHA and row["dirty"] is False
        assert row["evidence"]["fixture_kind"] == GENERATED_KIND
        assert row["evidence"]["evidence_label"] == EVIDENCE_LABEL
        assert row["evidence"]["real_population_gate_satisfied"] is False


def test_inventory_records_required_step1_fields() -> None:
    required = {
        "evidence", "sampling_frame", "verifier", "cutoff", "split", "answer_key_exposure",
        "inputs_and_configuration", "metric", "denominator", "missing_fields", "status",
        "held", "release_check",
    }  # fmt: skip
    for row in doc()["runs"]:
        assert required <= set(row), row["population"]
        # Independent families collapse grouped rows, so the metric never exceeds verified rows.
        assert row["metric"]["denominator_families"] <= row["denominator"]["verified"]
        assert row["denominator"]["reconciles"] == 1
        assert row["answer_key_exposure"]["mapper_view_protected_label_fields"] == 0


def test_summary_counts_attempted_completed_failed_and_held() -> None:
    document = doc()
    summary, rows = document["summary"], document["runs"]
    assert summary["attempted"] == len(rows) == summary["completed"] + summary["failed"]
    assert summary["failed"] == 0
    assert summary["held"] == sum(1 for r in rows if r["verdict"] != "evaluation_allowed")
    assert summary["matching_hand_checked_verdict"] == len(rows)
    assert summary["release_permitted"] == 0


def test_every_hand_checked_population_meets_its_expected_verdict() -> None:
    rows = {r["population"]: r for r in doc()["runs"]}
    assert rows["baseline"]["verdict"] == Verdict.EVALUATION_ALLOWED.value
    assert rows["mutation_b_no_critical_verification"]["coverage"]["critical"] == "0/4"
    assert rows["mutation_c_cross_split_sibling"]["verdict"] == Verdict.INVALID.value
    assert rows["mutation_d_post_freeze_relabel"]["verdict"] == Verdict.STALE.value
    assert all(r["expectation_met"] for r in rows.values())
    held = [r for r in rows.values() if r["held"]]
    assert all(r["release_check"]["outcome"] == "refused" for r in held)
    assert all(r["claims"]["alerts_emitted"] == 0 for r in rows.values())


def test_comparable_groups_each_have_a_baseline() -> None:
    groups = doc()["groups"]
    assert len(groups) == 2
    assert all(g["comparable_baseline_present"] for g in groups)


def test_step2_selects_missing_audit_because_no_independent_customer_audit_exists() -> None:
    document = doc()
    branch = document["step2_decision"]
    assert branch["branch"] == "missing_audit"
    assert "no_independent_customer_label_audit" in branch["triggers"]
    assert {f["disposition"] for f in document["injected_findings"]} == {"detected_as_designed"}


def test_step4_decision_record_names_baseline_reruns_and_holds() -> None:
    document = doc()
    record = document["step4_decision_record"]
    by_name = {r["population"]: r["run_id"] for r in document["runs"]}
    assert record["baseline_run_id"] == by_name["baseline"]
    assert record["rerun_ids"] == [by_name["baseline_rescore"], by_name["mutation_c_resealed"]]
    assert record["expectation_met"] is True
    assert record["observed"]["rerun_digests_match_baseline"] is True
    assert record["observed"]["baseline_customer_accuracy_alert_permitted"] is False
    assert record["observed"]["baseline_upper_error"] == pytest.approx(0.2209, abs=5e-5)
    assert record["evidence_level"] == EVIDENCE_LABEL
    assert record["decision"] == "hold_for_specified_evidence"
    assert record["change_revision"] == SHA
    assert record["step3_rerun"]["scorer_run_alone_lifts_hold"] is False
    assert "59 independent families" in record["next_iteration"]["stop_condition"]
    assert set(record["preserved_unsuccessful_trials"]) == {
        r["run_id"] for r in document["runs"] if r["held"]
    }
    assert {b["item"] for b in document["blocked"]} == {"signed audit receipts"}
    assert document["release_worker"]["status"] == "gap"


def test_output_is_redacted() -> None:
    text = json.dumps(doc())
    records = baseline_records()
    for record in records:
        assert record.family_id not in text
        assert record.final_label_digest not in text
        assert record.first_labeler_id not in text
        if record.adjudicator_id:
            assert record.adjudicator_id not in text
        if record.independent_source:
            assert record.independent_source not in text


def test_a_failed_run_is_inventoried_and_forces_an_unsafe_hold() -> None:
    def broken(_: Any) -> dict[str, Any]:
        raise RuntimeError("generated failure")

    document = doc(populations=POPULATIONS[:2], auditor=broken)
    assert document["summary"]["failed"] == 2 and document["summary"]["completed"] == 0
    assert all(r["status"] == "failed" and r["failure"] == "RuntimeError" for r in document["runs"])
    assert document["step2_decision"]["branch"] == "unsafe_effect_hold"
    assert document["step4_decision_record"]["expectation_met"] is False


def _rows() -> list[dict[str, Any]]:
    return json.loads(json.dumps(doc()["runs"]))


def test_adequate_branch_is_unreachable_from_generated_runs() -> None:
    rows = [r for r in _rows() if r["role"] in {"baseline", "rerun"}]
    assert select_branch(rows)["branch"] == "missing_audit"


def test_branch_order_with_synthetic_independent_rows() -> None:
    rows = [r for r in _rows() if r["role"] in {"baseline", "rerun"}]
    for row in rows:
        row["evidence"]["fixture_kind"] = "synthetic_independent_for_branch_test"
    assert select_branch(rows)["branch"] == "adequate"
    leaked = json.loads(json.dumps(rows))
    leaked[0]["reason_codes"] = ["family_crosses_split"]
    assert select_branch(leaked)["branch"] == "disagreement_or_leak"
    selective = json.loads(json.dumps(rows))
    selective[0]["reason_codes"] = ["correction_only_sample", "family_crosses_split"]
    picked = select_branch(selective)
    assert picked["branch"] == "missing_audit" and "selective_corrections" in picked["triggers"]
    thin = json.loads(json.dumps(rows))
    thin[0]["coverage"]["critical_status"] = "unavailable"
    assert "thin_critical_slices" in select_branch(thin)["triggers"]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r.update(expectation_met=False),
        lambda r: r["claims"].update(alerts_emitted=1),
        lambda r: (
            r.update(verdict="invalid")
            or r["claims"].update(customer_accuracy_alert_permitted=True)
        ),
        lambda r: r["release_check"].update(release_permitted=True),
        lambda r: r.update(status="failed"),
    ],
)
def test_unsafe_effects_precede_the_table(mutate: Any) -> None:
    rows = _rows()
    for row in rows:
        row["evidence"]["fixture_kind"] = "synthetic_independent_for_branch_test"
    mutate(rows[3])
    assert select_branch(rows)["branch"] == "unsafe_effect_hold"


def test_writer_refuses_jsonl_and_non_json_paths(tmp_path: Path) -> None:
    for name in ("inventory.jsonl", "inventory.txt", "inventory"):
        with pytest.raises(ValueError):
            write_document({"x": 1}, tmp_path / name)
    assert list(tmp_path.iterdir()) == []


def test_writer_replaces_the_whole_file_and_never_appends(tmp_path: Path) -> None:
    target = tmp_path / "inventory.json"
    target.write_text('{"previous": "' + "x" * 4096 + '"}\n')
    write_document({"current": 1}, target)
    write_document({"current": 1}, target)
    assert json.loads(target.read_text()) == {"current": 1}
    # Readable by any checkout user (a temp file would otherwise stay owner-only, 0600).
    assert target.stat().st_mode & 0o777 == 0o644
    assert [p.name for p in tmp_path.iterdir()] == ["inventory.json"]
