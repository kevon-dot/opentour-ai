"""Project an authenticated negative release receipt into a private revision request.

This reader does not cast a vote, accept a provider response, create a successor,
or authorize training or release. The existing release importer owns the decision.
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path
from typing import Any

from opentour_cell.pilot.operating_point import canonical, digest
from opentour_cell.pilot.receipt_recovery import _open_db
from opentour_cell.pilot.release_gate import inspect_recorded_release
from opentour_cell.pilot.repair_workflow import _require, managed_review_evidence
from opentour_cell.pilot.slack_adapter import Settings
from opentour_cell.pilot.training import LocalTrainingStore

Json = dict[str, Any]
SCHEMA = "opentour.pilot.revision-request.v1"


class RevisionRequests:
    """Use one operator-selected tenant directory and trusted worker settings file.

    A trusted adapter supplies its authenticated actor. This owner-local class is
    not an authentication endpoint and must not accept a remote settings path.
    """

    def __init__(self, directory: Path, organization: str, settings_file: Path):
        self.directory = directory
        self.organization = organization
        self.settings_file = settings_file

    def _settings(self, actor: str) -> Settings:
        cfg = Settings.load(self.settings_file)
        _require(
            cfg.directory == self.directory
            and cfg.organization == self.organization
            and cfg.explicit_recipient
            and actor == cfg.reviewer,
            "revision_request_unavailable",
        )
        role = cfg.permission(actor, "inspect")
        cfg.permission(actor, "review")
        _require(role["aggregate"], "revision_request_unavailable")
        return cfg

    def inspect(self, *, actor: str, run_id: str, run_revision: str, artifact_sha: str) -> Json:
        """Reopen only the exact closed version through current permitted evidence.

        No feedback field is required by this projection. Missing or unverifiable
        decisions remain unavailable; neither is inferred to be a decline.
        """
        self._settings(actor)  # Deny unknown actors before looking up a proposal.
        store = LocalTrainingStore(
            self.directory / "training.sqlite3", self.organization, existing_only=True
        )
        with store.engagement_guard():
            cfg = self._settings(actor)
            receipt = inspect_recorded_release(
                self.directory, self.organization, run_id, run_revision, artifact_sha
            )
            _require(
                receipt is not None and receipt["decision"] == "changes_requested",
                "authenticated_decline_required",
            )
            assert receipt is not None
            # The original sealed inspection verifies the authoritative settings
            # and grants. An alternate caller-selected file cannot change its app
            # identity or broaden the named reviewer's decision-history access.
            with _open_db(self.directory / "slack-pilot.sqlite3") as db:
                _require(db is not None, "revision_request_unavailable")
                assert db is not None
                binding = db.execute("SELECT binding FROM identity WHERE id=1").fetchone()
                _require(binding and binding[0] == cfg.binding, "revision_request_unavailable")
            _require(
                receipt["team"] == cfg.team
                and receipt["channel"] == cfg.channel
                and receipt["audience"] == cfg.recipient
                and receipt["reviewer"] == cfg.reviewer
                and receipt["provider"] == cfg.approval_provider,
                "revision_request_unavailable",
            )
            evidence = managed_review_evidence(
                self.directory, self.organization, run_id, cfg.recipient, cfg.team, cfg.channel
            )
            cfg.permission(actor, "inspect", evidence["dataset_id"])
            cfg.permission(actor, "review", evidence["dataset_id"])
            current = self._settings(actor)
            current.permission(actor, "inspect", evidence["dataset_id"])
            current.permission(actor, "review", evidence["dataset_id"])
            _require(current == cfg, "revision_request_unavailable")
            # This final check reopens the *original* sealed inspection's actual
            # authority file, even when selected settings are an equivalent copy.
            _require(
                inspect_recorded_release(
                    self.directory, self.organization, run_id, run_revision, artifact_sha
                )
                == receipt,
                "revision_request_unavailable",
            )
            identity = {
                "schema_version": SCHEMA,
                "organization": self.organization,
                "engagement": store.engagement_id,
                "run_id": run_id,
                "run_revision": run_revision,
                "artifact_sha": artifact_sha,
                "release_receipt_revision": digest(receipt),
            }
            return {
                **identity,
                "request_id": "rr-" + digest(identity),
                "state": "changes_requested",
                "closed": True,
                "provider": receipt["provider"],
                "reviewer": receipt["reviewer"],
                "destination": receipt["audience"],
            }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--organization", required=True)
    parser.add_argument("--settings", type=Path, required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-revision", required=True)
    parser.add_argument("--artifact-sha", required=True)
    args = parser.parse_args()
    try:
        result = RevisionRequests(args.directory, args.organization, args.settings).inspect(
            actor=args.actor,
            run_id=args.run_id,
            run_revision=args.run_revision,
            artifact_sha=args.artifact_sha,
        )
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        parser.exit(2, "revision_request_unavailable\n")
    print(canonical(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
