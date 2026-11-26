"""Generated customer permissions and simultaneous local worker claims."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import time
import unittest
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager, closing, contextmanager
from pathlib import Path
from threading import Event
from typing import Any
from unittest.mock import patch

from opentour_cell.pilot import learning_mandate as lm
from opentour_cell.pilot import open_weight, repair_workflow
from opentour_cell.pilot.custody import Closeout
from opentour_cell.pilot.operating_point import digest
from opentour_cell.pilot.simple_repair import membership
from opentour_cell.pilot.text_classifier import TrainingError
from opentour_cell.pilot.training import LocalTrainingStore

TERMS = {
    "agreement": "generated-terms",
    "custodian": "generated-custodian",
    "support": "generated-support",
    "due": "2040-04-01",
    "trigger": "withdrawal",
}


class TestLearningMandate(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name).resolve()
        self.store = LocalTrainingStore(self.directory / "training.sqlite3", "example-org")
        self.engagement = self.store.engagement_id
        self.ledger = lm.LearningMandates(
            self.directory, "example-org", self.engagement, owner="customer-owner"
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
            lm.LearningMandates(self.directory, "example-org", self.engagement, owner="other")
        with self.assertRaisesRegex(lm.MandateError, "registered_mandate_owner_required"):
            lm.LearningMandates(self.directory, "other-org", self.engagement)
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

    def test_closeout_fences_active_and_restored_mandate_authority(self) -> None:
        with closing(sqlite3.connect(self.store.path)) as db:
            self.assertEqual(db.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
        first, second = self.admit(1), self.admit(2)
        for run in (first, second):
            self.confirm(run["run_id"])
        first_fit, second_fit = [
            self.ledger.reserve(
                "standing-one", self.reservation(run["run_id"]), actor="local-worker"
            )["reservation_id"]
            for run in (first, second)
        ]
        self.ledger.claim_fit(
            first_fit, actor="local-worker", bundle=self.directory / "started/model"
        )
        other = self.directory / "other"
        other.mkdir(mode=0o700)
        other_store = LocalTrainingStore(other / "training.sqlite3", "neighbor-org")
        neighbor = lm.LearningMandates(
            other, "neighbor-org", other_store.engagement_id, owner="customer-owner"
        )
        neighbor.enroll(self.spec, actor="customer-owner")
        backup = sqlite3.connect(":memory:")
        self.addCleanup(backup.close)
        with closing(sqlite3.connect(self.store.path)) as active:
            active.backup(backup)

        Closeout(self.directory, "example-org").withdraw(**TERMS)
        accounting = self.ledger.inspect("standing-one", actor="customer-owner")
        with closing(sqlite3.connect(self.store.path)) as restored:
            backup.backup(restored)
            self.assertEqual(restored.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        after_restart = lm.LearningMandates(self.directory, "example-org", self.engagement)
        denied: list[Callable[[], Any]] = [
            lambda: after_restart.authorize_source(
                "standing-one", self.request(1), actor="local-worker"
            ),
            lambda: self.admit(3),
            lambda: after_restart.check_run(first["run_id"], actor="local-worker"),
            lambda: self.confirm(first["run_id"]),
            lambda: after_restart.reserve(
                "standing-one", self.reservation(second["run_id"]), actor="local-worker"
            ),
            lambda: after_restart.grant(first_fit, actor="local-worker"),
            lambda: after_restart.claim_fit(
                second_fit, actor="local-worker", bundle=self.directory / "untouched/model"
            ),
            lambda: after_restart.settle(
                first_fit,
                actor="local-worker",
                elapsed_seconds=1,
                artifact_sha256=digest("unpublished"),
            ),
            lambda: after_restart.enroll(self.spec, actor="customer-owner"),
        ]
        for index, action in enumerate(denied):
            with (
                self.subTest(action=index),
                self.assertRaisesRegex(lm.MandateError, "standing_engagement_unavailable"),
            ):
                action()
        self.assertEqual(after_restart.inspect("standing-one", actor="customer-owner"), accounting)
        settled = after_restart.settle(first_fit, actor="local-worker", elapsed_seconds=1)
        self.assertIsNone(settled["artifact_sha256"])
        self.assertFalse((self.directory / "untouched").exists())
        self.assertTrue(
            neighbor.authorize_source("standing-one", self.request(1), actor="local-worker")
        )

    def test_concurrent_mandate_admission_orders_before_durable_withdrawal(self) -> None:
        entered, resume, closing_started = Event(), Event(), Event()
        make_receipt = self.ledger._source_receipt

        def pause(*args: Any) -> dict[str, Any]:
            entered.set()
            self.assertTrue(resume.wait(3))
            return make_receipt(*args)

        def withdraw() -> dict[str, Any]:
            closing_started.set()
            return Closeout(self.directory, "example-org").withdraw(**TERMS)

        with patch.object(self.ledger, "_source_receipt", pause), ThreadPoolExecutor(2) as workers:
            first = workers.submit(self.admit)
            try:
                self.assertTrue(entered.wait(3))
                with closing(sqlite3.connect(self.store.path, timeout=0)) as competing:
                    with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                        competing.execute("BEGIN IMMEDIATE")
                closeout = workers.submit(withdraw)
                self.assertTrue(closing_started.wait(3))
                self.assertFalse(closeout.done())
            finally:
                resume.set()
            self.assertEqual(
                first.result(timeout=4)["source_revision"], self.request()["source_revision"]
            )
            self.assertTrue(closeout.result(timeout=4)["reconciled"])
        with self.assertRaisesRegex(lm.MandateError, "standing_engagement_unavailable"):
            self.admit()
        self.assertEqual(self.ledger.inspect("standing-one")["runs"], 1)

    def test_numeric_worker_input_cannot_dispatch_after_engagement_withdrawal(self) -> None:
        with closing(sqlite3.connect(self.store.path)) as db:
            self.assertEqual(db.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
        profile = self.directory / "profile.json"
        profile.write_text('{"files":[]}')
        guards = 0

        @contextmanager
        def dispatch() -> Iterator[None]:
            nonlocal guards
            guards += 1
            if guards == 2:
                Closeout(self.directory, "example-org").withdraw(**TERMS)
            with self.store.engagement_guard(engagement_id=self.engagement):
                yield

        with (
            patch.object(open_weight, "_resources", return_value=(profile, profile)),
            patch.object(open_weight.subprocess, "Popen") as launch,
        ):
            worker = launch.return_value.__enter__.return_value
            worker.poll.return_value = None
            with self.assertRaisesRegex(TrainingError, "engagement_withdrawn"):
                open_weight._run(
                    self.directory / "model",
                    self.directory,
                    {"labels": ["billing", "technical"], "max_seconds": 2},
                    None,
                    {"operation": "fit"},
                    dispatch=dispatch,
                )
            launch.assert_called_once()
            worker.communicate.assert_not_called()
            worker.kill.assert_called_once()
        self.assertFalse((self.directory / "model").exists())

    def test_managed_worker_dispatch_excludes_wal_writer(self) -> None:
        with closing(sqlite3.connect(self.store.path)) as db:
            self.assertEqual(db.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")

        def inspect_dispatch(*args: Any, **kwargs: Any) -> None:
            with kwargs["dispatch"](), closing(sqlite3.connect(self.store.path, timeout=0)) as db:
                self.assertIsNone(
                    db.execute("SELECT withdrawn_at FROM engagement WHERE singleton=1").fetchone()[
                        0
                    ]
                )
                with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                    db.execute("BEGIN IMMEDIATE")

        def inspect_fit_dispatch(*args: Any, **kwargs: Any) -> None:
            inspect_dispatch(*args, **kwargs)
            raise TrainingError("generated_dispatch_checked")

        with (
            patch.object(
                open_weight, "inspect_open_weight_installation", side_effect=inspect_dispatch
            ) as inspect_installation,
            patch.object(open_weight, "fit_open_weight", side_effect=inspect_fit_dispatch),
            self.assertRaisesRegex(TrainingError, "generated_dispatch_checked"),
        ):
            repair_workflow._model_arm(
                self.directory,
                self.directory / "work",
                {"organization": "example-org", "engagement": self.engagement},
                {},
                {"features": ["question"]},
                {},
                [],
                [],
                lambda: None,
            )
        inspect_installation.assert_called_once()

    def test_numeric_publication_uses_wal_fence_and_cleans_withdrawn_staging(self) -> None:
        with closing(sqlite3.connect(self.store.path)) as db:
            self.assertEqual(db.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
        run = self.admit()
        self.confirm(run["run_id"])
        development = [
            {
                "case_id": "case-" + label,
                "family_id": "family-" + label,
                "features": {"question": "Generated " + label},
                "label": label,
            }
            for label in ("billing", "technical")
        ]
        groups = [{"case_id": row["case_id"], "group_id": row["family_id"]} for row in development]
        residual: dict[str, Any] = {
            "route": "model",
            "model_authorized": True,
            "compute_authorized": True,
            "residual_count": 2,
            "schema_version": "opentour.pilot.development-decision.v1",
            "fit_calls": 0,
            "development_case_keys": sorted(membership(groups, "case_id")),
            "development_group_keys": sorted(membership(groups, "group_id")),
        }
        revision = digest(residual)
        residual.update(revision=revision, residual_revision=revision)
        reservation = self.ledger.reserve(
            "standing-one",
            self.reservation(run["run_id"]) | {"residual_revision": revision},
            actor="local-worker",
        )
        grant = self.ledger.grant(reservation["reservation_id"], actor="local-worker")
        profile = self.directory / "profile.json"
        profile.write_text('{"files":[]}')
        nominal = self.directory / "published/model"
        withdrawn = self.directory / "withdrawn/model"
        published: list[Path] = []
        raw_rename = Path.rename

        def checked_rename(stage: Path, target: Path) -> Path:
            with closing(sqlite3.connect(self.store.path, timeout=0)) as db:
                with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                    db.execute("BEGIN IMMEDIATE")
            published.append(target)
            return raw_rename(stage, target)

        def permission() -> AbstractContextManager[None]:
            return self.store.engagement_guard(engagement_id=self.engagement)

        def withdraw_then_check() -> AbstractContextManager[None]:
            Closeout(self.directory, "example-org").withdraw(**TERMS)
            return permission()

        def fit(
            bundle: Path, dispatch: Callable[[], AbstractContextManager[None]]
        ) -> dict[str, Any]:
            return open_weight.fit_open_weight(
                bundle,
                development,
                model_dir=self.directory,
                grant=grant,
                residual=residual,
                protected_fingerprints=[digest("protected-fixture")],
                dispatch=dispatch,
            )

        with (
            patch.object(open_weight, "_resources", return_value=(profile, profile)),
            patch.object(
                open_weight,
                "_run",
                side_effect=lambda *args, **kwargs: {"adapter": {"a": "YQ=="}, "pid": 1},
            ),
            patch.object(Path, "rename", checked_rename),
        ):
            self.assertEqual(fit(nominal, permission)["training_rows"], 2)
            with self.assertRaisesRegex(TrainingError, "engagement_withdrawn"):
                fit(withdrawn, withdraw_then_check)
        self.assertEqual(published, [nominal])
        self.assertTrue(nominal.is_dir())
        self.assertFalse(withdrawn.exists())
        self.assertEqual(list(withdrawn.parent.glob(".model.pending-*")), [])

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
        rest = lm.LearningMandates(self.directory, "example-org", self.engagement)
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
            self.engagement,
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
