"""Small, deterministic CPU training profile for owner-local text classification."""

from __future__ import annotations

import hashlib
import re
import time
import unicodedata
from collections import Counter
from collections.abc import Sequence
from typing import Any

PROFILE = "cpu_text_v1"
DIMENSIONS = 257  # Index zero is the intercept. The remaining features are hashed words.
CPU_LIMIT_SECONDS = 3
MAX_TRAIN_ROWS = 256
MAX_HELDOUT_ROWS = 128
MAX_TOKENS = 48
EPOCH_CHOICES = (1, 3, 5)


class TrainingError(ValueError):
    """A fixed error code safe to display without exposing owner records."""


def words(text: str) -> list[str]:
    tokens = re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", text).casefold())
    if not 1 <= len(tokens) <= MAX_TOKENS:
        raise TrainingError("input_outside_profile")
    return tokens


def _features(text: str) -> dict[int, int]:
    counts: Counter[int] = Counter()
    for token in words(text):
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        counts[1 + int.from_bytes(digest[:2], "big") % (DIMENSIONS - 1)] += 1
    return {0: 1, **{index: min(count, 3) for index, count in counts.items()}}


def feature_signature(text: str) -> list[tuple[int, int]]:
    """The exact model-visible feature vector, including collisions and clipped counts."""
    return sorted(_features(text).items())


def _best(weights: list[list[int]], features: dict[int, int]) -> int:
    scores = [sum(row[index] * count for index, count in features.items()) for row in weights]
    return scores.index(max(scores))


def check_budget(started: float) -> None:
    if time.thread_time() - started > CPU_LIMIT_SECONDS:
        raise TrainingError("compute_limit_reached")


def fit(
    training: Sequence[tuple[str, str]], epochs: int, started: float
) -> tuple[dict[str, Any], int]:
    """Fit only from training (text, label) pairs. No held-out argument or data access."""
    labels = sorted({label for _, label in training})
    if not 2 <= len(labels) <= 8 or not 2 <= len(training) <= MAX_TRAIN_ROWS:
        raise TrainingError("training_input_required")
    if type(epochs) is not int or epochs not in EPOCH_CHOICES:
        raise TrainingError("input_outside_profile")
    weights = [[0] * DIMENSIONS for _ in labels]
    observations = [(_features(text), labels.index(label)) for text, label in training]
    updates = 0
    for _ in range(epochs):
        for features, target in observations:
            check_budget(started)
            predicted = _best(weights, features)
            if predicted == target:
                continue
            for index, count in features.items():
                weights[target][index] += count
                weights[predicted][index] -= count
            updates += 1
    return {
        "schema_version": "opentour.pilot.cpu-text-model.v1",
        "profile": PROFILE,
        "labels": labels,
        "dimensions": DIMENSIONS,
        "weights": weights,
    }, updates


def predict(model: dict[str, Any], text: str) -> str:
    """Predict from the fitted parameters. The label is absent from the input."""
    return str(model["labels"][_best(model["weights"], _features(text))])
