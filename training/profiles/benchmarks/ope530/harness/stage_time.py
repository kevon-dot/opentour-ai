"""OPE-530 stage-time orchestrator for the generated open-weight rehearsal.

Per repeat and per cache condition it measures:
  A. the unmodified product CLI phases (prepare, fit, replay-first, replay-repeat, verify);
  B. an instrumented worker replica (fit, predict) with output parity against A;
  C. the service-side fit split (validation / worker subprocess / atomic publication).
After the repeats it runs D, the batch-size probe behind the opportunity bound (warm).

Cache conditions are applied before EVERY measured process, including each product phase:
  cold = POSIX_FADV_DONTNEED on the staged model files, the model venv and its CPython home,
         with mincore residency of model.safetensors recorded at the start of the process;
  warm = the same files read through so they are resident (residency recorded likewise).
The host page cache beneath a VM disk is not controllable from the guest.

Every row carries a run id, an ISO-8601 UTC start time, the measured git SHA and a dirty-tree
flag, both taken from the repository before anything is written. The output is one JSON
object with a "rows" array, rewritten in full ("w" mode) after each row. Product code is not
modified. The staging and work directories must be outside the repository.

Usage: stage_time.py --staging DIR --work-root DIR --out FILE [--repeats 5] [--probe-trials 10]
  DIR/model-base   the profile's four checkpoint files (exact SHA-256 values)
  DIR/model-python a venv on CPython 3.12.16 with the hash-locked numpy and tokenizers wheels
"""

from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import json
import os
import platform
import subprocess
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[4]
SCRIPT = REPO / "training/profiles/qualify_open_weight.py"
PROFILE = REPO / "training/profiles/smollm2_135m_lora_v1.json"
PHASES = ("prepare", "fit", "replay-first", "replay-repeat", "verify")
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
ENV = {
    **WORKER_ENV,
    "PATH": "/usr/bin:/bin",
    "PYTHONPATH": str(REPO / "services/cell-runtime/src"),
}
libc = ctypes.CDLL(None, use_errno=True)
libc.mmap.restype = ctypes.c_void_p
libc.mmap.argtypes = [
    ctypes.c_void_p,
    ctypes.c_size_t,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_long,
]
libc.mincore.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_ubyte)]
libc.munmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t]


def now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(REPO), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def inside(path: Path, root: Path) -> bool:
    return path.resolve() == root or root in path.resolve().parents


class Bench:
    def __init__(self, staging: Path, work_root: Path, out: Path) -> None:
        self.base = staging / "model-base"
        self.py = staging / "model-python/bin/python"
        self.work_root = work_root
        self.out = out
        home = next(
            line.split("=", 1)[1].strip()
            for line in (staging / "model-python/pyvenv.cfg").read_text().splitlines()
            if line.startswith("home")
        )
        roots = [self.base, staging / "model-python", Path(home).parent]
        self.files = [p for r in roots for p in r.rglob("*") if p.is_file() and not p.is_symlink()]
        self.batch_id = uuid.uuid4().hex[:12]
        self.sha = git("rev-parse", "HEAD")
        self.dirty = bool(git("status", "--porcelain", "--untracked-files=normal"))
        self.rows: list[dict] = []
        self.header = {
            "schema": "opentour.ope530.stage-time.v2",
            "batch_id": self.batch_id,
            "started_at": now(),
            "measured_git_sha": self.sha,
            "git_tree_dirty": self.dirty,
            "harness": "training/profiles/benchmarks/ope530/harness/stage_time.py",
            "cache_method": "fadvise-dontneed (cold) or read-through (warm) before every process",
            "machine": machine(),
            "loadavg_at_start": list(os.getloadavg()),
        }

    def resident(self) -> float:
        path = self.base / "model.safetensors"
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

    def condition(self, state: str) -> float:
        if state == "cold":
            os.sync()
            for path in self.files:
                fd = os.open(path, os.O_RDONLY)
                try:
                    os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
                finally:
                    os.close(fd)
        else:
            for path in self.files:
                with path.open("rb") as handle:
                    while handle.read(1 << 22):
                        pass
        return self.resident()

    def emit(self, rep: int | None, state: str, kind: str, started: str, body: dict) -> None:
        tag = f"r{rep}-{state}" if rep is not None else state
        row = {
            "run_id": f"{self.batch_id}-{tag}-{kind}",
            "batch_id": self.batch_id,
            "started_at": started,
            "measured_git_sha": self.sha,
            "git_tree_dirty": self.dirty,
            "repeat": rep,
            "state": state,
            "kind": kind,
            **body,
        }
        self.rows.append(row)
        self.write()

    def write(self, finished: bool = False) -> None:
        doc = dict(self.header)
        if finished:
            doc["finished_at"] = now()
            doc["git_head_after"] = git("rev-parse", "HEAD")
            doc["git_head_changed"] = doc["git_head_after"] != self.sha
            doc["loadavg_at_finish"] = list(os.getloadavg())
        doc["rows"] = self.rows
        tmp = self.out.with_name(self.out.name + ".tmp")
        with tmp.open("w") as handle:
            json.dump(doc, handle, indent=1)
            handle.write("\n")
        os.replace(tmp, self.out)

    def cli(self, phase: str, work: Path) -> list[str]:
        return [
            str(self.py),
            str(SCRIPT),
            phase,
            "--work-dir",
            str(work),
            "--model-dir",
            str(self.base),
            "--numeric-python",
            str(self.py),
        ]

    def product(self, work: Path, state: str) -> dict:
        out: dict = {"phases": {}}
        for phase in PHASES:
            level = self.condition(state)
            res0 = self.resident()
            t = time.perf_counter()
            run = subprocess.run(self.cli(phase, work), env=ENV, cwd=REPO, capture_output=True)
            wall = (time.perf_counter() - t) * 1000
            entry: dict = {
                "wall_ms": round(wall, 1),
                "exit": run.returncode,
                "resident_after_conditioning": level,
                "resident_at_start": res0,
                "loadavg_1m_at_start": os.getloadavg()[0],
            }
            if run.returncode:
                entry["stderr_tail"] = run.stderr.decode()[-400:]
            else:
                body = last_json(run.stdout)
                if phase == "fit":
                    training = body["training"]
                    entry["worker_elapsed_ms"] = training["elapsed_ms"]
                    for key in ("steps", "peak_rss_bytes", "initial_loss", "final_loss"):
                        entry[key] = training[key]
                    entry["artifact_sha256"] = body["artifact_sha256"]
                elif phase.startswith("replay"):
                    entry["replay_elapsed_ms"] = body["elapsed_ms"]
                    entry["output_sha256"] = body["output_sha256"]
                elif phase == "verify":
                    for key in (
                        "protected_count",
                        "automatic",
                        "wrong_automatic",
                        "observed_operating_point",
                        "fallback",
                        "real_customer_acceptances",
                        "incremental_compute_cost_cents",
                        "artifact_sha256",
                    ):
                        entry[key] = body[key]
            out["phases"][phase] = entry
            if run.returncode:
                break
        out["completed"] = all(out["phases"].get(p, {}).get("exit") == 0 for p in PHASES)
        out["end_to_end_ms"] = round(sum(v["wall_ms"] for v in out["phases"].values()), 1)
        return out

    def replica(self, work: Path, operation: str, state: str) -> dict:
        grant = json.loads((work / "grant.json").read_bytes())
        request: dict = {
            "operation": operation,
            "labels": grant["labels"],
            "model_dir": str(self.base.resolve()),
            "max_seconds": grant["max_seconds"],
        }
        if operation == "fit":
            # Same construction as open_weight.fit_open_weight: rows sorted by case_id,
            # targets are label indices of exactly those rows.
            rows = sorted(
                json.loads((work / "training.json").read_bytes()), key=lambda r: r["case_id"]
            )
            request["examples"] = [row["features"] for row in rows]
            request["targets"] = [grant["labels"].index(row["label"]) for row in rows]
        else:
            request["examples"] = [
                r["features"] for r in json.loads((work / "protected-inputs.json").read_bytes())
            ]
            request["adapter"] = json.loads((work / "model/adapter.json").read_bytes())
        level = self.condition(state)
        res0 = self.resident()
        load = os.getloadavg()[0]
        spawn = time.time()
        t = time.perf_counter()
        run = subprocess.run(
            [str(self.py), "-I", "-B", str(HERE / "worker_replica.py"), str(PROFILE), str(REPO)],
            input=json.dumps(request).encode(),
            env=WORKER_ENV,
            capture_output=True,
        )
        wall = (time.perf_counter() - t) * 1000
        if run.returncode:
            return {
                "exit": run.returncode,
                "resident_after_conditioning": level,
                "stdout_tail": run.stdout.decode()[-300:],
                "stderr_tail": run.stderr.decode()[-600:],
            }
        body = json.loads(run.stdout)
        body.update(
            exit=0,
            wall_ms=round(wall, 1),
            resident_after_conditioning=level,
            resident_at_start=res0,
            loadavg_1m_at_start=load,
        )
        body["durations_ms"]["interpreter_start_ms"] = round((body["t0_epoch"] - spawn) * 1000, 1)
        body["durations_ms"]["module_load_ms"] = round(
            (body["marks"]["interp_ready"] - body["t0_epoch"]) * 1000, 1
        )
        if operation == "fit":
            product_adapter = json.loads((work / "model/adapter.json").read_bytes())
            body["parity_with_product"] = body.pop("adapter") == product_adapter
        else:
            receipt = json.loads((work / "replay-first.json").read_bytes())
            body["parity_with_product"] = body["decisions"] == receipt["case_outputs"]
        return body

    def service_fit(self, work: Path, state: str) -> dict:
        subprocess.run(self.cli("prepare", work), env=ENV, cwd=REPO, check=True)
        level = self.condition(state)
        res0 = self.resident()
        load = os.getloadavg()[0]
        t = time.perf_counter()
        probe = HERE / "service_fit_probe.py"
        run = subprocess.run(
            [str(self.py), str(probe), str(REPO), str(work), str(self.base), str(self.py)],
            env=ENV,
            cwd=REPO,
            capture_output=True,
        )
        wall = (time.perf_counter() - t) * 1000
        if run.returncode:
            return {"exit": run.returncode, "stderr_tail": run.stderr.decode()[-600:]}
        body = last_json(run.stdout)
        body.update(
            exit=0,
            wall_ms=round(wall, 1),
            resident_after_conditioning=level,
            resident_at_start=res0,
            loadavg_1m_at_start=load,
        )
        return body

    def batch_probe(self, work: Path, trials: int) -> dict:
        level = self.condition("warm")
        load = os.getloadavg()[0]
        t = time.perf_counter()
        run = subprocess.run(
            [
                str(self.py),
                "-I",
                "-B",
                str(HERE / "batch_probe.py"),
                str(PROFILE),
                str(REPO),
                str(self.base),
                str(work),
                str(trials),
            ],
            env=WORKER_ENV,
            capture_output=True,
        )
        wall = (time.perf_counter() - t) * 1000
        if run.returncode:
            return {"exit": run.returncode, "stderr_tail": run.stderr.decode()[-600:]}
        body = json.loads(run.stdout)
        body.update(
            exit=0,
            wall_ms=round(wall, 1),
            resident_after_conditioning=level,
            loadavg_1m_at_start=load,
        )
        return body


def machine() -> dict:
    model = next(
        (
            line.split(":", 1)[1].strip()
            for line in Path("/proc/cpuinfo").read_text().splitlines()
            if line.startswith("model name")
        ),
        "unknown",
    )
    mem_kib = next(
        int(line.split()[1])
        for line in Path("/proc/meminfo").read_text().splitlines()
        if line.startswith("MemTotal")
    )
    return {
        "cpu_model": model,
        "logical_cpus": os.cpu_count(),
        "mem_total_gib": round(mem_kib / 2**20, 1),
        "kernel": platform.release(),
        "machine": platform.machine(),
        "virtualization": Path("/sys/class/dmi/id/sys_vendor").read_text().strip()
        if Path("/sys/class/dmi/id/sys_vendor").exists()
        else "unknown",
    }


def last_json(raw: bytes) -> dict:
    lines = [line for line in raw.decode().splitlines() if line.strip()]
    return json.loads(lines[-1]) if lines else {}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--staging", type=Path, required=True)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--probe-trials", type=int, default=10)
    args = parser.parse_args()
    if not SCRIPT.is_file() or not PROFILE.is_file():
        parser.error(f"repository not found from harness location: {REPO}")
    for name in ("staging", "work_root"):
        if inside(getattr(args, name), REPO):
            parser.error(f"--{name.replace('_', '-')} must be outside the repository")
    args.work_root.mkdir(parents=True, exist_ok=False)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    bench = Bench(args.staging.resolve(), args.work_root.resolve(), args.out)
    last_work: Path | None = None
    for rep in range(args.repeats):
        order = ("cold", "warm") if rep % 2 == 0 else ("warm", "cold")
        for state in order:
            tag = f"r{rep}-{state}"
            work = bench.work_root / f"work-{tag}-A"
            started = now()
            a = bench.product(work, state)
            bench.emit(rep, state, "A_product_cli", started, a)
            if not a["completed"]:
                print(json.dumps({"failed": tag}), flush=True)
                continue
            last_work = work
            for op in ("fit", "predict"):
                started = now()
                bench.emit(rep, state, f"B_replica_{op}", started, bench.replica(work, op, state))
            started = now()
            c = bench.service_fit(bench.work_root / f"work-{tag}-C", state)
            bench.emit(rep, state, "C_service_fit", started, c)
            print(
                json.dumps(
                    {
                        "done": tag,
                        "end_to_end_ms": a["end_to_end_ms"],
                        "fit_wall_ms": a["phases"]["fit"]["wall_ms"],
                    }
                ),
                flush=True,
            )
    if last_work is not None and args.probe_trials > 0:
        started = now()
        bench.emit(
            None, "warm", "D_batch_probe", started, bench.batch_probe(last_work, args.probe_trials)
        )
    bench.write(finished=True)
    failed = [r["run_id"] for r in bench.rows if not r.get("completed", r.get("exit") == 0)]
    print(json.dumps({"rows": len(bench.rows), "failed": failed}), flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
