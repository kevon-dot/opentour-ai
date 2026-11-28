"""Regenerate the OPE-526 run inventory and decision record from the generated fixtures.

Usage (from the repository root)::

    training/.venv/bin/python training/tests/evaluation/ope526_run_inventory.py \
        --output training/tests/evaluation/fixtures/ope526-run-inventory.json

Fixture kind: generated sentinel. Every eligible frozen run here is an ``audit()`` over a
hand-checked population built by ``audit_fixtures``. The output is generated fixture evidence:
it cannot satisfy a real-population, provider or customer-evidence gate.

The output path must end in ``.json`` (never ``.jsonl``). It is written whole to a temporary
file and moved into place; it is never opened in append mode. Every row carries its run id,
timestamp, measured git SHA and dirty flag. The SHA and flag describe the checkout measured; on
the branch that adds the output, that is the parent of the commit that adds it. A squash merge
drops both commits from ``main`` (see ``test_benchmark_run_inventory_artifact``).

Both generation and ``--verify-committed`` refuse a checkout whose running code may not be
HEAD's, with a ``CheckoutNotMeasurableError`` naming the reason:

* ``working_tree_dirty`` (``DirtyWorkingTreeError``): any modified, staged or untracked path;
* ``index_hides_working_tree_changes`` (``HiddenIndexChangeError``): any index entry flagged
  skip-worktree or assume-unchanged, because ``git status`` does not report an edit to it;
* ``imported_code_outside_checkout`` (``ForeignCodeError``): any imported project module
  (``opentour_*``, ``audit_fixtures``, this script) whose file does not resolve, through
  symlinks, to a path tracked in the checkout. This refuses a virtual environment installed
  from another checkout and an ignored file shadowing a tracked module.

Not covered: a hand-crafted bytecode cache in ``__pycache__`` whose recorded source size and
mtime match the tracked ``.py`` file is loaded in its place and is not detected here.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from audit_fixtures import (
    BINDING_MATERIALS,
    CONSUMED,
    OBSERVED_SNAPSHOT,
    baseline_records,
    commit,
    family,
    inputs_for,
    real_sample_records,
)
from opentour_training.evaluation.benchmark_audit.audit import audit
from opentour_training.evaluation.benchmark_audit.model import (
    AuditInputs,
    PopulationKind,
    PreprocessingRecord,
    SamplingDesign,
    SelectionUse,
    Verdict,
)
from opentour_training.evaluation.benchmark_audit.release_check import check_receipt_for_release
from opentour_training.evaluation.benchmark_audit.run_inventory import (
    EVIDENCE_LABEL,
    EVIDENCE_NOTICE,
    GENERATED_KIND,
    INVENTORY_SCHEMA,
    MISSING_FOR_EVERY_RUN,
    PSEUDONYMISATION,
    UNKEYED_DIGEST_FIELDS,
    Measurement,
    RunSpec,
    decision_record,
    group_runs,
    injected_findings,
    inventory_row,
    select_branch,
)
from opentour_training.evaluation.benchmark_audit.worker_store import WorkerAuditStore

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OUTPUT = ROOT / "training/tests/evaluation/fixtures/ope526-run-inventory.json"
V = Verdict


def _unverified(records: Any, predicate: Callable[[Any], bool]) -> tuple[Any, ...]:
    return tuple(
        replace(
            r,
            adjudicator_id=None,
            adjudicator_label_digest=None,
            independent_source=None,
            independent_label_digest=None,
        )
        if predicate(r)
        else r
        for r in records
    )


def _real(routine: int = 60, critical: int = 60, **prereg: object) -> AuditInputs:
    records, sizes = real_sample_records(routine, critical)
    return inputs_for(
        records,
        prereg_overrides={
            "population_kind": PopulationKind.REAL_SAMPLE,
            "evidence_origin": "observed",
            "population_frame": "generated-frame:stratified-real-sample-standin",
            "sampling_design": SamplingDesign.STRATIFIED_RANDOM,
            "sample_sizes": sizes,
            "tolerable_label_error": 0.02,
            **prereg,
        },
        candidate_snapshot=OBSERVED_SNAPSHOT,
    )


def _cross_split() -> AuditInputs:
    sibling = family(
        "generated-d09", "routine", "correct", split="development",
        root="generated-root-generated-r05",
    )  # fmt: skip
    return inputs_for((*baseline_records(), sibling))


def _relabeled() -> AuditInputs:
    frozen = baseline_records()
    relabeled = tuple(
        replace(r, final_label_digest=commit(r.family_id, "generated-changed-action"))
        if r.family_id == "generated-r03"
        else r
        for r in frozen
    )
    return inputs_for(relabeled, frozen_records=frozen)


def _overturned() -> AuditInputs:
    wrong = commit("generated-r04", "generated-wrong-action")
    right = commit("generated-r04", "generated-action-generated-r04")
    return inputs_for(
        tuple(
            replace(
                r,
                final_label_digest=wrong,
                adjudicator_label_digest=wrong,
                independent_label_digest=right,
            )
            if r.family_id == "generated-r04"
            else r
            for r in baseline_records()
        )  # fmt: skip
    )


def _unresolved() -> AuditInputs:
    records = _unverified(
        baseline_records(), lambda r: r.family_id in {"generated-r07", "generated-r08"}
    )
    return inputs_for(
        tuple(
            replace(r, unresolved=True) if r.family_id in {"generated-r07", "generated-r08"} else r
            for r in records
        )
    )


def _corrections_only(design: SamplingDesign | None) -> AuditInputs:
    kept = tuple(
        r for r in baseline_records() if r.split == "development" or r.outcome_kind == "correction"
    )
    overrides = {"sampling_design": design} if design else None
    return inputs_for(kept, prereg_overrides=overrides)


# (spec, builder). Expected verdicts are the hand-checked dispositions in the OPE-526 spec.
POPULATIONS: tuple[tuple[RunSpec, Callable[[], AuditInputs]], ...] = (
    (RunSpec("baseline", "baseline", V.EVALUATION_ALLOWED), lambda: inputs_for(baseline_records())),
    (
        RunSpec("baseline_rescore", "rerun", V.EVALUATION_ALLOWED, rerun_of="baseline"),
        lambda: inputs_for(baseline_records()),
    ),
    (
        RunSpec("mutation_a_corrections_only", "mutation", V.CLAIM_LIMITED),
        lambda: _corrections_only(SamplingDesign.CORRECTION_ONLY),
    ),
    (
        RunSpec("mutation_a_selective_under_full_design", "mutation", V.CLAIM_LIMITED),
        lambda: _corrections_only(None),
    ),
    (
        RunSpec("mutation_b_no_critical_verification", "mutation", V.CLAIM_LIMITED),
        lambda: inputs_for(_unverified(baseline_records(), lambda r: r.stratum == "critical")),
    ),
    (RunSpec("mutation_c_cross_split_sibling", "mutation", V.INVALID), _cross_split),
    (
        RunSpec("mutation_c_resealed", "rerun", V.EVALUATION_ALLOWED, rerun_of="baseline"),
        lambda: inputs_for(baseline_records()),
    ),
    (RunSpec("mutation_d_post_freeze_relabel", "mutation", V.STALE), _relabeled),
    (
        RunSpec("pack_policy_only_starter", "pack", V.STARTER_ONLY),
        lambda: inputs_for((), prereg_overrides={"sampling_design": SamplingDesign.POLICY_ONLY}),
    ),
    (RunSpec("pack_two_reviewers_overturned", "pack", V.INVALID), _overturned),
    (RunSpec("pack_unresolved_ambiguity", "pack", V.INSUFFICIENT_EVIDENCE), _unresolved),
    (
        RunSpec("pack_future_only_field", "pack", V.INVALID),
        lambda: inputs_for(baseline_records(), consumed_features=(*CONSUMED, "refund_settled_at")),
    ),
    (
        RunSpec("adverse_empty_audit", "adverse", V.INSUFFICIENT_EVIDENCE),
        lambda: inputs_for(_unverified(baseline_records(), lambda r: True)),
    ),
    (
        RunSpec("adverse_protected_selection", "adverse", V.INVALID),
        lambda: inputs_for(
            baseline_records(), selection_log=(SelectionUse("threshold", "protected"),)
        ),
    ),
    (
        RunSpec("adverse_preprocessing_fit_on_protected", "adverse", V.INVALID),
        lambda: inputs_for(
            baseline_records(),
            preprocessing=PreprocessingRecord(
                "generated-prep-r1", "protected", "generated-prep-r1"
            ),
        ),
    ),
    (
        RunSpec("adverse_unlogged_disclosure", "adverse", V.INVALID),
        lambda: inputs_for(baseline_records(), unlogged_disclosures=1),
    ),
    (
        RunSpec("adverse_preregistered_after_reveal", "adverse", V.INVALID),
        lambda: inputs_for(baseline_records(), candidate_outputs_revealed_day=100),
    ),
    (
        RunSpec("adverse_superseded", "adverse", V.STALE),
        lambda: inputs_for(baseline_records(), superseded_by="generated-successor"),
    ),
    (RunSpec("real_sample_standin", "baseline", V.EVALUATION_ALLOWED), _real),
    (
        RunSpec("real_sample_standin_small", "mutation", V.INSUFFICIENT_EVIDENCE),
        lambda: _real(20, 20, tolerable_label_error=0.0),
    ),
)


def worker_release_check(name: str, inputs: AuditInputs, receipt: dict[str, Any]) -> Any:
    """Issue the receipt into a fresh worker store and run the release check against it.

    The store holds the population's current records and the material of every opaque binding,
    so the check recomputes all six bindings itself. The store is recorded as generated.
    """
    benchmark = f"generated-benchmark-{name}"
    with tempfile.TemporaryDirectory(prefix="ope526-worker-store-") as scratch:
        directory = Path(scratch) / "store"
        store = WorkerAuditStore.create(directory)
        store.set_benchmark(
            f"benchmark:{name}", benchmark, inputs.records,
            evidence_kind="generated_sentinel", materials=BINDING_MATERIALS,
        )  # fmt: skip
        store.issue(f"issue:{name}", receipt, benchmark_id=benchmark)
        return check_receipt_for_release(receipt["receipt_id"], store_directory=directory)


def _git_stdout(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout


def dirty_paths(root: Path) -> tuple[str, ...]:
    """Modified, staged and untracked (not ignored) paths, as ``git status --porcelain``."""
    status = _git_stdout(root, "status", "--porcelain", "--untracked-files=all")
    return tuple(line for line in status.splitlines() if line.strip())


class CheckoutNotMeasurableError(RuntimeError):
    """The code that would run may not be HEAD's, so nothing is measured or verified."""

    code = "checkout_not_measurable"

    def __init__(self, paths: tuple[str, ...]) -> None:
        super().__init__(f"{self.code}: {len(paths)} path(s)")
        self.paths = paths


class DirtyWorkingTreeError(CheckoutNotMeasurableError):
    """The checkout has uncommitted changes, so the code that would run is not HEAD's."""

    code = "working_tree_dirty"


class HiddenIndexChangeError(CheckoutNotMeasurableError):
    """Index entries flagged skip-worktree or assume-unchanged hide edits from ``git status``."""

    code = "index_hides_working_tree_changes"


class ForeignCodeError(CheckoutNotMeasurableError):
    """An imported project module was not loaded from a file tracked in this checkout."""

    code = "imported_code_outside_checkout"


def hidden_index_paths(root: Path) -> tuple[str, ...]:
    """Index entries tagged skip-worktree (``S``, ``s``) or assume-unchanged (lowercase).

    Tags are those of ``git ls-files -v``; each entry is returned as ``"<tag> <path>"``.
    """
    listing = _git_stdout(root, "ls-files", "-v", "-z")
    entries = (entry for entry in listing.split("\0") if entry)
    return tuple(e for e in entries if e[0].islower() or e[0] == "S")


def require_clean_tree(root: Path) -> None:
    """Raise unless the working tree matches HEAD exactly and no index flag hides a change."""
    paths = dirty_paths(root)
    if paths:
        raise DirtyWorkingTreeError(paths)
    hidden = hidden_index_paths(root)
    if hidden:
        raise HiddenIndexChangeError(hidden)


PROJECT_SCRIPT_MODULES = frozenset({"audit_fixtures", "ope526_run_inventory"})


def imported_project_files() -> dict[str, str | None]:
    """Each imported project module and the file it was loaded from (``None`` if it has none).

    Project modules are the ``opentour_*`` packages, ``audit_fixtures`` and this script. A
    module without a file (a namespace package) runs no code of its own and is reported as
    ``None``.
    """
    found: dict[str, str | None] = {__name__: __file__}  # this script, also when run as __main__
    for name, module in list(sys.modules.items()):
        top = name.partition(".")[0]
        if top.startswith("opentour_") or top in PROJECT_SCRIPT_MODULES:
            file = getattr(module, "__file__", None)
            found[name] = file if isinstance(file, str) else None
    return found


def foreign_project_modules(
    root: Path, files: Mapping[str, str | None] | None = None
) -> tuple[str, ...]:
    """Project modules whose file's realpath is not a path tracked in the checkout at ``root``."""
    files = imported_project_files() if files is None else files
    top = Path(os.path.realpath(root))
    tracked = set(_git_stdout(root, "ls-files", "-z").split("\0"))
    foreign = []
    for name, file in sorted(files.items()):
        if file is None:
            continue
        try:
            relative = Path(os.path.realpath(file)).relative_to(top).as_posix()
        except ValueError:
            relative = None
        if relative not in tracked:
            foreign.append(f"{name}: {file}")
    return tuple(foreign)


def require_checkout_code(root: Path) -> None:
    """Raise ``ForeignCodeError`` unless every imported project module is tracked at ``root``."""
    foreign = foreign_project_modules(root)
    if foreign:
        raise ForeignCodeError(foreign)


def require_measurable_checkout(root: Path) -> None:
    """Clean tree, no hiding index flags, and the running project code is this checkout's."""
    require_clean_tree(root)
    require_checkout_code(root)


def git_measurement(root: Path, clock: Callable[[], str]) -> Measurement:
    return Measurement(
        timestamp=clock(),
        git_sha=_git_stdout(root, "rev-parse", "HEAD").strip(),
        dirty=bool(dirty_paths(root)),
    )


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def build_document(
    measured: Measurement,
    clock: Callable[[], str],
    *,
    populations: tuple[tuple[RunSpec, Callable[[], AuditInputs]], ...] = POPULATIONS,
    auditor: Callable[[AuditInputs], dict[str, Any]] = audit,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for spec, build in populations:
        inputs = build()
        started = time.perf_counter()
        try:
            receipt: dict[str, Any] | None = auditor(inputs)
            failure = None
        except Exception as exc:  # a failed run is inventoried, never dropped
            receipt, failure = None, type(exc).__name__
        elapsed = (time.perf_counter() - started) * 1000
        check = None
        if receipt is not None:
            check = worker_release_check(spec.name, inputs, receipt).as_json()
        row_measure = Measurement(clock(), measured.git_sha, measured.dirty)
        rows.append(
            inventory_row(
                spec, inputs, receipt, row_measure, elapsed_ms=elapsed, failure=failure,
                release_check=check,
            )
        )  # fmt: skip
    branch = select_branch(rows)
    completed = [r for r in rows if r["status"] == "completed"]
    return {
        "schema_version": INVENTORY_SCHEMA,
        "issue": "OPE-526",
        "evidence_label": EVIDENCE_LABEL,
        "fixture_kind": GENERATED_KIND,
        "evidence_notice": EVIDENCE_NOTICE,
        "generator": "training/tests/evaluation/ope526_run_inventory.py",
        "generated_at": measured.timestamp,
        "measured_git_sha": measured.git_sha,
        "dirty": measured.dirty,
        "redaction": PSEUDONYMISATION,
        "unkeyed_digest_fields": dict(UNKEYED_DIGEST_FIELDS),
        "summary": {
            "attempted": len(rows),
            "completed": len(completed),
            "failed": len(rows) - len(completed),
            "verdict_not_evaluation_allowed": sum(
                1 for r in rows if not r["verdict_allows_evaluation"]
            ),
            "release_held_or_refused": sum(
                1 for r in rows if (r.get("release_check") or {}).get("outcome") != "admitted"
            ),
            "evaluation_allowed": sum(
                1 for r in completed if r["verdict"] == V.EVALUATION_ALLOWED.value
            ),
            "matching_hand_checked_verdict": sum(1 for r in rows if r["expectation_met"]),
            "release_permitted": sum(
                1 for r in rows if (r.get("release_check") or {}).get("release_permitted")
            ),
        },
        "runs": rows,
        "groups": group_runs(rows),
        "step2_decision": branch,
        "injected_findings": injected_findings(rows),
        "step4_decision_record": decision_record(rows, branch, measured),
        "missing_for_every_run": list(MISSING_FOR_EVERY_RUN),
        "external_inputs": [
            {
                "input": "customer-approved population frame, sampling design and tolerances",
                "owner": "named customer evidence owner",
            },
            {
                "input": "blinded second qualified adjudicator or independent authoritative record",
                "owner": "customer-designated adjudicator",
            },
            {"input": "verified-candidate snapshot receipt", "owner": "OPE-416"},
            {"input": "independent result verification", "owner": "OPE-528"},
            {"input": "receipt signature verifier", "owner": "OPE-566"},
            {"input": "receipt issuer keys and custody", "owner": "OPE-582"},
            {
                "input": "one-family query parity in private Slack and the worker UI",
                "owner": "Codex lane (Slack and worker-UI query surface)",
            },
        ],
        "local_engineering_checks": [
            "audit verdict regressions over the hand-checked packs and mutations A-D",
            "rescore replay of identical inputs",
            "worker-side release check of the receipt, read from a worker store it opens itself "
            "(refuses non-allowed, stale and superseded receipts, and naive store copies or "
            "unrechained log edits; a self-consistent store built or rewritten with the "
            "worker's filesystem rights passes with only the unsigned holds)",
            "exposure guard fail-open fixes (PR #219, parallel lane)",
            "regeneration of this inventory",
        ],
        "release_worker": {
            "status": "gap",
            "finding": (
                "No release worker on main consumes the OPE-526 receipt. release_gate, "
                "release_authority and release_outbox (services/cell-runtime) gate a human "
                "release decision on repair runs, take no audit receipt and cannot import the "
                "training package. candidate_staging (OPE-545) stages, does not release, and "
                "reads a caller-supplied audit_verdict string. comparison_packet (OPE-421, "
                "services/cell-runtime) does consume opentour.benchmark-audit-receipt.v1, but "
                "checks only that its digest and id are self-consistent and reads verdict, "
                "status and bindings from the supplied document; it does not call this check "
                "and cannot import the training package."
            ),
            "existing_receipt_consumers": [
                {
                    "module": "services/cell-runtime/src/opentour_cell/pilot/comparison_packet.py",
                    "issue": "OPE-421",
                    "checks": "receipt_digest_and_id_self_consistency_only",
                    "uses_worker_release_check": False,
                }
            ],
            "interface_provided": [
                "opentour_training.evaluation.benchmark_audit.release_check."
                "check_receipt_for_release",
                "opentour_training.evaluation.benchmark_audit.worker_store.WorkerAuditStore",
            ],
        },
        "blocked": [
            {
                "item": "signed audit receipts",
                "blocked_on": ["OPE-566 verifier", "OPE-582 keys"],
                "built": False,
            }
        ],
    }


VOLATILE_ROW_KEYS = ("timestamp", "observed_audit_cpu_ms")


def comparable(document: dict[str, Any]) -> dict[str, Any]:
    """The document without wall-clock timestamps and CPU time; everything else must match."""
    copy = json.loads(json.dumps(document))
    copy.pop("generated_at", None)
    for row in copy.get("runs", []):
        for key in VOLATILE_ROW_KEYS:
            row.pop(key, None)
    return copy


def regenerate_like(document: dict[str, Any]) -> dict[str, Any]:
    """Rebuild the document at this checkout with the committed document's own measurement."""
    measured = Measurement(
        document["generated_at"], document["measured_git_sha"], document["dirty"]
    )
    return build_document(measured, utc_now)


def verify_committed(root: Path, path: Path) -> list[str]:
    """Git-backed checks of a committed inventory; needs full history (not a shallow clone).

    Before anything else it raises ``CheckoutNotMeasurableError`` (see the module docstring)
    unless the code that runs is HEAD's: a clean tree, no skip-worktree or assume-unchanged
    entry, and every imported project module loaded from a file tracked under ``root``.
    Reads the blob committed at HEAD (``git show HEAD:<path>``). The measured SHA must be an
    ancestor of HEAD and the parent of the last commit that changed the file, and regenerating
    at HEAD must reproduce the committed blob apart from timestamps and CPU ms.

    The two git checks hold only on the branch that wrote the file. After a squash merge
    neither the measured commit nor the writing commit is on ``main``, so both fail there.
    """

    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)

    require_measurable_checkout(root)
    relative = path.relative_to(root).as_posix()
    problems = []
    blob = git("show", f"HEAD:{relative}")
    if blob.returncode != 0:
        return [*problems, "inventory_not_committed_at_head"]
    document = json.loads(blob.stdout)
    sha = document["measured_git_sha"]
    if git("merge-base", "--is-ancestor", sha, "HEAD").returncode != 0:
        problems.append("measured_sha_not_ancestor_of_head")
    last = git("log", "-1", "--format=%H", "--", relative).stdout.strip()
    parent = git("rev-parse", f"{last}^").stdout.strip() if last else ""
    if parent != sha:
        problems.append("measured_sha_not_parent_of_commit_that_wrote_the_file")
    if comparable(regenerate_like(document)) != comparable(document):
        problems.append("regenerated_inventory_differs_from_committed")
    return problems


def write_document(document: dict[str, Any], output: Path) -> None:
    """Write the whole document atomically. Never appends; refuses anything but ``.json``."""
    if output.suffix != ".json":
        raise ValueError("output must be a .json file")
    text = json.dumps(document, indent=2, sort_keys=True, allow_nan=False) + "\n"
    fd, tmp = tempfile.mkstemp(dir=output.parent, prefix=".ope526-inventory-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, output)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--verify-committed",
        action="store_true",
        help="check the committed --output against git history and a regeneration; write nothing",
    )
    args = parser.parse_args(argv)
    try:
        if args.verify_committed:
            problems = verify_committed(ROOT, args.output.resolve())
            print(json.dumps({"output": str(args.output), "problems": problems}, sort_keys=True))
            return 1 if problems else 0
        require_measurable_checkout(ROOT)
    except CheckoutNotMeasurableError as exc:
        print(json.dumps({"error": exc.code, "paths": list(exc.paths)}, sort_keys=True))
        return 1
    measured = git_measurement(ROOT, utc_now)
    document = build_document(measured, utc_now)
    write_document(document, args.output)
    summary = document["summary"]
    print(json.dumps({"output": str(args.output), **summary}, sort_keys=True))
    failed = summary["failed"] or document["step2_decision"]["branch"] == "unsafe_effect_hold"
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
