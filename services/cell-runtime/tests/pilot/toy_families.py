"""Generated sentinel model families for connector tests. No real model, weights or data."""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import replace
from pathlib import Path
from typing import Any

from opentour_cell.pilot import model_connector as mc
from opentour_cell.pilot.connector_conformance import ConnectorHarness, FamilyScenario
from opentour_cell.pilot.model_family import (
    FixtureKind,
    ModelFamilyProfile,
    parse_profile,
)
from opentour_cell.pilot.open_weight_connector import (
    OpenWeightConnector,
    SubprocessFamilyRunner,
    development_digest,
)

WORKER = Path(__file__).parent / "fixtures" / "toy_family_worker.py"
BILLING = ("refund", "invoice", "charge", "receipt")
SHIPPING = ("parcel", "courier", "delivery", "tracking")
TOKENS = BILLING + SHIPPING
LABELS = ["billing", "shipping"]
TRAIN = tuple(
    {"text": text, "label": label}
    for text, label in (
        ("refund invoice", "billing"),
        ("charge receipt", "billing"),
        ("invoice charge refund", "billing"),
        ("receipt refund", "billing"),
        ("parcel courier", "shipping"),
        ("delivery tracking", "shipping"),
        ("courier delivery parcel", "shipping"),
        ("tracking parcel", "shipping"),
    )
)
EVAL_INPUTS = tuple(
    {"text": text}
    for text in (
        "refund receipt",
        "invoice refund charge",
        "charge invoice",
        "courier tracking",
        "parcel delivery tracking",
        "delivery courier",
    )
)
EVAL_REFERENCES = ("billing", "billing", "billing", "shipping", "shipping", "shipping")
ARCHITECTURES = {
    "generated-toy-linear": ("head", 11),
    "generated-toy-normmlp": ("w2", 23),
}


def _matrix(rng: random.Random, rows: int, cols: int) -> list[list[float]]:
    return [[rng.uniform(-1.0, 1.0) for _ in range(cols)] for _ in range(rows)]


def make_model_dir(root: Path, architecture: str, *, seed_shift: int = 0) -> Path:
    layer, seed = ARCHITECTURES[architecture]
    rng = random.Random(seed + seed_shift)
    hidden = 6
    base: dict[str, Any] = {
        "architecture": architecture,
        "labels": LABELS,
        "seed": seed,
        "layers": [layer],
    }
    if architecture == "generated-toy-linear":
        base["head"] = _matrix(rng, len(LABELS), len(TOKENS))
    else:
        base["w1"] = _matrix(rng, hidden, len(TOKENS))
        base["w2"] = _matrix(rng, len(LABELS), hidden)
        base["ws"] = _matrix(rng, len(LABELS), len(TOKENS))
    root.mkdir(parents=True, exist_ok=True)
    (root / "base.json").write_text(json.dumps(base, sort_keys=True))
    (root / "tokenizer.json").write_text(
        json.dumps({"tokens": {token: i for i, token in enumerate(TOKENS)}}, sort_keys=True)
    )
    return root


def profile_document(model_dir: Path, architecture: str, *, role: str) -> dict[str, Any]:
    layer, _ = ARCHITECTURES[architecture]
    files = {
        name: hashlib.sha256((model_dir / name).read_bytes()).hexdigest()
        for name in ("base.json", "tokenizer.json")
    }
    revision = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()[:40]
    return {
        "schema_version": "opentour.pilot.model-family.v1",
        "family_id": f"{architecture}-v1",
        "role": role,
        "architecture": architecture,
        "publisher_ref": "simulated:generated-publisher",
        "revision": revision,
        "license": "generated-test-fixture-only",
        "task": "generated-support-routing-v1",
        "formatter": "generated-text-label-v1",
        "tuning_method": "lora-rank-2-v1",
        "layer_targets": [layer],
        "tokenizer_file": "tokenizer.json",
        "files": files,
        "resource": {
            "max_seconds": 20,
            "max_rows": 16,
            "max_steps": 40,
            "max_address_space_bytes": 1 << 30,
            "incremental_cost_minor_units": 0,
        },
        "scorer": "exact-label-match-v1",
        "benchmark_contract": "generated-benchmark-contract-v1",
    }


class Family:
    """One generated family: model directory, profile and connector factories."""

    def __init__(self, tmp: Path, architecture: str, *, role: str = "candidate") -> None:
        self.tmp = tmp
        self.architecture = architecture
        self.model_dir = make_model_dir(tmp / f"{architecture}-model", architecture)
        document = profile_document(self.model_dir, architecture, role=role)
        self.profile_path = tmp / f"{architecture}.family.json"
        self.profile_path.write_text(json.dumps(document, sort_keys=True))
        self.profile: ModelFamilyProfile = parse_profile(document)

    @property
    def binding(self) -> mc.ModelBinding:
        return self._binding(self.profile)

    @staticmethod
    def _binding(profile: ModelFamilyProfile) -> mc.ModelBinding:
        assert profile.revision is not None
        return mc.ModelBinding(
            tenant="generated-tenant",
            model_ref=profile.family_id,
            immutable_revision=profile.revision,
            task=profile.task,
            input_schema="generated-text-v1",
            output_schema="generated-label-v1",
            preprocessing_revision=profile.formatter,
            route=mc.Route.LOCAL,
            account_or_host="simulated:local-machine.invalid",
            region="generated-local",
            credential_ref="generated-no-credential",
        )

    def intent(self, key: str, *, binding: mc.ModelBinding | None = None) -> mc.FitIntent:
        return mc.FitIntent(
            binding or self.binding,
            "generated-grant-v1",
            "generated-benchmark-v1",
            development_digest(TRAIN),
            key,
            frozenset({"text", "label"}),
            mc.FitBound("USD", 0, 60, 16, 64),
        )

    def connector(
        self,
        state: Path,
        residual_gate: Any = None,
        *,
        slow: bool = False,
        model_dir: Path | None = None,
    ) -> OpenWeightConnector:
        runner = SubprocessFamilyRunner(
            self.profile,
            model_dir or self.model_dir,
            WORKER,
            self.profile_path,
            request_extras={"step_delay_ms": 100} if slow else None,
        )
        return OpenWeightConnector(
            runner,
            state,
            allow_fixture_profiles=True,
            residual_gate=residual_gate,
            cancel_wait_seconds=20.0,
        )

    def incompatible_connector(self, state: Path) -> OpenWeightConnector:
        """A model directory whose base differs from the pinned bytes."""
        other = make_model_dir(
            self.tmp / f"{self.architecture}-incompatible", self.architecture, seed_shift=1
        )
        return self.connector(state, model_dir=other)

    def harness(self) -> ConnectorHarness:
        wrong_revision = "0" * 40
        return ConnectorHarness(
            make_connector=lambda state: self.connector(state),
            binding=self.binding,
            mismatched_binding=replace(self.binding, immutable_revision=wrong_revision),
            make_intent=self.intent,
            rows=TRAIN,
            executions=lambda state: len(list((state / "jobs").glob("*/journal.jsonl"))),
            make_slow_connector=lambda state: self.connector(state, slow=True),
        )

    def scenario(self, kind: FixtureKind = FixtureKind.GENERATED) -> FamilyScenario:
        return FamilyScenario(
            profile=self.profile,
            model_dir=self.model_dir,
            fixture_kind=kind,
            binding=self.binding,
            make_intent=self.intent,
            training_rows=TRAIN,
            eval_inputs=EVAL_INPUTS,
            eval_references=EVAL_REFERENCES,
            make_connector=lambda state, gate: self.connector(state, gate),
            make_incompatible_connector=self.incompatible_connector,
            make_slow_connector=lambda state: self.connector(state, slow=True),
        )
