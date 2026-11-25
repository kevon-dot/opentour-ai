"""Prepare an isolated, generated customer directory for either managed repair path."""

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

# The extracted kit keeps this example one directory below its runnable package.
kit = Path(__file__).resolve().parent.parent / "opentour-slack.pyz"
if kit.is_file():
    sys.path.insert(0, str(kit))

from opentour_cell.pilot.operating_point import digest  # noqa: E402
from opentour_cell.pilot.source_intake import discover_intake, freeze_intake  # noqa: E402
from opentour_cell.pilot.training import LocalTrainingStore  # noqa: E402


def save(path, value):
    with path.open("x") as stream:
        json.dump(value, stream, sort_keys=True)
        stream.write("\n")


def save_rows(path, rows):
    with path.open("x") as stream:
        for row in rows:
            stream.write(json.dumps(row, sort_keys=True) + "\n")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare(directory, route):
    os.umask(0o077)
    directory.mkdir(mode=0o700, exist_ok=False)
    org = "generated-co"
    store = LocalTrainingStore(directory / "training.sqlite3", org)
    texts = {
        "refund": [
            "return of an unopened shirt",
            "customer cancelled a book order",
            "buyer wants their payment returned",
            "credit the defective chair",
            "repay the customer after a return",
            "refund for a broken lamp",
            "credited funds for a returned coat",
            "cash returned for a faulty kettle",
        ],
        "sale": [
            "payment for delivered desk",
            "sold a new office chair",
            "customer paid for fresh groceries",
            "purchase of two lamps",
            "sold a printer at the store",
            "paid purchase of a blue coat",
            "incoming payment for a keyboard",
            "sale of a working radio",
        ],
    }
    traces, corrections = [], []
    for label, examples in texts.items():
        for i, description in enumerate(examples):
            tested = i >= 6
            n = 100 + i if not tested else 210 + i
            key = f"{label}-{i}"
            shared = {
                "organization": org,
                "workflow": "purchase-routing",
                "field": "disposition",
                "event_id": key,
                "policy_version": "policy-1",
                "source_schema_version": "schema-1",
            }
            flag = label if route == "rule" else "ordinary"
            traces.append(
                {
                    **shared,
                    "source": "event-log",
                    "customer_id": "later" if tested else "earlier",
                    "group_id": "group-" + key,
                    "decision_at": n,
                    "features_available_at": n - 1,
                    "features": {"event_type": flag, "description": description},
                    "incumbent": {"status": "automatic", "label": "sale"},
                    "slices": [],
                }
            )
            corrections.append(
                {
                    **shared,
                    "source": "verified-ledger",
                    "outcome_id": "outcome-" + key,
                    "adjudicator_id": "generated-verifier",
                    "facts_as_of": n,
                    "outcome_at": n + 1,
                    "verified_at": n + 2,
                    "reference": label,
                }
            )
    trace, correction = directory / "trace.jsonl", directory / "corrections.jsonl"
    source = {
        "schema_version": "opentour.pilot.source-grant.v1",
        "organization": org,
        "engagement": store.engagement_id,
        "dataset_id": "generated-repair",
        "workflow": "purchase-routing",
        "field": "disposition",
        "policy_version": "policy-1",
        "source_schema_version": "schema-1",
        "trace_source": "event-log",
        "correction_source": "verified-ledger",
        "trace_sha256": save_rows(trace, traces),
        "correction_sha256": save_rows(correction, corrections),
        "owner_id": "generated-owner",
        "owner_attestation": "att-" + "1" * 16,
        "adjudicators": ["generated-verifier"],
        "permitted_features": ["description", "event_type"],
        "permitted_slices": [],
        "evaluation_allowed": True,
        "as_of": 300,
        "expires_at": int(time.time()) + 3600,
        "split": {"mode": "chronological", "cutoff": 200},
        "retention_terms": "terms-" + "1" * 16,
    }
    if route == "model":
        source.update(
            training_attestation="att-" + "2" * 16, training_expires_at=source["expires_at"]
        )
    grant = directory / "source-grant.json"
    save(grant, source)
    found = discover_intake(store, grant, trace, correction)
    error_class = found["suspected_error_classes"][0]["class_id"]
    confirmation = {
        "discovery_revision": found["discovery_revision"],
        "class_id": error_class,
        "owner_id": source["owner_id"],
        "attestation": "att-" + "3" * 16,
    }
    intake = freeze_intake(
        store, grant, trace, correction, directory / "intake.json", confirmation=confirmation
    )
    manifest = {
        "schema_version": "opentour.pilot.managed-repair.v1",
        "organization": org,
        "engagement": store.engagement_id,
        "intake_file": "intake.json",
        "intake_revision": intake["intake_revision"],
        "incumbent_revision": digest(["always", "sale"]),
        "audience": "generated-recipient",
        "terms": {"minimum_coverage_bps": 10000, "error_ceiling_bps": 2500},
        "rule": {
            "feature": "event_type",
            "mapping": {"refund": "refund", "sale": "sale"},
            "fallback": "incumbent",
        },
        "handoff": {
            "policy_revision": digest("policy-1"),
            "exporter": "generated-exporter",
            "support_owner": "generated-custodian",
            "decision_time_assumptions": ["Use only the event as of its decision time."],
            "limitations": ["Generated evidence on four held-out decisions."],
            "fallback": "manual_review",
        },
    }
    if route == "model":
        manifest["open_weight_profile"] = "smollm2_135m_lora_v1"
    save(directory / "managed-plan.json", manifest)
    return {
        "plan_revision": digest(manifest),
        "organization": org,
        "engagement": store.engagement_id,
        "intake_revision": intake["intake_revision"],
        "route": route,
        "real_customers": 0,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="new private directory")
    parser.add_argument("--route", choices=["rule", "model"], default="rule")
    args = parser.parse_args()
    print(json.dumps(prepare(args.directory.absolute(), args.route), sort_keys=True))
