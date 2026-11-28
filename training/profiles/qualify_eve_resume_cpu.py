"""Offline process-restart proof on a generated tiny Mistral. Run with PYTHONPATH=training/src."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from opentour_training.training import eve_sft

MODULE = "opentour_training.training.eve_sft"


def make_base(path: Path) -> None:
    import torch
    from tokenizers import Tokenizer, decoders, models, pre_tokenizers
    from transformers import MistralConfig, MistralForCausalLM, PreTrainedTokenizerFast

    alphabet = ["<eos>", "<unk>", *sorted(pre_tokenizers.ByteLevel.alphabet())]
    backend = Tokenizer(models.BPE({c: i for i, c in enumerate(alphabet)}, [], unk_token="<unk>"))
    backend.pre_tokenizer = pre_tokenizers.ByteLevel(add_prefix_space=False)
    backend.decoder = decoders.ByteLevel()
    tokenizer = PreTrainedTokenizerFast(
        tokenizer_object=backend, eos_token="<eos>", unk_token="<unk>"
    )
    tokenizer.chat_template = (
        "{% for m in messages %}{{ '[' ~ m.role ~ ']' ~ m.content }}{% endfor %}"
        "{% if add_generation_prompt %}{{ '[assistant]' }}{% else %}{{ eos_token }}{% endif %}"
    )
    torch.manual_seed(77)
    config = MistralConfig(
        vocab_size=len(tokenizer),
        hidden_size=16,
        intermediate_size=32,
        num_hidden_layers=1,
        num_attention_heads=2,
        num_key_value_heads=1,
        head_dim=8,
        max_position_embeddings=512,
        attention_dropout=0.17,
        eos_token_id=tokenizer.eos_token_id,
        pad_token_id=tokenizer.eos_token_id,
    )
    MistralForCausalLM(config).save_pretrained(path)
    tokenizer.save_pretrained(path)


def fixture_process(root: Path, args: list[str]) -> None:
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    base = root / "base"
    if eve_sft.read_file(base / "config.json")["hidden_size"] != 16:
        raise ValueError("expected generated 16-wide CPU model")
    torch.set_num_threads(1)
    torch.use_deterministic_algorithms(True)

    def load_cpu(_torch, model_dir):
        if model_dir.resolve() != base.resolve():
            raise ValueError("unexpected CPU fixture path")
        # Rendezvous only the test's first load with the separate CLI control process.
        signal = os.environ.get("EVE_FIXTURE_WAIT")
        if signal:
            wait_for(Path(signal).exists)
        model = AutoModelForCausalLM.from_pretrained(
            base, dtype=torch.float32, attn_implementation="eager", local_files_only=True
        )
        return model, AutoTokenizer.from_pretrained(base, local_files_only=True)

    changes = {
        "BASE_REVISION": "generated-tiny-mistral-cpu",
        "BASE_SHA256": eve_sft.file_hashes(base, ("model.safetensors",))["model.safetensors"],
        "load_model": load_cpu,
        "runtime": lambda: torch,
        "training_recipe": lambda: recipe,
        "runtime_versions": lambda: {
            k: importlib.metadata.version(k) for k in ("torch", "peft", "transformers")
        },
    }
    recipe = eve_sft.training_recipe() | {"quantization": "none", "compute_dtype": "float32"}
    sys.argv = [MODULE, *args]
    with ExitStack() as stack:
        for key, value in changes.items():
            stack.enter_context(patch.object(eve_sft, key, value))
        for name, value in (
            ("reset_peak_memory_stats", None),
            ("max_memory_allocated", 0),
            ("get_device_name", "generated CPU fixture"),
        ):
            stack.enter_context(patch.object(torch.cuda, name, return_value=value))
        # Optimizer, checkpoint bytes, RNG, run journal, CLI control and model replay are real.
        eve_sft.main()


def wait_for(predicate) -> None:
    deadline = time.monotonic() + 120
    while not predicate():
        if time.monotonic() >= deadline:
            raise TimeoutError("CPU witness timed out waiting for its peer")
        time.sleep(0.025)


def verify(root: Path) -> dict:
    import torch
    from prepare_eve_rehearsal import write_group
    from safetensors.torch import load_file

    if not __debug__:
        raise RuntimeError("Run the CPU qualification with Python assertions enabled")

    fixtures, base = root / "fixtures", root / "base"
    make_base(base)
    write_group(fixtures)
    env = {
        **os.environ,
        "OMP_NUM_THREADS": "1",
        "HF_HUB_OFFLINE": "1",
        "HF_HUB_DISABLE_PROGRESS_BARS": "1",
        "TOKENIZERS_PARALLELISM": "false",
    }
    cli = [sys.executable, "-m", MODULE]
    child = [sys.executable, str(Path(__file__).resolve()), "--fixture-process", str(root)]

    def execute(command, *, expected_error=None):
        result = subprocess.run(
            [str(a) for a in command], env=env, text=True, capture_output=True, timeout=180
        )
        if expected_error is None and result.returncode:
            raise RuntimeError(f"Unexpected exit {result.returncode}: {result.stderr[-2000:]}")
        if expected_error is not None and (
            result.returncode == 0 or expected_error not in result.stderr
        ):
            raise RuntimeError(
                f"Expected {expected_error}, got {result.returncode}: {result.stderr[-1200:]}"
            )
        return result.stdout

    def start(name):
        return [
            "fit",
            "--model-dir",
            base,
            "--development",
            fixtures / "development.json",
            "--protected-membership",
            fixtures / "protected-membership.json",
            "--output",
            root / name,
            "--steps",
            "8",
        ]

    def control(action, run, *options):
        return json.loads(execute([*cli, action, "--run-dir", run, *options]))

    def pause_after_four(name):
        run = root / (name + ".run")
        signal = root / (name + ".ready")
        proc = subprocess.Popen(
            [str(a) for a in [*child, *start(name)]],
            env=env | {"EVE_FIXTURE_WAIT": str(signal)},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            wait_for(lambda: (run / "state.json").exists() or proc.poll() is not None)
            if proc.poll() is not None:
                raise RuntimeError(proc.communicate()[1][-2000:])
            control("pause", run, "--after-step", "4")
            signal.touch()
            out, err = proc.communicate(timeout=180)
            if proc.returncode:
                raise RuntimeError(err[-2000:])
            return run, json.loads(out)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    execute([*child, *start("continuous")])
    run, paused = pause_after_four("interrupted")
    assert paused["status"] == "paused" and paused["completed_steps"] == 4
    assert not (root / "interrupted").exists()
    checkpoint = torch.load(run / "checkpoint.pt", map_location="cpu", weights_only=True)
    adam_steps = {int(p["step"].item()) for p in checkpoint["optimizer"]["state"].values()}
    assert adam_steps == {4} and len(checkpoint["losses"]) == 4 and len(checkpoint["cpu_rng"]) > 0
    # A new worker polling or re-admitting the same fit stays paused without importing CUDA.
    assert json.loads(execute([*cli, *start("interrupted")])) == paused
    assert control("status", run) == paused
    original = (fixtures / "development.json").read_bytes()
    altered = json.loads(original)
    altered[0]["answer"] += " Changed."
    (fixtures / "development.json").write_text(json.dumps(altered))
    execute(
        [*child, "resume", "--run-dir", run], expected_error="checkpoint_run_or_inputs_mismatch"
    )
    assert control("status", run)["status"] == "paused"
    (fixtures / "development.json").write_bytes(original)
    saved_bytes = (run / "checkpoint.pt").read_bytes()
    (run / "checkpoint.pt").write_bytes(saved_bytes[:-1])
    execute([*child, "resume", "--run-dir", run], expected_error="checkpoint_digest_mismatch")
    assert control("status", run)["status"] == "paused"
    (run / "checkpoint.pt").write_bytes(saved_bytes)
    finished = json.loads(execute([*child, "resume", "--run-dir", run]))
    assert finished["run_id"] == paused["run_id"] and finished["resumed_from_step"] == 4
    assert (
        finished["status"] == "completed"
        and finished["completed_steps"] == finished["step_cap"] == 8
    )
    assert finished["active_seconds"] >= paused["active_seconds"]
    manifests, tensors, predictions = [], [], []
    for name in ("continuous", "interrupted"):
        output = root / name
        manifests.append(eve_sft.read_file(output / "manifest.json"))
        tensors.append(load_file(output / "adapter_model.safetensors"))
        replay = root / (name + ".predictions.json")
        execute(
            [
                *child,
                "replay",
                "--model-dir",
                base,
                "--adapter",
                output,
                "--inputs",
                fixtures / "held-out-inputs.json",
                "--output",
                replay,
                "--max-new-tokens",
                "4",
            ]
        )
        predictions.append(eve_sft.read_file(replay)["predictions"])
    assert tensors[0].keys() == tensors[1].keys()
    assert all(torch.equal(tensors[0][name], tensors[1][name]) for name in tensors[0])
    assert manifests[0]["loss"] == manifests[1]["loss"] and manifests[1]["changed_parameters"] > 0
    assert predictions[0] == predictions[1] and len(predictions[1]) > 0
    stopped_run, partially_trained = pause_after_four("cancelled")
    assert partially_trained["status"] == "paused" and partially_trained["completed_steps"] == 4
    stopped = control("stop", stopped_run)
    assert stopped["status"] == "stopped" and not (root / "cancelled").exists()
    execute(
        [*cli, "resume", "--run-dir", stopped_run], expected_error="resume_requires_confirmed_pause"
    )
    return {
        "fixture": "generated tiny Mistral on CPU, float32 LoRA",
        "gpu_hours": 0,
        "run_id": paused["run_id"],
        "confirmed_pause_step": 4,
        "restored_adam_step": 4,
        "resumed_at_step": finished["resumed_from_step"] + 1,
        "completed_steps": finished["completed_steps"],
        "step_cap": finished["step_cap"],
        "active_seconds_before_pause": paused["active_seconds"],
        "active_seconds_at_completion": finished["active_seconds"],
        "parked_across_restart": True,
        "bitwise_equal_adapter_tensors": len(tensors[1]),
        "changed_parameter_values": manifests[1]["changed_parameters"],
        "matching_loss": manifests[1]["loss"],
        "matching_fresh_replay_cases": len(predictions[1]),
        "changed_input_refused": True,
        "corrupt_checkpoint_refused": True,
        "stopped_at_step": stopped["completed_steps"],
        "stop_left_no_candidate": True,
        "runtime": manifests[1]["runtime"],
    }


if __name__ == "__main__":
    if sys.argv[1:2] == ["--fixture-process"]:
        fixture_process(Path(sys.argv[2]), sys.argv[3:])
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--receipt", type=Path)
        args = parser.parse_args()
        with tempfile.TemporaryDirectory() as root:
            result = verify(Path(root))
        payload = json.dumps(result, indent=2) + "\n"
        if args.receipt:
            with args.receipt.open("x") as handle:
                handle.write(payload)
        print(payload, end="")
