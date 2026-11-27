"""Wrap the qualified SmolLM2 decision adapter (``open_weight``) as a connector family runner.

The numeric worker, grant checks, residual comparator, protected-membership checks and withdrawal
markers stay in ``open_weight``; this module only adapts them to ``FamilyRunner`` so the shared
connector contract, journal, cancellation and qualification harness apply unchanged.

Simulated: tests inject fakes for the two ``open_weight`` entry points.
Not proven: no SmolLM2 weights, ``tokenizers`` wheel or numeric interpreter is available in the
control workspace, so the reference result is NOT reproduced by this module's tests. That remains
an unsatisfied external gate until the staged model run (docs/connectors/open-weight-connector.md).
The reference worker offers no unmodified-base inference, so ``predict(None, ...)`` is refused.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from opentour_cell.pilot import open_weight as ow
from opentour_cell.pilot.model_family import FamilyError, ModelFamilyProfile, verify_model_directory
from opentour_cell.pilot.open_weight_connector import FitOutput, RunControl, RunInterrupted


class ReferenceDecisionRunner:
    """``FamilyRunner`` over the pinned ``smollm2_135m_lora_v1`` profile."""

    def __init__(
        self,
        profile: ModelFamilyProfile,
        model_dir: Path,
        work_dir: Path,
        *,
        grant: Mapping[str, Any],
        residual: Mapping[str, Any],
        protected_fingerprints: Iterable[str],
        python: str | Path | None = None,
        fit_fn: Callable[..., dict[str, Any]] = ow.fit_open_weight,
        predict_fn: Callable[..., list[dict[str, str]]] = ow.predict_open_weight_batch,
    ) -> None:
        self.profile = profile
        self.model_dir = model_dir
        self.work_dir = work_dir
        self.grant = grant
        self.residual = residual
        self.protected = tuple(protected_fingerprints)
        self.python = python
        self.fit_fn = fit_fn
        self.predict_fn = predict_fn

    def installed_revision(self) -> str:
        try:
            return verify_model_directory(self.profile, self.model_dir)
        except FamilyError as exc:
            raise ValueError(str(exc)) from exc

    def fit(
        self, rows: tuple[Mapping[str, Any], ...], steps: int, seconds: int, control: RunControl
    ) -> FitOutput:
        # The pinned profile fixes steps; the grant fixes seconds. Neither is widened here.
        def check_permission() -> None:
            reason = control.stop_reason()
            if reason is not None:
                raise ow.OpenWeightError(reason)

        bundle = self.work_dir / "bundle"
        try:
            manifest = self.fit_fn(
                bundle,
                [dict(row) for row in rows],
                model_dir=self.model_dir,
                grant=self.grant,
                residual=self.residual,
                protected_fingerprints=self.protected,
                python=self.python,
                check_permission=check_permission,
            )
        except ow.OpenWeightError as exc:
            reason = control.stop_reason()
            if reason is not None:
                raise RunInterrupted(reason) from exc
            raise ValueError(str(exc).upper()) from exc
        training = manifest["training"]
        evidence = {
            "loss_first": training["initial_loss"],
            "loss_last": training["final_loss"],
            "parameters_changed": sum(training["changed_parameters"]),
            "base_sha256_before": manifest["identity"]["base_sha256"],
            "base_sha256_after": manifest["identity"]["base_sha256"],
            "steps": training["steps"],
        }
        files = {
            "adapter.json": (bundle / "adapter.json").read_bytes(),
            "manifest.json": (bundle / "manifest.json").read_bytes(),
        }
        return FitOutput(files, evidence)

    def predict(
        self, artifact_dir: Path | None, inputs: tuple[Mapping[str, Any], ...]
    ) -> tuple[Mapping[str, Any], ...]:
        if artifact_dir is None:
            raise ValueError("BASE_INFERENCE_NOT_SUPPORTED_BY_REFERENCE_WORKER")
        decisions = self.predict_fn(
            artifact_dir,
            [dict(item) for item in inputs],
            model_dir=self.model_dir,
            grant=self.grant,
            python=self.python,
        )
        return tuple(decisions)


def reference_profile_path() -> Path:
    """Locate the checked-in declarative family profile for the reference checkpoint."""
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "training/profiles/families/smollm2_135m_lora_v1.family.json"
        if candidate.is_file():
            return candidate
    raise FamilyError("FAMILY_PROFILE_NOT_FOUND")
