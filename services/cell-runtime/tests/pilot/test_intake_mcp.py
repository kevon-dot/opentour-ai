"""Generated two-session and durable approval-boundary proof for the intake surface."""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from opentour_cell.pilot import source_intake
from opentour_cell.pilot.intake_admission import AuthorizedSource, DurableIntake
from opentour_cell.pilot.intake_local import (
    SELECTION_VERSION,
    STARTER_GRANT_VERSION,
    STARTER_PURPOSE,
    STARTER_SELECTION_VERSION,
    LocalHistoricalAuthority,
)
from opentour_cell.pilot.intake_mcp import (
    DISCOVER,
    GET_RUN,
    SUBMIT,
    BenchmarkView,
    CatalogFeed,
    CatalogPage,
    CatalogSource,
    IntakeError,
    IntakePrincipal,
    LiveBinding,
    serve_intake,
)
from opentour_cell.pilot.learning_mandate import LearningMandates, MandateError
from opentour_cell.pilot.mcp import PROTOCOL_VERSION
from opentour_cell.pilot.operating_point import canonical
from opentour_cell.pilot.training import LocalTrainingStore

ACTOR_A = IntakePrincipal("org-a", "worker", "intake")
PAIR = {
    "pair_ref": "allowed-pair",
    "source_revision": "trace-v1",
    "correction_revision": "verified-v1",
    "policy_revision": "policy-v1",
    "idempotency_key": "first",
}
STARTER = {
    "mode": "starter",
    "creation_ref": "creation-a",
    "policy_resource_ref": "policy-resource",
    "source_revision": "policy-source-v1",
    "setup_policy_revision": "setup-v1",
    "idempotency_key": "starter",
}
LIVE = {
    "mode": "live",
    "feed_binding_ref": "decision-feed",
    "source_revision": "feed-v1",
    "idempotency_key": "live",
}


class _GeneratedAuthority:
    def __init__(self, directory: Path):
        self.directory = directory
        self.flags = {}

    @contextmanager
    def guard(self, actor):
        self.flags = json.loads((self.directory / "fixture-state.json").read_text())
        if self.flags.get("expired"):
            raise IntakeError("intake_expired")
        yield

    def historical(self, actor, request):
        store = LearningMandates(self.directory, actor.tenant, "epoch-a")
        grant = store.inspect("fixture-mandate", actor=actor.actor)
        if grant["expired"]:
            raise IntakeError("intake_expired")
        scope = grant["scope"]
        if (
            grant["withdrawn"]
            or grant["delegate"] != actor.actor
            or request.pair_ref != PAIR["pair_ref"]
            or request.source_revision not in {"trace-v1", "trace-v2"}
            or request.correction_revision != PAIR["correction_revision"]
            or request.policy_revision != PAIR["policy_revision"]
        ):
            return None
        return AuthorizedSource(
            scope["workflow"],
            "epoch-a",
            "evaluation",
            "pair-resource",
            grant["mandate_revision"],
        )

    def starter(self, _actor, request):
        if request.policy_resource_ref != STARTER["policy_resource_ref"]:
            return None
        if request.source_revision != STARTER["source_revision"]:
            return None
        if request.setup_policy_revision != STARTER["setup_policy_revision"]:
            return None
        if request.creation_ref not in ("creation-a", "creation-b", None):
            return None
        return AuthorizedSource(
            request.workflow_ref, "epoch-a", "starter", "policy-resource", "setup-v1"
        )

    def resolve(self, reference, actor):
        if actor.tenant != "org-a":
            return None
        if reference == "outcome-feed" and self.flags.get("include_outcome"):
            return LiveBinding(
                actor.tenant,
                "generated-observed-workflow",
                "epoch-a",
                "outcome-binding",
                "v1",
                "independent_outcome",
                "outcome-resource",
                "grant-outcome",
                "current",
                "outcome-v1",
            )
        if reference not in {"decision-feed", "decision-successor"}:
            return None
        original = LiveBinding(
            actor.tenant,
            "generated-observed-workflow",
            "epoch-a",
            "binding-id",
            "v1",
            "decision",
            self.flags.get("resource", "decision-resource"),
            "grant-v1",
            "current",
            self.flags.get("revision", "feed-v1"),
        )
        if original.engagement_epoch != self.flags.get("active_epoch", "epoch-a"):
            return None
        if self.flags.get("foreign_binding"):
            return replace(original, tenant="org-b")
        if self.flags.get("revoked"):
            return replace(original, state="revoked")
        if self.flags.get("superseded") and reference == "decision-feed":
            return replace(original, state="superseded", successor_ref="decision-successor")
        if reference == "decision-successor":
            if not self.flags.get("permit_successor"):
                return None
            return replace(original, binding_version="v2")
        return original

    def discover(self, actor, *, limit, cursor):
        sources = [CatalogSource("policy", "policy-resource", "policy-source-v1")]
        for ref in ("decision-feed", "outcome-feed"):
            item = self.resolve(ref, actor)
            if item is not None and item.state == "current":
                sources.append(
                    CatalogFeed(ref, item.binding_version, item.feed_kind, item.source_revision)
                )
            elif ref == "decision-feed" and self.flags.get("stale_catalog_entry"):
                sources.append(CatalogFeed(ref, "v1", "decision", "feed-v1"))
        if cursor not in (None, "next-page"):
            raise IntakeError("invalid_arguments")
        if cursor:
            sources = sources[1:]
        return CatalogPage(tuple(sources[:limit]), "next-page" if len(sources) > limit else None)

    def live_reader_available(self, _actor, _workflow):
        if self.flags.get("forbid_reader_call"):
            raise AssertionError("discovery invoked the live reader boundary")
        return not bool(self.flags.get("missing_reader"))

    def permits_live_revision(self, _actor, binding, source_revision):
        return source_revision in {"feed-v1", binding.source_revision}

    def benchmark(self, actor, workflow):
        if actor.tenant == "org-a" and workflow == "generated-observed-workflow":
            return BenchmarkView("frozen-benchmark", 3, "frozen")
        return None


def _mandate(directory: Path, tenant: str, engagement: str = "epoch-a", *, max_runs: int = 3):
    store = LearningMandates(directory, tenant, engagement, owner="owner")
    template = Path(__file__).resolve().parents[2] / "examples/pilot-learning-mandate.template.json"
    scope = json.loads(template.read_text())
    scope.update(
        mandate_id="fixture-mandate",
        owner_id="owner",
        delegate="worker",
        trainer="worker",
        expires_at=int(time.time()) + 900,
        agreement_revision="e" * 64,
        max_runs=max_runs,
    )
    return store.enroll(scope, actor="owner")


STARTER_FIXTURE_KEY = "c" * 64


def _write_private(path: Path, value) -> None:
    path.write_text(json.dumps(value))
    path.chmod(0o600)


def _sealed_starter(selection, **changes):
    body = {
        **selection,
        "schema_version": STARTER_GRANT_VERSION,
        "custodian_id": "policy-custodian",
        "purpose": STARTER_PURPOSE,
        "policy_resource_ref": STARTER["policy_resource_ref"],
        "source_revision": STARTER["source_revision"],
        "setup_policy_revision": STARTER["setup_policy_revision"],
        "creation_ref": STARTER["creation_ref"],
        "expires_at": int(time.time()) + 600,
        **changes,
    }
    seal = hmac.digest(bytes.fromhex(STARTER_FIXTURE_KEY), canonical(body).encode(), "sha256")
    return {"grant": body, "seal_sha256": seal.hex()}


def _starter_only(directory: Path):
    directory.mkdir(mode=0o700)
    training = LocalTrainingStore(directory / "training.sqlite3", "org-a")
    LearningMandates(directory, "org-a", training.engagement_id, owner="founder")
    selection = {
        "schema_version": STARTER_SELECTION_VERSION,
        "tenant": "org-a",
        "actor": "worker",
        "owner_id": "founder",
        "engagement_epoch": training.engagement_id,
        "grant_ref": "registered-starter",
    }
    selection_path = directory / "selection.json"
    _write_private(selection_path, selection)
    _write_private(directory / "starter-intake-key.json", {"key": STARTER_FIXTURE_KEY})
    _write_private(
        directory / "starter-intake-grant-registered-starter.json", _sealed_starter(selection)
    )
    return selection_path, selection


def _stdio_main(directory: Path, tenant: str, role: str, crash: bool) -> int:
    admission = DurableIntake(directory, _GeneratedAuthority(directory))
    if crash:
        original = admission.submit_intake

        def submit(actor, request):
            original(actor, request)
            os._exit(75)

        admission.submit_intake = submit

    try:
        return serve_intake(
            IntakePrincipal(tenant, "worker", role), admission, sys.stdin.buffer, sys.stdout.buffer
        )
    except IntakeError as error:
        print(str(error), file=sys.stderr)
        return 2


class TestIntakeMcp(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.flags()
        _mandate(self.root, "org-a")
        _mandate(self.root, "org-b")

    def flags(self, **flags):
        (self.root / "fixture-state.json").write_text(json.dumps(flags))

    def conversation(
        self,
        *calls,
        tenant="org-a",
        role="intake",
        crash=False,
        check=False,
        selection=None,
    ):
        frames = [
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "generated", "version": "1"},
                },
            },
            {"method": "notifications/initialized"},
            {"id": 2, "method": "tools/list"},
        ]
        for index, (name, arguments) in enumerate(calls, start=3):
            frames.append(
                {
                    "id": index,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                }
            )
        request = "".join(json.dumps({"jsonrpc": "2.0", **frame}) + "\n" for frame in frames)
        command = (
            [sys.executable, "-m", "opentour_cell.pilot.intake_local", str(selection)]
            if selection is not None
            else [sys.executable, __file__, str(self.root), tenant, role, str(int(crash))]
        )
        result = subprocess.run(
            command,
            input=request,
            text=True,
            capture_output=True,
            timeout=15,
            check=check,
        )
        return result, {
            item.get("id"): item for item in map(json.loads, result.stdout.splitlines())
        }

    def records(self, table, tenant="org-a"):
        queries = {
            "runs": "SELECT run_ref FROM intake_runs WHERE tenant=?",
            "workflows": "SELECT workflow_ref FROM intake_workflows WHERE tenant=?",
        }
        with sqlite3.connect(self.root / "intake.sqlite3") as db:
            return [row[0] for row in db.execute(queries[table], (tenant,))]

    def call(self, name, args, **kwargs):
        _, rows = self.conversation((name, args), check=True, **kwargs)
        return rows[3].get("result", rows[3])

    def submit(self, args, **kwargs):
        return self.call(SUBMIT, args, **kwargs)["structuredContent"]

    def run_status(self, ref, **kwargs):
        return self.call(GET_RUN, {"run_ref": ref}, **kwargs)["structuredContent"]

    def assert_tool_error(self, code, name, arguments, **kwargs):
        result = self.call(name, arguments, **kwargs)
        self.assertTrue(result["isError"])
        self.assertEqual(result["content"], [{"type": "text", "text": code}])

    def test_exact_tool_list_and_privilege(self):
        _, rows = self.conversation(("start_offline_evaluation", {}), check=True)
        self.assertEqual(
            [tool["name"] for tool in rows[2]["result"]["tools"]], [DISCOVER, SUBMIT, GET_RUN]
        )
        self.assertNotIn("result", rows[3])
        result, denied = self.conversation(role="reviewer")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(denied, {})
        self.assertIn("intake_role_required", result.stderr)

    def test_no_mode_historical_pair_and_new_key_same_intent(self):
        first = self.submit(PAIR)
        self.assertEqual(set(first), {"run_ref", "state", "correlation_ref"})
        other = self.submit({**PAIR, "mode": "historical", "idempotency_key": "other"})
        self.assertEqual(first, other)
        self.assertEqual(self.records("runs"), [first["run_ref"]])
        with sqlite3.connect(self.root / "learning-mandates.sqlite3") as db:
            self.assertEqual(db.execute("SELECT count(*) FROM reservations").fetchone()[0], 0)

    def test_changed_intent_conflicts_with_retained_key(self):
        first = self.submit(PAIR)
        self.assert_tool_error(
            "idempotency_conflict", SUBMIT, {**PAIR, "source_revision": "trace-v2"}
        )
        self.assertEqual(self.records("runs"), [first["run_ref"]])

    def test_retry_returns_same_workflow_id(self):
        first = self.submit(STARTER)
        status = self.run_status(first["run_ref"])
        same = self.submit({**STARTER, "idempotency_key": "new-starter-key"})
        self.assertEqual(same, first)
        by_workflow = {k: v for k, v in STARTER.items() if k != "creation_ref"}
        self.assertEqual(
            self.submit({**by_workflow, "workflow_ref": status["workflow_ref"]}), first
        )
        self.assertEqual(self.records("workflows"), [status["workflow_ref"]])

    def test_get_run_schema(self):
        first = self.submit(STARTER)
        self.assertEqual(
            set(self.run_status(first["run_ref"])),
            {"run_ref", "state", "next_action", "reason_code", "workflow_ref"},
        )

    def test_two_sessions_same_refs(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = pool.map(
                lambda key: self.submit({**STARTER, "idempotency_key": key}), ("one", "two")
            )
        self.assertEqual(first, second)
        foreign = self.submit(STARTER, tenant="org-b")
        self.assertNotEqual(first["run_ref"], foreign["run_ref"])
        self.assertNotEqual(
            self.run_status(first["run_ref"])["workflow_ref"],
            self.run_status(foreign["run_ref"], tenant="org-b")["workflow_ref"],
        )
        self.assertEqual(
            self.call(GET_RUN, {"run_ref": foreign["run_ref"]}),
            self.call(GET_RUN, {"run_ref": "unknown-run"}),
        )

    def test_crash_after_commit_followed_by_retry(self):
        process, rows = self.conversation((SUBMIT, PAIR), crash=True)
        self.assertEqual(process.returncode, 75)
        self.assertNotIn(3, rows)
        (run_ref,) = self.records("runs")
        self.assertEqual(self.submit({**PAIR, "idempotency_key": "recovery"})["run_ref"], run_ref)

    def test_live_new_key_same_intent_and_new_revision(self):
        first = self.submit(LIVE)
        self.assertEqual(first, self.submit({**LIVE, "idempotency_key": "another-live"}))
        before = self.run_status(first["run_ref"])
        self.flags(revision="feed-v2")
        reusing = {**LIVE, "source_revision": "feed-v2"}
        self.assert_tool_error("idempotency_conflict", SUBMIT, reusing)
        second = self.submit({**reusing, "idempotency_key": "live-v2"})
        after = self.run_status(second["run_ref"])
        self.assertNotEqual(first["run_ref"], second["run_ref"])
        self.assertEqual(
            [
                before[key]
                for key in ("workflow_ref", "benchmark_ref", "version", "benchmark_state")
            ],
            [after[key] for key in ("workflow_ref", "benchmark_ref", "version", "benchmark_state")],
        )
        self.assertEqual(self.records("workflows"), [])

    def test_live_extra_field_rejected(self):
        for field in ("workflow_ref", "correction_revision", "case", "tenant", "decision"):
            with self.subTest(field=field):
                self.assert_tool_error("invalid_arguments", SUBMIT, {**LIVE, field: "forbidden"})
        self.assertEqual(self.records("runs"), [])

    def test_discover_returns_binding_revision(self):
        result = self.call(DISCOVER, {"limit": 1})["structuredContent"]
        self.assertEqual(len(result["sources"]), 1)
        second = self.call(DISCOVER, {"limit": 1, "cursor": result["next_cursor"]})[
            "structuredContent"
        ]
        self.assertEqual(
            second,
            {
                "sources": [
                    {
                        "feed_binding_ref": "decision-feed",
                        "binding_version": "v1",
                        "feed_kind": "decision",
                        "source_revision": "feed-v1",
                    }
                ],
                "next_cursor": None,
            },
        )
        foreign = self.call(DISCOVER, {}, tenant="org-b")["structuredContent"]
        self.assertNotIn("decision-feed", json.dumps(foreign))

    def test_superseded_binding_version_held(self):
        self.flags(superseded=True)
        self.assertEqual(self.submit(LIVE), {"state": "held", "reason_code": "binding_superseded"})
        self.flags(superseded=True, permit_successor=True)
        hold = self.submit(LIVE)
        self.assertEqual(hold["successor_feed_binding_ref"], "decision-successor")
        self.assertEqual(self.records("runs"), [])

    def test_retry_after_revocation_discloses_nothing(self):
        first = self.submit(LIVE)
        self.flags(revoked=True)
        self.assert_tool_error("intake_unavailable", SUBMIT, LIVE)
        self.assert_tool_error("intake_unavailable", GET_RUN, {"run_ref": first["run_ref"]})
        self.assert_tool_error(
            "intake_unavailable", SUBMIT, {**PAIR, "idempotency_key": LIVE["idempotency_key"]}
        )
        self.assertEqual(self.records("runs"), [first["run_ref"]])

    def test_missing_reader_pending_without_new_workflow(self):
        self.flags(missing_reader=True)
        first = self.submit(LIVE)
        state = self.run_status(first["run_ref"])
        self.assertEqual(state["state"], "pending")
        self.assertEqual(state["reason_code"], "live_reader_unavailable")
        self.assertEqual(self.records("workflows"), [])
        self.flags()
        self.assertEqual(self.run_status(first["run_ref"])["state"], "queued")

    def test_expired_and_overlong_ref(self):
        self.assert_tool_error("invalid_arguments", SUBMIT, {**PAIR, "pair_ref": "a" * 129})
        self.flags(expired=True)
        self.assert_tool_error("intake_expired", SUBMIT, PAIR)
        self.assertEqual(self.records("runs"), [])

    def test_mixed_mode_and_bounded_rate(self):
        mixed = {**STARTER, "workflow_ref": "cannot-have-both"}
        self.assert_tool_error("invalid_arguments", SUBMIT, mixed)
        self.assert_tool_error("invalid_arguments", DISCOVER, {"limit": 51})
        _, rows = self.conversation(*([(DISCOVER, {})] * 121), check=True)
        self.assertEqual(
            rows[123]["result"]["content"], [{"type": "text", "text": "intake_rate_limited"}]
        )
        self.assertEqual(self.records("runs"), [])

    def test_materialized_status_is_durable_and_a_duplicate_worker_does_no_reads(self):
        queue = DurableIntake(self.root, _GeneratedAuthority(self.root))
        ref = self.submit(LIVE)["run_ref"]
        before = self.run_status(ref)
        reads = []

        def read(work):
            reads.append(work)
            return "opaque-private-receipt"

        self.assertTrue(queue.materialize(ACTOR_A, ref, read))
        self.assertFalse(queue.materialize(ACTOR_A, ref, read))
        self.assertEqual(len(reads), 1)
        self.assertEqual(reads[0].revisions, ("v1", "feed-v1"))
        after = self.run_status(ref)
        self.assertEqual(after["state"], "materialized")
        self.assertEqual(
            after,
            {
                **before,
                "state": "materialized",
                "next_action": "inspect_workflow",
            },
        )
        self.assertNotIn("opaque-private-receipt", json.dumps(after))

    def test_worker_fences_absent_reader_before_read_and_revocation_before_commit(self):
        queue = DurableIntake(self.root, _GeneratedAuthority(self.root))
        self.flags(missing_reader=True)
        ref = self.submit(LIVE)["run_ref"]
        self.assertFalse(queue.materialize(ACTOR_A, ref, lambda _: self.fail("forbidden read")))
        self.flags()

        def read(_):
            self.flags(revoked=True)
            return "uncommitted-receipt"

        with self.assertRaisesRegex(IntakeError, "intake_unavailable"):
            queue.materialize(ACTOR_A, ref, read)
        with sqlite3.connect(self.root / "intake.sqlite3") as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM intake_materialization").fetchone()[0], 0
            )
        self.assert_tool_error("intake_unavailable", GET_RUN, {"run_ref": ref})

    def test_live_worker_preserves_exact_resource_and_refuses_immutable_retarget(self):
        queue = DurableIntake(self.root, _GeneratedAuthority(self.root))
        ref = self.submit(LIVE)["run_ref"]
        observed = []

        def read(work):
            observed.append(work.resource.resource_id)
            self.assertEqual(work.resource.binding_id, "binding-id")
            self.flags(resource="different-resource", revision="feed-v2")
            return "uncommitted-retargeted-receipt"

        with self.assertRaisesRegex(IntakeError, "intake_unavailable"):
            queue.materialize(ACTOR_A, ref, read)
        self.assertEqual(observed, ["decision-resource"])
        self.assert_tool_error("intake_unavailable", GET_RUN, {"run_ref": ref})
        self.assert_tool_error("intake_unavailable", SUBMIT, LIVE)
        self.assert_tool_error(
            "intake_unavailable",
            SUBMIT,
            {
                **LIVE,
                "source_revision": "feed-v2",
                "idempotency_key": "retarget-new-key",
            },
        )
        self.assertEqual(self.records("runs"), [ref])
        with sqlite3.connect(self.root / "intake.sqlite3") as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM intake_materialization").fetchone()[0], 0
            )
        self.flags()
        self.assertTrue(queue.materialize(ACTOR_A, ref, lambda _: "current-resource-receipt"))

    def test_discovery_hides_retargeted_or_revoked_feed_without_writing(self):
        run = self.submit(LIVE)
        for rejected in (
            {"resource": "retargeted"},
            {"revoked": True, "stale_catalog_entry": True},
        ):
            with self.subTest(rejected=rejected):
                self.flags(include_outcome=True, forbid_reader_call=True, **rejected)
                discovery = self.call(DISCOVER, {})["structuredContent"]
                self.assertEqual(
                    [source.get("feed_binding_ref") for source in discovery["sources"]],
                    [None, "outcome-feed"],
                )
                self.assertEqual(discovery["sources"][1]["feed_kind"], "independent_outcome")
                self.assertEqual(self.records("runs"), [run["run_ref"]])
                with sqlite3.connect(self.root / "intake.sqlite3") as db:
                    self.assertEqual(
                        db.execute("SELECT count(*) FROM intake_live_resources").fetchone()[0], 1
                    )
                    self.assertEqual(
                        db.execute("SELECT count(*) FROM intake_materialization").fetchone()[0], 0
                    )

    def test_normal_starter_uses_only_scoped_policy_and_resumes_its_workflow(self):
        selected_path, selection = _starter_only(self.root / "starter-only")
        directory = selected_path.parent
        _, first = self.conversation(
            (DISCOVER, {}),
            (SUBMIT, STARTER),
            (SUBMIT, {**STARTER, "source_revision": "unapproved"}),
            (SUBMIT, {**STARTER, "creation_ref": "unapproved"}),
            (SUBMIT, {**STARTER, "tenant": "other-tenant"}),
            (SUBMIT, {**STARTER, "fit_budget": "unapproved"}),
            (SUBMIT, LIVE),
            selection=selected_path,
            check=True,
        )
        self.assertEqual(
            first[3]["result"]["structuredContent"]["sources"],
            [
                {
                    "source_kind": "policy",
                    "source_ref": STARTER["policy_resource_ref"],
                    "source_revision": STARTER["source_revision"],
                }
            ],
        )
        original = first[4]["result"]["structuredContent"]
        for idx in (5, 6, 7, 8, 9):
            self.assertTrue(first[idx]["result"]["isError"])
        _, restarted = self.conversation(
            (GET_RUN, {"run_ref": original["run_ref"]}),
            (SUBMIT, {**STARTER, "idempotency_key": "restart"}),
            selection=selected_path,
            check=True,
        )
        workflow = restarted[3]["result"]["structuredContent"]["workflow_ref"]
        self.assertEqual(restarted[4]["result"]["structuredContent"], original)
        workflow_request = {
            **{k: v for k, v in STARTER.items() if k != "creation_ref"},
            "workflow_ref": workflow,
            "idempotency_key": "resume-by-workflow",
        }
        _, resumed = self.conversation(
            (SUBMIT, workflow_request), selection=selected_path, check=True
        )
        self.assertEqual(resumed[3]["result"]["structuredContent"], original)

        other = {**selection, "grant_ref": "second-starter"}
        alternate_path = directory / "second-selection.json"
        _write_private(alternate_path, other)
        _write_private(
            directory / "starter-intake-grant-second-starter.json",
            _sealed_starter(other, creation_ref="other-creation"),
        )
        _, foreign = self.conversation(
            (SUBMIT, workflow_request),
            (SUBMIT, {**workflow_request, "workflow_ref": "unknown-workflow"}),
            (GET_RUN, {"run_ref": original["run_ref"]}),
            (GET_RUN, {"run_ref": "unknown-run"}),
            selection=alternate_path,
            check=True,
        )
        self.assertEqual(foreign[3]["result"], foreign[4]["result"])
        self.assertEqual(foreign[5]["result"], foreign[6]["result"])
        self.assertTrue(foreign[3]["result"]["isError"])
        self.assertTrue(foreign[5]["result"]["isError"])
        self.assertEqual(list(directory.glob("cycle-source-*")), [])
        with sqlite3.connect(directory / "intake.sqlite3") as db:
            self.assertEqual(db.execute("SELECT count(*) FROM intake_workflows").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM intake_runs").fetchone()[0], 1)
            self.assertEqual(
                db.execute("SELECT count(*) FROM intake_live_resources").fetchone()[0], 0
            )
        with sqlite3.connect(directory / "learning-mandates.sqlite3") as db:
            self.assertEqual(db.execute("SELECT count(*) FROM mandates").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM reservations").fetchone()[0], 0)

    def test_normal_starter_rejects_forged_expired_or_withdrawn_grants_before_discovery(self):
        selected_path, selection = _starter_only(self.root / "deny-starter")
        directory = selected_path.parent
        grant_path = directory / "starter-intake-grant-registered-starter.json"
        good = _sealed_starter(selection)
        expired = _sealed_starter(selection, expires_at=1)
        wrong_owner = {**selection, "owner_id": "other-founder"}
        unavailable = "private_intake_selection_unavailable"
        for selected, grant, reason in (
            (selection, expired, "intake_expired"),
            (
                selection,
                {**expired, "grant": {**expired["grant"], "source_revision": "tampered"}},
                unavailable,
            ),
            ({**selection, "actor": "other-worker"}, good, unavailable),
            (wrong_owner, _sealed_starter(wrong_owner, expires_at=1), unavailable),
        ):
            _write_private(selected_path, selected)
            _write_private(grant_path, grant)
            denied, rows = self.conversation((DISCOVER, {}), selection=selected_path)
            self.assertEqual((denied.returncode, denied.stderr.strip(), rows), (2, reason, {}))
            self.assertFalse((directory / "intake.sqlite3").exists())
        _write_private(selected_path, selection)
        _write_private(grant_path, good)
        _, admitted = self.conversation((SUBMIT, STARTER), selection=selected_path, check=True)
        ref = admitted[3]["result"]["structuredContent"]["run_ref"]
        _write_private(
            directory / "starter-intake-withdrawn-registered-starter.json", {"withdrawn": True}
        )
        _write_private(grant_path, expired)
        denied, rows = self.conversation((GET_RUN, {"run_ref": ref}), selection=selected_path)
        self.assertEqual((denied.returncode, denied.stderr.strip(), rows), (2, unavailable, {}))

    def test_owner_local_command_uses_real_mandate_and_rejects_forged_authority(self):
        training = LocalTrainingStore(self.root / "training.sqlite3", "org-a")
        epoch = training.engagement_id
        grant = _mandate(self.root, "org-a", epoch, max_runs=1)
        ledger = LearningMandates(self.root, "org-a", epoch)
        scope = grant["scope"]
        source = {
            "source_revision": "d" * 64,
            "trace_sha256": "a" * 64,
            "correction_sha256": "b" * 64,
            "workflow": scope["workflow"],
            "connector": scope["connectors"][0],
            "trace_source": scope["trace_source"],
            "correction_source": scope["correction_source"],
            "field": scope["field"],
            "fields": scope["fields"],
            "rows": 2,
        }
        with self.assertRaisesRegex(MandateError, "source_row_budget_exceeded"):
            ledger.authorize_source(
                grant["mandate_id"],
                {**source, "rows": scope["max_rows_per_run"] + 1},
                actor="worker",
            )
        approved = ledger.authorize_source(grant["mandate_id"], source, actor="worker")
        with self.assertRaisesRegex(MandateError, "standing_source_budget_exhausted"):
            ledger.authorize_source(
                grant["mandate_id"],
                {**source, "source_revision": "e" * 64, "trace_sha256": "c" * 64},
                actor="worker",
            )
        selected = {
            "schema_version": SELECTION_VERSION,
            "tenant": "org-a",
            "actor": "worker",
            "engagement_epoch": epoch,
            "mandate_id": grant["mandate_id"],
            "mandate_revision": grant["mandate_revision"],
            "source_run_id": approved["run_id"],
            "workflow_ref": grant["scope"]["workflow"],
            "trace_source": grant["scope"]["trace_source"],
            "correction_source": grant["scope"]["correction_source"],
            "pair_ref": "owner-pair",
            "source_revision": "a" * 64,
            "correction_revision": "b" * 64,
            "policy_revision": grant["scope"]["agreement_revision"],
        }
        selection_path = self.root / "selection.json"

        def select(value):
            selection_path.write_text(json.dumps(value))
            selection_path.chmod(0o600)

        select(selected)
        missing_grant, empty = self.conversation(selection=selection_path)
        self.assertEqual(missing_grant.returncode, 2)
        self.assertEqual(empty, {})
        self.assertFalse((self.root / "intake.sqlite3").exists())
        template = Path(__file__).resolve().parents[2] / "examples/pilot-source-grant.template.json"
        source_grant = json.loads(template.read_text())
        source_grant.update(
            organization="org-a",
            engagement=epoch,
            owner_id=scope["owner_id"],
            expires_at=scope["expires_at"],
            permitted_features=scope["fields"],
            **{
                key: source[key]
                for key in (
                    "workflow",
                    "field",
                    "trace_source",
                    "correction_source",
                    "trace_sha256",
                    "correction_sha256",
                )
            },
            delegation={
                "authority_kind": approved["authority_kind"],
                "run_id": approved["run_id"],
                "delegate": "worker",
            },
        )
        grant_file = self.root / ("cycle-source-" + approved["run_id"] + ".json")
        grant_file.write_text(json.dumps(source_grant))
        grant_file.chmod(0o600)

        for incorrect in (
            {**selected, "source_run_id": "learning-fake"},
            {**selected, "source_revision": "c" * 64},
            {**selected, "correction_revision": "c" * 64},
        ):
            with self.subTest(incorrect=incorrect):
                select(incorrect)
                refused, empty = self.conversation(selection=selection_path)
                self.assertEqual(refused.returncode, 2)
                self.assertEqual(empty, {})
                self.assertFalse((self.root / "intake.sqlite3").exists())
        select(selected)
        request = {
            key: selected[key]
            for key in ("pair_ref", "source_revision", "correction_revision", "policy_revision")
        }
        request["idempotency_key"] = "trusted"
        _, rows = self.conversation(
            (DISCOVER, {}),
            (SUBMIT, request),
            (SUBMIT, {**request, "tenant": "org-b"}),
            (SUBMIT, {**request, "source_revision": "c" * 64}),
            (SUBMIT, STARTER),
            selection=selection_path,
            check=True,
        )
        self.assertEqual(
            rows[3]["result"]["structuredContent"]["sources"],
            [
                {
                    "source_kind": "historical",
                    "source_ref": "owner-pair",
                    "source_revision": "a" * 64,
                }
            ],
        )
        admitted = rows[4]["result"]["structuredContent"]
        self.assertEqual(self.records("runs"), [admitted["run_ref"]])
        self.assertTrue(all(rows[i]["result"]["isError"] for i in (5, 6, 7)))
        _, readback = self.conversation(
            (GET_RUN, {"run_ref": admitted["run_ref"]}),
            selection=selection_path,
            check=True,
        )
        self.assertEqual(
            readback[3]["result"]["structuredContent"]["workflow_ref"], selected["workflow_ref"]
        )
        authority = LocalHistoricalAuthority(selection_path)
        queue = DurableIntake(self.root, authority)

        def read(work):
            self.assertEqual(work.resource.grant_revision, grant["mandate_revision"])
            self.assertEqual(work.revisions, ("a" * 64, "b" * 64, "e" * 64))
            return "generated-read-receipt"

        self.assertTrue(queue.materialize(authority.principal, admitted["run_ref"], read))
        _, restarted = self.conversation(
            (GET_RUN, {"run_ref": admitted["run_ref"]}),
            (SUBMIT, {**request, "idempotency_key": "retry-after-restart"}),
            selection=selection_path,
            check=True,
        )
        self.assertEqual(restarted[3]["result"]["structuredContent"]["state"], "materialized")
        self.assertEqual(
            restarted[4]["result"]["structuredContent"], {**admitted, "state": "materialized"}
        )
        usage = ledger.inspect(grant["mandate_id"], actor="worker")
        self.assertEqual((usage["runs"], usage["rows"], usage["reserved_fits"]), (1, 2, 0))

        # The source grant can expire independently of the still-current standing mandate.
        before_expiry_probe = queue.path.read_bytes()
        grant_file.write_text("{")
        with self.assertRaisesRegex(IntakeError, "^intake_unavailable$"):
            queue.discover_sources(authority.principal, limit=1, cursor=None)
        expired_grant = {**source_grant, "expires_at": int(time.time()) - 1}
        _write_private(grant_file, expired_grant)
        with self.assertRaisesRegex(IntakeError, "^intake_expired$"):
            queue.discover_sources(authority.principal, limit=1, cursor=None)
        expired, rows = self.conversation((DISCOVER, {}), selection=selection_path)
        self.assertEqual(
            (expired.returncode, expired.stderr.strip(), rows), (2, "intake_expired", {})
        )
        unrelated_grant = {
            **expired_grant,
            "delegation": {**expired_grant["delegation"], "run_id": "learning-unrelated"},
        }
        _write_private(grant_file, unrelated_grant)
        with self.assertRaisesRegex(IntakeError, "^intake_unavailable$"):
            queue.discover_sources(authority.principal, limit=1, cursor=None)

        _write_private(grant_file, expired_grant)
        source_intake.withdraw_intake(training, source_grant["owner_attestation"])
        with self.assertRaisesRegex(IntakeError, "^intake_unavailable$"):
            queue.discover_sources(authority.principal, limit=1, cursor=None)
        self.assertEqual(queue.path.read_bytes(), before_expiry_probe)
        self.assertFalse(ledger.inspect(grant["mandate_id"], actor="worker")["withdrawn"])
        stopped, rows = self.conversation(selection=selection_path)
        self.assertEqual(stopped.returncode, 2)
        self.assertEqual(rows, {})
        ledger.withdraw(grant["mandate_id"], actor="owner")
        stopped, rows = self.conversation(selection=selection_path)
        self.assertEqual(stopped.returncode, 2)
        self.assertEqual(rows, {})


async def _official_sdk_probe() -> None:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary).resolve()
        fixture = directory / "fixture-state.json"
        fixture.write_text('{"include_outcome":true}')
        _mandate(directory, "org-a")
        parameters = StdioServerParameters(
            command=sys.executable,
            args=[__file__, str(directory), "org-a", "intake", "0"],
            env={"PYTHONPATH": os.environ["PYTHONPATH"]},
        )

        async def visit(
            requests, expected_states, server_parameters=parameters, expected_feeds=None
        ):
            async with (
                stdio_client(server_parameters) as (read, write),
                ClientSession(read, write) as client,
            ):
                await client.initialize()
                listed = await client.list_tools()
                assert [tool.name for tool in listed.tools] == [DISCOVER, SUBMIT, GET_RUN]
                if expected_feeds is not None:
                    result = await client.call_tool(DISCOVER, {})
                    assert not result.isError and result.structuredContent is not None
                    feeds = [
                        item["feed_binding_ref"]
                        for item in result.structuredContent["sources"]
                        if "feed_binding_ref" in item
                    ]
                    assert feeds == expected_feeds
                receipts = []
                for request, expected_state in zip(requests, expected_states, strict=True):
                    admitted = await client.call_tool(SUBMIT, request)
                    assert admitted.isError is False and admitted.structuredContent is not None
                    receipts.append(admitted.structuredContent)
                    status = await client.call_tool(
                        GET_RUN, {"run_ref": admitted.structuredContent["run_ref"]}
                    )
                    assert not status.isError and status.structuredContent is not None
                    assert status.structuredContent["workflow_ref"]
                    assert status.structuredContent["state"] == expected_state
                return receipts

        first = await visit(
            [PAIR, STARTER, LIVE],
            ["queued"] * 3,
            expected_feeds=["decision-feed", "outcome-feed"],
        )
        queue = DurableIntake(directory, _GeneratedAuthority(directory))
        assert queue.materialize(ACTOR_A, first[2]["run_ref"], lambda _: "sdk-materialized-receipt")
        second = await visit(
            [
                {**request, "idempotency_key": "retry-" + request["idempotency_key"]}
                for request in (PAIR, STARTER, LIVE)
            ],
            ["queued", "queued", "materialized"],
        )
        assert second == [*first[:2], {**first[2], "state": "materialized"}]
        stores = [directory / "intake.sqlite3", directory / "learning-mandates.sqlite3"]
        unchanged_stores = [path.read_bytes() for path in stores]
        fixture.write_text(
            json.dumps(
                {
                    "include_outcome": True,
                    "resource": "sdk-unauthorized-retarget",
                    "forbid_reader_call": True,
                }
            )
        )
        await visit([], [], expected_feeds=["outcome-feed"])
        assert unchanged_stores == [path.read_bytes() for path in stores]
        starter_selection, _ = _starter_only(directory / "normal-starter")
        starter_parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "opentour_cell.pilot.intake_local", str(starter_selection)],
            env={"PYTHONPATH": os.environ["PYTHONPATH"]},
        )
        owner_first = await visit([STARTER], ["queued"], starter_parameters)
        owner_retry = await visit(
            [{**STARTER, "idempotency_key": "normal-owner-retry"}],
            ["queued"],
            starter_parameters,
        )
        assert owner_first == owner_retry
        print(
            json.dumps(
                {
                    "fixture": "generated",
                    "sdk_sessions": 5,
                    "stable_receipts": len(first),
                    "durable_materialization_read_back": True,
                    "normal_starter_resume_verified": True,
                    "adverse_discovery": {
                        "retargeted_feed_visible": False,
                        "independent_feed_visible": True,
                        "reader_calls": 0,
                        "updated_stores": 0,
                    },
                }
            )
        )


if __name__ == "__main__":
    if sys.argv[1:] == ["--official-sdk"]:
        asyncio.run(_official_sdk_probe())
        raise SystemExit(0)
    raise SystemExit(
        _stdio_main(Path(sys.argv[1]), sys.argv[2], sys.argv[3], bool(int(sys.argv[4])))
    )
