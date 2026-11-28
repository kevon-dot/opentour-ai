"""OPE-526: the committed run inventory is labelled, bound to the commit it measured, and current.

Fixture kind: generated. The file is evidence measured at ``MEASURED_SHA``, the parent of the
commit that wrote it. Two checks bind it:

* the measured SHA is pinned here in full, so any other 40-hex value fails;
* the inventory is regenerated at this checkout (with the committed measurement injected) and
  must equal the committed file apart from wall-clock timestamps and observed CPU time. A
  hand-edited verdict, a changed receipt digest or drift in any code it depends on fails.

Whenever the generator, the audit or the release check changes output (for example after the
OPE-526 exposure fail-open change, PR #219, lands), regenerate the file from a clean checkout and
update ``MEASURED_SHA`` to the commit it was measured at.

Git ancestry (the measured SHA is an ancestor of HEAD and the parent of the commit that wrote
the file) is not asserted here: CI checks out a single commit with no history, and squash merges
drop the measured commit from ``main``. It is checked by
``ope526_run_inventory.py --verify-committed`` on a full clone instead.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ope526_run_inventory import POPULATIONS, comparable, regenerate_like
from opentour_training.evaluation.benchmark_audit.model import Verdict
from opentour_training.evaluation.benchmark_audit.run_inventory import (
    EVIDENCE_LABEL,
    GENERATED_KIND,
)

COMMITTED = Path(__file__).parent / "fixtures" / "ope526-run-inventory.json"
MEASURED_SHA = "df422e4bff152c4172d6ee465f2b8f60358fdb09"


def committed() -> dict[str, Any]:
    return json.loads(COMMITTED.read_text())


def test_committed_inventory_is_bound_to_the_pinned_measured_commit() -> None:
    document = committed()
    assert document["measured_git_sha"] == MEASURED_SHA
    assert document["dirty"] is False
    assert document["step4_decision_record"]["change_revision"] == MEASURED_SHA
    assert document["step4_decision_record"]["change_revision_dirty"] is False
    for row in document["runs"]:
        assert row["measured_git_sha"] == MEASURED_SHA
        assert row["dirty"] is False
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", row["timestamp"])


def test_committed_inventory_matches_a_regeneration_at_this_checkout() -> None:
    document = committed()
    assert comparable(regenerate_like(document)) == comparable(document)


def test_committed_verdicts_match_the_hand_checked_expectations() -> None:
    document = committed()
    runs = document["runs"]
    assert [r["population"] for r in runs] == [s.name for s, _ in POPULATIONS]
    for row, (spec, _) in zip(runs, POPULATIONS, strict=True):
        assert row["verdict"] == spec.expected_verdict, spec.name
        assert row["expectation_met"] is True
        assert row["verdict_allows_evaluation"] is (
            spec.expected_verdict == Verdict.EVALUATION_ALLOWED
        )
        assert row["release_check"]["release_permitted"] is False


def test_committed_inventory_is_labelled_and_complete() -> None:
    document = committed()
    assert document["evidence_label"] == EVIDENCE_LABEL
    assert document["fixture_kind"] == GENERATED_KIND
    for row in document["runs"]:
        assert row["run_id"].startswith(f"ope526-fixture-{row['population']}-")
        assert row["evidence"]["real_population_gate_satisfied"] is False
    assert document["summary"]["failed"] == 0
    assert document["summary"]["release_permitted"] == 0
    assert document["step2_decision"]["branch"] == "missing_audit"
    assert document["step4_decision_record"]["decision"] == "hold_for_specified_evidence"
    assert not list(COMMITTED.parent.glob("*.jsonl"))
