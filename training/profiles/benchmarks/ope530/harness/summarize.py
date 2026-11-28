"""Summarize OPE-530 measurements.json into per-stage median/min/worst, cold vs warm.

Shares are percentages of the per-run time to verified verdict, rounded to 2 decimals.
Usage: summarize.py MEASUREMENTS_JSON OUT_JSON
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

PHASES = ("prepare", "fit", "replay-first", "replay-repeat", "verify")


def stat(xs: list[float], digits: int = 1) -> dict:
    return {
        "n": len(xs),
        "median": round(statistics.median(xs), digits),
        "min": round(min(xs), digits),
        "worst": round(max(xs), digits),
        "values": [round(x, digits) for x in xs],
    }


def pct(part: float, whole: float) -> float:
    return 100 * part / whole


def ok(row: dict) -> bool:
    return bool(row.get("completed", row.get("exit") == 0))


def replays(row: dict) -> float:
    return row["phases"]["replay-first"]["wall_ms"] + row["phases"]["replay-repeat"]["wall_ms"]


def product_table(rows: list[dict]) -> dict:
    t: dict = {}
    for ph in PHASES:
        t[f"A.{ph}.wall_ms"] = stat([r["phases"][ph]["wall_ms"] for r in rows])
        t[f"A.{ph}.resident_at_start"] = stat(
            [r["phases"][ph]["resident_at_start"] for r in rows], 4
        )
    t["A.fit.worker_elapsed_ms"] = stat([r["phases"]["fit"]["worker_elapsed_ms"] for r in rows])
    t["A.replays_total_ms"] = stat([replays(r) for r in rows])
    t["A.end_to_end_ms"] = stat([r["end_to_end_ms"] for r in rows])
    for ph in ("prepare", "fit", "verify"):
        t[f"A.{ph}_share_pct"] = stat(
            [pct(r["phases"][ph]["wall_ms"], r["end_to_end_ms"]) for r in rows], 2
        )
    t["A.replays_share_pct"] = stat([pct(replays(r), r["end_to_end_ms"]) for r in rows], 2)
    loads = [
        r["phases"][ph]["loadavg_1m_at_start"]
        for r in rows
        for ph in PHASES
        if "loadavg_1m_at_start" in r["phases"][ph]
    ]
    t["A.loadavg_1m_at_phase_start"] = stat(loads, 2) if loads else None
    t["A.peak_rss_mb"] = stat([r["phases"]["fit"]["peak_rss_bytes"] / 2**20 for r in rows])
    return t


def replica_table(rows: list[dict], op: str) -> dict:
    t: dict = {}
    for key in rows[0]["durations_ms"]:
        t[f"B.{op}.{key}"] = stat([r["durations_ms"][key] for r in rows])
    t[f"B.{op}.wall_ms"] = stat([r["wall_ms"] for r in rows])
    work = "optimizer_steps_ms" if op == "fit" else "score_ms"
    t[f"B.{op}.{work.removesuffix('_ms')}_share_of_wall_pct"] = stat(
        [pct(r["durations_ms"][work], r["wall_ms"]) for r in rows], 2
    )
    t[f"B.{op}.forward_share_of_wall_pct"] = stat(
        [pct(r["durations_ms"]["frozen_forward_ms"], r["wall_ms"]) for r in rows], 2
    )
    t[f"B.{op}.hash_share_of_wall_pct"] = stat(
        [
            pct(
                r["durations_ms"]["hash_inputs_ms"] + r["durations_ms"]["rehash_base_ms"],
                r["wall_ms"],
            )
            for r in rows
        ],
        2,
    )
    t[f"B.{op}.padding_pct"] = stat([100 * r["tokens"]["padding_fraction"] for r in rows], 2)
    t[f"B.{op}.resident_at_start"] = stat([r["resident_at_start"] for r in rows], 4)
    return t


def main() -> int:
    doc = json.loads(Path(sys.argv[1]).read_text())
    rows = doc["rows"]
    run_ids = [r["run_id"] for r in rows]
    out: dict = {
        "source": Path(sys.argv[1]).name,
        "binding": {
            "batch_id": doc["batch_id"],
            "measured_git_sha": doc["measured_git_sha"],
            "git_tree_dirty": doc["git_tree_dirty"],
            "git_head_changed": doc.get("git_head_changed"),
            "started_at": doc["started_at"],
            "finished_at": doc.get("finished_at"),
            "rows": len(rows),
            "run_ids_unique": len(set(run_ids)) == len(run_ids),
            "rows_bound_to_measured_sha": all(
                r["measured_git_sha"] == doc["measured_git_sha"] for r in rows
            ),
            "cache_method": doc["cache_method"],
            "loadavg_at_start": doc.get("loadavg_at_start"),
            "loadavg_at_finish": doc.get("loadavg_at_finish"),
            "machine": doc["machine"],
        },
        "attempted": {},
        "completed": {},
        "failed": {},
        "parity_failures": 0,
    }
    for r in rows:
        k = f"{r['kind']}:{r['state']}"
        out["attempted"][k] = out["attempted"].get(k, 0) + 1
        bucket = "completed" if ok(r) else "failed"
        out[bucket][k] = out[bucket].get(k, 0) + 1
        if r["kind"].startswith("B_") and r.get("parity_with_product") is not True:
            out["parity_failures"] += 1
    tables = {}
    for state in ("cold", "warm"):
        a = [r for r in rows if r["kind"] == "A_product_cli" and r["state"] == state and ok(r)]
        t = product_table(a)
        for op in ("fit", "predict"):
            b = [
                r for r in rows if r["kind"] == f"B_replica_{op}" and r["state"] == state and ok(r)
            ]
            t.update(replica_table(b, op))
        c = [r for r in rows if r["kind"] == "C_service_fit" and r["state"] == state and ok(r)]
        for key in (
            "import_service_ms",
            "validate_ms",
            "worker_subprocess_ms",
            "publish_ms",
            "worker_internal_elapsed_ms",
            "wall_ms",
        ):
            t[f"C.{key}"] = stat([r[key] for r in c])
        tables[state] = t
    out["tables"] = tables
    a_all = [r for r in rows if r["kind"] == "A_product_cli" and ok(r)]
    verdict_keys = (
        "protected_count",
        "automatic",
        "wrong_automatic",
        "observed_operating_point",
        "real_customer_acceptances",
        "incremental_compute_cost_cents",
    )
    out["verdicts"] = sorted(
        {json.dumps({k: r["phases"]["verify"][k] for k in verdict_keys}) for r in a_all}
    )
    out["artifact_sha256"] = sorted({r["phases"]["fit"]["artifact_sha256"] for r in a_all})
    out["replay_output_sha256"] = sorted(
        {r["phases"][p]["output_sha256"] for r in a_all for p in ("replay-first", "replay-repeat")}
    )
    for op in ("fit", "predict"):
        first = next(r for r in rows if r["kind"] == f"B_replica_{op}" and ok(r))
        out[f"token_lengths_{op}"] = first["tokens"]["lengths"]
        out[f"useful_padded_{op}"] = [
            first["tokens"]["useful_tokens"],
            first["tokens"]["padded_tokens"],
        ]
        if op == "fit":
            out["runtime"] = first["runtime"]
    probe = next((r for r in rows if r["kind"] == "D_batch_probe" and ok(r)), None)
    if probe is not None:
        savings = [t["saving_ms"] for t in probe["trials"]]
        out["batch_probe"] = {
            "run_id": probe["run_id"],
            "incumbent_batches": probe["incumbent_batches"],
            "merged_batches": probe["merged_batches"],
            "max_abs_hidden_diff": probe["max_abs_hidden_diff"],
            "incumbent_ms": stat([t["incumbent_ms"] for t in probe["trials"]]),
            "merged_ms": stat([t["merged_ms"] for t in probe["trials"]]),
            "saving_ms": stat(savings),
        }
        # Upper estimate of what cached tokenization + length-bucketed, token-budgeted batches
        # could remove from the fit on this fixture: all tokenization, all padded positions of
        # the forward pass, and the measured saving from merging the forward into one batch.
        bound = {}
        for state in ("cold", "warm"):
            t = tables[state]
            removable = (
                t["B.fit.tokenize_ms"]["median"]
                + t["B.fit.padding_pct"]["median"] / 100 * t["B.fit.frozen_forward_ms"]["median"]
                + max(0.0, statistics.median(savings))
            )
            bound[state] = {
                "removable_ms": round(removable, 1),
                "fit_wall_ms_median": t["A.fit.wall_ms"]["median"],
                "share_of_fit_wall_pct": round(pct(removable, t["A.fit.wall_ms"]["median"]), 2),
            }
        out["opportunity_bound"] = bound
    Path(sys.argv[2]).write_text(json.dumps(out, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
