"""Generated pre-existing CPU parameters for the deterministic invoice journey."""

import hashlib
import json
from pathlib import Path

import pytest
from opentour_cell.pilot import text_classifier as cpu
from opentour_cell.pilot.operating_point import canonical
from test_model_registry import write


def write_p1_cpu_fixture(path: Path) -> str:
    """Write the private CPU artifact and return its exact registry revision."""
    signals = {
        "SOFTWARE": ("annual", "software", "subscription"),
        "SUPPLIES": ("carrier", "freight", "office", "supplies"),
        "UTILITIES": ("utility", "service"),
        "TAX": ("tax", "remittance"),
        "REVIEW": ("null",),
    }
    weights = []
    used = set()
    for label, tokens in signals.items():
        row = [0] * cpu.DIMENSIONS
        for token in tokens:
            (feature,) = [i for i, _ in cpu.feature_signature(token) if i]
            assert feature not in used
            used.add(feature)
            row[feature] = 24 if label == "REVIEW" else 2
        if label == "REVIEW":
            # JSON contains the PO field name once. A PO-prefixed identifier adds
            # another occurrence; the generated token "b" hashes like "null".
            (po_feature,) = [i for i, _ in cpu.feature_signature("po") if i]
            assert po_feature not in used
            row[po_feature] = -12
        weights.append(row)
    write(
        path,
        dict(
            schema_version="opentour.pilot.cpu-text-model.v1",
            profile=cpu.PROFILE,
            labels=list(signals),
            dimensions=cpu.DIMENSIONS,
            weights=weights,
        ),
    )
    return "sha256-" + hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("po", ["po-a", "po-b", "po-c", "another-891", "invoice-ref-307"])
def test_preexisting_invoice_parameters(tmp_path, monkeypatch, po):
    def forbid(*args, **kwargs):
        pytest.fail("fixture attempted fitting")

    monkeypatch.setattr(cpu, "fit", forbid)
    path = tmp_path / "generated-model.json"
    revision = write_p1_cpu_fixture(path)
    assert revision == "sha256-" + hashlib.sha256(path.read_bytes()).hexdigest()
    model = json.loads(path.read_bytes())
    for category, reference, expected in (
        ("annual_software_subscription", po, "SOFTWARE"),
        ("carrier_freight", po, "SUPPLIES"),
        ("office_supplies", po, "SUPPLIES"),
        ("annual_software_subscription", None, "REVIEW"),
    ):
        for facts in (
            {"category": category, "po": reference},
            {"po": reference, "category": category},
        ):
            assert cpu.predict(model, canonical(facts)) == expected
