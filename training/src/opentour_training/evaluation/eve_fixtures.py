"""Generated sentinel EVE fixtures, prepared-export replay adapter and command line.

All content is invented (``GEN-`` / ``ANS-GEN-`` sentinels). Nothing here is Pi School data,
an EVE output, or provider evidence. The CLI labels every result ``generated``.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from opentour_training.evaluation.eve_benchmark import (
    BASELINE_ARM,
    REPAIR_ARM,
    ArmConfig,
    ArmOutput,
    ArmRun,
    BenchmarkError,
    BenchmarkResult,
    EveCase,
    ExclusionRecord,
    FitRow,
    FrozenManifest,
    ReferenceJudgment,
    assess_development,
    freeze_manifest,
    manifest_to_json,
    record_one_disposition,
    score_heldout,
    verify_isolation,
    write_once,
)

RUBRIC_TEXT = (
    "generated-rubric: supported answer with valid citations is correct; abstain only when "
    "evidence is absent; unsupported or miscited answers are serious errors."
)
ARM_CONFIGS = (
    ArmConfig(BASELINE_ARM, "none", "gen-rev-1", "gen-snap-1", "gen-ret-off"),
    ArmConfig(
        REPAIR_ARM, "retrieval_when_evidence_missing", "gen-rev-1", "gen-snap-1", "gen-ret-on"
    ),
)


@dataclass(frozen=True)
class FixtureSet:
    development_cases: tuple[EveCase, ...]
    development_key: tuple[ReferenceJudgment, ...]
    heldout_cases: tuple[EveCase, ...]
    heldout_key: tuple[ReferenceJudgment, ...]
    exclusions: tuple[ExclusionRecord, ...]


def _kind_case(prefix: str, i: int) -> tuple[EveCase, ReferenceJudgment, int]:
    kind = i % 5
    cid = f"{prefix}-{i:03d}"
    case = EveCase(
        cid, f"GEN-FAM-{prefix}-{i // 2:02d}", f"generated question {cid}", (f"EV-{cid}",)
    )
    if kind == 2:
        ref = ReferenceJudgment(cid, True, None, ())
    else:
        ref = ReferenceJudgment(cid, False, f"ANS-GEN-{cid}", (f"EV-{cid}",))
    return case, ref, kind


def build_fixture_set(*, dev_cases: int = 20, heldout_cases: int = 30) -> FixtureSet:
    dev = [_kind_case("DEV", i) for i in range(dev_cases)]
    held = [_kind_case("HOLD", i) for i in range(heldout_cases)]
    held_refs = list(r for _, r, _ in held)
    # one disputed reference (unresolved) and one excluded case, both kept visible
    held_refs[7] = ReferenceJudgment("HOLD-007", False, "ANS-GEN-HOLD-007", ("EV-HOLD-007",), True)
    return FixtureSet(
        tuple(c for c, _, _ in dev),
        tuple(r for _, r, _ in dev),
        tuple(c for c, _, _ in held),
        tuple(held_refs),
        (ExclusionRecord("HOLD-011", "context_not_authorized"),),
    )


def _kinds(cases: Sequence[EveCase]) -> dict[str, int]:
    return {c.case_id: int(c.case_id.split("-")[1]) % 5 for c in cases}


def simulate_arm(cfg: ArmConfig, cases: Sequence[EveCase], manifest: FrozenManifest) -> ArmRun:
    """Deterministic stub systems standing in for EVE. Not a model, not an EVE output."""
    repair = cfg.arm_id == REPAIR_ARM
    outs: list[ArmOutput] = []
    for c in cases:
        kind = int(c.case_id.split("-")[1]) % 5
        ans, cites, abst = f"ANS-GEN-{c.case_id}", (f"EV-{c.case_id}",), False
        if kind == 1 and not repair:  # evidence missing: baseline answers wrongly
            ans = "ANS-GEN-WRONG"
        elif kind == 2:  # evidence absent: both abstain
            ans, cites, abst = None, (), True  # type: ignore[assignment]
        elif kind == 3 and repair and int(c.case_id.split("-")[1]) % 2 == 1:
            cites = ("EV-GEN-OTHER",)  # simple change regresses some cases (miscitation)
        elif kind == 4 and not repair:  # baseline over-refuses
            ans, cites, abst = None, (), True  # type: ignore[assignment]
        outs.append(
            ArmOutput(
                c.case_id,
                ans,
                cites,
                abst,
                cfg.served_revision,
                cfg.source_snapshot_id,
                cfg.retrieval_settings_id,
            )
        )
    return ArmRun(
        cfg.arm_id, manifest.manifest_digest, tuple(c.case_id for c in cases), tuple(outs)
    )


def run_fixture_benchmark(
    *, include_repair: bool = True, heldout_cases: int = 30
) -> BenchmarkResult:
    fx = build_fixture_set(heldout_cases=heldout_cases)
    manifest = freeze_manifest(
        benchmark_id="generated-eve-benchmark-1",
        evidence_tier="generated",
        development_cases=fx.development_cases,
        heldout_cases=fx.heldout_cases,
        heldout_exclusions=fx.exclusions,
        heldout_key=fx.heldout_key,
        development_key=fx.development_key,
        arms=ARM_CONFIGS,
        rubric_version="generated-rubric-1",
        rubric_text=RUBRIC_TEXT,
        alpha=0.1,
        materiality_min_errors=2,
        min_recurring_families=2,
        adjudicator_kind="generated_fixture",
    )
    cfgs = {c.arm_id: c for c in ARM_CONFIGS}
    dev_base = simulate_arm(cfgs[BASELINE_ARM], fx.development_cases, manifest)
    dev_rep = simulate_arm(cfgs[REPAIR_ARM], fx.development_cases, manifest)
    finding = assess_development(
        manifest,
        development_cases=fx.development_cases,
        development_key=fx.development_key,
        baseline=dev_base,
        repair=dev_rep,
    )
    fit_rows: list[FitRow] = []  # baseline arm is not fitted; simple repair is a retrieval switch
    prompts = [c.question for c in fx.heldout_cases]
    iso = verify_isolation(
        manifest, fit_rows=fit_rows, prompt_texts=prompts, heldout_key=fx.heldout_key
    )
    eligible = tuple(c for c in fx.heldout_cases if c.case_id in manifest.heldout_eligible)
    runs = [simulate_arm(cfgs[BASELINE_ARM], eligible, manifest)]
    if include_repair:
        runs.append(simulate_arm(cfgs[REPAIR_ARM], eligible, manifest))
    card = score_heldout(
        manifest,
        heldout_cases=fx.heldout_cases,
        heldout_key=fx.heldout_key,
        fit_rows=fit_rows,
        prompt_texts=prompts,
        isolation=iso,
        runs=runs,
    )
    return BenchmarkResult(manifest, finding, card)


# ---------------------------------------------------------------- prepared-export replay adapter


def load_prepared_export(path: Path, arm_id: str, manifest_digest: str) -> ArmRun:
    """Thin replay adapter for prepared exports (JSON lines, one aligned output per case).

    Required per line: case_id, answer (nullable), citations, abstained, served_revision,
    source_snapshot_id, retrieval_settings_id. Extra keys (for example a reference answer)
    are refused so protected material cannot ride along.
    """
    allowed = {
        "case_id", "answer", "citations", "abstained", "served_revision",
        "source_snapshot_id", "retrieval_settings_id",
    }  # fmt: skip
    outs: list[ArmOutput] = []
    for n, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            row: dict[str, Any] = json.loads(line)
        except ValueError:
            raise BenchmarkError("export_row_not_json", f"line {n}") from None
        if type(row) is not dict or set(row) != allowed:
            raise BenchmarkError("export_row_shape_invalid", f"line {n}")
        cites = row["citations"]
        ok = (
            type(row["abstained"]) is bool
            and (row["answer"] is None or type(row["answer"]) is str)
            and type(cites) is list
            and all(type(c) is str for c in cites)
            and all(
                type(row[k]) is str and bool(row[k])
                for k in (
                    "case_id",
                    "served_revision",
                    "source_snapshot_id",
                    "retrieval_settings_id",
                )
            )
        )
        if not ok:
            raise BenchmarkError("export_row_type_invalid", f"line {n}")
        outs.append(
            ArmOutput(
                row["case_id"], row["answer"], tuple(cites), row["abstained"],
                row["served_revision"], row["source_snapshot_id"], row["retrieval_settings_id"],
            )
        )  # fmt: skip
    return ArmRun(arm_id, manifest_digest, tuple(o.case_id for o in outs), tuple(outs))


# ---------------------------------------------------------------- command line


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="eve_benchmark", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run-generated", help="run the benchmark on generated sentinel fixtures")
    run.add_argument("--out", type=Path, required=True)
    run.add_argument("--baseline-only", action="store_true")
    run.add_argument("--disposition", default=None)
    args = p.parse_args(argv)
    try:
        result = run_fixture_benchmark(include_repair=not args.baseline_only)
        out: Path = args.out
        write_once(out / "manifest.json", manifest_to_json(result.manifest))
        write_once(out / "scorecard.json", result.scorecard.to_json())
        if result.finding is not None:
            write_once(out / "development.json", result.finding.to_json())
        if args.disposition:
            # No owner is named in the generated freeze, so this id is recorded as
            # caller-asserted and cannot satisfy the named-owner gate.
            record_one_disposition(
                out, result.scorecard, result.finding, disposition=args.disposition,
                owner_id="generated-owner-1", recorded_at="2026-01-01T00:00:00Z",
            )  # fmt: skip
    except BenchmarkError as exc:
        print(f"REFUSED {exc.code}: {exc}", file=sys.stderr)
        return 2
    body = result.scorecard.body
    paired = body["paired_comparison"]
    caveat = ""
    if paired is not None:
        ci, fi = paired["interval"], paired["family_interval"]
        caveat = (
            f" case_interval=[{ci['lower']:.3f},{ci['upper']:.3f}](assumes case independence)"
            f" family_interval=[{fi['lower']:.3f},{fi['upper']:.3f}]({fi['units']} families)"
            " descriptive-only"
        )
    print(
        f"scorecard {result.scorecard.scorecard_digest} claim={body['improvement_claim']}"
        f"{caveat} tier=generated"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
