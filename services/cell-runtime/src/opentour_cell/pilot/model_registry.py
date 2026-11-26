"""Read the current model and purpose grants from the owner's private registration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from opentour_cell.pilot import model_connector as model
from opentour_cell.pilot.qualification import _private_json

VERSION = "opentour.pilot.model-registration.v1"
_MODEL_FIELDS = {
    "model_ref",
    "immutable_revision",
    "task",
    "input_schema",
    "output_schema",
    "preprocessing_revision",
    "route",
    "account_or_host",
    "region",
    "credential_ref",
}
_GRANT_FIELDS = {
    "actor",
    "model_ref",
    "immutable_revision",
    "purpose",
    "revision",
    "expires_at",
    "permitted_fields",
    "retention_until",
    "fit_bound",
    "benchmark_revisions",
}
_BOUND_FIELDS = {
    "currency",
    "maximum_minor_units",
    "maximum_seconds",
    "maximum_rows",
    "maximum_steps",
}


def _record(value: Any, fields: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or value.keys() != fields:
        raise ValueError("invalid_model_registration")
    return value


def _reference(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 256 or value.strip() != value:
        raise ValueError("invalid_model_registration_reference")
    return value


def _count(value: Any, *, minimum: int = 1) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError("invalid_model_registration_bound")
    return value


def _references(value: Any) -> frozenset[str]:
    if not isinstance(value, list):
        raise ValueError("invalid_model_registration_scope")
    refs = frozenset(_reference(item) for item in value)
    if len(refs) != len(value):
        raise ValueError("ambiguous_model_registration_scope")
    return refs


def _model(tenant: str, raw: Any) -> model.ModelBinding:
    item = _record(raw, _MODEL_FIELDS)
    return model.ModelBinding(
        tenant=tenant,
        model_ref=_reference(item["model_ref"]),
        immutable_revision=_reference(item["immutable_revision"]),
        task=_reference(item["task"]),
        input_schema=_reference(item["input_schema"]),
        output_schema=_reference(item["output_schema"]),
        preprocessing_revision=_reference(item["preprocessing_revision"]),
        route=model.Route(item["route"]),
        account_or_host=_reference(item["account_or_host"]),
        region=_reference(item["region"]),
        credential_ref=_reference(item["credential_ref"]),
    )


def _budget(raw: Any) -> model.FitBound | None:
    if raw is None:
        return None
    item = _record(raw, _BOUND_FIELDS)
    return model.FitBound(
        _reference(item["currency"]),
        _count(item["maximum_minor_units"], minimum=0),
        _count(item["maximum_seconds"]),
        _count(item["maximum_rows"]),
        _count(item["maximum_steps"]),
    )


def _grant(binding: model.ModelBinding, item: dict[str, Any]) -> model.Grant:
    purpose = model.Purpose(item["purpose"])
    bound = _budget(item["fit_bound"])
    benchmarks = _references(item["benchmark_revisions"])
    if purpose is not model.Purpose.TRAINING and (bound is not None or benchmarks):
        raise ValueError("training_scope_requires_training_purpose")
    return model.Grant(
        actor=_reference(item["actor"]),
        binding=binding,
        purpose=purpose,
        revision=_reference(item["revision"]),
        expires_at=_count(item["expires_at"]),
        permitted_fields=_references(item["permitted_fields"]),
        retention_until=_count(item["retention_until"]),
        fit_bound=bound,
        benchmark_revisions=benchmarks,
    )


class ModelRegistry:
    """Resolve exact model bindings and current grants using a trusted local path."""

    def __init__(self, path: Path, tenant: str) -> None:
        self.path = path
        self.tenant = _reference(tenant)

    def _load(
        self,
    ) -> tuple[dict[str, model.ModelBinding], dict[tuple[str, str, model.Purpose], model.Grant]]:
        raw = _record(_private_json(self.path), {"schema_version", "tenant", "models", "grants"})
        if (
            raw["schema_version"] != VERSION
            or raw["tenant"] != self.tenant
            or not isinstance(raw["models"], list)
            or not isinstance(raw["grants"], list)
        ):
            raise ValueError("model_registration_scope_unavailable")
        bindings = {}
        for item in raw["models"]:
            binding = _model(self.tenant, item)
            if binding.model_ref in bindings:
                raise ValueError("ambiguous_model_registration")
            bindings[binding.model_ref] = binding
        grants = {}
        for raw_grant in raw["grants"]:
            item = _record(raw_grant, _GRANT_FIELDS)
            registered = bindings.get(_reference(item["model_ref"]))
            if registered is None or registered.immutable_revision != item["immutable_revision"]:
                raise ValueError("model_grant_revision_unavailable")
            grant = _grant(registered, item)
            key = (grant.actor, registered.model_ref, grant.purpose)
            if key in grants:
                raise ValueError("ambiguous_model_grant")
            grants[key] = grant
        return bindings, grants

    def binding(self, model_ref: str) -> model.ModelBinding:
        try:
            return self._load()[0][model_ref]
        except (OSError, ValueError, KeyError, TypeError):
            raise model.CapabilityError("model_registration_unavailable") from None

    def current_grant(
        self, actor: str, binding: model.ModelBinding, purpose: model.Purpose
    ) -> model.Grant | None:
        if binding.tenant != self.tenant:
            return None
        try:
            registered, grants = self._load()
        except (OSError, ValueError, KeyError, TypeError):
            return None
        if registered.get(binding.model_ref) != binding:
            return None
        return grants.get((actor, binding.model_ref, purpose))
