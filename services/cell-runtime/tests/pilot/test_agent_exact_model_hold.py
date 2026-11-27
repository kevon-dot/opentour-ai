"""Check unsupported exact-model handling through the ordinary worker."""

from __future__ import annotations

import sqlite3
from contextlib import closing

from opentour_cell.pilot import open_weight
from test_autonomous_cycle import Catalog, Customer


def test_unavailable_exact_checkpoint_stops_before_trainer_or_review(tmp_path, monkeypatch):
    customer = Customer(tmp_path, model=True)
    calls = []

    def trainer_transport(bundle, model_dir, grant, python, request, *, check_permission, dispatch):
        check_permission()
        with dispatch():
            calls.append(request["operation"])
            # The fixture describes a different checkpoint although the rest of its
            # installation metadata matches. No examples should reach this transport.
            assert request == {"operation": "inspect_installation"}
            return {
                "profile": open_weight.PROFILE,
                "checkpoint_revision": "unselected-generated-checkpoint",
                "verified_base_sha256": open_weight.BASE_SHA256,
                "verified_tokenizer_sha256": open_weight.TOKENIZER_SHA256,
            }

    monkeypatch.setattr(open_weight, "_run", trainer_transport)

    def reservation():
        with closing(sqlite3.connect(customer.ledger.path.as_uri() + "?mode=ro", uri=True)) as db:
            return db.execute("SELECT id, state, bundle, settlement FROM reservations").fetchall()

    assert reservation() == []
    held = customer.run()
    assert held["state"] == "held"
    assert held["reason"] == "open_weight_installation_evidence_invalid"
    assert held["result_revision"] is None
    rows = reservation()
    assert len(rows) == 1
    assert rows[0][1:] == ("reserved", None, None)
    assert calls == ["inspect_installation"]

    customer.connector = Catalog(customer.owner)
    retried = customer.run()
    assert retried == held
    assert reservation() == rows
    assert calls == ["inspect_installation", "inspect_installation"]
    for pattern in (
        "managed-outputs/run-*/model/manifest.json",
        "managed-outputs/run-*/run.json",
        "approval-ready-*.json",
        "**/release-decision.json",
    ):
        assert not list(customer.owner.glob(pattern))
