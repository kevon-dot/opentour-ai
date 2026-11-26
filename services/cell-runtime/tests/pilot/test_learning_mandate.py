"""Generated customer permissions and simultaneous local worker claims."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from unittest.mock import patch

from opentour_cell.pilot import learning_mandate as lm
from opentour_cell.pilot import open_weight
from opentour_cell.pilot.operating_point import digest


class TestLearningMandate(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.ledger = lm.LearningMandates(
            self.directory, "example-org", "example-engagement", owner="customer-owner"
        )
        self.spec = {
            "mandate_id": "standing-one",
            "owner_id": "customer-owner",
            "delegate": "local-worker",
            "mode": "autonomous",
            "workflow": "support",
            "connectors": ["approved-mcp"],
            "trace_source": "tickets",
            "correction_source": "outcomes",
            "field": "route",
            "fields": ["question"],
            "labels": ["billing", "technical"],
            "profile": open_weight.PROFILE,
            "uses": ["inspect", "evaluate", "train"],
            "permitted_error_pairs": [["billing", "technical"]],
            "agreement_revision": digest("operating-limits"),
            "trainer": "local-worker",
            "payer": "customer-zero-cost-cpu",
            "reviewer": "independent-reviewer",
            "audience": "private-review-channel",
            "destination": "customer-local",
            "expires_at": int(time.time()) + 600,
            "max_runs": 5,
            "max_fits": 2,
            "max_total_seconds": 30,
            "max_seconds_per_fit": 25,
            "max_rows_per_run": 40,
            "max_total_rows": 120,
            "max_cost_cents": 0,
        }
        self.ledger.enroll(self.spec, actor="customer-owner")

    def request(self, idx: int = 1) -> dict[str, Any]:
        return {
            "source_revision": digest(["source", idx]),
            "trace_sha256": digest(["traces", idx]),
            "correction_sha256": digest(["outcomes", idx]),
            "workflow": "support",
            "connector": "approved-mcp",
            "trace_source": "tickets",
            "correction_source": "outcomes",
            "field": "route",
            "fields": ["question"],
            "rows": 24,
        }

    def admit(self, idx: int = 1) -> dict[str, Any]:
        return self.ledger.authorize_source("standing-one", self.request(idx), actor="local-worker")

    def confirm(self, run: str) -> dict[str, Any]:
        pair = ["billing", "technical"]
        return self.ledger.confirm_class(
            run,
            actor="local-worker",
            discovery_revision=digest(["discovery", run]),
            candidate={
                "from_label": pair[0],
                "to_label": pair[1],
                "class_id": "class-" + digest(pair)[:24],
                "count": 4,
                "groups": 3,
            },
        )

    def reservation(self, run: str, seconds: int = 15) -> dict[str, Any]:
        return {
            "run_id": run,
            "plan_revision": digest(["plan", run]),
            "intake_revision": digest(["intake", run]),
            "residual_revision": digest(["residual", run]),
            "max_seconds": seconds,
        }

    def test_owner_and_scope_are_immutable_explicit_and_isolated(self) -> None:
        with self.assertRaisesRegex(lm.MandateError, "registered_owner_mismatch"):
            lm.LearningMandates(self.directory, "example-org", "example-engagement", owner="other")
        with self.assertRaisesRegex(lm.MandateError, "registered_mandate_owner_required"):
            lm.LearningMandates(self.directory, "other-org", "example-engagement")
        with self.assertRaisesRegex(lm.MandateError, "registered_owner_approval_required"):
            self.ledger.enroll(self.spec, actor="local-worker")
        with self.assertRaisesRegex(lm.MandateError, "immutable_mandate_conflict"):
            self.ledger.enroll(dict(self.spec, max_runs=6), actor="customer-owner")
        for key, value in [
            ("max_cost_cents", 1),
            ("max_seconds_per_fit", 301),
            ("reviewer", "local-worker"),
            ("profile", "unknown"),
            ("fields", ["reference"]),
            ("permitted_error_pairs", [["billing", "other"]]),
        ]:
            with self.subTest(key=key), self.assertRaises(lm.MandateError):
                self.ledger.enroll(
                    dict(self.spec, mandate_id="new", **{key: value}), actor="customer-owner"
                )
        self.assertEqual(
            self.ledger.inspect("standing-one", actor="customer-owner")["reserved_fits"], 0
        )

    def test_rules_spend_zero_compute_and_manual_stops_before_source(self) -> None:
        rule = dict(
            self.spec,
            mandate_id="rule",
            profile=None,
            uses=["inspect", "evaluate"],
            max_fits=0,
            max_total_seconds=0,
            max_seconds_per_fit=0,
        )
        self.ledger.enroll(rule, actor="customer-owner")
        row = self.ledger.authorize_source("rule", self.request(), actor="local-worker")
        self.confirm(row["run_id"])
        self.assertIsNone(row["training_attestation"])
        with self.assertRaisesRegex(lm.MandateError, "outside_prospective_scope"):
            self.ledger.reserve("rule", self.reservation(row["run_id"]), actor="local-worker")
        for key in ("reserved_seconds", "reserved_fits"):
            self.assertEqual(self.ledger.inspect("rule")[key], 0)
        self.ledger.enroll(dict(rule, mandate_id="manual", mode="manual"), actor="customer-owner")
        with self.assertRaisesRegex(lm.MandateError, "manual_learning_only"):
            self.ledger.authorize_source("manual", self.request(), actor="local-worker")

    def test_event_retry_class_confirmation_and_scope_stay_bound(self) -> None:
        with ThreadPoolExecutor(max_workers=6) as workers:
            events = list(workers.map(lambda _: self.admit(), range(12)))
        self.assertTrue(all(e == events[0] for e in events))
        event = events[0]
        self.assertEqual(event, self.ledger.check_run(event["run_id"], actor="local-worker"))
        self.assertEqual(self.ledger.inspect("standing-one")["rows"], 24)
        self.assertEqual(event["authority_kind"], "standing_customer_mandate")
        self.assertEqual(
            len(
                {
                    event[k]
                    for k in ("source_attestation", "training_attestation", "agreement_attestation")
                }
            ),
            3,
        )
        for field, value, error in [
            ("source_revision", digest("alias"), "source_change_conflict"),
            ("trace_sha256", digest("altered"), "source_change_conflict"),
            ("rows", 41, "row_budget"),
            ("connector", "other", "outside_prospective"),
            ("correction_source", "different", "outside_prospective"),
            ("workflow", "different", "outside_prospective"),
            ("fields", ["secret"], "outside_prospective"),
            ("organization", "other", "scope_mismatch"),
        ]:
            with self.subTest(field=field), self.assertRaisesRegex(lm.MandateError, error):
                self.ledger.authorize_source(
                    "standing-one", dict(self.request(), **{field: value}), actor="local-worker"
                )
        with self.assertRaisesRegex(lm.MandateError, "confirmation_required"):
            self.ledger.reserve(
                "standing-one", self.reservation(event["run_id"]), actor="local-worker"
            )
        case = self.confirm(event["run_id"])
        self.assertEqual(case, self.confirm(event["run_id"]))
        self.assertEqual(case["schema_version"], lm.CLASS)
        for pair, groups in [(["technical", "billing"], 2), (["billing", "technical"], 1)]:
            with self.assertRaisesRegex(lm.MandateError, "unapproved_recurring_error_class"):
                self.ledger.confirm_class(
                    event["run_id"],
                    actor="local-worker",
                    discovery_revision=digest("changed"),
                    candidate={
                        "class_id": "class-" + digest(pair)[:24],
                        "from_label": pair[0],
                        "to_label": pair[1],
                        "count": 3,
                        "groups": groups,
                    },
                )

    def test_concurrent_model_budget_charges_uncertain_reservations(self) -> None:
        keys = [self.admit(i)["run_id"] for i in range(5)]
        for key in keys:
            self.confirm(key)

        def reserve(run: str) -> str:
            try:
                return str(
                    self.ledger.reserve(
                        "standing-one", self.reservation(run, 20), actor="local-worker"
                    )["reservation_id"]
                )
            except lm.MandateError as exc:
                return str(exc)

        with ThreadPoolExecutor(max_workers=5) as workers:
            outcomes = list(workers.map(reserve, keys))
        wins = [x for x in outcomes if x.startswith("fit-")]
        self.assertEqual(len(wins), 1)
        self.assertEqual(outcomes.count("standing_fit_budget_exhausted"), 4)
        rid = wins[0]
        permission = self.ledger.grant(rid, actor="local-worker")
        run = permission["run_id"]
        self.assertEqual(
            self.ledger.reserve("standing-one", self.reservation(run, 20), actor="local-worker")[
                "reservation_id"
            ],
            rid,
        )
        with self.assertRaisesRegex(lm.MandateError, "fit_reservation_conflict"):
            self.ledger.reserve("standing-one", self.reservation(run, 19), actor="local-worker")
        self.assertEqual(permission["profile"], open_weight.PROFILE)
        self.assertEqual(permission["publication"], "independent_slack_approval_required")

        def claim(_: int) -> str:
            try:
                return str(
                    self.ledger.claim_fit(
                        rid, actor="local-worker", bundle=self.directory / "output" / "model"
                    )["rights_attestation"]
                )
            except lm.MandateError as exc:
                return str(exc)

        with ThreadPoolExecutor(max_workers=6) as workers:
            claims = list(workers.map(claim, range(6)))
        self.assertEqual(claims.count(permission["rights_attestation"]), 1)
        self.assertEqual(claims.count("fit_claim_already_used"), 5)
        rest = lm.LearningMandates(self.directory, "example-org", "example-engagement")
        self.assertEqual(rest.inspect("standing-one")["reserved_seconds"], 20)
        with self.assertRaisesRegex(open_weight.OpenWeightError, "incomplete"):
            rest.settle(
                rid, actor="local-worker", elapsed_seconds=2, artifact_sha256=digest("fabrication")
            )
        settled = rest.settle(rid, actor="local-worker", elapsed_seconds=2)
        self.assertEqual(settled, rest.settle(rid, actor="local-worker", elapsed_seconds=2))
        self.assertEqual(rest.inspect("standing-one")["reserved_seconds"], 20)
        with self.assertRaisesRegex(lm.MandateError, "immutable_fit_settlement_conflict"):
            rest.settle(rid, actor="local-worker", elapsed_seconds=1)

    def test_expiry_withdrawal_stop_replay_and_running_bundle(self) -> None:
        row = self.admit()
        self.confirm(row["run_id"])
        rid = self.ledger.reserve(
            "standing-one", self.reservation(row["run_id"]), actor="local-worker"
        )["reservation_id"]
        with self.assertRaisesRegex(lm.MandateError, "private_new_fit_destination"):
            self.ledger.claim_fit(
                rid, actor="local-worker", bundle=self.directory.parent / "elsewhere"
            )
        original = self.ledger.claim_fit(
            rid, actor="local-worker", bundle=self.directory / "output" / "model"
        )
        with patch(
            "opentour_cell.pilot.learning_mandate.time.time", return_value=self.spec["expires_at"]
        ):
            actions: list[Callable[[], Any]] = [
                lambda: self.ledger.grant(rid, actor="local-worker"),
                lambda: self.ledger.check_run(row["run_id"], actor="local-worker"),
                lambda: self.confirm(row["run_id"]),
            ]
            for action in actions:
                with self.assertRaisesRegex(lm.MandateError, "expired"):
                    action()
        with self.assertRaisesRegex(lm.MandateError, "registered_owner_approval_required"):
            self.ledger.withdraw("standing-one", actor="local-worker")
        receipt = self.ledger.withdraw("standing-one", actor="customer-owner")
        self.assertEqual(receipt["signaled_fit_destinations"], 1)
        self.assertEqual(receipt, self.ledger.withdraw("standing-one", actor="customer-owner"))
        self.assertTrue(
            json.loads((self.directory / "output/.model.withdrawn").read_bytes())["grant_sha256"]
        )
        with self.assertRaisesRegex(open_weight.OpenWeightError, "withdrawn"):
            open_weight.inspect_open_weight(self.directory / "output/model", grant=original)
        with self.assertRaisesRegex(lm.MandateError, "withdrawn"):
            self.ledger.grant(rid, actor="local-worker")
        with self.assertRaisesRegex(lm.MandateError, "withdrawn"):
            self.admit(2)
        with self.assertRaisesRegex(lm.MandateError, "withdrawn"):
            self.ledger.enroll(self.spec, actor="customer-owner")

    def test_each_new_source_consumes_whole_run_and_total_row_caps(self) -> None:
        for idx in range(5):
            self.admit(idx)
        before = self.ledger.inspect("standing-one")
        self.assertEqual((before["runs"], before["rows"]), (5, 120))
        with self.assertRaisesRegex(lm.MandateError, "standing_source_budget_exhausted"):
            self.admit(10)
        self.assertEqual(self.admit(0)["source_revision"], self.request(0)["source_revision"])

    def test_prospective_model_fields_and_closed_renewal_history(self) -> None:
        narrowed = dict(
            self.spec,
            mandate_id="projection",
            fields=["question", "category"],
            model_fields=["question"],
        )
        self.ledger.enroll(narrowed, actor="customer-owner")
        admitted = self.ledger.authorize_source(
            "projection",
            dict(self.request(), fields=["question", "category"]),
            actor="local-worker",
        )
        self.confirm(admitted["run_id"])
        r = self.ledger.reserve(
            "projection", self.reservation(admitted["run_id"]), actor="local-worker"
        )
        self.assertEqual(
            self.ledger.grant(r["reservation_id"], actor="local-worker")["features"], ["question"]
        )
        other = self.ledger.authorize_source(
            "projection", dict(self.request(2), fields=["category"]), actor="local-worker"
        )
        self.confirm(other["run_id"])
        with self.assertRaisesRegex(lm.MandateError, "model_projection_outside_admitted_source"):
            self.ledger.reserve(
                "projection", self.reservation(other["run_id"]), actor="local-worker"
            )
        with self.assertRaisesRegex(lm.MandateError, "model_projection_outside_authority"):
            self.ledger.enroll(
                dict(narrowed, mandate_id="bad", model_fields=["secret"]), actor="customer-owner"
            )
        new = dict(narrowed, mandate_id="next")
        with self.assertRaisesRegex(lm.MandateError, "prior_mandate_still_active"):
            self.ledger.reauthorize("projection", new, actor="customer-owner")
        before = self.ledger.inspect("projection")
        self.ledger.withdraw("projection", actor="customer-owner")
        repaired = self.ledger.reauthorize("projection", new, actor="customer-owner")
        self.assertEqual(
            repaired, self.ledger.reauthorize("projection", new, actor="customer-owner")
        )
        self.assertEqual(repaired["prior_revision"], before["mandate_revision"])
        self.assertEqual(self.ledger.inspect("projection")["reserved_fits"], 1)
        self.assertEqual(self.ledger.inspect("next")["reserved_fits"], 0)
        with self.assertRaisesRegex(lm.MandateError, "reauthorization_conflict"):
            self.ledger.reauthorize(
                "projection", dict(new, mandate_id="another"), actor="customer-owner"
            )

    def test_operator_cli_requires_private_spec_and_reports_independent_owner_action(self) -> None:
        import contextlib
        import io

        path = self.directory / "spec.json"
        path.write_text(json.dumps(dict(self.spec, mandate_id="cli")))
        common = [
            "--directory",
            str(self.directory),
            "--organization",
            "example-org",
            "--engagement",
            "example-engagement",
        ]

        def invoke(command: str, *args: str) -> tuple[int, dict[str, Any]]:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = lm.main([command, *common, *args])
            return code, json.loads(out.getvalue())

        path.chmod(0o644)
        self.assertEqual(
            invoke("enroll", "--owner", "customer-owner", "--spec", str(path)),
            (2, {"state": "held", "reason": "private_mandate_spec_required"}),
        )
        path.chmod(0o600)
        code, receipt = invoke("enroll", "--owner", "customer-owner", "--spec", str(path))
        self.assertEqual(code, 0)
        self.assertEqual(receipt["owner_id"], "customer-owner")
        self.assertEqual(
            invoke("status", "--actor", "local-worker", "--mandate", "cli")[1]["runs"], 0
        )
        self.assertEqual(invoke("withdraw", "--owner", "customer-owner", "--mandate", "cli")[0], 0)
        path.write_text(json.dumps(dict(self.spec, mandate_id="cli-replacement")))
        code, receipt = invoke(
            "reauthorize", "--owner", "customer-owner", "--supersedes", "cli", "--spec", str(path)
        )
        self.assertEqual(code, 0)
        self.assertEqual(receipt["supersedes"], "cli")

    def test_damaged_destination_cannot_prevent_other_cancellations(self) -> None:
        for i in range(2):
            admitted = self.admit(i)
            self.confirm(admitted["run_id"])
            reservation = self.ledger.reserve(
                "standing-one", self.reservation(admitted["run_id"]), actor="local-worker"
            )
            self.ledger.claim_fit(
                reservation["reservation_id"],
                actor="local-worker",
                bundle=self.directory / f"output-{i}" / "model",
            )
        corrupt = self.directory / "output-0/model"
        corrupt.mkdir(parents=True)
        (corrupt / "manifest.json").write_text("{}")
        result = self.ledger.withdraw("standing-one", actor="customer-owner")
        self.assertEqual(result["pending_fit_destinations"], 1)
        self.assertEqual(result["signaled_fit_destinations"], 1)
        self.assertTrue((self.directory / "output-1/.model.withdrawn").is_file())
        with self.assertRaisesRegex(lm.MandateError, "withdrawn"):
            self.admit(9)
