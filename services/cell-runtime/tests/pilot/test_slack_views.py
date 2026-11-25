"""Exercise the native payloads with generated data and no Slack service."""

from __future__ import annotations

import json
import unittest
from collections.abc import Iterator, Sequence
from copy import deepcopy
from typing import Any

from opentour_cell.pilot import slack_views as views
from opentour_cell.pilot.readout import INPUT_VERSION, evaluate_manifest

Block = dict[str, Any]
CONTEXT = "ctx-opaque-001"
REVISION = "readout-004"
SCOPE: Block = {"revision": REVISION, "reviewer": "reviewer-1", "recipient": "pilot-readout-team"}
DATASET: Block = {
    "mode": "training",
    "can_train": True,
    "dataset_id": "training-set-a",
    "dataset_revision": "frozen-data-04",
    "train_count": 16,
    "heldout_count": 8,
    "fields": ["case_id", "label"],
    "purpose": "local_text_classification",
}
JOB: Block = {
    "job_id": "job-041",
    "dataset_id": DATASET["dataset_id"],
    "dataset_revision": DATASET["dataset_revision"],
    "revision": "training-proposal-041",
    "status": "awaiting_approval",
    "profile": "cpu_text_v1",
    "epochs": 3,
    "cpu_limit_seconds": 3,
    "max_train_updates": 48,
    "train_count": 16,
    "heldout_count": 8,
    "result": None,
}


def walk(value: Any) -> Iterator[Block]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def actions(value: Block) -> dict[str, Block]:
    return {b["action_id"]: b for b in walk(value) if b.get("type") == "button"}


def words(value: Block) -> str:
    return "\n".join(b["text"] for b in walk(value) if b.get("type") == "plain_text")


def decision(status: str) -> Block:
    if status in ("review", "missing"):
        return {"status": status}
    return {"status": "automatic", "label": status}


def readout(rows: Sequence[tuple[str | None, str, str]]) -> Block:
    return evaluate_manifest(
        {
            "schema_version": INPUT_VERSION,
            "error_ceiling_bps": 300,
            "cases": [
                {
                    "case_id": f"never-disclose-case-{i}",
                    "reference": truth,
                    "current": decision(current),
                    "repair": decision(repair),
                }
                for i, (truth, current, repair) in enumerate(rows)
            ],
        }
    )


def generated_hundred() -> Block:
    a, b = "never-disclose-class-a", "never-disclose-class-b"
    return readout(
        [(a, b, b)] * 2 + [(a, b, a)] * 4 + [(a, a, a)] * 74 + [(a, "review", "review")] * 20
    )


class TestNativeWorkbench(unittest.TestCase):
    def test_root_without_a_candidate_and_role_specific_controls(self) -> None:
        modal = views.home_modal(context=CONTEXT, revision=None, reviewer="r", recipient="t")
        self.assertEqual(modal["type"], "modal")
        self.assertEqual(modal["private_metadata"], CONTEXT)
        self.assertEqual(set(actions(modal)), {"ot_browse", "ot_tune"})
        self.assertNotIn("submit", modal)
        self.assertIn("Awaiting a completed evaluation", words(modal))
        spectator = views.home_modal(
            context=CONTEXT,
            **SCOPE,
            state="ready",
            report=generated_hundred(),
            job_id="job-041",
            channel_aggregates=True,
            evidence_reviewed=True,
            decision_allowed=False,
            tune_allowed=False,
        )
        self.assertEqual(set(actions(spectator)), {"ot_browse", "ot_status", "ot_evidence"})
        self.assertEqual(actions(spectator)["ot_status"]["value"], "job-041")

    def test_native_buttons_require_inspected_evidence_and_bind_recipient_revision(self) -> None:
        report = generated_hundred()
        unseen = views.workbench_message(**SCOPE, report=report, channel_aggregates=True)
        self.assertNotIn("ot_approve", actions(unseen))
        self.assertIn("ot_evidence", actions(unseen))
        seen = views.workbench_message(
            **SCOPE,
            report=report,
            channel_aggregates=True,
            evidence_reviewed=True,
            dataset_revision=DATASET["dataset_revision"],
        )
        self.assertFalse(seen["mrkdwn"])
        self.assertFalse(seen["unfurl_links"])
        self.assertFalse(seen["unfurl_media"])
        for s in (
            "6/80 (7.5%)",
            "2/80 (2.5%)",
            "80/100 (80%)",
            "Paired fixes 4, regressions 0",
            "Unknown references 0",
            "≤3%",
            "incumbent exceeded",
            "repair met",
        ):
            self.assertIn(s, words(seen))
            self.assertIn(s, seen["text"])
        self.assertNotIn("never-disclose", json.dumps(seen))
        self.assertIn(DATASET["dataset_revision"], words(seen))
        for action, label in (
            ("ot_approve", "Approve for readout"),
            ("ot_changes", "Request changes"),
        ):
            button = actions(seen)[action]
            self.assertEqual(button["value"], REVISION)
            self.assertEqual(button["text"]["text"], label)
            self.assertEqual(button["confirm"]["confirm"]["text"], label)
            self.assertEqual(button["confirm"]["deny"]["text"], "Cancel")
            self.assertIn(REVISION, button["confirm"]["text"]["text"])
            self.assertIn(SCOPE["recipient"], button["confirm"]["text"]["text"])

    def test_privacy_witness_and_durable_disposition(self) -> None:
        report = generated_hundred()
        withheld = views.workbench_message(**SCOPE, report=report, evidence_reviewed=True)
        self.assertNotIn("ot_approve", actions(withheld))
        self.assertNotIn("ot_evidence", actions(withheld))
        self.assertNotIn("6/80", json.dumps(withheld))
        for reviewed in (False, True):
            with self.subTest(reviewed=reviewed):
                private = views.workbench_message(
                    **SCOPE,
                    report=report,
                    private_evidence=True,
                    evidence_reviewed=reviewed,
                )
                self.assertNotIn("6/80", json.dumps(private))
                self.assertIn("ot_evidence", actions(private))
                self.assertEqual(reviewed, "ot_approve" in actions(private))
        witnessed = views.workbench_message(**SCOPE, report=report, witnessed=True)
        self.assertIn("ot_approve", actions(witnessed))
        self.assertNotIn("6/80", json.dumps(witnessed))
        for state in (
            "pending",
            "failed",
            "expired",
            "withdrawn",
            "running",
            "stale",
            "uncertain",
            "mystery",
        ):
            with self.subTest(state=state):
                held = views.workbench_message(**SCOPE, state=state, witnessed=True)
                self.assertNotIn("ot_approve", actions(held))
                self.assertNotIn("ot_changes", actions(held))
                self.assertNotIn("Cleared for readout", held["text"])
        for state, final in (("approved", "Cleared for readout"), ("changes", "Changes requested")):
            self.assertIn(
                "Pending verification", views.workbench_message(**SCOPE, state=state)["text"]
            )
            completed = views.workbench_message(**SCOPE, state=state, receipt="receipt-01")
            self.assertIn(f"{final} · owner receipt receipt-01", completed["text"])
            self.assertNotIn("ot_approve", actions(completed))
        incomplete = views.workbench_message(
            revision=None, reviewer="r", recipient="t", state="approved", receipt="receipt-01"
        )
        self.assertNotIn("Cleared for readout", incomplete["text"])

    def test_evidence_explains_denominators_intersection_and_uncertainty(self) -> None:
        report = generated_hundred()
        modal = views.evidence_modal(
            context=CONTEXT,
            revision=REVISION,
            report=report,
            recipient=SCOPE["recipient"],
            dataset_revision=DATASET["dataset_revision"],
        )
        content = words(modal)
        for s in (
            "Eligible pairs 80/100",
            "outside the paired comparison 20",
            "Known references 100",
            "20/100 (20%)",
            "Both correct 74, both incorrect 2",
            "Missing outputs · incumbent 0, repair 0",
            "unmatched records",
            "Time saved and future performance were not measured",
            REVISION,
            SCOPE["recipient"],
            DATASET["dataset_revision"],
        ):
            self.assertIn(s.lower(), content.lower())
        self.assertNotIn("never-disclose", content)
        self.assertEqual(set(actions(modal)), {"ot_home"})
        self.assertNotIn("submit", modal)
        mismatch = readout([("a", "a", "a"), ("a", "a", "review"), ("a", "review", "a")])
        different = words(views.evidence_modal(context=CONTEXT, revision=REVISION, report=mismatch))
        self.assertIn("Eligible pairs 1/3", different)
        self.assertIn("Different automatic cases", different)
        self.assertIn("incumbent alone 1, by repair alone 1", different)
        sparse = readout([(None, "a", "a"), ("a", "missing", "review")])
        uncertain = words(views.evidence_modal(context=CONTEXT, revision=REVISION, report=sparse))
        for s in (
            "0/0 (undefined)",
            "unknown 1",
            "Missing outputs · incumbent 1, repair 0",
            "incumbent undetermined",
            "Insufficient evidence",
        ):
            self.assertIn(s, uncertain)
        empty = words(views.evidence_modal(context=CONTEXT, revision=REVISION, report=readout([])))
        self.assertIn("0/0 (undefined)", empty)
        self.assertNotIn("0/0 (0%)", empty)

    def test_corrupt_numeric_evidence_never_opens_a_decision(self) -> None:
        report = generated_hundred()
        self.assertTrue(views.evidence_available(report))
        for path, changed in (
            (("schema_version",), "other"),
            (("cohort_size",), True),
            (("unknown_references",), 20),
            (("current", "observed_error_rate", "denominator"), 100),
            (("repair", "coverage", "numerator"), 70),
            (("current", "automatic"), -3),
            (("paired", "ties_correct"), 75),
            (("changes", "current_only_automatic"), 1),
            (("observed_comparison",), "regressed"),
        ):
            with self.subTest(path=path):
                invalid = deepcopy(report)
                branch = invalid
                for key in path[:-1]:
                    branch = branch[key]
                branch[path[-1]] = changed
                self.assertFalse(views.evidence_available(invalid))
                for share in (True, False):
                    blocked = views.workbench_message(
                        **SCOPE,
                        report=invalid,
                        channel_aggregates=share,
                        private_evidence=True,
                        evidence_reviewed=True,
                    )
                    self.assertNotIn("ot_approve", actions(blocked))
                    self.assertNotIn("ot_evidence", actions(blocked))
                self.assertIn(
                    "Handoff is held",
                    words(views.evidence_modal(context=CONTEXT, revision=REVISION, report=invalid)),
                )
        missing_reports: tuple[Block | None, ...] = (None, {})
        for missing in missing_reports:
            self.assertFalse(views.evidence_available(missing))


class TestNativeDataAndTraining(unittest.TestCase):
    def test_catalog_search_window_and_training_only_field_projection(self) -> None:
        datasets = [
            {**DATASET, "dataset_id": f"dataset-{i}", "display_name": "leak-canary"}
            for i in range(22)
        ]
        listing = views.dataset_modal(context=CONTEXT, datasets=datasets, query="label")
        self.assertEqual(listing["callback_id"], "ot_dataset_search")
        self.assertEqual(listing["submit"]["text"], "Search")
        field = listing["blocks"][0]
        self.assertEqual((field["block_id"], field["element"]["action_id"]), ("query", "query"))
        self.assertEqual(field["element"]["initial_value"], "label")
        self.assertEqual(
            [b["value"] for b in walk(listing) if b.get("action_id") == "ot_dataset_open"],
            [f"dataset-{i}" for i in range(20)],
        )
        self.assertNotIn("leak-canary", json.dumps(listing))
        empty = views.dataset_modal(context=CONTEXT, datasets=[])
        self.assertIn("No authorized datasets match", words(empty))
        permitted = {
            **DATASET,
            "scope": "training_only",
            "fields": ["label", "disallowed-column"],
            "example_limit": 3,
            "rows": [
                {
                    "case_id": "leak-canary-id",
                    "text": "leak-canary-text",
                    "disallowed-column": "leak-canary-unknown",
                    "label": f"<!channel> sample{i}",
                }
                for i in range(4)
            ],
        }
        detail = views.dataset_modal(context=CONTEXT, datasets=[], detail=permitted)
        self.assertEqual(detail["callback_id"], "ot_dataset_detail")
        self.assertNotIn("submit", detail)
        self.assertNotIn("leak-canary", json.dumps(detail))
        self.assertNotIn("<!channel>", words(detail))
        self.assertIn("&lt;!channel&gt; sample2", words(detail))
        self.assertNotIn("sample3", words(detail))
        self.assertEqual(actions(detail)["ot_tune_dataset"]["value"], DATASET["dataset_id"])
        restrictions: tuple[Block, ...] = (
            {"scope": "heldout"},
            {"example_limit": 0},
            {"fields": []},
        )
        for override in restrictions:
            with self.subTest(override=override):
                blocked = views.dataset_modal(
                    context=CONTEXT,
                    datasets=[],
                    detail={**permitted, **override},
                    tune_allowed=False,
                )
                self.assertNotIn("sample0", words(blocked))
                self.assertNotIn("ot_tune_dataset", actions(blocked))

    def test_tuning_selection_real_profile_and_exclusive_bounded_confirm(self) -> None:
        allowed = [{**DATASET, "dataset_id": f"set-{i}"} for i in range(102)]
        tune = views.tune_modal(context=CONTEXT, datasets=allowed)
        self.assertEqual(tune["callback_id"], "ot_tune_preview")
        self.assertEqual(tune["submit"]["text"], "Preview training")
        selects = {b["action_id"]: b for b in walk(tune) if b.get("type") == "static_select"}
        self.assertEqual(len(selects["dataset"]["options"]), 100)
        self.assertEqual([o["value"] for o in selects["profile"]["options"]], ["cpu_text_v1"])
        self.assertEqual([o["value"] for o in selects["epochs"]["options"]], ["1", "3", "5"])
        self.assertEqual(selects["epochs"]["initial_option"]["value"], "3")
        self.assertNotIn("submit", views.tune_modal(context=CONTEXT, datasets=[]))
        proposal = views.proposal_modal(context=CONTEXT, job=JOB, recipient=SCOPE["recipient"])
        self.assertEqual(proposal["callback_id"], "ot_tune_start")
        self.assertEqual(proposal["submit"]["text"], "Start training")
        self.assertEqual(proposal["private_metadata"], CONTEXT)
        for text in (
            "job-041",
            "frozen-data-04",
            "CPU text classifier",
            "Training cases 16",
            "Held-out cases 8",
            "48 training updates and 3 CPU seconds",
            "separate authorization",
        ):
            self.assertIn(text, words(proposal))
        for override in (
            {"status": "running"},
            {"status": "completed"},
            {"profile": "external-model"},
            {"epochs": 99},
            {"epochs": True},
            {"cpu_limit_seconds": 4},
            {"train_count": 0},
            {"heldout_count": 0},
            {"max_train_updates": 49},
            {"epochs": None},
        ):
            with self.subTest(override=override):
                invalid = views.proposal_modal(
                    context=CONTEXT, job={**JOB, **override}, recipient=SCOPE["recipient"]
                )
                self.assertNotIn("submit", invalid)
                self.assertEqual(invalid["callback_id"], "ot_held_proposal")
                self.assertIn("Proposal held", words(invalid))

    def test_durable_status_never_confuses_training_id_with_presentation_revision(self) -> None:
        result = {
            "artifact_digest": "fixture-content-digest",
            "update_count": 7,
            "changed_weights": 12,
            "private-note": "leak-canary",
        }
        complete = {**JOB, "status": "completed", "result": result}
        for stage, label in (
            ("awaiting_approval", "Awaiting trainer confirmation"),
            ("running", "Running"),
            ("failed", "Failed"),
            ("rejected", "Rejected"),
            ("mystery", "unverified"),
        ):
            with self.subTest(stage=stage):
                modal = views.status_modal(
                    context=CONTEXT, job={**JOB, "status": stage}, evidence_revision=REVISION
                )
                self.assertIn(label, words(modal))
                self.assertEqual(set(actions(modal)), {"ot_status_refresh", "ot_home"})
                self.assertEqual(actions(modal)["ot_status_refresh"]["value"], JOB["job_id"])
                self.assertNotIn("submit", modal)
        self.assertNotIn("ot_evidence", actions(views.status_modal(context=CONTEXT, job=complete)))
        final = views.status_modal(context=CONTEXT, job=complete, evidence_revision=REVISION)
        self.assertEqual(actions(final)["ot_evidence"]["value"], REVISION)
        self.assertNotIn(JOB["revision"], json.dumps(final))
        self.assertNotIn("leak-canary", json.dumps(final))
        self.assertIn("Completed training updates 7. Changed weights 12.", words(final))
        restricted = views.status_modal(
            context=CONTEXT,
            job={**complete, "result": None},
            result_visible=False,
            evidence_revision=REVISION,
        )
        self.assertIn("Completed", words(restricted))
        self.assertIn("Aggregate details are restricted", words(restricted))
        self.assertNotIn("changed weights", words(restricted).lower())
        self.assertNotIn("ot_evidence", actions(restricted))
        abandoned = views.status_modal(
            context=CONTEXT,
            job={**JOB, "status": "running", "recoverable": True},
        )
        self.assertIn("Pending verification", words(abandoned))
        self.assertIn("worker lease expired", words(abandoned))
        self.assertIn("reconcile", words(abandoned))
        self.assertNotIn("Running", words(abandoned))
        self.assertNotIn("ot_evidence", actions(abandoned))
        for bad in (None, {}, {**result, "update_count": True}, {**result, "artifact_digest": ""}):
            corrupt = views.status_modal(
                context=CONTEXT, job={**complete, "result": bad}, evidence_revision=REVISION
            )
            self.assertIn("Pending verification", words(corrupt))
            self.assertNotIn("ot_evidence", actions(corrupt))


class TestNativeBlockLimits(unittest.TestCase):
    def test_reserved_text_and_opaque_values(self) -> None:
        content = views.workbench_message(
            revision="rev-select",
            reviewer="<@U123>",
            recipient="<!channel> & group",
            witnessed=True,
        )
        self.assertIn("&lt;!channel&gt; &amp; group", words(content))
        self.assertNotIn("<@U123>", json.dumps(content))
        for bad in (
            "../outside/file",
            "/some/local/folder",
            "https://example.invalid",
            "some person",
            "x" * 161,
            "<@bad>",
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                views.workbench_message(revision=bad, reviewer="r", recipient="t", witnessed=True)
        with self.assertRaises(ValueError):
            views.home_modal(context="/etc/secret", **SCOPE)
        with self.assertRaises(ValueError):
            views.tune_modal(context=CONTEXT, datasets=[{**DATASET, "dataset_id": "x" * 151}])
        longest = views.workbench_message(
            revision="r" * 160, reviewer="u" * 120, recipient="t" * 120, witnessed=True
        )
        self.assertLessEqual(len(actions(longest)["ot_approve"]["confirm"]["text"]["text"]), 300)

    def test_payloads_respect_native_slack_shapes_and_length_limits(self) -> None:
        report = generated_hundred()
        for view in (
            views.workbench_message(
                **SCOPE, channel_aggregates=True, report=report, evidence_reviewed=True
            ),
            views.home_modal(context=CONTEXT, **SCOPE, private_evidence=True),
            views.dataset_modal(context=CONTEXT, datasets=[DATASET]),
            views.tune_modal(context=CONTEXT, datasets=[DATASET]),
            views.proposal_modal(context=CONTEXT, job=JOB, recipient=SCOPE["recipient"]),
            views.status_modal(context=CONTEXT, job=JOB),
            views.evidence_modal(context=CONTEXT, revision=REVISION, report=report),
        ):
            with self.subTest(view=view.get("callback_id", "message")):
                self.assertLessEqual(len(view["blocks"]), 50)
                if "type" in view:
                    self.assertEqual(view["type"], "modal")
                    self.assertLessEqual(len(view["title"]["text"]), 24)
                    self.assertEqual(view["private_metadata"], CONTEXT)
                for block in walk(view):
                    self.assertNotEqual(block.get("type"), "mrkdwn")
                    if block.get("type") == "actions":
                        identifiers = [e["action_id"] for e in block["elements"]]
                        self.assertEqual(len(identifiers), len(set(identifiers)))
                        self.assertLessEqual(len(identifiers), 25)
                    if block.get("type") == "button":
                        self.assertEqual(block["text"]["type"], "plain_text")
                        self.assertLessEqual(len(block["text"]["text"]), 75)
                        self.assertLessEqual(len(block["value"]), 2000)
                        self.assertNotIn("url", block)
                    if block.get("type") == "section":
                        self.assertLessEqual(len(block["text"]["text"]), 3000)
                        self.assertEqual(block["text"]["type"], "plain_text")
                    if block.get("type") == "static_select":
                        self.assertLessEqual(len(block["options"]), 100)
                        for entry in block["options"]:
                            self.assertLessEqual(len(entry["text"]["text"]), 75)
                            self.assertLessEqual(len(entry["value"]), 150)


class TestScopedNativeEvaluation(unittest.TestCase):
    def test_eval_only_catalog_detail_and_scope_signals(self) -> None:
        frozen = {
            **DATASET,
            "dataset_id": "supplied-decisions",
            "mode": "evaluation_only",
            "can_train": False,
            "train_count": 0,
            "purpose": "evaluation_only",
            "scope": "training_only",
            "example_limit": 3,
            "visible_fields": ["current", "reference", "secret-column-canary"],
            "fields": ["label", "text"],
            "rows": [{"text": "protected-heldout-canary", "label": "protected-label"}],
            "cohort_digest": "frozen-group-01",
        }
        catalog = views.dataset_modal(context=CONTEXT, datasets=[frozen, DATASET])
        self.assertIn("Evaluation only", words(catalog))
        self.assertIn("Training and evaluation", words(catalog))
        detail = views.dataset_modal(
            context=CONTEXT, datasets=[], detail=frozen, evidence_revision=REVISION
        )
        self.assertNotIn("ot_tune_dataset", actions(detail))
        self.assertEqual(actions(detail)["ot_evidence"]["value"], REVISION)
        self.assertNotIn("protected-", words(detail))
        self.assertIn("Authorized field names · current, reference", words(detail))
        self.assertNotIn("secret-column-canary", words(detail))
        self.assertIn("frozen-group-01", words(detail))
        self.assertIn("scoring supplied decisions", words(detail))
        restricted = views.dataset_modal(context=CONTEXT, datasets=[], detail=frozen)
        self.assertNotIn("ot_evidence", actions(restricted))
        for mode, can_train in (("evaluation_only", True), ("unknown", True), ("training", False)):
            disabled = {**frozen, "mode": mode, "can_train": can_train}
            self.assertNotIn("submit", views.tune_modal(context=CONTEXT, datasets=[disabled]))
            self.assertNotIn(
                "ot_tune_dataset",
                actions(views.dataset_modal(context=CONTEXT, datasets=[], detail=disabled)),
            )
        menu = views.tune_modal(context=CONTEXT, datasets=[frozen, DATASET])
        choices = [
            b
            for b in walk(menu)
            if b.get("type") == "static_select" and b.get("action_id") == "dataset"
        ]
        self.assertEqual([item["value"] for item in choices[0]["options"]], [DATASET["dataset_id"]])

    def test_scoped_proposal_needs_audience(self) -> None:
        for empty in (None, "", " "):
            self.assertNotIn(
                "submit", views.proposal_modal(context=CONTEXT, job=JOB, recipient=empty)
            )
        approved = views.proposal_modal(context=CONTEXT, job=JOB, recipient="UDEST")
        self.assertIn("Intended readout audience · UDEST", words(approved))
        self.assertEqual(approved["submit"]["text"], "Start training")

    def test_evaluation_evidence_has_digest_population_viewer_and_no_training_claims(self) -> None:
        audit = {
            "mode": "evaluation_only",
            "job_id": "score-a",
            "dataset_id": "set-a",
            "model_revision": "supplied-v5",
            "cohort_revision": "heldout-8",
            "evidence_digest": "sha256:5a55",
            "reviewer": "UINSPECTOR",
            "team": "TLOCAL",
            "channel": "CPRIVATE",
            "epochs": 3,
            "cpu_limit_seconds": 3,
            "unauthorized_payload": "raw-metadata-canary",
        }
        report = generated_hundred()
        seen = views.evidence_modal(
            context=CONTEXT,
            revision=REVISION,
            report=report,
            audit=audit,
            recipient=SCOPE["recipient"],
            dataset_revision="dset-r2",
        )
        content = words(seen)
        for value in (
            "supplied incumbent and repair outputs",
            "score-a",
            "set-a",
            "supplied-v5",
            "heldout-8",
            "sha256:5a55",
            "UINSPECTOR",
            "TLOCAL",
            "CPRIVATE",
            "dset-r2",
            SCOPE["recipient"],
            "Supplied evaluation cases 100",
            "6/80 (7.5%)",
        ):
            self.assertIn(value, content)
        self.assertNotIn("CPU", content)
        self.assertNotIn("raw-metadata-canary", content)
        self.assertNotIn("Presentation recorded", content)
        recorded = views.home_modal(
            context=CONTEXT,
            **SCOPE,
            state="ready",
            private_evidence=True,
            evidence_reviewed=True,
            audit={**audit, "presented_at": 1700000000},
        )
        self.assertIn("2023-11-14", words(recorded))
        learned = views.evidence_modal(
            context=CONTEXT, revision=REVISION, report=report, audit={**audit, "mode": "training"}
        )
        self.assertIn("CPU text classifier", words(learned))
        self.assertIn("3 epochs, 3 CPU seconds", words(learned))
        denied = views.evidence_modal(context=CONTEXT, revision=REVISION, report=None, audit=audit)
        self.assertNotIn("sha256:5a55", words(denied))

    def test_evaluation_status_and_native_loading_have_no_mutations(self) -> None:
        supplied = {
            **JOB,
            "mode": "evaluation_only",
            "status": "completed",
            "result": {"readout": generated_hundred()},
        }
        status = views.status_modal(context=CONTEXT, job=supplied, evidence_revision=REVISION)
        self.assertEqual(status["title"]["text"], "Evaluation status")
        self.assertIn("supplied incumbent and repair outputs were scored", words(status))
        self.assertNotIn("weights", words(status))
        self.assertIn("ot_evidence", actions(status))
        blocked = views.status_modal(
            context=CONTEXT, job={**supplied, "result": {"readout": {}}}, evidence_revision=REVISION
        )
        self.assertIn("Pending verification", words(blocked))
        self.assertNotIn("ot_evidence", actions(blocked))
        for key in ("checking", "working", "denied", "uncertain", "untrusted-input-canary"):
            notice = views.notice_modal(key)
            self.assertNotIn("submit", notice)
            self.assertFalse(actions(notice))
            self.assertNotIn("untrusted-input-canary", words(notice))
        self.assertIn("dataset grant", words(views.notice_modal("denied")))
        self.assertIn("Checking", words(views.notice_modal("checking")))
