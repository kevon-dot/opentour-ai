"""Isolated CPU worker for the pinned SmolLM2 supervised decision adapter."""

from __future__ import annotations

import base64
import hashlib
import importlib
import json
import math
import mmap
import os
import platform
import resource
import sys
import time
from contextlib import ExitStack
from pathlib import Path
from typing import Any

PROFILE_SHA256 = "4bf04f573c4d762f5c4288f56aca2e680ab00f9c57db183ef58dfbbbddb7804c"


def checksum(handle: Any) -> str:
    handle.seek(0)
    digest = hashlib.file_digest(handle, "sha256").hexdigest()
    handle.seek(0)
    return digest


def checked_matrix(np: Any, value: str, shape: tuple[int, int]) -> Any:
    raw = base64.b64decode(value, validate=True)
    if len(raw) != math.prod(shape) * 4:
        raise ValueError("adapter_shape_mismatch")
    matrix = np.frombuffer(raw, dtype="<f4").reshape(shape)
    if not np.isfinite(matrix).all() or not np.any(matrix):
        raise ValueError("invalid_adapter_tensor")
    return matrix


def encode_matrix(matrix: Any) -> str:
    return base64.b64encode(matrix.astype("<f4").tobytes()).decode("ascii")


class FrozenModel:
    """Read-only BF16 safetensors, evaluated in FP32 with causal grouped-query attention."""

    def __init__(self, np: Any, weight_file: Any, config: dict[str, Any], stack: ExitStack):
        self.np = np
        self.config = config
        self.mapping = stack.enter_context(
            mmap.mmap(weight_file.fileno(), 0, access=mmap.ACCESS_READ)
        )
        size = int.from_bytes(self.mapping[:8], "little")
        self.start = 8 + size
        self.tensors = json.loads(self.mapping[8 : self.start])

    def weight(self, key: str, indices: Any = None) -> Any:
        np = self.np
        tensor = self.tensors[key]
        if tensor["dtype"] != "BF16":
            raise ValueError("wrong_weight_format")
        offset = self.start + tensor["data_offsets"][0]
        matrix = np.ndarray(tensor["shape"], dtype="<u2", buffer=self.mapping, offset=offset)
        if indices is not None:
            matrix = matrix[indices]
        return (matrix.astype("<u4") << 16).view("<f4")

    def norm(self, x: Any, scale: Any) -> Any:
        np = self.np
        return (
            x
            * (1.0 / np.sqrt(np.mean(x * x, axis=-1, keepdims=True) + self.config["rms_norm_eps"]))
            * scale
        )

    def hidden(self, sequences: list[list[int]]) -> Any:
        np = self.np
        batch, length = len(sequences), max(map(len, sequences))
        positions = np.array([len(seq) - 1 for seq in sequences])
        # Right padding can never change an earlier causal token.
        ids = np.zeros((batch, length), dtype=np.int64)
        for index, seq in enumerate(sequences):
            ids[index, : len(seq)] = seq
        x = self.weight("model.embed_tokens.weight", ids)
        heads = self.config["num_attention_heads"]
        kv_heads = self.config["num_key_value_heads"]
        width = self.config["hidden_size"] // heads
        inv_freq = self.config["rope_theta"] ** (-np.arange(0, width, 2, dtype=np.float32) / width)
        freq = np.arange(length, dtype=np.float32)[:, None] * inv_freq
        phase = np.concatenate((freq, freq), axis=1)
        cos, sin = np.cos(phase), np.sin(phase)
        causal = np.triu(np.ones((length, length), dtype=bool), k=1)

        def rotary(tensor: Any) -> Any:
            halves = np.split(tensor, 2, axis=-1)
            return tensor * cos + np.concatenate((-halves[1], halves[0]), axis=-1) * sin

        for index in range(self.config["num_hidden_layers"]):
            prefix = f"model.layers.{index}."
            normalized = self.norm(x, self.weight(prefix + "input_layernorm.weight"))

            def project(
                name: str, count: int, layer: str = prefix, source: Any = normalized
            ) -> Any:
                projected = source @ self.weight(layer + "self_attn." + name + "_proj.weight").T
                return projected.reshape(batch, length, count, width).transpose(0, 2, 1, 3)

            query = rotary(project("q", heads))
            key = np.repeat(rotary(project("k", kv_heads)), heads // kv_heads, axis=1)
            value = np.repeat(project("v", kv_heads), heads // kv_heads, axis=1)
            scores = (query @ key.swapaxes(-2, -1)) / math.sqrt(width)
            scores[..., causal] = -np.inf
            probs = np.exp(scores - scores.max(axis=-1, keepdims=True))
            attention = (probs / probs.sum(axis=-1, keepdims=True)) @ value
            attention = attention.transpose(0, 2, 1, 3).reshape(batch, length, -1)
            x += attention @ self.weight(prefix + "self_attn.o_proj.weight").T
            normalized = self.norm(x, self.weight(prefix + "post_attention_layernorm.weight"))
            gate = normalized @ self.weight(prefix + "mlp.gate_proj.weight").T
            up = normalized @ self.weight(prefix + "mlp.up_proj.weight").T
            activated = (gate / (1.0 + np.exp(-np.clip(gate, -80, 80)))) * up
            x += activated @ self.weight(prefix + "mlp.down_proj.weight").T
        return self.norm(x[np.arange(batch), positions], self.weight("model.norm.weight"))


def prompt(features: dict[str, str], labels: list[str]) -> str:
    choices = ", ".join(f"{chr(65 + i)}={label}" for i, label in enumerate(labels))
    payload = json.dumps(features, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return (
        "<|im_start|>system\nClassify the record. Return one decision code.<|im_end|>\n"
        f"<|im_start|>user\nChoices: {choices}\nRecord: {payload}<|im_end|>\n"
        "<|im_start|>assistant\n"
    )


def probabilities(np: Any, logits: Any) -> Any:
    values = np.exp(logits - logits.max(axis=1, keepdims=True))
    return values / values.sum(axis=1, keepdims=True)


def fit(
    np: Any, x: Any, base: Any, y: Any, code_ids: list[int], settings: dict[str, Any]
) -> dict[str, Any]:
    count, width = x.shape
    rank = settings["rank"]
    scale = settings["alpha"] / rank
    rng = np.random.default_rng(settings["seed"])
    a = (rng.normal(size=(width, rank)) / math.sqrt(width)).astype(np.float32)
    b = np.zeros((rank, len(code_ids)), dtype=np.float32)
    a0, b0 = a.copy(), b.copy()
    moments = [(np.zeros_like(a), np.zeros_like(a)), (np.zeros_like(b), np.zeros_like(b))]
    rows = np.arange(count)
    base_logits = x @ base.T
    initial = float(-np.log(np.maximum(probabilities(np, base_logits)[rows, y], 1e-30)).mean())
    for step in range(1, settings["steps"] + 1):
        latent = x @ a
        error = probabilities(np, base_logits + scale * latent @ b)
        error[rows, y] -= 1.0
        error *= scale / count
        grads = (x.T @ (error @ b.T), latent.T @ error)
        for param, grad, (first, second) in zip((a, b), grads, moments, strict=True):
            first *= 0.9
            first += 0.1 * grad
            second *= 0.999
            second += 0.001 * grad * grad
            param -= (
                settings["learning_rate"]
                * (first / (1 - 0.9**step))
                / (np.sqrt(second / (1 - 0.999**step)) + 1e-8)
            )
    prediction = base_logits + scale * (x @ a) @ b
    loss = float(-np.log(np.maximum(probabilities(np, prediction)[rows, y], 1e-30)).mean())
    changes = [int(np.count_nonzero(a - a0)), int(np.count_nonzero(b - b0))]
    if not all(changes) or not np.isfinite(prediction).all() or not math.isfinite(loss):
        raise ValueError("no_finite_trainable_update")
    adapter = {
        "format": settings["format"],
        "dtype": "float32-le",
        "rank": rank,
        "alpha": settings["alpha"],
        "code_ids": code_ids,
        "a_shape": list(a.shape),
        "b_shape": list(b.shape),
        "a": encode_matrix(a),
        "b": encode_matrix(b),
    }
    return {
        "adapter": adapter,
        "initial_loss": initial,
        "final_loss": loss,
        "changed_parameters": changes,
        "nonzero_parameters": [int(np.count_nonzero(a)), int(np.count_nonzero(b))],
        "steps": settings["steps"],
    }


def run(request: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    limits = profile["limits"]
    resource.setrlimit(resource.RLIMIT_AS, (limits["max_address_space_bytes"],) * 2)
    resource.setrlimit(resource.RLIMIT_CPU, (request["max_seconds"],) * 2)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    import numpy as np

    tokenizers = importlib.import_module("tokenizers")

    versions = profile["runtime"]
    if (
        platform.python_version() != versions["python"]
        or platform.system() != "Linux"
        or platform.machine() != "x86_64"
        or np.__version__ != versions["numpy"]
        or tokenizers.__version__ != versions["tokenizers"]
    ):
        raise ValueError("unqualified_numeric_runtime")
    directory = Path(request["model_dir"])
    started = time.monotonic()
    with ExitStack() as stack:
        opened = {}
        for name, expected in profile["files"].items():
            file = directory / name
            if file.is_symlink() or not file.is_file():
                raise ValueError("staged_model_file_required")
            handle = stack.enter_context(file.open("rb"))
            if checksum(handle) != expected:
                raise ValueError("staged_model_hash_mismatch")
            opened[name] = handle
        if request["operation"] == "inspect_installation":
            return {
                "profile": profile["profile"],
                "checkpoint_revision": profile["revision"],
                "verified_base_sha256": profile["files"]["model.safetensors"],
                "verified_tokenizer_sha256": profile["files"]["tokenizer.json"],
            }
        tokenizer = tokenizers.Tokenizer.from_buffer(opened["tokenizer.json"].read())
        model = FrozenModel(
            np, opened["model.safetensors"], json.load(opened["config.json"]), stack
        )
        labels = request["labels"]
        code_ids = [tokenizer.token_to_id(chr(65 + i)) for i in range(len(labels))]
        base = model.weight("model.embed_tokens.weight", code_ids)
        examples = request["examples"]
        positions, inputs = [], []
        scores: list[Any] = [None] * len(examples)
        for index, example in enumerate(examples):
            seq = tokenizer.encode(prompt(example, labels), add_special_tokens=False).ids
            if len(seq) > limits["max_tokens"]:
                if request["operation"] == "fit":
                    raise ValueError("training_prompt_outside_context")
            else:
                inputs.append(seq)
                positions.append(index)
        batch = limits["batch_size"]
        hidden = [model.hidden(inputs[i : i + batch]) for i in range(0, len(inputs), batch)]
        x = (
            np.concatenate(hidden)
            if hidden
            else np.empty((0, model.config["hidden_size"]), np.float32)
        )
        if request["operation"] == "fit":
            result = fit(np, x, base, np.asarray(request["targets"]), code_ids, profile["adapter"])
        elif request["operation"] == "predict":
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
                raise ValueError("adapter_profile_mismatch")
            a = checked_matrix(np, adapter["a"], (model.config["hidden_size"], rank))
            b = checked_matrix(np, adapter["b"], (rank, len(labels)))
            logits = x @ base.T + (adapter["alpha"] / rank) * (x @ a) @ b
            if not np.isfinite(logits).all():
                raise ValueError("nonfinite_inference")
            for index, label in zip(positions, logits.argmax(axis=1).tolist(), strict=True):
                scores[index] = {"status": "automatic", "label": labels[label]}
            result = {"decisions": [value or {"status": "review"} for value in scores]}
        else:
            raise ValueError("unsupported_operation")
        if checksum(opened["model.safetensors"]) != profile["files"]["model.safetensors"]:
            raise ValueError("base_changed")
    result.update(
        peak_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
        elapsed_ms=round((time.monotonic() - started) * 1_000),
        verified_base_sha256=profile["files"]["model.safetensors"],
        pid=os.getpid(),
    )
    return result


if __name__ == "__main__":
    try:
        document = Path(sys.argv[1]).read_bytes()
        if hashlib.sha256(document).hexdigest() != PROFILE_SHA256:
            raise ValueError("unsupported_profile_lock")
        incoming = json.load(sys.stdin)
        print(
            json.dumps(run(incoming, json.loads(document)), separators=(",", ":"), allow_nan=False)
        )
    except (OSError, ValueError, KeyError, ImportError, MemoryError, TypeError) as exc:
        code = str(exc)
        if not code.isidentifier() or len(code) > 64:
            code = "invalid_or_unavailable_numeric_runtime"
        print(json.dumps({"error": code}))
        sys.exit(2)
