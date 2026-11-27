"""Instrumented replica of open_weight_worker.run() for OPE-530 stage timing.

Calls the product worker's own functions (prompt, FrozenModel, fit) in the same order as
run(), adding timers only. Reads the request on stdin and the profile path as argv[1],
exactly like the product worker. The orchestrator checks output parity with the product.
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
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


def main() -> None:
    marks: dict[str, float] = {"interp_ready": time.time()}
    document = Path(sys.argv[1]).read_bytes()
    assert hashlib.sha256(document).hexdigest() == worker.PROFILE_SHA256
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
    qualified = (
        runtime["python"] == versions["python"]
        and runtime["numpy"] == versions["numpy"]
        and runtime["tokenizers"] == versions["tokenizers"]
        and platform.system() == "Linux"
        and platform.machine() == "x86_64"
    )
    if not qualified:
        raise SystemExit(json.dumps({"error": "unqualified_numeric_runtime", **runtime}))
    directory = Path(request["model_dir"])
    with ExitStack() as stack:
        t = p()
        opened = {}
        for name, expected in profile["files"].items():
            handle = stack.enter_context((directory / name).open("rb"))
            assert worker.checksum(handle) == expected
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
            if len(seq) <= limits["max_tokens"]:
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
        x = np.concatenate(hidden)
        d["frozen_forward_ms"] = (p() - t) * 1000
        t = p()
        out: dict = {}
        if request["operation"] == "fit":
            res = worker.fit(
                np, x, base, np.asarray(request["targets"]), code_ids, profile["adapter"]
            )
            out["adapter"] = res["adapter"]
            out["final_loss"] = res["final_loss"]
            d["optimizer_steps_ms"] = (p() - t) * 1000
        else:
            adapter = request["adapter"]
            rank = profile["adapter"]["rank"]
            a = worker.checked_matrix(np, adapter["a"], (model.config["hidden_size"], rank))
            b = worker.checked_matrix(np, adapter["b"], (rank, len(labels)))
            logits = x @ base.T + (adapter["alpha"] / rank) * (x @ a) @ b
            scores = [None] * len(request["examples"])
            for index, label in zip(positions, logits.argmax(axis=1).tolist(), strict=True):
                scores[index] = {"status": "automatic", "label": labels[label]}
            out["decisions"] = [v or {"status": "review"} for v in scores]
            d["score_ms"] = (p() - t) * 1000
        t = p()
        assert worker.checksum(opened["model.safetensors"]) == profile["files"]["model.safetensors"]
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
