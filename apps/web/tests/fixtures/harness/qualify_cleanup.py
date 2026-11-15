"""Exercise the actual OPE-123 supervisor against an established real browser."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(ROOT))
from tools import test_runner  # noqa: E402


def process_rows():
    output = subprocess.check_output(["ps", "-axo", "pid=,ppid=,pgid=,stat=,comm="], text=True)
    rows = [line.split(maxsplit=4) for line in output.splitlines() if line.strip()]
    return [row if len(row) == 5 else [*row, ""] for row in rows if len(row) >= 4]


def main():
    mode = sys.argv[1]
    node = sys.argv[2]
    original = test_runner._MANAGED_POPEN
    handle = []
    observed = {}

    def capture(argv, *args, **kwargs):
        child = original(argv, *args, **kwargs)
        if argv[0] == node and "probe-driver.ts" in argv[1]:
            handle.append(child)
        return child

    with tempfile.TemporaryDirectory(prefix="opentour-owned-cleanup-") as temporary:
        directory = Path(temporary).resolve()
        report = directory / "result.json"
        ready = Path(str(report) + ".ready")

        def inspect_and_interrupt():
            try:
                deadline = time.monotonic() + 6
                while (not handle or not ready.exists()) and time.monotonic() < deadline:
                    time.sleep(0.02)
                if not ready.exists():
                    observed["error"] = "browser_readiness_missing"
                    return
                child = handle[0]
                state = json.loads(ready.read_text())
                rows = process_rows()
                owned = {child.pid}
                changed = True
                while changed:
                    previous = len(owned)
                    owned.update(int(row[0]) for row in rows if int(row[1]) in owned)
                    changed = len(owned) != previous
                members = [row for row in rows if int(row[0]) in owned]
                observed.update(
                    {
                        "group": child.pid,
                        "member_count": len(members),
                        "browser_count": sum("chrome-headless-shell" in row[4] for row in members),
                        "same_group": all(int(row[2]) == child.pid for row in members),
                        "origin": state["origin"],
                    }
                )
                if mode == "crash":
                    child.kill()  # Exact unreaped Popen handle; never a sampled unrelated PID.
            except Exception as error:
                observed["error"] = type(error).__name__
                observed["captured_handles"] = len(handle)

        test_runner._MANAGED_POPEN = capture
        worker = threading.Thread(target=inspect_and_interrupt)
        worker.start()
        try:
            result = test_runner.execute(
                test_runner.Invocation(
                    argv=(
                        node,
                        str(ROOT / "apps/web/tests/fixtures/harness/probe-driver.ts"),
                        str(ROOT),
                        str(report),
                        uuid.uuid4().hex,
                        "hold",
                    ),
                    cwd=ROOT,
                    environment={
                        "PATH": os.environ["PATH"],
                        "HOME": str(directory),
                        "TMPDIR": str(directory),
                    },
                    report=report,
                    target="apps/web/tests/fixtures/harness/probes/probe with spaces.spec.ts",
                    runner="playwright",
                    timeout=8,
                )
            )
        except Exception as error:
            observed["wrapper_error"] = type(error).__name__
            result = test_runner.ProcessResult(handle[0].poll() or -1, cleanup_complete=False)
        finally:
            worker.join(timeout=10)
            test_runner._MANAGED_POPEN = original
        deadline = time.monotonic() + 3
        survivors = []
        while time.monotonic() < deadline:
            survivors = [
                int(row[0])
                for row in process_rows()
                if int(row[2]) == observed.get("group") and not row[3].startswith("Z")
            ]
            if not survivors:
                break
            time.sleep(0.05)
        origin = urlparse(observed.get("origin", ""))
        listener_closed = False
        if origin.hostname == "127.0.0.1" and origin.port:
            with socket.socket() as client:
                client.settimeout(0.5)
                listener_closed = client.connect_ex((origin.hostname, origin.port)) != 0
        outcome = {
            **observed,
            "mode": mode,
            "raw_exit": result.returncode,
            "timed_out": result.timed_out,
            "wrapper_cleanup_complete": result.cleanup_complete,
            "survivors": survivors,
            "listener_closed": listener_closed,
            "report_exists": report.exists(),
        }
        print(json.dumps(outcome))
        assert not observed.get("error") and not observed.get("wrapper_error"), outcome
        assert observed.get("browser_count", 0) > 0 and observed.get("same_group"), outcome
        assert result.returncode != 0 and not survivors and listener_closed, outcome
        assert mode != "timeout" or result.timed_out, outcome


if __name__ == "__main__":
    main()
