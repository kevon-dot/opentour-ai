"""Prove installed exchange controls reach the real optimizer in a generated CPU rehearsal."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from opentour_cell.pilot.eve_exchange import Exchange, digest
from opentour_training.training.eve_host import TrainerHost
from opentour_training.training.eve_run import Run
from opentour_training.training.eve_sft import BASE_REVISION
from prepare_eve_rehearsal import write_group
from qualify_eve_resume_cpu import fixture_process, make_base


def paced_fixture(root, args):
    import torch

    real_step = torch.optim.AdamW.step

    def paced(optimizer, *a, **kw):
        # Keep each genuine optimizer step observable by a separate control process.
        time.sleep(0.3)
        return real_step(optimizer, *a, **kw)

    with patch.object(torch.optim.AdamW, "step", paced):
        fixture_process(root, args)


def qualify(root: Path, image_id: str) -> dict:
    if not __debug__:
        raise RuntimeError("qualification requires assertions")
    make_base(root / "base")
    write_group(root / "fixtures")
    inputs, work = root / "inputs", root / "work"
    inputs.mkdir(mode=0o700)
    work.mkdir(mode=0o700)
    exchange = Exchange(root / "exchange")
    children = []

    def spawn(args):
        child = subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-B",
                "-c",
                "import pathlib,runpy,sys; p=sys.argv.pop(1); "
                "sys.path.insert(0,str(pathlib.Path(p).parent)); "
                "runpy.run_path(p,run_name='__main__')",
                str(Path(__file__).resolve()),
                "--fixture-process",
                str(root),
                *args,
            ],
            stdout=subprocess.DEVNULL,
        )
        children.append(child)
        return child

    def supervisor():
        return TrainerHost(exchange, root / "base", inputs, work, image_id, spawn=spawn)

    def stage(name):
        data = (root / "fixtures" / name).read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        (inputs / (sha + ".json")).write_bytes(data)
        return sha

    now = int(time.time())
    terms = {
        "tenant": "generated",
        "engagement_id": "cpu-fixture",
        "workflow": "fixture-workflow",
        "reviewer": "fixture-owner",
        "starts_at": now - 1,
        "deadline": now + 600,
        "steps": 8,
        "max_rows": 100,
        "image_id": image_id,
        "model_revision": BASE_REVISION,
        "development_sha256": stage("development.json"),
        "protected_sha256": stage("protected-membership.json"),
    }
    host = supervisor()

    def wait(ref, predicate):
        end = time.monotonic() + 150
        while time.monotonic() < end:
            row = exchange.get(ref)
            exchange.renew(ref, row["spec_digest"])
            host.tick()
            row = exchange.get(ref)
            if predicate(row):
                return row
            if row["state"] in {"held", "failed", "expired"}:
                raise RuntimeError(f"unexpected trainer state {row['state']}")
            time.sleep(0.025)
        raise TimeoutError(f"waiting for {ref}: {row['state']} at step {row['completed_steps']}")

    def click(row, action):
        return exchange.control(
            row["reservation_ref"], action, row["revision"], digest([row["revision"], action])
        )

    def admit(ref):
        spec = terms | {"reservation_ref": ref, "plan_ref": ref, "mandate_revision": "1" * 64}
        return exchange.admit(ref, spec, actor=terms["reviewer"], decision_key="generated-" + ref)

    try:
        ref = "fixture-resume"
        job = admit(ref)
        running = wait(ref, lambda r: r["state"] == "running" and (r["completed_steps"] or 0) >= 2)
        assert click(running, "pause")["state"] == "pausing"
        paused = wait(ref, lambda r: r["state"] == "paused")
        run = Run(work / job["run_ref"] / "trainer")
        step = paused["completed_steps"]
        assert 2 <= step < 8 and paused["checkpoint_step"] == step and run.state_file.exists()
        for child in children:
            assert child.wait(timeout=30) == 0
        host = supervisor()
        assert wait(ref, lambda r: r["state"] == "paused")["completed_steps"] == step
        assert click(paused, "resume")["state"] == "resuming"
        completed = wait(ref, lambda r: r["state"] == "completed")
        assert completed["trainer_id"] == paused["trainer_id"] and completed["completed_steps"] == 8
        assert run.control("status")["resumed_from_step"] == step
        adapter = work / job["run_ref"] / "candidate" / "adapter_model.safetensors"
        assert adapter.is_file() and completed["active_seconds"] >= paused["active_seconds"]
        stopref = "fixture-stop"
        second = admit(stopref)
        active = wait(
            stopref, lambda r: r["state"] == "running" and (r["completed_steps"] or 0) >= 1
        )
        assert click(active, "stop")["state"] == "stopping"
        stopped = wait(stopref, lambda r: r["state"] == "stopped")
        assert 1 <= stopped["completed_steps"] < 8
        assert not (work / second["run_ref"] / "candidate").exists()
        assert len(children) == 3
        return {
            "fixture": "generated tiny Mistral on CPU, real optimizer and packaged worker exchange",
            "image_config_id": image_id,
            "run_ref": job["run_ref"],
            "trainer_id": paused["trainer_id"],
            "confirmed_pause_step": step,
            "resumed_from_step": step,
            "completed_steps": 8,
            "active_seconds": completed["active_seconds"],
            "stopped_step": stopped["completed_steps"],
            "stopped_candidate_exists": False,
            "optimizer_processes": len(children),
            "adapter_sha256": hashlib.sha256(adapter.read_bytes()).hexdigest(),
        }
    finally:
        for child in children:
            if child.poll() is None:
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--fixture-process":
        paced_fixture(Path(sys.argv[2]), sys.argv[3:])
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--receipt", type=Path, required=True)
        parser.add_argument("--image-id", required=True)
        args = parser.parse_args()
        with tempfile.TemporaryDirectory() as temporary:
            receipt = qualify(Path(temporary).resolve(), args.image_id)
        args.receipt.write_text(json.dumps(receipt, indent=2) + "\n")
        print(json.dumps(receipt))
