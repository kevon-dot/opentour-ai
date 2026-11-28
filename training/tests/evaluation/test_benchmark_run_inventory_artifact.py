"""OPE-526: the committed run inventory is labelled and bound to the commit it measured.

Fixture kind: generated. The file is historical evidence bound to its measured SHA, not to
HEAD, so this test checks labels, bindings and completeness rather than recomputed digests.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ope526_run_inventory import POPULATIONS
from opentour_training.evaluation.benchmark_audit.run_inventory import (
    EVIDENCE_LABEL,
    GENERATED_KIND,
)

COMMITTED = Path(__file__).parent / "fixtures" / "ope526-run-inventory.json"


def test_committed_inventory_is_labelled_bound_to_its_measured_commit_and_complete() -> None:
    """The committed file is historical evidence bound to the SHA it measured, not to HEAD."""
    document = json.loads(COMMITTED.read_text())
    assert document["evidence_label"] == EVIDENCE_LABEL
    assert document["fixture_kind"] == GENERATED_KIND
    assert re.fullmatch(r"[0-9a-f]{40}", document["measured_git_sha"])
    assert document["dirty"] is False
    assert [r["population"] for r in document["runs"]] == [s.name for s, _ in POPULATIONS]
    for row in document["runs"]:
        assert row["measured_git_sha"] == document["measured_git_sha"]
        assert row["dirty"] is False
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", row["timestamp"])
        assert row["run_id"].startswith(f"ope526-fixture-{row['population']}-")
        assert row["evidence"]["real_population_gate_satisfied"] is False
    assert document["summary"]["failed"] == 0
    assert document["summary"]["release_permitted"] == 0
    assert document["step2_decision"]["branch"] == "missing_audit"
    assert document["step4_decision_record"]["decision"] == "hold_for_specified_evidence"
    assert not list(COMMITTED.parent.glob("*.jsonl"))
