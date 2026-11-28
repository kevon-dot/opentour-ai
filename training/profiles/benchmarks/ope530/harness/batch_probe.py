"""Batch-size probe behind the OPE-530 opportunity bound for length-bucketed batches.

Measures, in one warm worker-like process, the frozen forward pass over the generated fit
prompts as the incumbent recipe runs it (batches of limits.batch_size = 4, here two batches)
against the largest token-budgeted batch the fixture allows (all prompts in one batch). The
order alternates per trial. Hidden states must be identical between the two layouts.
Uses the product worker's own checksum, FrozenModel and prompt. Product code is not modified.
argv: PROFILE_JSON REPO MODEL_DIR WORK_DIR TRIALS
"""

import hashlib
import importlib.util
import json
import resource
import sys
import time
from contextlib import ExitStack
from pathlib import Path

profile_path, repo, model_dir, work = (Path(a) for a in sys.argv[1:5])
trials = int(sys.argv[5])
spec = importlib.util.spec_from_file_location(
    "open_weight_worker", repo / "training/src/opentour_training/training/open_weight_worker.py"
)
assert spec is not None and spec.loader is not None
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


def main() -> None:
    document = profile_path.read_bytes()
    if hashlib.sha256(document).hexdigest() != worker.PROFILE_SHA256:
        raise SystemExit("unsupported_profile_lock")
    profile = json.loads(document)
    limits = profile["limits"]
    resource.setrlimit(resource.RLIMIT_AS, (limits["max_address_space_bytes"],) * 2)
    import numpy as np
    import tokenizers

    grant = json.loads((work / "grant.json").read_bytes())
    rows = sorted(json.loads((work / "training.json").read_bytes()), key=lambda r: r["case_id"])
    labels = grant["labels"]
    p = time.perf_counter
    with ExitStack() as stack:
        opened = {}
        for name, expected in profile["files"].items():
            handle = stack.enter_context((model_dir / name).open("rb"))
            if worker.checksum(handle) != expected:
                raise SystemExit("staged_model_hash_mismatch")
            opened[name] = handle
        tokenizer = tokenizers.Tokenizer.from_buffer(opened["tokenizer.json"].read())
        model = worker.FrozenModel(
            np, opened["model.safetensors"], json.load(opened["config.json"]), stack
        )
        inputs = [
            tokenizer.encode(worker.prompt(r["features"], labels), add_special_tokens=False).ids
            for r in rows
        ]
        batch = limits["batch_size"]

        def incumbent() -> object:
            return np.concatenate(
                [model.hidden(inputs[i : i + batch]) for i in range(0, len(inputs), batch)]
            )

        def merged() -> object:
            return model.hidden(inputs)

        # One untimed warm-up of each layout so both run with mapped, resident pages.
        reference = incumbent()
        candidate = merged()
        max_abs_diff = float(np.max(np.abs(reference - candidate)))
        results = []
        for trial in range(trials):
            order = ("incumbent", "merged") if trial % 2 == 0 else ("merged", "incumbent")
            times: dict[str, float] = {}
            for name in order:
                t = p()
                out = incumbent() if name == "incumbent" else merged()
                times[name] = round((p() - t) * 1000, 1)
                max_abs_diff = max(max_abs_diff, float(np.max(np.abs(reference - out))))
            results.append(
                {
                    "trial": trial,
                    "order": list(order),
                    "incumbent_ms": times["incumbent"],
                    "merged_ms": times["merged"],
                    "saving_ms": round(times["incumbent"] - times["merged"], 1),
                }
            )
    lengths = [len(s) for s in inputs]
    print(
        json.dumps(
            {
                "examples": len(inputs),
                "lengths": lengths,
                "incumbent_batches": [
                    len(inputs[i : i + batch]) for i in range(0, len(inputs), batch)
                ],
                "merged_batches": [len(inputs)],
                "max_abs_hidden_diff": max_abs_diff,
                "trials": results,
                "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
            }
        )
    )


main()
