"""Summarize OPE-530 measurements.jsonl into per-stage median/worst, cold vs warm."""
import json, statistics, sys
from pathlib import Path
rows = [json.loads(l) for l in Path(sys.argv[1]).read_text().splitlines()]
def stat(xs): return {"n": len(xs), "median": round(statistics.median(xs), 1), "min": round(min(xs), 1), "worst": round(max(xs), 1)}
out = {"attempted": {}, "completed": {}, "failed": {}, "parity_failures": 0}
for r in rows:
    k = f"{r['kind']}:{r['state']}"
    out["attempted"][k] = out["attempted"].get(k, 0) + 1
    ok = r.get("completed", r.get("exit") == 0)
    out["completed" if ok else "failed"][k] = out["completed" if ok else "failed"].get(k, 0) + 1
    if r["kind"].startswith("B_") and r.get("parity_with_product") is not True: out["parity_failures"] += 1
tables = {}
for state in ("cold", "warm"):
    A = [r for r in rows if r["kind"] == "A_product_cli" and r["state"] == state and r["completed"]]
    t = {}
    for ph in ("prepare", "fit", "replay-first", "replay-repeat", "verify"):
        t[f"A.{ph}.wall_ms"] = stat([r["phases"][ph]["wall_ms"] for r in A])
    t["A.fit.worker_elapsed_ms"] = stat([r["phases"]["fit"]["worker_elapsed_ms"] for r in A])
    t["A.replays_total_ms"] = stat([r["phases"]["replay-first"]["wall_ms"] + r["phases"]["replay-repeat"]["wall_ms"] for r in A])
    t["A.end_to_end_ms"] = stat([r["end_to_end_ms"] for r in A])
    t["A.fit_share_of_e2e"] = stat([r["phases"]["fit"]["wall_ms"] / r["end_to_end_ms"] for r in A])
    t["A.replay_share_of_e2e"] = stat([(r["phases"]["replay-first"]["wall_ms"] + r["phases"]["replay-repeat"]["wall_ms"]) / r["end_to_end_ms"] for r in A])
    t["A.prepare_share_of_e2e"] = stat([r["phases"]["prepare"]["wall_ms"] / r["end_to_end_ms"] for r in A])
    t["A.verify_share_of_e2e"] = stat([r["phases"]["verify"]["wall_ms"] / r["end_to_end_ms"] for r in A])
    t["A.peak_rss_mb"] = stat([r["phases"]["fit"]["peak_rss_bytes"] / 2**20 for r in A])
    t["A.fit_resident_at_start"] = stat([r["phases"]["fit"]["resident_at_start"] for r in A])
    for op in ("fit", "predict"):
        B = [r for r in rows if r["kind"] == f"B_replica_{op}" and r["state"] == state and r["exit"] == 0]
        for key in B[0]["durations_ms"]:
            t[f"B.{op}.{key}"] = stat([r["durations_ms"][key] for r in B])
        t[f"B.{op}.wall_ms"] = stat([r["wall_ms"] for r in B])
        t[f"B.{op}.optimizer_or_score_share_of_wall"] = stat([r["durations_ms"].get("optimizer_steps_ms", r["durations_ms"].get("score_ms")) / r["wall_ms"] for r in B])
        t[f"B.{op}.forward_share_of_wall"] = stat([r["durations_ms"]["frozen_forward_ms"] / r["wall_ms"] for r in B])
        t[f"B.{op}.hash_share_of_wall"] = stat([(r["durations_ms"]["hash_inputs_ms"] + r["durations_ms"]["rehash_base_ms"]) / r["wall_ms"] for r in B])
        t[f"B.{op}.padding_fraction"] = stat([r["tokens"]["padding_fraction"] for r in B])
        t[f"B.{op}.resident_at_start"] = stat([r["resident_at_start"] for r in B])
    C = [r for r in rows if r["kind"] == "C_service_fit" and r["state"] == state and r["exit"] == 0]
    for key in ("import_service_ms", "validate_ms", "worker_subprocess_ms", "publish_ms", "worker_internal_elapsed_ms", "wall_ms"):
        t[f"C.{key}"] = stat([r[key] for r in C])
    tables[state] = t
out["tables"] = tables
A = [r for r in rows if r["kind"] == "A_product_cli"]
out["verdicts"] = sorted({json.dumps({k: r["phases"]["verify"][k] for k in ("protected_count", "automatic", "wrong_automatic", "observed_operating_point", "real_customer_acceptances", "incremental_compute_cost_cents")}) for r in A})
out["artifact_sha256"] = sorted({r["phases"]["fit"]["artifact_sha256"] for r in A})
out["replay_output_sha256"] = sorted({r["phases"][p]["output_sha256"] for r in A for p in ("replay-first", "replay-repeat")})
B = [r for r in rows if r["kind"] == "B_replica_fit"]
out["token_lengths_fit"] = B[0]["tokens"]["lengths"]; out["useful_padded_fit"] = [B[0]["tokens"]["useful_tokens"], B[0]["tokens"]["padded_tokens"]]
Bp = [r for r in rows if r["kind"] == "B_replica_predict"]
out["token_lengths_predict"] = Bp[0]["tokens"]["lengths"]; out["useful_padded_predict"] = [Bp[0]["tokens"]["useful_tokens"], Bp[0]["tokens"]["padded_tokens"]]
out["runtime"] = B[0]["runtime"]
print(json.dumps(out, indent=1))
