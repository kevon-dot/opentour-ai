"""Generated toy model-family worker for software conformance only.

This is NOT a real model. It provides two deliberately different generated architectures so the
connector, supervision and qualification harness can be exercised with a genuine parameter update
in a fresh process, with no GPU, network or customer data.

* ``generated-toy-linear``: logits = W x, adapter on layer ``head``.
* ``generated-toy-normmlp``: logits = W2 tanh(LayerNorm(W1 x)) + Ws x, adapter on layer ``w2``.

Protocol: argv[1] is the family profile JSON, stdin one JSON request, stdout one JSON reply.
The pinned base is only read, never written; its digest is reported before and after.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import sys
import time
from pathlib import Path

RANK = 2
SCALE = 1.0
LEARNING_RATE = 0.5


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _matvec(matrix: list[list[float]], vector: list[float]) -> list[float]:
    return [sum(w * v for w, v in zip(row, vector, strict=True)) for row in matrix]


def _features(base: dict, tokens: dict, text: str) -> list[float]:
    vector = [0.0] * len(tokens)
    for word in text.lower().split():
        if word in tokens:
            vector[tokens[word]] += 1.0
    if base["architecture"] == "generated-toy-linear":
        return vector
    hidden = _matvec(base["w1"], vector)
    mean = sum(hidden) / len(hidden)
    variance = sum((h - mean) ** 2 for h in hidden) / len(hidden)
    return [math.tanh((h - mean) / math.sqrt(variance + 1e-5)) for h in hidden]


def _base_logits(base: dict, tokens: dict, text: str, phi: list[float]) -> list[float]:
    if base["architecture"] == "generated-toy-linear":
        return _matvec(base["head"], phi)
    raw = [0.0] * len(tokens)
    for word in text.lower().split():
        if word in tokens:
            raw[tokens[word]] += 1.0
    skip = _matvec(base["ws"], raw)
    out = _matvec(base["w2"], phi)
    return [a + b for a, b in zip(out, skip, strict=True)]


def _softmax(logits: list[float]) -> list[float]:
    top = max(logits)
    exps = [math.exp(v - top) for v in logits]
    total = sum(exps)
    return [e / total for e in exps]


def _delta(adapter: dict, phi: list[float]) -> list[float]:
    projected = _matvec(adapter["v"], phi)
    return [SCALE * v for v in _matvec(adapter["u"], projected)]


def main() -> None:
    profile = json.loads(Path(sys.argv[1]).read_bytes())
    request = json.load(sys.stdin)
    model_dir = Path(request["model_dir"])
    base_path = model_dir / "base.json"
    before = _digest(base_path)
    base = json.loads(base_path.read_bytes())
    tokens = json.loads((model_dir / "tokenizer.json").read_bytes())["tokens"]
    labels = base["labels"]
    target = profile["layer_targets"][0]
    if target not in base["layers"]:
        print(json.dumps({"error": "LAYER_TARGET_UNSUPPORTED"}))
        sys.exit(2)

    def sample(text: str) -> tuple[list[float], list[float]]:
        phi = _features(base, tokens, text)
        return phi, _base_logits(base, tokens, text, phi)

    if request["operation"] == "predict":
        adapter = request["adapter"]
        predictions = []
        for item in request["inputs"]:
            phi, logits = sample(item["text"])
            if adapter is not None:
                logits = [a + b for a, b in zip(logits, _delta(adapter, phi), strict=True)]
            best = max(range(len(labels)), key=lambda i: (logits[i], -i))
            predictions.append({"label": labels[best]})
        print(json.dumps({"predictions": predictions}))
        return

    rng = random.Random(base["seed"])
    width = len(sample("x")[0])
    adapter = {
        "target": target,
        "rank": RANK,
        "u": [[0.0] * RANK for _ in labels],
        "v": [[rng.uniform(-0.3, 0.3) for _ in range(width)] for _ in range(RANK)],
    }
    initial = json.dumps(adapter, sort_keys=True)
    rows = [(sample(r["text"]), labels.index(r["label"])) for r in request["rows"]]
    delay = request.get("step_delay_ms", 0) / 1000.0
    first_loss = last_loss = 0.0
    for step in range(request["steps"]):
        total = 0.0
        grad_u = [[0.0] * RANK for _ in labels]
        grad_v = [[0.0] * width for _ in range(RANK)]
        for (phi, logits), target_index in rows:
            projected = _matvec(adapter["v"], phi)
            delta = [SCALE * v for v in _matvec(adapter["u"], projected)]
            probs = _softmax([a + b for a, b in zip(logits, delta, strict=True)])
            total -= math.log(max(probs[target_index], 1e-12))
            err = [p - (1.0 if i == target_index else 0.0) for i, p in enumerate(probs)]
            for i, e in enumerate(err):
                for k in range(RANK):
                    grad_u[i][k] += SCALE * e * projected[k]
            back = [
                SCALE * sum(adapter["u"][i][k] * err[i] for i in range(len(labels)))
                for k in range(RANK)
            ]
            for k in range(RANK):
                for j in range(width):
                    grad_v[k][j] += back[k] * phi[j]
        for i in range(len(labels)):
            for k in range(RANK):
                adapter["u"][i][k] -= LEARNING_RATE * grad_u[i][k] / len(rows)
        for k in range(RANK):
            for j in range(width):
                adapter["v"][k][j] -= LEARNING_RATE * grad_v[k][j] / len(rows)
        if step == 0:
            first_loss = total / len(rows)
        last_loss = total / len(rows)
        if delay:
            time.sleep(delay)
    start = json.loads(initial)
    squared = sum(
        (a - b) ** 2
        for name in ("u", "v")
        for ra, rb in zip(adapter[name], start[name], strict=True)
        for a, b in zip(ra, rb, strict=True)
    )
    evidence = {
        "loss_first": round(first_loss, 9),
        "loss_last": round(last_loss, 9),
        "parameter_delta_l2": round(math.sqrt(squared), 9),
        "parameters_changed": sum(
            1
            for name in ("u", "v")
            for ra, rb in zip(adapter[name], start[name], strict=True)
            for a, b in zip(ra, rb, strict=True)
            if a != b
        ),
        "base_sha256_before": before,
        "base_sha256_after": _digest(base_path),
        "steps": request["steps"],
    }
    print(json.dumps({"adapter": adapter, "evidence": evidence}))


if __name__ == "__main__":
    main()
