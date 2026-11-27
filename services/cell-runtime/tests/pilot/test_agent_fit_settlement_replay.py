"""Replay a committed fit settlement with a generated trainer transport."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing

from opentour_cell.pilot import open_weight
from opentour_cell.pilot.learning_mandate import LearningMandates
from test_autonomous_cycle import Catalog, Customer


def test_cycle_recovers_committed_fit_after_lost_settlement_reply(tmp_path, monkeypatch):
    customer = Customer(tmp_path, model=True)
    protected_inputs = {
        "credited funds for a returned coat",
        "cash returned for a faulty kettle",
        "incoming payment for a keyboard",
        "sale of a working radio",
    }
    calls = []
    fit_features = []

    def trainer_transport(bundle, model_dir, grant, python, request, *, check_permission, dispatch):
        check_permission()
        with dispatch():
            operation = request["operation"]
            calls.append(operation)
            if operation == "inspect_installation":
                assert set(request) == {"operation"}
                return {
                    "profile": open_weight.PROFILE,
                    "checkpoint_revision": open_weight.REVISION,
                    "verified_base_sha256": open_weight.BASE_SHA256,
                    "verified_tokenizer_sha256": open_weight.TOKENIZER_SHA256,
                }
            if operation == "fit":
                assert set(request) == {"operation", "examples", "targets"}
                assert len(request["examples"]) == len(request["targets"]) == 12
                fit_features.extend(row["description"] for row in request["examples"])
                return {"adapter": {"a": "YQ=="}, "pid": 1, "elapsed_ms": 1700}
            assert operation == "predict"
            assert set(request) == {"operation", "examples", "adapter"}
            assert request["adapter"] == {"a": "YQ=="}
            return {
                "decisions": [
                    {
                        "status": "automatic",
                        "label": "refund" if "returned" in row["description"] else "sale",
                    }
                    for row in request["examples"]
                ]
            }

    monkeypatch.setattr(open_weight, "_run", trainer_transport)
    real_settle = LearningMandates.settle
    settlements = []

    def lose_first_reply(ledger, *args, **kwargs):
        receipt = real_settle(ledger, *args, **kwargs)
        settlements.append(receipt)
        if len(settlements) == 1:
            raise OSError("generated_settlement_reply_lost")
        return receipt

    monkeypatch.setattr(LearningMandates, "settle", lose_first_reply)

    def durable_effects():
        with closing(sqlite3.connect(customer.ledger.path.as_uri() + "?mode=ro", uri=True)) as db:
            fit_rows = db.execute(
                "SELECT id, state, seconds, settlement FROM reservations"
            ).fetchall()
        return (
            fit_rows,
            sorted(customer.owner.glob("managed-outputs/run-*/model/manifest.json")),
            sorted(customer.owner.glob("managed-outputs/run-*/run.json")),
            sorted(customer.owner.glob("approval-ready-*.json")),
            sorted(customer.owner.rglob("release-decision.json")),
        )

    assert durable_effects() == ([], [], [], [], [])
    first = customer.run()
    fit_rows, models, results, pointers, releases = durable_effects()
    assert first["state"] == "held"
    assert first["result_revision"] is None
    assert calls == ["inspect_installation", "fit"]
    assert len(fit_features) == 12 and protected_inputs.isdisjoint(fit_features)
    assert len(models) == len(fit_rows) == 1
    assert not (results or pointers or releases)
    model_bytes = models[0].read_bytes()
    fit_id, state, reserved_seconds, settlement = fit_rows[0]
    assert state == "settled" and settlement is not None
    model = json.loads(model_bytes)
    stored = json.loads(settlement)
    assert reserved_seconds == stored["reserved_seconds_charged"] == 120
    assert stored["reservation_id"] == fit_id
    assert stored["elapsed_seconds"] == 2
    assert stored["artifact_sha256"] == model["artifact_sha256"]
    assert model["incremental_compute_cost_cents"] == 0

    # Replace the connector and use the normal worker entry again. The stopped invocation's
    # temporary objects cannot supply the prior fit, settlement or result.
    customer.connector = Catalog(customer.owner)
    recovered = customer.run()
    assert recovered["run_id"] == first["run_id"]
    assert recovered["state"] == "pending_slack_review", recovered
    assert recovered["outcome"] == "model_candidate"
    assert calls == ["inspect_installation", "fit", "predict"]
    assert settlements == [stored, stored]
    replay_rows, replay_models, replay_results, replay_pointers, replay_releases = durable_effects()
    assert replay_rows == fit_rows
    assert replay_models == models and replay_models[0].read_bytes() == model_bytes
    assert len(replay_results) == len(replay_pointers) == 1
    assert not replay_releases
    result_path = replay_results[0]
    assert json.loads(result_path.read_bytes())["result_revision"] == recovered["result_revision"]

    assert customer.run() == recovered
    assert durable_effects() == (
        replay_rows,
        replay_models,
        replay_results,
        replay_pointers,
        replay_releases,
    )
    assert calls == ["inspect_installation", "fit", "predict"]
