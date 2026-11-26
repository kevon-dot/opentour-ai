"""Rehearse the optional local model on generated data and keep the evidence owner-private."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from opentour_cell.pilot import open_weight, simple_repair

Json = dict[str, Any]
TERMS = {"minimum_coverage_bps": 8000, "error_ceiling_bps": 500}
TRAIN = (
    ("I need a refund for a duplicate charge.", "billing"),
    ("The invoice lists the wrong subscription price.", "billing"),
    ("Why did the card payment go through twice?", "billing"),
    ("Please credit the extra amount on my bill.", "billing"),
    ("The upload fails with a server error.", "technical"),
    ("The application crashes when I open a file.", "technical"),
    ("My network connection times out during sync.", "technical"),
    ("The report download shows a blank screen.", "technical"),
)
# The model sees only independently sealed inputs. Outcomes are read after fresh inference.
PROTECTED_INPUTS = (
    "My account was billed twice for this order.",
    "Please explain the charge on our latest invoice.",
    "The software freezes after I sign in.",
    "The file upload service returns a timeout.",
    "The dashboard does not load in my browser.",
)
PROTECTED_LABELS = ("billing", "billing", "technical", "technical", "technical")


def canonical(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def read(path: Path) -> Any:
    return json.loads(path.read_bytes())


def record(path: Path, value: Any) -> None:
    document = canonical(value)
    if path.exists():
        if path.read_bytes() != document:
            raise ValueError("frozen_qualification_record_conflict")
        return
    with path.open("xb") as handle:
        os.chmod(path, 0o600)
        handle.write(document)


def inputs(split: str, texts: tuple[str, ...]) -> list[Json]:
    return [
        {
            "case_id": f"generated-{split}-{idx}",
            "family_id": f"family-{split}-{idx}",
            "features": {"question": text},
        }
        for idx, text in enumerate(texts)
    ]


def development(rows: list[Json], *, simple: bool) -> Json:
    cases = [
        {
            "case_id": row["case_id"],
            "group_id": row["family_id"],
            "features": {"category": row["label"] if simple else "general"},
            "reference": row["label"],
            "incumbent": {"status": "automatic", "label": "billing"},
        }
        for row in rows
    ]
    rule = simple_repair.fit_rule(
        cases, feature="category", allowed_features=["category", "question"]
    )
    return simple_repair.assess_development(
        cases, rule, terms=TERMS, model_authorized=not simple, compute_authorized=not simple
    )


def prepare(work: Path) -> None:
    work.mkdir(mode=0o700, parents=True, exist_ok=True)
    projected = inputs("dev", tuple(name for name, _ in TRAIN))
    rows = [dict(row, label=expected) for row, (_, expected) in zip(projected, TRAIN, strict=True)]
    protected = inputs("protected", PROTECTED_INPUTS)
    denied, eligible = development(rows, simple=True), development(rows, simple=False)
    if denied["route"] != "simple" or denied["fit_calls"] or eligible["route"] != "model":
        raise ValueError("generated_residual_gate_mismatch")
    grant = {
        "organization": "generated-org",
        "engagement": "generated-support-routing",
        "trainer": "generated-engineering-operator",
        "payer": "generated-zero-cost-session",
        "destination": "owner-private-generated-qualification",
        "profile": open_weight.PROFILE,
        "residual_revision": eligible["revision"],
        "max_seconds": 120,
        "max_cost_cents": 0,
        "hourly_cost_cents": 0,
        "features": ["question"],
        "labels": ["billing", "technical"],
        "rights_attestation": "public-apache-base-generated-training-data-owner-local",
        "expires_at": 4102444800,
        **dict.fromkeys(open_weight.GRANTS, True),
    }
    for name, item in {
        "training.json": rows,
        "protected-inputs.json": protected,
        "protected-keys.json": sorted(open_weight.partition_fingerprints(protected)),
        "protected-outcomes.json": list(PROTECTED_LABELS),
        "grant.json": grant,
        "residual.json": eligible,
        "simple-decision.json": denied,
    }.items():
        record(work / name, item)


def fit(work: Path, base: Path, python: Path) -> None:
    # This process and its numeric child consume only the sealed protected fingerprints.
    grant = read(work / "grant.json")
    result = open_weight.fit_open_weight(
        work / "model",
        read(work / "training.json"),
        model_dir=base,
        grant=grant,
        residual=read(work / "residual.json"),
        protected_fingerprints=read(work / "protected-keys.json"),
        python=python,
    )
    print(
        json.dumps(
            {
                "state": "fitted",
                "artifact_sha256": result["artifact_sha256"],
                "training": result["training"],
            }
        )
    )


def replay(work: Path, base: Path, python: Path, name: str) -> None:
    started = time.monotonic()
    grant = read(work / "grant.json")
    points = [row["features"] for row in read(work / "protected-inputs.json")]
    points.append({"unknown": "outside authorized schema"})
    actions = open_weight.predict_open_weight_batch(
        work / "model", points, model_dir=base, grant=grant, python=python
    )
    receipt = {
        "artifact_sha256": open_weight.inspect_open_weight(work / "model", grant=grant)[
            "artifact_sha256"
        ],
        "case_outputs": actions[:-1],
        "fallback": actions[-1],
    }
    record(work / (name + ".json"), receipt)
    print(
        json.dumps(
            {
                "state": "replayed",
                "elapsed_ms": round((time.monotonic() - started) * 1000),
                "output_sha256": hashlib.sha256(canonical(receipt)).hexdigest(),
            }
        )
    )


def verify(work: Path, base: Path, python: Path) -> Json:
    first, repeat = (
        (work / "replay-first.json").read_bytes(),
        (work / "replay-repeat.json").read_bytes(),
    )
    if first != repeat:
        raise ValueError("fresh_process_outputs_differ")
    results = json.loads(first)
    expected = read(work / "protected-outcomes.json")
    decisions = results["case_outputs"]
    if results["fallback"] != {"status": "review"} or len(expected) != len(decisions):
        raise ValueError("wrong_protected_cohort_or_fallback")
    automatic = [i for i, action in enumerate(decisions) if action["status"] == "automatic"]
    wrong = sum(decisions[i]["label"] != expected[i] for i in automatic)
    passes = (
        bool(automatic)
        and len(automatic) * 10_000 >= len(expected) * TERMS["minimum_coverage_bps"]
        and wrong * 10_000 <= len(automatic) * TERMS["error_ceiling_bps"]
    )
    fit_doc = open_weight.inspect_open_weight(work / "model", grant=read(work / "grant.json"))
    disk = shutil.disk_usage(work)
    memory = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    result = {
        "schema": "opentour.generated-open-weight-qualification.v1",
        "evidence_class": "generated_engineering_rehearsal",
        "real_customer_acceptances": 0,
        "trainer": "authorized-work-session-generated-run",
        "payer": "owner-local-session",
        "use_rights": "public-Apache-2.0-base-and-generated-records",
        "scope": "private-local-directory",
        "profile": open_weight.PROFILE,
        "revision": open_weight.REVISION,
        "base_sha256": open_weight.BASE_SHA256,
        "artifact_sha256": fit_doc["artifact_sha256"],
        "adapter_sha256": fit_doc["identity"]["adapter_sha256"],
        "source_sha256": fit_doc["identity"]["source_sha256"],
        "profile_sha256": open_weight.PROFILE_SHA256,
        "training": fit_doc["training"],
        "protected_count": len(expected),
        "automatic": len(automatic),
        "wrong_automatic": wrong,
        "observed_operating_point": "met" if passes else "held",
        "operating_limits": TERMS,
        "simple_route": read(work / "simple-decision.json")["route"],
        "simple_fit_calls": read(work / "simple-decision.json")["fit_calls"],
        "model_fit_calls": 1,
        "fallback": results["fallback"],
        "two_fresh_replays_sha256": hashlib.sha256(first).hexdigest(),
        "runtime": {
            "hardware": platform.machine(),
            "system": platform.system(),
            "cpu_visible": os.cpu_count(),
            "host_ram_bytes": memory,
            "disk_available_bytes": disk.free,
            "work_directory": str(work.resolve()),
            "staged_base": str(base.resolve()),
            "numeric_interpreter": str(python.absolute()),
            "deadline_seconds": 120,
        },
        "incremental_compute_cost_cents": 0,
        "cost_basis": "existing local CPU with zero incremental charge",
        "aws_resources_provisioned": 0,
    }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "phase", choices=["prepare", "fit", "replay-first", "replay-repeat", "verify"]
    )
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--numeric-python", type=Path, required=True)
    args = parser.parse_args()
    work, base, python = args.work_dir, args.model_dir, args.numeric_python
    if args.phase == "prepare":
        prepare(work)
    elif args.phase == "fit":
        fit(work, base, python)
    elif args.phase.startswith("replay-"):
        replay(work, base, python, args.phase)
    else:
        print(json.dumps(verify(work, base, python), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
