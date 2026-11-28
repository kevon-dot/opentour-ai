"""OPE-526 steps 1-4: run inventory, grouping, branch choice and decision record.

Fixture kind: generated. Measurement (SHA, dirty flag, clock) is injected into the document
builder; ``git_measurement`` itself is tested against throwaway git repositories in ``tmp_path``.
A generated run can never select the "adequate" branch or satisfy a real-population gate.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import fields
from pathlib import Path
from typing import Any

import ope526_run_inventory
import pytest
from ope526_run_inventory import (
    POPULATIONS,
    DirtyWorkingTreeError,
    build_document,
    git_measurement,
    require_clean_tree,
    verify_committed,
    write_document,
)
from opentour_training.evaluation.benchmark_audit.audit import audit
from opentour_training.evaluation.benchmark_audit.model import FamilyRecord, Verdict
from opentour_training.evaluation.benchmark_audit.run_inventory import (
    EVIDENCE_LABEL,
    GENERATED_KIND,
    ROW2_CODES,
    Measurement,
    RunSpec,
    inventory_row,
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
        "verdict_allows_evaluation", "release_check",
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
    assert summary["verdict_not_evaluation_allowed"] == sum(
        1 for r in rows if r["verdict"] != "evaluation_allowed"
    )
    assert summary["release_held_or_refused"] == len(rows)
    assert summary["matching_hand_checked_verdict"] == len(rows)
    assert summary["release_permitted"] == 0


def test_every_hand_checked_population_meets_its_expected_verdict() -> None:
    rows = {r["population"]: r for r in doc()["runs"]}
    assert rows["baseline"]["verdict"] == Verdict.EVALUATION_ALLOWED.value
    assert rows["mutation_b_no_critical_verification"]["coverage"]["critical"] == "0/4"
    assert rows["mutation_c_cross_split_sibling"]["verdict"] == Verdict.INVALID.value
    assert rows["mutation_d_post_freeze_relabel"]["verdict"] == Verdict.STALE.value
    assert all(r["expectation_met"] for r in rows.values())
    for spec, _ in POPULATIONS:
        assert rows[spec.name]["verdict"] == spec.expected_verdict, spec.name
        allowed = spec.expected_verdict == Verdict.EVALUATION_ALLOWED
        assert rows[spec.name]["verdict_allows_evaluation"] is allowed
    not_allowed = [r for r in rows.values() if not r["verdict_allows_evaluation"]]
    assert all(r["release_check"]["outcome"] == "refused" for r in not_allowed)
    allowed_rows = [r for r in rows.values() if r["verdict_allows_evaluation"]]
    assert all(r["release_check"]["outcome"] == "held" for r in allowed_rows)
    assert all(r["claims"]["alerts_emitted"] == 0 for r in rows.values())


def test_release_check_rows_recompute_every_binding_and_state_the_unsigned_basis() -> None:
    for row in doc()["runs"]:
        check = row["release_check"]
        assert check["ledger_signed"] is False and check["issuer_authenticated"] is False
        if check["outcome"] == "held":
            recomputed = check["decision_basis"]["bindings_recomputed_by_worker"]
            assert len(recomputed) == 6
            assert check["decision_basis"]["bindings_caller_asserted"] == []


def test_declared_observed_origin_is_flagged_against_the_generated_fixture_kind() -> None:
    rows = {r["population"]: r for r in doc()["runs"]}
    for name, row in rows.items():
        observed = row["evidence"]["evidence_origin_declared"] == "observed"
        assert row["evidence"]["declared_origin_contradicts_fixture_kind"] is observed, name
    standin = rows["real_sample_standin"]
    assert standin["evidence"]["declared_origin_contradicts_fixture_kind"] is True
    assert standin["verdict_allows_evaluation"] is True
    holds = standin["release_check"]["holds"]
    assert "generated_fixture_evidence_only" in holds
    assert "evidence_origin_contradicts_worker_record" in holds
    assert standin["release_check"]["release_permitted"] is False


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
        r["run_id"] for r in document["runs"] if not r["verdict_allows_evaluation"]
    }
    assert {b["item"] for b in document["blocked"]} == {"signed audit receipts"}
    gap = document["release_worker"]
    assert gap["status"] == "gap"
    assert "comparison_packet" in gap["finding"]
    assert [c["issue"] for c in gap["existing_receipt_consumers"]] == ["OPE-421"]


def _identifying_strings(record: FamilyRecord) -> set[str]:
    """Every string a family record carries that could identify a family, label or person."""
    found = set()
    for f in fields(FamilyRecord):
        value = getattr(record, f.name)
        if isinstance(value, str) and f.name not in {"split", "stratum", "outcome_kind"}:
            found.add(value)
    return found - {"generated-slice-routine", "generated-slice-critical"}


def test_output_is_redacted_for_every_population() -> None:
    text = json.dumps(doc())
    checked = 0
    for spec, build in POPULATIONS:
        for record in build().records:
            for value in _identifying_strings(record):
                assert value not in text, (spec.name, value)
                checked += 1
    assert checked > 1000  # real-sample stand-ins included, not just the 15 baseline families


def test_unkeyed_digests_are_labelled_pseudonymised_not_redacted() -> None:
    document = doc()
    assert document["redaction"].startswith(
        "pseudonymised_with_unkeyed_digests_dictionary_attackable"
    )
    assert {"verifier.adjudicator_digests", "sampling_frame.frame_digest"} <= set(
        document["unkeyed_digest_fields"]
    )


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
    selective[0]["reason_codes"] = ["correction_only_sample"]
    picked = select_branch(selective)
    assert picked["branch"] == "missing_audit" and "selective_corrections" in picked["triggers"]
    thin = json.loads(json.dumps(rows))
    thin[0]["coverage"]["critical_status"] = "unavailable"
    assert "thin_critical_slices" in select_branch(thin)["triggers"]


@pytest.mark.parametrize("code", sorted(ROW2_CODES))
def test_invalid_evidence_in_a_baseline_outranks_missing_audit(code: str) -> None:
    """Step 2 prioritizes invalid evidence: row 2 is chosen even when row 1 also applies."""
    rows = [r for r in _rows() if r["role"] in {"baseline", "rerun"}]
    assert select_branch(rows)["branch"] == "missing_audit"  # generated: no independent audit
    rows[0]["reason_codes"] = ["correction_only_sample", code]
    rows[0]["coverage"]["critical_status"] = "unavailable"
    picked = select_branch(rows)
    assert picked["branch"] == "disagreement_or_leak"
    assert picked["triggers"] == {rows[0]["run_id"]: [code]}


def test_row2_codes_in_deliberate_mutations_do_not_quarantine_the_baseline() -> None:
    document = doc()
    assert document["step2_decision"]["branch"] == "missing_audit"
    assert {f["codes"][0] for f in document["injected_findings"]} >= {"family_crosses_split"}


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


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=generated", "-c", "user.email=generated@example.invalid",
         "-c", "commit.gpgsign=false", "-c", "safe.directory=*", *args],
        cwd=repo, check=True, capture_output=True, text=True,
    ).stdout  # fmt: skip


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "tracked.txt").write_text("generated\n")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-q", "-m", "generated")
    return repo


def test_git_measurement_reports_the_head_sha_and_a_clean_tree(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    measured = git_measurement(repo, clock)
    assert measured.git_sha == _git(repo, "rev-parse", "HEAD").strip()
    assert re.fullmatch(r"[0-9a-f]{40}", measured.git_sha)
    assert measured.dirty is False and measured.timestamp == clock()


@pytest.mark.parametrize("change", ["modified", "untracked", "staged"])
def test_git_measurement_reports_a_dirty_tree(tmp_path: Path, change: str) -> None:
    repo = _repo(tmp_path)
    if change == "modified":
        (repo / "tracked.txt").write_text("changed\n")
    elif change == "untracked":
        (repo / "new.txt").write_text("new\n")
    else:
        (repo / "staged.txt").write_text("staged\n")
        _git(repo, "add", "staged.txt")
    assert git_measurement(repo, clock).dirty is True


def test_claim_provenance_fields_are_copied_when_the_receipt_reports_them() -> None:
    """PR #219 adds these claim fields; the row carries them as reported, never invents them."""
    spec, build = POPULATIONS[0]
    inputs = build()
    receipt = audit(inputs)
    bare = json.loads(json.dumps(receipt))
    for key in ("caller_asserted_dependencies", "customer_accuracy_alert_blocked_by"):
        bare["claims"].pop(key, None)
    plain = inventory_row(spec, inputs, bare, MEASURED, elapsed_ms=0.0)
    assert "caller_asserted_dependencies" not in plain["claims"]
    assert "customer_accuracy_alert_blocked_by" not in plain["claims"]
    reported = json.loads(json.dumps(bare))
    reported["claims"]["caller_asserted_dependencies"] = ["exposed_families"]
    reported["claims"]["customer_accuracy_alert_blocked_by"] = ["evidence_origin_not_observed"]
    row = inventory_row(spec, inputs, reported, MEASURED, elapsed_ms=0.0)
    assert row["claims"]["caller_asserted_dependencies"] == ["exposed_families"]
    assert row["claims"]["customer_accuracy_alert_blocked_by"] == ["evidence_origin_not_observed"]
    assert isinstance(spec, RunSpec)


def _dirty(repo: Path, change: str) -> str:
    if change == "modified":
        (repo / "tracked.txt").write_text("changed\n")
        return "tracked.txt"
    if change == "untracked":
        (repo / "new.txt").write_text("new\n")
        return "new.txt"
    (repo / "staged.txt").write_text("staged\n")
    _git(repo, "add", "staged.txt")
    return "staged.txt"


@pytest.mark.parametrize("change", ["modified", "untracked", "staged"])
def test_require_clean_tree_refuses_any_uncommitted_path(tmp_path: Path, change: str) -> None:
    repo = _repo(tmp_path)
    require_clean_tree(repo)  # a clean tree passes
    path = _dirty(repo, change)
    with pytest.raises(DirtyWorkingTreeError) as raised:
        require_clean_tree(repo)
    assert raised.value.code == "working_tree_dirty"
    assert any(line.endswith(path) for line in raised.value.paths)


@pytest.mark.parametrize("change", ["modified", "untracked", "staged"])
def test_generator_refuses_a_dirty_tree_and_writes_nothing(
    tmp_path: Path, change: str, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    repo = _repo(tmp_path)
    _dirty(repo, change)
    monkeypatch.setattr(ope526_run_inventory, "ROOT", repo)
    output = tmp_path / "inventory.json"
    assert ope526_run_inventory.main(["--output", str(output)]) == 1
    assert not output.exists()
    assert json.loads(capsys.readouterr().out)["error"] == "working_tree_dirty"


def _committed_inventory(repo: Path, measured: Measurement) -> Path:
    target = repo / "fixtures" / "inventory.json"
    target.parent.mkdir(exist_ok=True)
    write_document(build_document(measured, clock), target)
    _git(repo, "add", "fixtures/inventory.json")
    _git(repo, "commit", "-q", "-m", "generated inventory")
    return target


def test_verify_committed_reads_the_committed_blob_and_refuses_a_dirty_tree(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    """A throwaway repository holding a freshly built inventory; its measured SHA is absent."""
    repo = _repo(tmp_path)
    target = _committed_inventory(repo, MEASURED)
    clean = verify_committed(repo, target)
    assert "regenerated_inventory_differs_from_committed" not in clean
    assert "measured_sha_not_ancestor_of_head" in clean  # the measured commit is not in this repo
    committed_text = target.read_text()
    edited = json.loads(committed_text)
    edited["runs"][0]["verdict"] = "claim_limited_to_named_population"
    target.write_text(json.dumps(edited))
    # L-a2: the regeneration runs working-tree code, so any uncommitted change is refused
    # outright instead of being checked against HEAD's blob.
    with pytest.raises(DirtyWorkingTreeError) as raised:
        verify_committed(repo, target)
    assert any(line.endswith("fixtures/inventory.json") for line in raised.value.paths)
    monkeypatch.setattr(ope526_run_inventory, "ROOT", repo)
    assert ope526_run_inventory.main(["--verify-committed", "--output", str(target)]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "working_tree_dirty"
    _git(repo, "commit", "-q", "-am", "hand-edited inventory")
    assert "regenerated_inventory_differs_from_committed" in verify_committed(repo, target)
    # A committed edit reverted only in the working tree is still refused, not passed.
    target.write_text(committed_text)
    with pytest.raises(DirtyWorkingTreeError):
        verify_committed(repo, target)


def test_verify_committed_requires_the_measured_sha_to_be_the_writing_commits_parent(
    tmp_path: Path,
) -> None:
    """V3: an ancestor of HEAD that is not the parent of the writing commit is refused."""
    repo = _repo(tmp_path)
    measured_sha = _git(repo, "rev-parse", "HEAD").strip()
    measured = Measurement("2026-01-01T00:00:00Z", measured_sha, False)
    bound = _committed_inventory(repo, measured)
    assert verify_committed(repo, bound) == []

    (tmp_path / "second").mkdir()
    repo2 = _repo(tmp_path / "second")
    measured_sha = _git(repo2, "rev-parse", "HEAD").strip()
    (repo2 / "later.txt").write_text("a commit between the measurement and the inventory\n")
    _git(repo2, "add", "later.txt")
    _git(repo2, "commit", "-q", "-m", "intervening commit")
    measured = Measurement("2026-01-01T00:00:00Z", measured_sha, False)
    unbound = _committed_inventory(repo2, measured)
    assert verify_committed(repo2, unbound) == [
        "measured_sha_not_parent_of_commit_that_wrote_the_file"
    ]
