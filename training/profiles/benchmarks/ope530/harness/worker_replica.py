"""Instrumented replica of open_weight_worker.run() for OPE-530 stage timing.

Calls the product worker's own functions (checksum, FrozenModel, prompt, fit, checked_matrix)
in the same order as run(), adding timers only. It applies the product's checks too: the
symlink/regular-file check, the training-prompt context check (fit refuses an over-length
prompt instead of silently dropping it, so targets stay aligned with examples exactly as in the
product), the adapter-profile check and the nonfinite-logit check. Reads the request on stdin;
argv: PROFILE_JSON REPO. The orchestrator checks output parity with the product.
"""

import time

T0 = time.time()
import hashlib  # noqa: E402
import importlib.util  # noqa: E402
import json  # noqa: E402
import platform  # noqa: E402
import resource  # noqa: E402
import sys  # noqa: E402
from contextlib import ExitStack  # noqa: E402
from pathlib import Path  # noqa: E402

REPO = Path(sys.argv[2])
spec = importlib.util.spec_from_file_location(
    "open_weight_worker", REPO / "training/src/opentour_training/training/open_weight_worker.py"
)
assert spec is not None and spec.loader is not None
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


def fail(code: str) -> None:
    print(json.dumps({"error": code}))
    sys.exit(2)


def main() -> None:
    marks: dict[str, float] = {"interp_ready": time.time()}
    document = Path(sys.argv[1]).read_bytes()
    if hashlib.sha256(document).hexdigest() != worker.PROFILE_SHA256:
        fail("unsupported_profile_lock")
    profile = json.loads(document)
    request = json.load(sys.stdin)
    limits = profile["limits"]
    resource.setrlimit(resource.RLIMIT_AS, (limits["max_address_space_bytes"],) * 2)
    resource.setrlimit(resource.RLIMIT_CPU, (request["max_seconds"],) * 2)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    p = time.perf_counter
    t = p()
    import numpy as np
    import tokenizers

    d: dict[str, float] = {"import_numeric_ms": (p() - t) * 1000}
    versions = profile["runtime"]
    runtime = {
        "python": platform.python_version(),
        "numpy": np.__version__,
        "tokenizers": tokenizers.__version__,
    }
    if (
        runtime["python"] != versions["python"]
        or runtime["numpy"] != versions["numpy"]
        or runtime["tokenizers"] != versions["tokenizers"]
        or platform.system() != "Linux"
        or platform.machine() != "x86_64"
    ):
        fail("unqualified_numeric_runtime")
    directory = Path(request["model_dir"])
    operation = request["operation"]
    with ExitStack() as stack:
        t = p()
        opened = {}
        for name, expected in profile["files"].items():
            file = directory / name
            if file.is_symlink() or not file.is_file():
                fail("staged_model_file_required")
            handle = stack.enter_context(file.open("rb"))
            if worker.checksum(handle) != expected:
                fail("staged_model_hash_mismatch")
            opened[name] = handle
        d["hash_inputs_ms"] = (p() - t) * 1000
        t = p()
        tokenizer = tokenizers.Tokenizer.from_buffer(opened["tokenizer.json"].read())
        d["tokenizer_load_ms"] = (p() - t) * 1000
        t = p()
        model = worker.FrozenModel(
            np, opened["model.safetensors"], json.load(opened["config.json"]), stack
        )
        labels = request["labels"]
        code_ids = [tokenizer.token_to_id(chr(65 + i)) for i in range(len(labels))]
        base = model.weight("model.embed_tokens.weight", code_ids)
        d["model_map_ms"] = (p() - t) * 1000
        t = p()
        inputs, positions = [], []
        for index, example in enumerate(request["examples"]):
            seq = tokenizer.encode(worker.prompt(example, labels), add_special_tokens=False).ids
            if len(seq) > limits["max_tokens"]:
                if operation == "fit":
                    # Product behaviour: a training prompt outside context refuses the fit.
                    fail("training_prompt_outside_context")
            else:
                inputs.append(seq)
                positions.append(index)
        d["tokenize_ms"] = (p() - t) * 1000
        batch = limits["batch_size"]
        lengths = [len(s) for s in inputs]
        padded = sum(
            max(lengths[i : i + batch]) * len(lengths[i : i + batch])
            for i in range(0, len(lengths), batch)
        )
        t = p()
        hidden, per_batch = [], []
        for i in range(0, len(inputs), batch):
            tb = p()
            hidden.append(model.hidden(inputs[i : i + batch]))
            per_batch.append(round((p() - tb) * 1000, 1))
        x = (
            np.concatenate(hidden)
            if hidden
            else np.empty((0, model.config["hidden_size"]), np.float32)
        )
        d["frozen_forward_ms"] = (p() - t) * 1000
        t = p()
        out: dict = {}
        if operation == "fit":
            # Targets index the same rows as examples; fit refused above if any was dropped.
            targets = np.asarray(request["targets"])
            res = worker.fit(np, x, base, targets, code_ids, profile["adapter"])
            out["adapter"] = res["adapter"]
            out["final_loss"] = res["final_loss"]
            d["optimizer_steps_ms"] = (p() - t) * 1000
        elif operation == "predict":
            adapter = request["adapter"]
            rank = profile["adapter"]["rank"]
            if (
                adapter["format"] != profile["adapter"]["format"]
                or adapter["dtype"] != "float32-le"
                or adapter["rank"] != rank
                or adapter["alpha"] != profile["adapter"]["alpha"]
                or adapter["code_ids"] != code_ids
                or adapter["a_shape"] != [model.config["hidden_size"], rank]
                or adapter["b_shape"] != [rank, len(labels)]
            ):
                fail("adapter_profile_mismatch")
            a = worker.checked_matrix(np, adapter["a"], (model.config["hidden_size"], rank))
            b = worker.checked_matrix(np, adapter["b"], (rank, len(labels)))
            logits = x @ base.T + (adapter["alpha"] / rank) * (x @ a) @ b
            if not np.isfinite(logits).all():
                fail("nonfinite_inference")
            scores: list = [None] * len(request["examples"])
            for index, label in zip(positions, logits.argmax(axis=1).tolist(), strict=True):
                scores[index] = {"status": "automatic", "label": labels[label]}
            out["decisions"] = [v or {"status": "review"} for v in scores]
            d["score_ms"] = (p() - t) * 1000
        else:
            fail("unsupported_operation")
        t = p()
        if worker.checksum(opened["model.safetensors"]) != profile["files"]["model.safetensors"]:
            fail("base_changed")
        d["rehash_base_ms"] = (p() - t) * 1000
    marks["done"] = time.time()
    print(
        json.dumps(
            {
                "t0_epoch": T0,
                "marks": marks,
                "durations_ms": {k: round(v, 1) for k, v in d.items()},
                "tokens": {
                    "examples": len(inputs),
                    "lengths": lengths,
                    "useful_tokens": sum(lengths),
                    "padded_tokens": padded,
                    "padding_fraction": round(1 - sum(lengths) / padded, 4) if padded else 0,
                    "batch_size": batch,
                    "per_batch_forward_ms": per_batch,
                },
                "runtime": runtime,
                "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
                **out,
            }
        )
    )


main()
