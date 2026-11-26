import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest
from opentour_cell.pilot import model_connector as model
from opentour_cell.pilot import model_registry


def write(path: Path, document: dict) -> None:
    draft = path.with_suffix(".new")
    draft.write_text(json.dumps(document), encoding="utf-8")
    draft.chmod(0o600)
    draft.replace(path)


@pytest.fixture
def registered(tmp_path):
    root = tmp_path / "owner"
    root.mkdir(mode=0o700)
    path = (root / "current-models.json").resolve()
    binding = {
        "model_ref": "invoice-incumbent",
        "immutable_revision": "pinned-v1",
        "task": "invoice-routing",
        "input_schema": "decision-inputs-v1",
        "output_schema": "invoice-label-v1",
        "preprocessing_revision": "tokenizer-v1",
        "route": model.Route.CUSTOM.value,
        "account_or_host": "owned-test-host",
        "region": "test-region",
        "credential_ref": "local-store-reference",
    }
    baseline = {
        "actor": "authorized-operator",
        "model_ref": "invoice-incumbent",
        "immutable_revision": "pinned-v1",
        "purpose": model.Purpose.BASELINE.value,
        "revision": "bound-grant-v1",
        "expires_at": 100,
        "permitted_fields": ["description"],
        "retention_until": 150,
        "fit_bound": None,
        "benchmark_revisions": [],
    }
    training = {
        **baseline,
        "actor": "authorized-trainer",
        "purpose": model.Purpose.TRAINING.value,
        "fit_bound": {
            "currency": "USD",
            "maximum_minor_units": 0,
            "maximum_seconds": 60,
            "maximum_rows": 32,
            "maximum_steps": 250,
        },
        "benchmark_revisions": ["frozen-benchmark"],
    }
    body = {
        "schema_version": model_registry.VERSION,
        "tenant": "test-tenant",
        "models": [binding],
        "grants": [baseline, training],
    }
    write(path, body)
    return model_registry.ModelRegistry(path, "test-tenant"), path, body


def test_role_scope_and_job_binding_remain_separate_after_restart(registered):
    registry, path, _ = registered
    binding = registry.binding("invoice-incumbent")
    recovered = model_registry.ModelRegistry(path, "test-tenant")
    assert recovered.binding("invoice-incumbent") == binding
    trainer = recovered.current_grant("authorized-trainer", binding, model.Purpose.TRAINING)
    assert trainer.benchmark_revisions == {"frozen-benchmark"}
    assert trainer.fit_bound == model.FitBound("USD", 0, 60, 32, 250)
    assert recovered.current_grant("authorized-operator", binding, model.Purpose.TRAINING) is None
    assert recovered.current_grant("authorized-operator", binding, model.Purpose.BASELINE)
    assert recovered.current_grant("authorized-trainer", binding, model.Purpose.DISCLOSURE) is None
    assert (
        recovered.current_grant(
            "authorized-trainer", replace(binding, tenant="foreign"), model.Purpose.TRAINING
        )
        is None
    )


def test_current_reader_observes_atomic_revocation_and_denies_old_alias(registered):
    registry, path, doc = registered
    original = registry.binding("invoice-incumbent")
    assert registry.current_grant("authorized-trainer", original, model.Purpose.TRAINING)
    doc["grants"].pop()
    write(path, doc)
    assert registry.current_grant("authorized-trainer", original, model.Purpose.TRAINING) is None
    doc["models"][0]["immutable_revision"] = "pinned-v2"
    write(path, doc)
    with pytest.raises(model.CapabilityError, match="model_registration_unavailable"):
        registry.binding("invoice-incumbent")
    doc["grants"][0]["immutable_revision"] = "pinned-v2"
    write(path, doc)
    assert registry.binding("invoice-incumbent").immutable_revision == "pinned-v2"
    assert registry.current_grant("authorized-operator", original, model.Purpose.BASELINE) is None


@pytest.mark.parametrize(
    "mutation",
    [
        lambda doc: doc["models"].append(doc["models"][0]),
        lambda doc: doc["grants"][0].update(fit_bound=doc["grants"][1]["fit_bound"]),
        lambda doc: doc["models"][0].update(connection_token="generated-forbidden-value"),
        lambda doc: doc["grants"][1]["fit_bound"].update(maximum_seconds=True),
    ],
)
def test_invalid_registry_holds_without_disclosing_model_or_credential(registered, mutation):
    registry, path, doc = registered
    expected = registry.binding("invoice-incumbent")
    mutation(doc)
    write(path, doc)
    with pytest.raises(model.CapabilityError, match="^model_registration_unavailable$"):
        registry.binding("invoice-incumbent")
    assert registry.current_grant("authorized-trainer", expected, model.Purpose.TRAINING) is None


def test_private_registry_loader_refuses_world_readable_and_symlink_paths(registered):
    registry, path, _ = registered
    expected = registry.binding("invoice-incumbent")
    path.chmod(0o644)
    assert registry.current_grant("authorized-trainer", expected, model.Purpose.TRAINING) is None
    path.chmod(0o600)
    link = path.with_name("registry-link.json")
    link.symlink_to(path)
    with pytest.raises(model.CapabilityError, match="model_registration_unavailable"):
        model_registry.ModelRegistry(link, "test-tenant").binding("invoice-incumbent")


def test_registry_backed_preflight_fences_a_revocation_during_estimate(registered):
    registry, path, body = registered
    binding = registry.binding("invoice-incumbent")
    backend = Mock(spec=model.Connector)
    backend.observe.return_value = model.Observation(
        binding=binding,
        source_ref="generated-evidence",
        observed_at=5,
        expires_at=20,
        resolved_revision=binding.immutable_revision,
        inference_supported=True,
        training_supported=True,
        hard_cost_cap=True,
        cancellation=False,
        progress=True,
        checkpoint_recovery=False,
        capture={},
    )
    bound = model.FitBound("USD", 0, 60, 32, 250)
    intent = model.FitIntent(
        binding,
        "bound-grant-v1",
        "frozen-benchmark",
        "generated-development-digest",
        "original-dispatch-key",
        frozenset({"description"}),
        bound,
    )
    preflight = model.Preflight(backend, registry.current_grant, clock=lambda: 10)

    def revoke(_):
        body["grants"].pop()
        write(path, body)
        return bound

    backend.estimate.side_effect = revoke
    with pytest.raises(model.CapabilityError, match="model_capability_unavailable"):
        preflight.prepare("authorized-trainer", intent)
    assert (
        preflight.discover("authorized-operator", binding, model.Purpose.BASELINE).state
        is model.State.BENCHMARK_ONLY
    )
    backend.serialize_development.assert_not_called()
    backend.submit.assert_not_called()
