"""Time the service-side fit (validation, worker subprocess, atomic publication) in process.

Wraps open_weight._run with timers only; calls the product qualify_open_weight.fit unchanged.
argv: repo work_dir model_dir numeric_python
"""

import time

T0 = time.time()
import contextlib  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

repo, work, base, python = (Path(a) for a in sys.argv[1:5])
sys.path[:0] = [str(repo / "services/cell-runtime/src"), str(repo / "training/profiles")]
p = time.perf_counter
t = p()
import qualify_open_weight as q  # noqa: E402
from opentour_cell.pilot import open_weight as ow  # noqa: E402

import_ms = (p() - t) * 1000
rec: dict[str, float] = {}
original = ow._run


def timed_run(*args, **kwargs):  # type: ignore[no-untyped-def]
    rec["run_start"] = p()
    try:
        return original(*args, **kwargs)
    finally:
        rec["run_end"] = p()


ow._run = timed_run
buf = io.StringIO()
t_fit = p()
with contextlib.redirect_stdout(buf):
    q.fit(work, base, python)
t_end = p()
manifest = json.loads((work / "model/manifest.json").read_bytes())
print(
    json.dumps(
        {
            "t0_epoch": T0,
            "import_service_ms": round(import_ms, 1),
            "validate_ms": round((rec["run_start"] - t_fit) * 1000, 1),
            "worker_subprocess_ms": round((rec["run_end"] - rec["run_start"]) * 1000, 1),
            "publish_ms": round((t_end - rec["run_end"]) * 1000, 1),
            "worker_internal_elapsed_ms": manifest["training"]["elapsed_ms"],
            "artifact_sha256": manifest["artifact_sha256"],
        }
    )
)
