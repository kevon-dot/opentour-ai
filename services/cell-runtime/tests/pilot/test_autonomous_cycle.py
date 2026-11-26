"""Exercise delegated discovery, protected comparison, fitting and the private Slack queue."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from opentour_cell.pilot import autonomous_cycle as cycle
from opentour_cell.pilot import repair_workflow, source_intake
from opentour_cell.pilot.learning_mandate import LearningMandates
from opentour_cell.pilot.operating_point import canonical, digest
from opentour_cell.pilot.training import LocalTrainingStore

Json = dict[str, Any]


def save(path: Path, value: Json) -> None:
    path.write_text(canonical(value) + "\n", encoding="utf-8")
    path.chmod(0o600)


def read(path: Path) -> Json:
    return json.loads(path.read_bytes())


class Catalog:
    """Owner-provided generated adapter seam. Never touches the network."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.choice = "v1"
        self.pulls = 0
        self.listed = 0
        self.rows = (9, 9)

    def discover_workplace(
        self,
        store: LocalTrainingStore,
        connection: Path,
        *,
        max_total_rows: int,
        expected_connection_revision: str,
    ) -> Json:
        assert expected_connection_revision == digest(read(connection))
        self.listed += 1
        if self.choice == "ambiguous":
            raise ValueError("ambiguous_workplace_sources")
        if sum(self.rows) > max_total_rows:
            raise ValueError("workplace_row_cap_exceeded")
        return {
            "state": "ready",
            "pair_revision": digest(self.choice),
            "sources": {
                "trace": {"source_id": "event-log"},
                "correction": {"source_id": "verified-ledger"},
            },
            "counts": {"trace_rows": self.rows[0], "correction_rows": self.rows[1]},
        }

    def acquire_workplace(
        self,
        store: LocalTrainingStore,
        connection: Path,
        *,
        expected_pair_revision: str,
        max_total_rows: int,
        expected_connection_revision: str,
    ) -> Json:
        assert expected_connection_revision == digest(
            read(connection)
        ) and expected_pair_revision == digest(self.choice)
        self.pulls += 1
        g = read(self.directory / "source-grant.json")
        return {
            "state": "ready",
            "pair_revision": expected_pair_revision,
            "snapshot_revision": digest([self.choice, g["trace_sha256"], g["correction_sha256"]]),
            "trace_path": str(self.directory / "trace.jsonl"),
            "correction_path": str(self.directory / "corrections.jsonl"),
            "grant_path": str(self.directory / "source-grant.json"),
            "counts": {"trace_rows": self.rows[0], "correction_rows": self.rows[1]},
        }


class Customer:
    def __init__(
        self,
        root: Path,
        *,
        model: bool = False,
        manual: bool = False,
        real_reader: bool = False,
        total_rows: int = 60,
        rolling: bool = False,
    ):
        self.owner = root / "owner"
        example = Path(__file__).resolve().parents[2] / "examples" / "prepare-repair-example.py"
        subprocess.run(
            [
                sys.executable,
                str(example),
                str(self.owner),
                "--route",
                "model" if model else "rule",
            ],
            check=True,
            capture_output=True,
        )
        manifest = read(self.owner / "managed-plan.json")
        self.org, self.eng = manifest["organization"], manifest["engagement"]
        self.delegate, self.mandate = "generated-worker", "generated-mandate"
        self.connector = Catalog(self.owner)
        connection = {
            "reader": {"provider": "generated-reader"},
            "scope": {
                "organization": self.org,
                "engagement": self.eng,
                "workflow": "purchase-routing",
                "field": "disposition",
            },
        }
        self.real_reader = None
        if real_reader:
            from test_workplace_context import WorkplaceFixture

            self.real_reader = WorkplaceFixture.from_repair(self.owner)
            if model:
                # New prospectively generated replay cohort for this scoped one-feature profile.
                # The earlier two-feature qualification keeps its original inconclusive receipt.
                unseen = {
                    "refund-6": "Refund issued after merchandise was returned to the shop.",
                    "refund-7": "Credit sent to the buyer for a broken appliance.",
                    "sale-6": "Payment received for a completed purchase of notebooks.",
                    "sale-7": "Sale of a new bicycle to the customer.",
                }
                for row in self.real_reader.rows["trace"]:
                    key = row["event_id"]
                    if key in unseen:
                        row.update(event_id="cycle-unseen-" + key, group_id="unseen-family-" + key)
                        row["features"]["description"] = unseen[key]
                for row in self.real_reader.rows["correction"]:
                    key = row["event_id"]
                    if key in unseen:
                        row.update(
                            event_id="cycle-unseen-" + key, outcome_id="unseen-outcome-" + key
                        )
                self.real_reader.rebuild()
            if rolling:
                self.real_reader.cfg["watermark_mode"] = "provider_verified"
                watermarks(self.real_reader, 305, 300)
            connection = self.real_reader.cfg
        else:
            save(self.owner / "connection.json", connection)
        expiry = int(time.time()) + 900
        policy = {
            "reviewer": "UREVIEWER",
            "team": "TOWNER",
            "channel": "CPRIVATE",
            "audience": "UAUDIENCE",
        }
        self.agreement = {
            k: manifest[k] for k in ("incumbent_revision", "terms", "rule", "handoff")
        }
        self.agreement.update(
            connection_revision=digest(connection),
            release_review=policy,
            slack_review={
                "team": policy["team"],
                "channel": policy["channel"],
                "audience": policy["audience"],
                "aggregate": True,
                "expires_at": expiry - 10,
                "owner_attestation": "att-" + "7" * 16,
            },
        )
        save(self.owner / "standing-plan.json", self.agreement)
        self.scope = {
            "mandate_id": self.mandate,
            "owner_id": "generated-owner",
            "delegate": self.delegate,
            "mode": "manual" if manual else "autonomous",
            "workflow": "purchase-routing",
            "connectors": ["generated-reader"],
            "trace_source": "event-log",
            "correction_source": "verified-ledger",
            "field": "disposition",
            "fields": ["description", "event_type"],
            "labels": ["refund", "sale"],
            "profile": None if not model else repair_workflow.MODEL_PROFILE,
            "uses": ["evaluate", "inspect", "train"] if model else ["evaluate", "inspect"],
            "permitted_error_pairs": [["sale", "refund"]],
            "agreement_revision": digest(self.agreement),
            "trainer": "generated-trainer",
            "payer": "generated-payer",
            "reviewer": policy["reviewer"],
            "audience": policy["audience"],
            "destination": policy["audience"],
            "expires_at": expiry,
            "max_runs": 2,
            "max_fits": 1 if model else 0,
            "max_total_seconds": 120 if model else 0,
            "max_seconds_per_fit": 120 if model else 0,
            "max_rows_per_run": 40,
            "max_total_rows": total_rows,
            "max_cost_cents": 0,
        }
        if model:
            self.scope["model_fields"] = ["description"]
        self.ledger = LearningMandates(self.owner, self.org, self.eng, owner="generated-owner")
        self.ledger.enroll(self.scope, actor="generated-owner")
        self.config = {
            "schema_version": cycle.SCHEMA,
            "organization": self.org,
            "engagement": self.eng,
            "mandate_id": self.mandate,
            "delegate": self.delegate,
            "connector": "generated-reader",
            "connection_file": self.real_reader.connection.name
            if self.real_reader
            else "connection.json",
            "agreement_file": "standing-plan.json",
        }
        save(self.owner / "cycle.json", self.config)

    def run(self) -> Json:
        return cycle.cycle_once(
            self.owner,
            self.org,
            self.config,
            connector=None if self.real_reader else self.connector,
        )


def test_rule_queues_only_exact_result_duplicate_concurrent_and_release_denied(
    tmp_path: Path,
) -> None:
    c = Customer(tmp_path)
    barrier = threading.Barrier(2)

    def concurrently(_: int) -> Json:
        barrier.wait()
        return c.run()

    with ThreadPoolExecutor(max_workers=2) as pool:
        one, two = pool.map(concurrently, range(2))
    assert one["state"] == "pending_slack_review", one
    assert two == one and c.connector.pulls == 1
    assert one["cases"] == 4 and one["outcome"] == "simple_repair"
    work = c.owner / "managed-outputs" / one["run_id"]
    assert not (work / "model").exists() and not (work / "release-decision.json").exists()
    assert len(list(c.owner.glob("approval-ready-*.json"))) == 1
    frozen = read(c.owner / ("cycle-intake-" + one["run_id"] + ".json"))
    assert frozen["authority_kind"] == "standing_customer_mandate"
    assert frozen["error_class"]["authority_kind"] == "standing_customer_mandate"
    plan = read(c.owner / ("cycle-plan-" + one["run_id"] + ".json"))
    with pytest.raises((OSError, ValueError)):
        repair_workflow.prepare_handoff(c.owner, c.org, plan, one["run_id"])
    c.ledger.withdraw(c.mandate, actor="generated-owner")
    assert c.run()["state"] == "held" and c.connector.pulls == 1
    with pytest.raises((OSError, ValueError)):
        source_intake.inspect_intake(
            LocalTrainingStore(c.owner / "training.sqlite3", c.org), frozen["intake_revision"]
        )


def test_ambiguous_changed_scope_and_manual_mandates_hold_before_content(tmp_path: Path) -> None:
    c = Customer(tmp_path)
    c.connector.choice = "ambiguous"
    assert c.run()["reason"] == "ambiguous_workplace_sources" and c.connector.pulls == 0
    c.connector.choice = "v1"
    source = read(c.owner / "connection.json")
    save(c.owner / "connection.json", source | {"unapproved_change": True})
    assert c.run()["reason"] == "workplace_connection_outside_agreement" and c.connector.pulls == 0
    save(c.owner / "connection.json", source)
    c.connector.rows = (40, 1)
    assert c.run()["reason"] == "workplace_row_cap_exceeded" and c.connector.pulls == 0
    other = tmp_path / "manual"
    other.mkdir(mode=0o700)
    manual = Customer(other, manual=True)
    assert manual.run()["reason"] == "autonomous_mandate_required" and manual.connector.listed == 0


def test_revised_catalog_cannot_score_previously_opened_holdout(tmp_path: Path) -> None:
    c = Customer(tmp_path)
    accepted = c.run()
    assert accepted["state"] == "pending_slack_review", accepted
    c.connector.choice = "new-provider-snapshot"
    changed = c.run()
    assert changed["state"] == "held", changed
    assert not list(c.owner.rglob("release-decision.json"))
    assert len(list(c.owner.glob("approval-ready-*.json"))) == 1


@pytest.mark.local_integration
def qualify_real_pretrained_cycle_fits_under_delegation_then_waits_for_slack(
    tmp_path: Path,
) -> None:
    base = os.environ["OPENTOUR_QUALIFICATION_BASE"]
    c = Customer(tmp_path, model=True, real_reader=True)
    shutil.copytree(base, c.owner / "model-base")
    result = c.run()
    assert result["state"] == "pending_slack_review" and result["outcome"] == "model_candidate", (
        result
    )
    version = read(c.owner / "managed-outputs" / result["run_id"] / "model" / "manifest.json")
    assert version["training"]["changed_parameters"] and version["training"]["steps"] > 0
    assert (
        c.run() == result
        and sum(r["method"] == "resources/read" for r in c.real_reader.received()) == 2
    )
    c.ledger.withdraw(c.mandate, actor="generated-owner")
    assert c.run()["state"] == "held"
    assert not list(c.owner.rglob("release-decision.json"))


def test_native_command_discovers_through_fresh_stdio_mcp_and_reuses_bound_snapshot(
    tmp_path: Path,
) -> None:
    c = Customer(tmp_path, real_reader=True)

    def start() -> Json:
        proc = subprocess.run(
            [
                sys.executable,
                "-B",
                "-m",
                "opentour_cell.pilot.autonomous_cycle",
                "--directory",
                str(c.owner),
                "--organization",
                c.org,
                "--config",
                str(c.owner / "cycle.json"),
                "--once",
            ],
            capture_output=True,
            text=True,
            timeout=25,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        return json.loads(proc.stdout)

    first = start()
    assert first["state"] == "pending_slack_review" and start() == first
    requests = c.real_reader.received()
    reads = [r["params"]["uri"] for r in requests if r["method"] == "resources/read"]
    assert (
        len(reads) == 2 and len(set(reads)) == 2 and all("/approved/" in value for value in reads)
    )
    assert "generated-secret-never-print" not in canonical(first)
    manifest = read(c.owner / ("cycle-plan-" + first["run_id"] + ".json"))
    assert manifest["release_review"]["provider"] == "zapier_slack"


def test_unapproved_source_and_counterfeit_confirmation_hold(tmp_path: Path) -> None:
    c = Customer(tmp_path)
    result = c.run()
    assert result["state"] == "pending_slack_review", result
    raw = read(c.owner / ("cycle-intake-" + result["run_id"] + ".json"))
    source = next(c.owner.glob("cycle-source-learning-*.json"))
    fake = raw["error_class"] | {"attestation": "att-" + "1" * 16}
    with pytest.raises(ValueError, match="stale_or_unconfirmed_delegation"):
        source_intake.freeze_intake(
            LocalTrainingStore(c.owner / "training.sqlite3", c.org),
            source,
            c.owner / "trace.jsonl",
            c.owner / "corrections.jsonl",
            c.owner / "unapproved.json",
            confirmation=fake,
        )
    assert not (c.owner / "unapproved.json").exists()
    cfg = c.config | {"connector": "foreign-reader"}
    assert cycle.cycle_once(c.owner, c.org, cfg, connector=c.connector)["state"] == "held"
    assert c.connector.pulls == 1


def test_no_remaining_budget_for_new_catalog_pair_stops_before_reads(tmp_path: Path) -> None:
    c = Customer(tmp_path, total_rows=40)
    assert c.run()["state"] == "pending_slack_review"
    c.connector.choice = "more-rows"
    c.connector.rows = (14, 14)
    assert c.run()["state"] == "held" and c.connector.pulls == 1


def watermarks(fixture: Any, trace: int, correction: int) -> None:
    for desc in fixture.catalog:
        tags = desc.get("_meta", {}).get("ai.opentour/workplace", {})
        if desc["uri"] in fixture.contents:
            limit = trace if tags["role"] == "trace" else correction
            tags["covered_through"] = limit
            fixture.contents[desc["uri"]]["projection"]["covered_through"] = limit
    fixture.save()


def test_evolving_verified_watermark_admits_only_fresh_protected_data_on_same_mandate(
    tmp_path: Path,
) -> None:
    c = Customer(tmp_path, real_reader=True, rolling=True, total_rows=80)
    f = c.real_reader
    first = c.run()
    assert first["state"] == "pending_slack_review", first
    initial = read(c.owner / ("cycle-intake-" + first["run_id"] + ".json"))
    assert initial["as_of"] == 300
    for row in f.rows["trace"]:
        if row["decision_at"] >= 200:
            key = row["event_id"]
            row.update(
                event_id="next-" + key,
                group_id="next-family-" + key,
                decision_at=408,
                features_available_at=407,
            )
            row["features"]["description"] = "Fresh generated decision for " + key
    for row in f.rows["correction"]:
        if row["outcome_at"] >= 200:
            key = row["event_id"]
            row.update(
                event_id="next-" + key,
                outcome_id="next-adjudication-" + key,
                facts_as_of=408,
                outcome_at=409,
                verified_at=410,
            )
    f.versions = {"trace": "source-v2", "correction": "source-v2"}
    f.rebuild()
    watermarks(f, 425, 415)
    second = c.run()
    assert second["state"] == "pending_slack_review" and second["run_id"] != first["run_id"], second
    later = read(c.owner / ("cycle-intake-" + second["run_id"] + ".json"))
    assert later["as_of"] == 415 and all(x["decision_at"] > 300 for x in later["protected"])
    assert len(f.reads()) == 4 and c.ledger.inspect(c.mandate)["runs"] == 2
    assert not {r["case_id"] for r in initial["protected"]} & {
        r["case_id"] for r in later["protected"]
    }
    assert c.run() == second and len(f.reads()) == 4
    watermarks(f, 445, 440)
    assert c.run()["state"] == "held" and c.ledger.inspect(c.mandate)["runs"] == 2
