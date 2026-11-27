"""Reference customer-hosted trainer: the onboarding example for the versioned job adapter.

``ReferenceTrainer`` implements only the public ``TrainerTransport`` contract from
``custom_trainer_connector`` and shares no private state with the connector. It performs a real
(tiny) update: a token-count text classifier is fitted from the serialized development payload,
sealed with a digest and an HMAC-signed result, and replayed deterministically. State lives in a
directory so a restarted trainer object serves the same jobs.

Simulated: the "customer" is this generated, in-process sentinel trainer; nothing leaves the
machine. The model is a toy and says nothing about a real proprietary checkpoint.
Not proven: no real customer trainer, GPU or paid compute is involved. Passing the conformance
suite against this class is ``EvidenceTier.FIXTURE`` evidence only and establishes no provider or
customer evidence.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from opentour_cell.pilot import model_connector as mc
from opentour_cell.pilot.custom_trainer_connector import (
    ADAPTER_VERSION,
    Credential,
    TrainerJobRequest,
    TrainerManifest,
    TrainerResult,
    TrainerUsage,
    TrainerView,
    sign_result,
)

TRAINER_VERSION = "generated-reference-trainer-1.0"
SERIALIZER_VERSION = "generated-text-label-serializer-v1"
SCORER_VERSION = "generated-exact-label-scorer-v1"


def reference_manifest(model_revision: str, tasks: frozenset[str]) -> TrainerManifest:
    return TrainerManifest(
        adapter_version=ADAPTER_VERSION,
        trainer_version=TRAINER_VERSION,
        model_revision=model_revision,
        supported_tasks=tasks,
        serializer_version=SERIALIZER_VERSION,
        scorer_version=SCORER_VERSION,
        replay_mode="deterministic",
        replay_tolerance=0.0,
        training=True,
        cancellation=True,
        progress=True,
        stop_semantics=True,
        hard_cost_cap=True,
        checkpoint_recovery=False,
    )


class ReferenceTrainer:
    """A customer-operated trainer. ``run_pending`` plays the customer's own scheduler."""

    def __init__(
        self,
        root: Path,
        secret: str,
        model_revision: str,
        tasks: frozenset[str],
        *,
        manifest: TrainerManifest | None = None,
    ) -> None:
        self.root = root
        self.secret = secret
        self.manifest = manifest or reference_manifest(model_revision, tasks)
        (root / "jobs").mkdir(parents=True, exist_ok=True)
        (root / "artifacts").mkdir(parents=True, exist_ok=True)

    # ---- storage ----

    def _job_path(self, job_id: str) -> Path:
        return self.root / "jobs" / f"{job_id}.json"

    def _load(self, job_id: str) -> dict[str, object]:
        try:
            loaded: dict[str, object] = json.loads(self._job_path(job_id).read_text())
        except FileNotFoundError:
            raise LookupError("job_not_found") from None
        return loaded

    def _save(self, job: dict[str, object]) -> None:
        path = self._job_path(str(job["job_id"]))
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(job, sort_keys=True))
        os.replace(temporary, path)

    def _authorize(self, credential: Credential) -> None:
        if credential.value != self.secret:
            raise PermissionError("trainer_credential_rejected")

    def _view(self, job: dict[str, object]) -> TrainerView:
        usage = job.get("usage")
        return TrainerView(
            str(job["job_id"]),
            str(job["client_token"]),
            str(job["state"]),
            TrainerUsage(**usage) if isinstance(usage, dict) else None,
        )

    # ---- public adapter ----

    def describe(self, credential: Credential) -> TrainerManifest:
        self._authorize(credential)
        return self.manifest

    def quote(self, credential: Credential, task: str, bound: mc.FitBound) -> mc.FitBound | None:
        self._authorize(credential)
        return replace(
            bound,
            maximum_minor_units=min(bound.maximum_minor_units, 10),
            maximum_seconds=min(bound.maximum_seconds, 5),
            maximum_steps=min(bound.maximum_steps, 3),
        )

    def start(self, credential: Credential, request: TrainerJobRequest) -> TrainerView:
        self._authorize(credential)
        existing = self.find(credential, request.client_token)
        if existing is not None:
            return existing  # duplicate client token: no second execution
        job_id = "generated-job-" + hashlib.sha256(request.client_token.encode()).hexdigest()[:12]
        job: dict[str, object] = {
            "job_id": job_id,
            "client_token": request.client_token,
            "state": "queued",
            "request": {
                "model_revision": request.model_revision,
                "development": request.development.decode(),
                "bound_rows": request.bound.maximum_rows,
            },
        }
        self._save(job)
        return self._view(job)

    def find(self, credential: Credential, client_token: str) -> TrainerView | None:
        self._authorize(credential)
        for path in sorted((self.root / "jobs").glob("*.json")):
            job = json.loads(path.read_text())
            if job["client_token"] == client_token:
                return self._view(job)
        return None

    def poll(self, credential: Credential, job_id: str) -> TrainerView:
        self._authorize(credential)
        return self._view(self._load(job_id))

    def request_cancel(self, credential: Credential, job_id: str) -> TrainerView:
        self._authorize(credential)
        job = self._load(job_id)
        if job["state"] in ("queued", "running"):
            job["state"] = "canceled"
            self._save(job)
        return self._view(job)

    def fetch_result(self, credential: Credential, job_id: str) -> TrainerResult:
        self._authorize(credential)
        job = self._load(job_id)
        if job["state"] != "completed":
            raise LookupError("result_not_available")
        unsigned = {
            "job_id": str(job["job_id"]),
            "client_token": str(job["client_token"]),
            "trainer_version": self.manifest.trainer_version,
            "parent_revision": str(job["parent_revision"]),
            "artifact_ref": str(job["artifact_ref"]),
            "artifact_digest": str(job["artifact_digest"]),
        }
        return TrainerResult(**unsigned, signature=sign_result(self.secret, unsigned))

    def predict(
        self, credential: Credential, artifact_ref: str, inputs: tuple[Mapping[str, object], ...]
    ) -> tuple[Mapping[str, object], ...]:
        self._authorize(credential)
        model = json.loads((self.root / "artifacts" / f"{artifact_ref}.json").read_text())
        return tuple({"label": _classify(model, str(row["text"]))} for row in inputs)

    # ---- the customer's own scheduler (not part of the adapter contract) ----

    def run_pending(self) -> None:
        """Run queued jobs to completion: a real update of the toy model from the payload."""
        for path in sorted((self.root / "jobs").glob("*.json")):
            job = json.loads(path.read_text())
            if job["state"] != "queued":
                continue
            payload = json.loads(job["request"]["development"])
            rows = payload["rows"]
            counts: dict[str, Counter[str]] = {}
            for row in rows:
                for token in str(row["text"]).lower().split():
                    counts.setdefault(token, Counter())[str(row["label"])] += 1
            labels = Counter(str(row["label"]) for row in rows)
            model = {
                "base": job["request"]["model_revision"],
                "prior": sorted(labels.items()),
                "tokens": {token: sorted(c.items()) for token, c in sorted(counts.items())},
            }
            body = json.dumps(model, sort_keys=True)
            digest = hashlib.sha256(body.encode()).hexdigest()
            ref = f"generated-artifact-{digest[:16]}"
            (self.root / "artifacts" / f"{ref}.json").write_text(body)
            job.update(
                state="completed",
                parent_revision=job["request"]["model_revision"],
                artifact_ref=ref,
                artifact_digest=digest,
                usage={
                    "currency": "USD",
                    "minor_units": 3,
                    "seconds": 1,
                    "rows": len(rows),
                    "steps": 1,
                },
            )
            self._save(job)


def _classify(model: Mapping[str, object], text: str) -> str:
    scores: Counter[str] = Counter()
    for label, count in model["prior"]:  # type: ignore[attr-defined]
        scores[label] += count * 0.01
    tokens: Mapping[str, list[tuple[str, int]]] = model["tokens"]  # type: ignore[assignment]
    for token in text.lower().split():
        for label, count in tokens.get(token, []):
            scores[label] += count
    best = max(sorted(scores), key=lambda label: scores[label])
    return best
