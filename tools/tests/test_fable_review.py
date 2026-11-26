"""Generated tests for the advisory Fable review gate."""

import json
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from tools.ci import fable_review as review

HEAD = "a" * 40
BASE = "b" * 40
MERGE = "c" * 40
BRANCH = "codex/ope-535-founder-settings"
REPO = {"id": 1, "full_name": review.REPOSITORY}
ENV = {
    "GITHUB_TOKEN": "generated-read",
    "FABLE_CHECK_TOKEN": "generated-check",
    "FABLE_CHECK_APP": "generated-app",
    "ANTHROPIC_API_KEY": "generated-review",
    "FABLE_LINEAR_API_KEY": "generated-linear",
}


def fixture_pr():
    return {
        "number": 91,
        "state": "open",
        "draft": False,
        "head": {"sha": HEAD, "ref": BRANCH, "repo": REPO},
        "base": {"sha": BASE, "ref": "main", "repo": REPO},
        "merge_commit_sha": MERGE,
        "changed_files": 1,
    }


def fixture_event():
    return {
        "repository": REPO,
        "workflow_run": {
            "id": 9,
            "run_attempt": 2,
            "workflow_id": review.CI_WORKFLOW_ID,
            "event": "pull_request",
            "conclusion": "success",
            "head_branch": BRANCH,
            "head_sha": HEAD,
            "head_repository": REPO,
            "pull_requests": [fixture_pr()],
        },
    }


def fixture_response(verdict="approved", blockers=None):
    return {
        "model": review.MODEL,
        "stop_reason": "end_turn",
        "content": [{
            "type": "text",
            "text": json.dumps({"verdict": verdict, "blockers": blockers or []}),
        }],
    }


class FableReviewTests(unittest.TestCase):
    def test_workflow_uses_trusted_code_and_requires_pilot_opt_in(self):
        file = Path(__file__).resolve().parents[2] / ".github/workflows/fable-review.yml"
        workflow = json.loads(file.read_text())
        self.assertEqual(set(workflow["on"]), {"workflow_run"})
        self.assertEqual(workflow["on"]["workflow_run"]["types"], ["completed"])
        self.assertEqual(workflow["on"]["workflow_run"]["branches"], ["codex/ope-535-*"])
        self.assertTrue(all(value == "read" for value in workflow["permissions"].values()))
        job = workflow["jobs"]["review"]
        self.assertIn("vars.FABLE_REVIEW_ENABLED == 'true'", job["if"])
        self.assertEqual(job["environment"], "fable-review")
        self.assertEqual(job["steps"][0]["with"]["ref"], "${{ github.sha }}")
        self.assertFalse(job["steps"][0]["with"]["persist-credentials"])
        self.assertEqual(job["steps"][2]["with"]["permission-checks"], "write")

    def test_only_the_selected_ci_and_same_repository_pilot_are_eligible(self):
        for change in (
            {"head_repository": {"id": 2}},
            {"head_branch": "codex/ope-534-fable-review-pilot"},
            {"conclusion": "failure"},
            {"workflow_id": 2},
            {"event": "workflow_dispatch"},
        ):
            with self.subTest(change=change):
                event = fixture_event()
                event["workflow_run"].update(change)
                self.assertIsNone(review.Candidate.from_event(event))
        self.assertEqual(review.Candidate.from_event(fixture_event()).head, HEAD)

    def test_ci_requires_every_success_and_matching_screened_artifacts(self):
        candidate = review.Candidate.from_event(fixture_event())
        jobs = [
            {"id": num, "name": name, "status": "completed", "conclusion": "success"}
            for num, name in enumerate(review.CHECKS)
        ]
        artifacts = [{"name": f"{name}-{MERGE}", "expired": False} for name in review.CHECKS]
        with patch.object(
            review, "github_object", side_effect=[{"jobs": jobs}, {"artifacts": artifacts}]
        ):
            self.assertEqual(len(review.ci_evidence(candidate, MERGE, "read")), len(review.CHECKS))
        for overrides in ({"jobs": jobs[:-1]}, {"artifacts": artifacts[:-1]}):
            with self.subTest(overrides=list(overrides)), patch.object(
                review, "github_object",
                side_effect=[
                    {"jobs": overrides.get("jobs", jobs)},
                    {"artifacts": overrides.get("artifacts", artifacts)},
                ],
            ), self.assertRaises(review.ReviewHeld):
                review.ci_evidence(candidate, MERGE, "read")

    def test_public_report_contains_only_allowed_domains_and_changed_paths(self):
        result = fixture_response("changes_required", [
            {"domain": "functionality", "path": "src/pilot.py"},
            {"domain": "architecture", "path": "private-input"},
        ])
        self.assertEqual(
            review.review_verdict(result, {"src/pilot.py"}),
            ("changes_required", ["- functionality — src/pilot.py", "- architecture"]),
        )

    def test_invalid_or_incomplete_model_response_cannot_approve(self):
        for sample in (
            {**fixture_response(), "stop_reason": "max_tokens"},
            {**fixture_response(), "model": "another-model"},
            {**fixture_response(), "content": [{"type": "text", "text": "{}"}]},
            fixture_response(blockers=[{"domain": "ux", "path": ""}]),
            fixture_response("changes_required"),
        ):
            with self.subTest(sample=sample):
                self.assertEqual(
                    review.review_verdict(sample, set()), ("insufficient_evidence", [])
                )

    def test_commit_base_and_criteria_are_rechecked_before_publishing(self):
        for mutation in ("head", "base", "merge_commit_sha", "criteria"):
            with self.subTest(mutation=mutation):
                final = deepcopy(fixture_pr())
                if mutation in {"head", "base"}:
                    final[mutation]["sha"] = "d" * 40
                if mutation == "merge_commit_sha":
                    final[mutation] = "d" * 40
                self.run_pipeline(
                    final, expect_check=False, changed_criteria=mutation == "criteria"
                )

    def test_publishes_check_only_on_the_reviewed_commit(self):
        self.run_pipeline(fixture_pr(), expect_check=True)

    def test_second_delivery_of_the_same_review_reuses_the_receipt(self):
        self.run_pipeline(fixture_pr(), expect_check=True, repeat=True)

    def run_pipeline(self, final, *, expect_check, changed_criteria=False, repeat=False):
        diff = b"diff --git a/src/pilot.py b/src/pilot.py\n--- a/src/pilot.py\n+++ b/src/pilot.py\n"
        receipts = []
        readback_count = 0

        def github_object(path, _token, payload=None):
            nonlocal readback_count
            if path == "pulls/91":
                readback_count += 1
                return fixture_pr() if readback_count % 2 == 1 or repeat else final
            if f"commits/{HEAD}/check-runs?" in path:
                return {"check_runs": [
                    {
                        "external_id": receipt["external_id"],
                        "status": "completed",
                        "app": {"slug": ENV["FABLE_CHECK_APP"]},
                    }
                    for receipt in receipts
                ]}
            if path == "check-runs":
                receipts.append(payload)
                return {}
            self.fail(f"Unexpected GitHub path {path}")

        def provider(_url, _headers, payload):
            data = json.loads(payload["messages"][0]["content"])
            self.assertEqual(
                (data["head"], data["base"], data["tested_merge"]), (HEAD, BASE, MERGE)
            )
            self.assertEqual(
                data["criteria"], {"Observable acceptance": "Private fixture criterion"}
            )
            return json.dumps(fixture_response()).encode()

        criteria = {"Observable acceptance": "Private fixture criterion"}
        final_version = "version-b" if changed_criteria else "version-a"
        versions = [("version-a", criteria), (final_version, criteria)]
        if repeat:
            versions.append(("version-a", criteria))
        with (
            patch.dict(review.os.environ, ENV),
            patch.object(review, "github_object", side_effect=github_object),
            patch.object(review, "github", return_value=diff),
            patch.object(review, "ci_evidence", return_value=[]),
            patch.object(review, "linear_criteria", side_effect=versions),
            patch.object(review, "request", side_effect=provider) as model,
        ):
            if expect_check:
                review.run_review(fixture_event())
                if repeat:
                    review.run_review(fixture_event())
                self.assertEqual(model.call_count, 1)
            else:
                with self.assertRaises(review.ReviewHeld):
                    review.run_review(fixture_event())
        if expect_check:
            self.assertEqual(len(receipts), 1)
            self.assertEqual(receipts[0]["head_sha"], HEAD)
            self.assertEqual(receipts[0]["conclusion"], "success")
            self.assertNotIn("Private fixture criterion", json.dumps(receipts))
        else:
            self.assertEqual(receipts, [])
