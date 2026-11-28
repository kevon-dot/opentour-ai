"""Owner-local EVE QLoRA rehearsal with separate fit and fresh replay entry points."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import os
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from opentour_training.training.eve_run import ADAPTER_FILES, Run, file_hashes, sync_directory

BASE_REVISION = "b4965c4acbb27299078e4f97171530644fcc839f"
BASE_SHA256 = "150815d5b113536a83f24099528794686055675953cfcc14cdc6dbdadd86edfc"
# SHA-256 of the exact companion bytes at the same public Hugging Face revision.
COMPANION_SHA256 = {
    "config.json": "e9574c34dcf37fae40421e1ec28290c130edca69e71fb5445477a6bbe413450a",
    "tokenizer_config.json": "07a2f401f0bf58141834abe743dd370b833f675e507540097d88c262cb65576c",
    "tokenizer.json": "286acad9b0e27fce778ac429763536accf618ccb6ed72963b6f94685e531c5c7",
    "chat_template.jinja": "c7b796d8290d47053174a4de00a5c3091cb5584fdb9d681b10bdc30c2ea3110d",
    "generation_config.json": "08c2c8aeb78969016addd4f8e3205a1f0713cce2e29f23634e725c311b8c6348",
}
MODEL_FILES = (
    "model.safetensors",
    "config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "chat_template.jinja",
    "generation_config.json",
)
TARGET_MODULES = ("q_proj", "k_proj", "v_proj", "o_proj")
MAX_TOKENS = 2048
LORA_RANK = 16
LORA_ALPHA = 32
LEARNING_RATE = 2e-4
SEED = 42


def digest(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def read_file(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def verify_base(directory: Path) -> dict[str, str]:
    hashes = file_hashes(directory, MODEL_FILES)
    if hashes["model.safetensors"] != BASE_SHA256:
        raise ValueError("eve_checkpoint_digest_mismatch")
    config = read_file(directory / "config.json")
    if config.get("architectures") != ["MistralForCausalLM"]:
        raise ValueError("unexpected_eve_architecture")
    if any(hashes[name] != expected for name, expected in COMPANION_SHA256.items()):
        raise ValueError("eve_companion_digest_mismatch")
    return hashes


def validate_messages(messages: Any) -> list[dict[str, str]]:
    if not isinstance(messages, list) or not all(isinstance(m, dict) for m in messages):
        raise ValueError("invalid_prompt_messages")
    if [m.get("role") for m in messages] not in (["user"], ["system", "user"]):
        raise ValueError("expected_one_user_and_optional_system_message")
    for message in messages:
        if set(message) != {"role", "content"} or not isinstance(message["content"], str):
            raise ValueError("invalid_prompt_message")
        if not message["content"].strip():
            raise ValueError("empty_prompt_message")
    return messages


def encode(tokenizer: Any, messages: list[dict[str, str]], *, generation: bool) -> list[int]:
    rendered = tokenizer.apply_chat_template(
        messages, tokenize=False, add_generation_prompt=generation
    )
    tokens = tokenizer.encode(rendered, add_special_tokens=False, truncation=False)
    if not isinstance(tokens, list) or not all(type(token) is int for token in tokens):
        raise ValueError("expected_integer_token_ids")
    return tokens


def completion_example(
    tokenizer: Any, messages: list[dict[str, str]], answer: str
) -> dict[str, list[int]]:
    prefix = encode(tokenizer, messages, generation=True)
    full = encode(
        tokenizer, [*messages, {"role": "assistant", "content": answer}], generation=False
    )
    if not prefix or full[: len(prefix)] != prefix or len(full) <= len(prefix):
        raise ValueError("unverified_assistant_token_boundary")
    if len(full) > MAX_TOKENS:
        raise ValueError("training_example_exceeds_context")
    return {"input_ids": full, "labels": [-100] * len(prefix) + full[len(prefix) :]}


def fit_examples(tokenizer: Any, rows: Any, protected: Any) -> list[dict[str, list[int]]]:
    if not isinstance(rows, list) or not rows or len(rows) > 128:
        raise ValueError("invalid_development_dataset")
    if not isinstance(protected, dict) or set(protected) != {
        "case_refs",
        "family_refs",
        "prompt_digests",
    }:
        raise ValueError("protected_membership_required")
    if not all(
        isinstance(group, list)
        and group
        and all(isinstance(item, str) and item.strip() for item in group)
        for group in protected.values()
    ):
        raise ValueError("invalid_protected_membership")

    cases_seen = set(protected["case_refs"])
    prompts_seen = set(protected["prompt_digests"])
    protected_families = set(protected["family_refs"])
    examples = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {
            "case_ref",
            "family_ref",
            "messages",
            "answer",
        }:
            raise ValueError("invalid_development_row")
        case, family, answer = (row[key] for key in ("case_ref", "family_ref", "answer"))
        if not all(isinstance(value, str) and value.strip() for value in (case, family, answer)):
            raise ValueError("missing_case_family_or_answer")
        messages = validate_messages(row["messages"])
        prompt_digest = digest(messages)
        if case in cases_seen or family in protected_families or prompt_digest in prompts_seen:
            raise ValueError("duplicate_or_protected_training_example")
        examples.append(completion_example(tokenizer, messages, answer))
        cases_seen.add(case)
        prompts_seen.add(prompt_digest)
    return examples


def load_model(torch: Any, model_dir: Path) -> tuple[Any, Any]:
    transformers = importlib.import_module("transformers")

    options = transformers.BitsAndBytesConfig(
        load_in_4bit=True, bnb_4bit_quant_type="nf4", bnb_4bit_compute_dtype=torch.bfloat16
    )
    model = transformers.AutoModelForCausalLM.from_pretrained(
        model_dir,
        local_files_only=True,
        trust_remote_code=False,
        use_safetensors=True,
        quantization_config=options,
        device_map={"": 0},
        dtype=torch.bfloat16,
    )
    tokenizer = transformers.AutoTokenizer.from_pretrained(
        model_dir, local_files_only=True, trust_remote_code=False
    )
    return model, tokenizer


def runtime() -> Any:
    torch = importlib.import_module("torch")

    if not torch.cuda.is_available() or not torch.cuda.is_bf16_supported():
        raise ValueError("compatible_cuda_gpu_required")
    return torch


def training_recipe() -> dict[str, Any]:
    return {
        "quantization": "nf4",
        "compute_dtype": "bfloat16",
        "rank": LORA_RANK,
        "alpha": LORA_ALPHA,
        "target_modules": TARGET_MODULES,
        "learning_rate": LEARNING_RATE,
        "max_tokens": MAX_TOKENS,
    }


def runtime_versions() -> dict[str, str]:
    return {
        name: importlib.metadata.version(name)
        for name in ("torch", "transformers", "peft", "bitsandbytes")
    }


def fit(args: argparse.Namespace) -> None:
    settings = None
    if args.action == "fit":
        if not args.output.parent.is_dir():
            raise FileNotFoundError(args.output.parent)
        paths = ("model_dir", "development", "protected_membership", "output")
        settings = {name: str(getattr(args, name).resolve()) for name in paths}
        settings.update(steps=args.steps, deadline=args.deadline)
        directory = args.run_dir or args.output.with_name(args.output.name + ".run")
        directory.mkdir(mode=0o700, exist_ok=True)
    else:
        directory = args.run_dir
    run = Run(directory)
    with run.worker() as acquired:
        if not acquired:
            raise ValueError("trainer_already_active")
        if settings is not None and not run.start(settings):
            print(json.dumps(run.control("status")))
            return
        with run.edit() as state:
            if not state:
                raise ValueError("unknown_run")
            if settings is None:
                run.reconcile_idle(state)
            run.expire(state)
            if settings is None and state["status"] not in {"paused", "expired"}:
                raise ValueError("resume_requires_confirmed_pause")
            if state["status"] == "stopping":
                state["status"] = "stopped"
            if state["status"] in {"stopped", "expired"}:
                print(json.dumps(run.summary(state)))
                return
            saved = state.copy()
            if settings is None:
                state.update(status="resuming", pause_at=None)
        options = saved["settings"]
        training = argparse.Namespace(
            **{
                k: Path(options[k])
                for k in ("model_dir", "development", "protected_membership", "output")
            },
            steps=options["steps"],
        )
        started = time.monotonic()

        def elapsed() -> float:
            return float(saved["active_seconds"]) + (time.monotonic() - started)

        try:
            train(training, run, saved, elapsed)
        except Exception:
            with run.edit() as state:
                if state["status"] == "resuming":
                    state["status"] = "paused"
                else:
                    run.reconcile_idle(state)
            raise
        finally:
            with run.edit() as state:
                state["active_seconds"] = elapsed()
        print(json.dumps(run.control("status")))


def train(
    args: argparse.Namespace, run: Run, saved: dict[str, Any], elapsed: Callable[[], float]
) -> None:
    torch = runtime()
    peft = importlib.import_module("peft")

    base_hashes = verify_base(args.model_dir)
    rows, protected = read_file(args.development), read_file(args.protected_membership)
    torch.manual_seed(SEED)
    torch.cuda.reset_peak_memory_stats()
    model, tokenizer = load_model(torch, args.model_dir)
    examples = fit_examples(tokenizer, rows, protected)
    model = peft.prepare_model_for_kbit_training(model, use_gradient_checkpointing=True)
    model.config.use_cache = False
    model = peft.get_peft_model(
        model,
        peft.LoraConfig(
            task_type="CAUSAL_LM",
            r=LORA_RANK,
            lora_alpha=LORA_ALPHA,
            target_modules=list(TARGET_MODULES),
        ),
    )
    trainable = {name: p for name, p in model.named_parameters() if p.requires_grad}
    if not trainable or any(".lora_" not in name for name in trainable):
        raise ValueError("unexpected_trainable_parameters")
    before = {name: parameter.detach().clone() for name, parameter in trainable.items()}
    optimizer = torch.optim.AdamW(trainable.values(), lr=LEARNING_RATE)
    device = model.get_input_embeddings().weight.device
    model.train()
    recipe = training_recipe()
    versions = runtime_versions()
    identity = digest(
        [
            saved["run_id"],
            saved["settings"],
            BASE_REVISION,
            base_hashes,
            digest(rows),
            digest(protected),
            recipe,
            SEED,
            versions,
        ]
    )
    losses: list[float] = []
    checkpoint_path = run.directory / "checkpoint.pt"
    if saved["status"] == "paused":
        with checkpoint_path.open("rb") as handle:
            if hashlib.file_digest(handle, "sha256").hexdigest() != saved["checkpoint"]["sha256"]:
                raise ValueError("checkpoint_digest_mismatch")
            handle.seek(0)
            checkpoint = torch.load(handle, map_location="cpu", weights_only=True)
        losses = checkpoint["losses"]
        if (
            checkpoint["identity"] != identity
            or len(losses) != saved["completed_steps"]
            or checkpoint["weights"].keys() != trainable.keys()
        ):
            raise ValueError("checkpoint_run_or_inputs_mismatch")
        with torch.no_grad():
            for name, parameter in trainable.items():
                parameter.copy_(checkpoint["weights"][name])
        optimizer.load_state_dict(checkpoint["optimizer"])
        torch.set_rng_state(checkpoint["cpu_rng"])
        if device.type == "cuda":
            torch.cuda.set_rng_state(checkpoint["device_rng"], device)
        del checkpoint
        with run.edit() as state:
            run.expire(state)
            if state["status"] == "stopping":
                state["status"] = "stopped"
            if state["status"] != "resuming":
                return
            state.update(
                status="running" if state["pause_at"] is None else "pausing",
                resumed_from_step=len(losses),
            )

    def save_checkpoint() -> dict[str, Any]:
        payload = {
            "identity": identity,
            "losses": losses,
            "weights": {name: p.detach().cpu() for name, p in trainable.items()},
            "optimizer": optimizer.state_dict(),
            "cpu_rng": torch.get_rng_state(),
            "device_rng": torch.cuda.get_rng_state(device) if device.type == "cuda" else None,
        }
        with tempfile.NamedTemporaryFile(dir=run.directory, mode="w+b", delete=False) as tmp:
            try:
                torch.save(payload, tmp)
                tmp.flush()
                os.fsync(tmp.fileno())
                tmp.seek(0)
                checksum = hashlib.file_digest(tmp, "sha256").hexdigest()
                os.replace(tmp.name, checkpoint_path)
                sync_directory(run.directory)
            finally:
                Path(tmp.name).unlink(missing_ok=True)
        return {"sha256": checksum}

    def at_boundary(state: dict[str, Any]) -> bool:
        return run.boundary(
            state, len(losses), elapsed, torch.cuda.max_memory_allocated(), save_checkpoint
        )

    for step in range(len(losses), args.steps + 1):
        with run.edit() as state:
            if at_boundary(state):
                return
        if step == args.steps:
            break
        batch = {
            key: torch.tensor([value], device=device, dtype=torch.long)
            for key, value in examples[step % len(examples)].items()
        }
        optimizer.zero_grad(set_to_none=True)
        loss = model(**batch).loss
        value = loss.item()
        if not math.isfinite(value):
            raise ValueError("nonfinite_training_loss")
        loss.backward()
        optimizer.step()
        losses.append(value)
    changed = sum(
        (parameter.detach() != before[name]).sum().item() for name, parameter in trainable.items()
    )
    if not changed or any(not torch.isfinite(p).all().item() for p in trainable.values()):
        raise ValueError("no_finite_adapter_update")
    if verify_base(args.model_dir) != base_hashes:
        raise ValueError("base_changed_during_fit")

    with tempfile.TemporaryDirectory(dir=args.output.parent, prefix=".eve-fit-") as stage:
        candidate = Path(stage) / "candidate"
        candidate.mkdir(mode=0o700)
        model.save_pretrained(candidate, safe_serialization=True)
        manifest = {
            "schema": "opentour.training.eve-rehearsal.v1",
            "base_revision": BASE_REVISION,
            "base_files": base_hashes,
            "adapter_files": file_hashes(candidate, ADAPTER_FILES),
            "development_digest": digest(rows),
            "protected_membership_digest": digest(protected),
            "steps": args.steps,
            "seed": SEED,
            "changed_parameters": changed,
            "loss": {"first": losses[0], "last": losses[-1]},
            "recipe": recipe,
            "trainable_parameters": sum(p.numel() for p in trainable.values()),
            "gpu": torch.cuda.get_device_name(),
            "peak_gpu_bytes": max(saved["peak_gpu_bytes"], torch.cuda.max_memory_allocated()),
            "elapsed_seconds": round(elapsed(), 3),
            "run_id": saved["run_id"],
            "runtime": versions,
        }
        (candidate / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        for name in (*ADAPTER_FILES, "manifest.json"):
            with (candidate / name).open("rb") as handle:
                os.fsync(handle.fileno())
        sync_directory(candidate)
        with run.edit() as state:
            if at_boundary(state):
                return
            publish_candidate(candidate, args.output)
            state["status"] = "completed"


def publish_candidate(candidate: Path, output: Path) -> None:
    output.mkdir(mode=0o700)
    try:
        os.replace(candidate, output)
    except OSError:
        output.rmdir()
        raise
    sync_directory(output.parent)


def replay(args: argparse.Namespace) -> None:
    peft = importlib.import_module("peft")

    torch = runtime()
    base_hashes = verify_base(args.model_dir)
    manifest = read_file(args.adapter / "manifest.json")
    if manifest["base_revision"] != BASE_REVISION or manifest["base_files"] != base_hashes:
        raise ValueError("adapter_base_or_tokenizer_mismatch")
    if manifest["adapter_files"] != file_hashes(args.adapter, ADAPTER_FILES):
        raise ValueError("adapter_digest_mismatch")
    model, tokenizer = load_model(torch, args.model_dir)
    model = peft.PeftModel.from_pretrained(model, args.adapter, is_trainable=False)
    model.eval()
    device = model.get_input_embeddings().weight.device
    results = []
    case_refs = set()
    rows = read_file(args.inputs)
    if not isinstance(rows, list) or not 1 <= len(rows) <= 128:
        raise ValueError("invalid_replay_dataset")
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"case_ref", "messages"}:
            raise ValueError("expected_unlabeled_replay_input")
        case_ref = row["case_ref"]
        if not isinstance(case_ref, str) or not case_ref.strip() or case_ref in case_refs:
            raise ValueError("invalid_or_duplicate_replay_case")
        case_refs.add(case_ref)
        tokens = encode(tokenizer, validate_messages(row["messages"]), generation=True)
        if not tokens or len(tokens) + args.max_new_tokens > MAX_TOKENS:
            raise ValueError("replay_input_exceeds_context")
        input_ids = torch.tensor([tokens], dtype=torch.long, device=device)
        with torch.inference_mode():
            with model.disable_adapter():
                baseline = generate(model, tokenizer, input_ids, args.max_new_tokens)
            candidate = generate(model, tokenizer, input_ids, args.max_new_tokens)
        results.append({"case_ref": row["case_ref"], "baseline": baseline, "candidate": candidate})
    payload = {
        "base_revision": BASE_REVISION,
        "adapter_files": manifest["adapter_files"],
        "max_new_tokens": args.max_new_tokens,
        "predictions": results,
    }
    with open(args.output, "x", encoding="utf-8", opener=private_open) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def private_open(path: str, flags: int) -> int:
    return os.open(path, flags, 0o600)


def generate(model: Any, tokenizer: Any, input_ids: Any, max_new_tokens: int) -> str:
    output = model.generate(
        input_ids,
        attention_mask=input_ids.new_ones(input_ids.shape),
        do_sample=False,
        max_new_tokens=max_new_tokens,
        pad_token_id=tokenizer.eos_token_id,
    )
    text = tokenizer.decode(output[0, input_ids.shape[1] :], skip_special_tokens=True)
    if not isinstance(text, str):
        raise ValueError("expected_decoded_text")
    return text


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    fit_cli = commands.add_parser("fit")
    for name in ("model-dir", "development", "protected-membership", "output"):
        fit_cli.add_argument(f"--{name}", required=True, type=Path)
    fit_cli.add_argument("--steps", type=int, choices=range(8, 17), default=8)
    fit_cli.add_argument(
        "--run-dir", type=Path, help="Private state directory (default: OUTPUT.run)"
    )
    fit_cli.add_argument("--deadline", type=int, help="Absolute Unix cutoff, including pauses")
    fit_cli.set_defaults(run=fit)
    for name in ("pause", "resume", "stop", "status"):
        control_cli = commands.add_parser(name)
        control_cli.add_argument("--run-dir", type=Path, required=True)
        if name == "pause":
            control_cli.add_argument(
                "--after-step", type=int, help="Pause at this optimizer boundary"
            )
    replay_cli = commands.add_parser("replay")
    for name in ("model-dir", "adapter", "inputs", "output"):
        replay_cli.add_argument(f"--{name}", required=True, type=Path)
    replay_cli.add_argument(
        "--max-new-tokens", type=int, choices=range(1, 513), default=128, metavar="N"
    )
    replay_cli.set_defaults(run=replay)
    args = parser.parse_args()
    try:
        if args.action == "resume":
            fit(args)
        elif args.action in {"pause", "stop", "status"}:
            print(
                json.dumps(
                    Run(args.run_dir).control(args.action, getattr(args, "after_step", None))
                )
            )
        else:
            args.run(args)
    except (ImportError, KeyError, OSError, RuntimeError, TypeError, ValueError) as error:
        reason = (
            str(error)
            if isinstance(error, ValueError) and str(error).isidentifier()
            else type(error).__name__
        )
        parser.exit(1, f"EVE rehearsal held ({reason})\n")


if __name__ == "__main__":
    main()
