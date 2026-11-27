"""OPE-530 stage-time orchestrator for the generated open-weight rehearsal.

Measures, per repeat and per cache condition (cold = page cache evicted with
POSIX_FADV_DONTNEED for the model files and numeric runtime, verified with mincore;
warm = resident from the preceding run):
  A. the unmodified product CLI phases (prepare, fit, replay-first, replay-repeat, verify);
  B. an instrumented worker replica (fit + predict) with output parity against A;
  C. the service-side fit split (validation / worker subprocess / atomic publication).
Product code is not modified. Usage: stage_time.py OUT_DIR REPEATS
"""

from __future__ import annotations

import ctypes
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

REPO = Path("/home/user/opentour-ai")
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
BASE = ROOT / "model-base"
PY = ROOT / "model-python/bin/python"
SCRIPT = REPO / "training/profiles/qualify_open_weight.py"
PROFILE = REPO / "training/profiles/smollm2_135m_lora_v1.json"
PHASES = ("prepare", "fit", "replay-first", "replay-repeat", "verify")
ENV = {
    "PATH": "/usr/bin:/bin",
    "PYTHONPATH": str(REPO / "services/cell-runtime/src"),
    "LANG": "C.UTF-8",
    "OMP_NUM_THREADS": "1",
    "OPENBLAS_NUM_THREADS": "1",
    "TOKENIZERS_PARALLELISM": "false",
    "HF_HUB_OFFLINE": "1",
    "TRANSFORMERS_OFFLINE": "1",
}
WORKER_ENV = {
    "LANG": "C.UTF-8",
    "OPENBLAS_NUM_THREADS": "1",
    "OMP_NUM_THREADS": "1",
    "MKL_NUM_THREADS": "1",
    "RAYON_NUM_THREADS": "1",
    "TOKENIZERS_PARALLELISM": "false",
    "HF_HUB_OFFLINE": "1",
    "TRANSFORMERS_OFFLINE": "1",
}
libc = ctypes.CDLL(None, use_errno=True)
libc.mmap.restype = ctypes.c_void_p
libc.mmap.argtypes = [
    ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_long
]
libc.mincore.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_ubyte)]
libc.munmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t]


def resident(path: Path) -> float:
    size = path.stat().st_size
    fd = os.open(path, os.O_RDONLY)
    try:
        addr = libc.mmap(None, size, 1, 1, fd, 0)
        pages = (size + 4095) // 4096
        vec = (ctypes.c_ubyte * pages)()
        if libc.mincore(addr, size, vec) != 0:
            raise OSError(ctypes.get_errno(), "mincore")
        libc.munmap(addr, size)
    finally:
        os.close(fd)
    return round(sum(v & 1 for v in vec) / pages, 4)


def cache_files() -> list[Path]:
    roots = [BASE, ROOT / "model-python", ROOT / "cpython-3.12.16"]
    return [p for r in roots for p in r.rglob("*") if p.is_file() and not p.is_symlink()]


def evict() -> float:
    os.sync()
    for path in cache_files():
        fd = os.open(path, os.O_RDONLY)
        try:
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        finally:
            os.close(fd)
    return resident(BASE / "model.safetensors")


def prime() -> float:
    for path in cache_files():
        with path.open("rb") as handle:
            while handle.read(1 << 22):
                pass
    return resident(BASE / "model.safetensors")


def last_json(raw: bytes) -> dict:
    lines = [line for line in raw.decode().splitlines() if line.strip()]
    return json.loads(lines[-1]) if lines else {}


def product(work: Path) -> dict:
    out: dict = {"phases": {}}
    for phase in PHASES:
        res0 = resident(BASE / "model.safetensors")
        cmd = [str(PY), str(SCRIPT), phase, "--work-dir", str(work), "--model-dir", str(BASE),
               "--numeric-python", str(PY)]
        t = time.perf_counter()
        run = subprocess.run(cmd, env=ENV, cwd=REPO, capture_output=True)
        wall = (time.perf_counter() - t) * 1000
        entry = {"wall_ms": round(wall, 1), "exit": run.returncode, "resident_at_start": res0}
        if run.returncode:
            entry["stderr_tail"] = run.stderr.decode()[-400:]
        else:
            body = last_json(run.stdout)
            if phase == "fit":
                entry["worker_elapsed_ms"] = body["training"]["elapsed_ms"]
                entry["steps"] = body["training"]["steps"]
                entry["peak_rss_bytes"] = body["training"]["peak_rss_bytes"]
                entry["initial_loss"] = body["training"]["initial_loss"]
                entry["final_loss"] = body["training"]["final_loss"]
                entry["artifact_sha256"] = body["artifact_sha256"]
            elif phase.startswith("replay"):
                entry["replay_elapsed_ms"] = body["elapsed_ms"]
                entry["output_sha256"] = body["output_sha256"]
            elif phase == "verify":
                for key in ("protected_count", "automatic", "wrong_automatic",
                            "observed_operating_point", "fallback", "real_customer_acceptances",
                            "incremental_compute_cost_cents", "artifact_sha256"):
                    entry[key] = body[key]
        out["phases"][phase] = entry
        if run.returncode:
            break
    out["completed"] = all(out["phases"].get(p, {}).get("exit") == 0 for p in PHASES)
    out["end_to_end_ms"] = round(sum(v["wall_ms"] for v in out["phases"].values()), 1)
    return out


def replica(work: Path, operation: str) -> dict:
    grant = json.loads((work / "grant.json").read_bytes())
    request: dict = {"operation": operation, "labels": grant["labels"],
                     "model_dir": str(BASE.resolve()), "max_seconds": grant["max_seconds"]}
    if operation == "fit":
        rows = sorted(json.loads((work / "training.json").read_bytes()),
                      key=lambda row: row["case_id"])
        request["examples"] = [row["features"] for row in rows]
        request["targets"] = [grant["labels"].index(row["label"]) for row in rows]
    else:
        request["examples"] = [r["features"] for r in
                               json.loads((work / "protected-inputs.json").read_bytes())]
        request["adapter"] = json.loads((work / "model/adapter.json").read_bytes())
    res0 = resident(BASE / "model.safetensors")
    spawn = time.time()
    t = time.perf_counter()
    run = subprocess.run(
        [str(PY), "-I", "-B", str(HERE / "worker_replica.py"), str(PROFILE), str(REPO)],
        input=json.dumps(request).encode(), env=WORKER_ENV, capture_output=True,
    )
    wall = (time.perf_counter() - t) * 1000
    if run.returncode:
        return {"exit": run.returncode, "stderr_tail": run.stderr.decode()[-600:]}
    body = json.loads(run.stdout)
    body["exit"] = 0
    body["wall_ms"] = round(wall, 1)
    body["resident_at_start"] = res0
    body["durations_ms"]["interpreter_start_ms"] = round((body["t0_epoch"] - spawn) * 1000, 1)
    body["durations_ms"]["module_load_ms"] = round(
        (body["marks"]["interp_ready"] - body["t0_epoch"]) * 1000, 1)
    if operation == "fit":
        product_adapter = json.loads((work / "model/adapter.json").read_bytes())
        body["parity_with_product"] = body.pop("adapter") == product_adapter
    else:
        receipt = json.loads((work / "replay-first.json").read_bytes())
        body["parity_with_product"] = body["decisions"] == receipt["case_outputs"]
    return body


def service_prepare(work: Path) -> None:
    subprocess.run([str(PY), str(SCRIPT), "prepare", "--work-dir", str(work), "--model-dir",
                    str(BASE), "--numeric-python", str(PY)], env=ENV, cwd=REPO, check=True)


def service_fit(work: Path) -> dict:
    res0 = resident(BASE / "model.safetensors")
    t = time.perf_counter()
    run = subprocess.run([str(PY), str(HERE / "service_fit_probe.py"), str(REPO), str(work),
                          str(BASE), str(PY)], env=ENV, cwd=REPO, capture_output=True)
    wall = (time.perf_counter() - t) * 1000
    if run.returncode:
        return {"exit": run.returncode, "stderr_tail": run.stderr.decode()[-600:]}
    body = last_json(run.stdout)
    body.update(exit=0, wall_ms=round(wall, 1), resident_at_start=res0)
    return body


def condition(state: str) -> float:
    return evict() if state == "cold" else prime_if_needed()


def prime_if_needed() -> float:
    level = resident(BASE / "model.safetensors")
    return level if level > 0.99 else prime()


def main() -> int:
    out_dir = Path(sys.argv[1])
    repeats = int(sys.argv[2])
    out_dir.mkdir(parents=True, exist_ok=True)
    log = (out_dir / "measurements.jsonl").open("a")
    for rep in range(repeats):
        order = ("cold", "warm") if rep % 2 == 0 else ("warm", "cold")
        for state in order:
            tag = f"r{rep}-{state}"
            level = condition(state)
            a = product(out_dir / f"work-{tag}-A")
            record = {"repeat": rep, "state": state, "kind": "A_product_cli",
                      "resident_after_conditioning": level, **a}
            log.write(json.dumps(record) + "\n")
            log.flush()
            if not a["completed"]:
                print(json.dumps({"failed": tag}), flush=True)
                continue
            for op in ("fit", "predict"):
                level = condition(state)
                b = replica(out_dir / f"work-{tag}-A", op)
                log.write(json.dumps({"repeat": rep, "state": state, "kind": f"B_replica_{op}",
                                      "resident_after_conditioning": level, **b}) + "\n")
                log.flush()
            service_prepare(out_dir / f"work-{tag}-C")
            level = condition(state)
            c = service_fit(out_dir / f"work-{tag}-C")
            log.write(json.dumps({"repeat": rep, "state": state, "kind": "C_service_fit",
                                  "resident_after_conditioning": level, **c}) + "\n")
            log.flush()
            print(json.dumps({"done": tag, "end_to_end_ms": a["end_to_end_ms"],
                              "fit_wall_ms": a["phases"]["fit"]["wall_ms"]}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
