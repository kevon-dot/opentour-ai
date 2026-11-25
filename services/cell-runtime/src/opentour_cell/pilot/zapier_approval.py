"""Bridge an explicitly configured Zapier Slack Request Approval to the owner-local release gate."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import hmac
import http.client
import os
import re
import secrets
import sqlite3
import sys
import time
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any

from opentour_cell.pilot import release_gate
from opentour_cell.pilot.onboarding_mcp import _read_private
from opentour_cell.pilot.operating_point import canonical, digest
from opentour_cell.pilot.repair_workflow import _record
from opentour_cell.pilot.slack_adapter import _hook_path, hook_fingerprint
from opentour_cell.pilot.training import LocalTrainingStore, _check_directory, _check_file
from opentour_cell.pilot.training_data import identifier

Json = dict[str, Any]
POLICY = "opentour.pilot.zapier-slack-policy.v1"
EVENT = "opentour.pilot.zapier-slack-decision.v1"
REQUEST = "opentour.pilot.zapier-slack-request.v1"
HEX = re.compile("[0-9a-f]{64}")
OPAQUE = re.compile("[a-zA-Z0-9_.:-]{1,96}")
REQ = re.compile("za-[a-f0-9]{32}")
FIELDS = {
    "schema_version",
    "request_id",
    "nonce",
    "zap_id",
    "zap_version",
    "event_id",
    "decided_at",
    "status",
    "actor_id",
}
BIND = (
    "run_id",
    "run_revision",
    "artifact_sha",
    "reviewer",
    "team",
    "channel",
    "audience",
    "inspection_ref",
)


class ApprovalError(ValueError):
    """Fixed public diagnostic from an owner-local operation."""


def require(condition: object, code: str) -> None:
    if not condition:
        raise ApprovalError(code)


def _sig(secret: bytes, context: str, value: Json) -> str:
    return hmac.new(
        secret, context.encode() + b"\n" + canonical(value).encode(), hashlib.sha256
    ).hexdigest()


def _post(hook: str, payload: Json) -> int:
    with closing(http.client.HTTPSConnection("hooks.zapier.com", timeout=5)) as client:
        client.request(
            "POST",
            _hook_path(hook),
            body=canonical(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with closing(client.getresponse()) as response:
            return response.status


class ApprovalBridge:
    def __init__(self, directory: Path, organization: str, config: Path):
        self.directory = directory
        self.organization = identifier(organization)
        self.config = config
        require(
            directory.is_absolute()
            and directory.resolve(strict=True) == directory
            and config.parent == directory,
            "private_configuration_required",
        )
        _check_directory(config)

    def _policy(self) -> tuple[Json, bytes]:
        p = _read_private(self.config, 16_384)
        require(
            set(p)
            == {
                "schema_version",
                "enabled",
                "organization",
                "engagement",
                "team",
                "channel",
                "reviewer",
                "audience",
                "zap_id",
                "zap_version",
                "approver_mode",
                "setup_attestation",
                "key_file",
                "expires_at",
                "hook_fingerprint",
            },
            "invalid_plugin_policy",
        )
        require(
            p["schema_version"] == POLICY
            and p["enabled"] is True
            and p["organization"] == self.organization,
            "plugin_not_authorized",
        )
        require(
            all(
                isinstance(p[k], str) and OPAQUE.fullmatch(p[k])
                for k in (
                    "engagement",
                    "team",
                    "channel",
                    "reviewer",
                    "audience",
                    "zap_id",
                    "zap_version",
                )
            ),
            "invalid_plugin_scope",
        )
        require(
            re.fullmatch(r"T[A-Z0-9]+", p["team"])
            and re.fullmatch(r"[CG][A-Z0-9]+", p["channel"])
            and re.fullmatch(r"[UW][A-Z0-9]+", p["reviewer"]),
            "invalid_slack_scope",
        )
        require(
            p["approver_mode"] in {"verified_actor", "verified_single_approver"}
            and isinstance(p["setup_attestation"], str)
            and re.fullmatch(r"att-[a-f0-9]{16,64}", p["setup_attestation"]),
            "unverified_plugin_configuration",
        )
        require(
            type(p["expires_at"]) is int and time.time() < p["expires_at"] < 2**63,
            "plugin_policy_expired",
        )
        require(
            isinstance(p["key_file"], str)
            and re.fullmatch(r"[A-Za-z0-9_-]{1,64}\.json", p["key_file"]),
            "invalid_signer_reference",
        )
        require(
            isinstance(p["hook_fingerprint"], str) and HEX.fullmatch(p["hook_fingerprint"]),
            "invalid_plugin_destination",
        )
        key = _read_private(self.directory / p["key_file"], 1024)
        require(
            set(key) == {"secret_hex"}
            and isinstance(key["secret_hex"], str)
            and HEX.fullmatch(key["secret_hex"]),
            "private_signer_required",
        )
        store = LocalTrainingStore(
            self.directory / "training.sqlite3", self.organization, existing_only=True
        )
        require(
            store.engagement_active() and store.engagement_id == p["engagement"],
            "engagement_unavailable",
        )
        return p, bytes.fromhex(key["secret_hex"])

    def check(self) -> Json:
        p, _ = self._policy()
        return {"state": "configured", "provider": "zapier_slack", "policy_revision": digest(p)}

    def _path(self, request_id: str, kind: str) -> Path:
        require(isinstance(request_id, str) and REQ.fullmatch(request_id), "invalid_request_id")
        return self.directory / f"zapier-approval-{request_id}-{kind}.json"

    def _inspect(self, req: Json) -> Json:
        inspection = release_gate.check_release_inspection(
            self.directory, self.organization, **{k: req[k] for k in BIND}
        )
        require(
            inspection["provider"] == "zapier_slack"
            and inspection["engagement"] == req["engagement"],
            "plugin_not_selected",
        )
        return inspection

    def _request(self, request_id: str, p: Json, key: bytes, *, fresh: bool = True) -> Json:
        wrapped = _read_private(self._path(request_id, "request"), 16_384)
        req = wrapped.get("request")
        require(
            isinstance(req, dict)
            and isinstance(wrapped.get("seal"), str)
            and hmac.compare_digest(wrapped["seal"], _sig(key, REQUEST, req)),
            "request_seal_mismatch",
        )
        assert isinstance(req, dict)
        require(
            req["request_id"] == request_id
            and req["policy_revision"] == digest(p)
            and req["engagement"] == p["engagement"],
            "plugin_policy_changed",
        )
        require(not fresh or int(time.time()) < req["expires_at"], "request_expired")
        return req

    @staticmethod
    def _wire(req: Json, key: bytes) -> Json:
        payload = {
            k: req[k]
            for k in (
                "schema_version",
                "request_id",
                "nonce",
                "expires_at",
                "team",
                "channel",
                "reviewer",
                "run_revision",
                "artifact_sha",
                "audience",
            )
        }
        return payload | {"request_signature": _sig(key, REQUEST + "/outbound", payload)}

    def issue(
        self,
        *,
        run_id: str,
        run_revision: str,
        artifact_sha: str,
        inspection_ref: str,
        ttl_seconds: int = 900,
    ) -> Json:
        p, key = self._policy()
        require(
            isinstance(run_id, str)
            and re.fullmatch("run-[a-f0-9]{32}", run_id)
            and HEX.fullmatch(run_revision)
            and HEX.fullmatch(artifact_sha)
            and OPAQUE.fullmatch(inspection_ref),
            "invalid_release_binding",
        )
        require(type(ttl_seconds) is int and 1 <= ttl_seconds <= 86400, "invalid_request_lifetime")
        binding = {
            "run_id": run_id,
            "run_revision": run_revision,
            "artifact_sha": artifact_sha,
            "inspection_ref": inspection_ref,
            **{k: p[k] for k in ("engagement", "team", "channel", "reviewer", "audience")},
            "policy_revision": digest(p),
        }
        store = LocalTrainingStore(
            self.directory / "training.sqlite3", self.organization, existing_only=True
        )
        with store.engagement_guard(engagement_id=p["engagement"]):
            require(_read_private(self.config, 16_384) == p, "plugin_policy_changed")
            inspection = self._inspect(binding)
            binding["inspection_ref"] = inspection["presentation_id"]
            scope = {k: v for k, v in binding.items() if k != "inspection_ref"}
            prefix = "za-" + _sig(key, REQUEST + "/scope", scope)[:16]
            now = int(time.time())
            for known in self.directory.glob(f"zapier-approval-{prefix}*-request.json"):
                prior_id = known.name.removeprefix("zapier-approval-").removesuffix("-request.json")
                prior = self._request(prior_id, p, key, fresh=False)
                require(all(prior[k] == v for k, v in scope.items()), "request_scope_mismatch")
                if prior["expires_at"] > now:
                    self._inspect(prior)
                    return self._wire(prior, key)
            # Reopening after expiry generates a new challenge only after a new sealed exposure.
            rid = prefix + _sig(key, REQUEST + "/presentation", binding)[:16]
            path = self._path(rid, "request")
            if not os.path.lexists(path):
                req = binding | {
                    "schema_version": REQUEST,
                    "request_id": rid,
                    "nonce": secrets.token_hex(32),
                    "issued_at": now,
                    "expires_at": min(p["expires_at"], inspection["expires_at"], now + ttl_seconds),
                }
                _record(path, {"request": req, "seal": _sig(key, REQUEST, req)})
            req = self._request(rid, p, key)
        return self._wire(req, key)

    def flush(self, request_id: str, hook: str, post: Callable[[str, Json], int] = _post) -> Json:
        p, key = self._policy()
        require(hook_fingerprint(hook) == p["hook_fingerprint"], "plugin_destination_mismatch")
        path = self._path(request_id, "request")
        _check_file(path)
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            req = self._request(request_id, p, key)
            self._inspect(req)
            final = self._path(request_id, "transport")
            admitted = self._path(request_id, "admitted")
            if os.path.lexists(final):
                return _read_private(final, 1024)
            if os.path.lexists(admitted):
                return {"request_id": request_id, "transport": "uncertain"}
            store = LocalTrainingStore(
                self.directory / "training.sqlite3", self.organization, existing_only=True
            )
            with store.engagement_guard(engagement_id=p["engagement"]):
                self._inspect(req)
                current = _read_private(self.config, 16_384)
                require(current == p, "plugin_policy_changed")
                _record(admitted, {"request_id": request_id, "admitted_at": int(time.time())})
                dir_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            try:
                status = post(hook, self._wire(req, key))
                disposition = "accepted" if 200 <= status < 300 else "failed"
            except (OSError, http.client.HTTPException):
                disposition = "uncertain"
            receipt = {"request_id": request_id, "transport": disposition}
            _record(final, receipt)
            return receipt

    def flush_ready(self, hook: str, post: Callable[[str, Json], int] = _post) -> Json:
        """One supervised pass. Uncertain submissions need the original downstream receipt."""
        self._policy()
        sent = held = attempted = 0
        for path in sorted(self.directory.glob("zapier-approval-za-*-request.json")):
            request_id = path.name.removeprefix("zapier-approval-").removesuffix("-request.json")
            if not REQ.fullmatch(request_id) or os.path.lexists(self._path(request_id, "admitted")):
                continue
            if attempted >= 25:
                break
            try:
                receipt = self.flush(request_id, hook, post)
            except (ValueError, OSError, sqlite3.Error):
                held += 1
                continue
            attempted += 1
            sent += int(receipt["transport"] == "accepted")
            held += int(receipt["transport"] != "accepted")
        return {"accepted": sent, "held": held}

    def consume(self, envelope: Json) -> Json:
        p, key = self._policy()
        require(
            set(envelope) == {"event", "signature"}
            and isinstance(envelope["event"], dict)
            and isinstance(envelope["signature"], str)
            and HEX.fullmatch(envelope["signature"]),
            "invalid_provider_envelope",
        )
        event = envelope["event"]
        require(
            set(event) == FIELDS
            and event["schema_version"] == EVENT
            and hmac.compare_digest(envelope["signature"], _sig(key, EVENT, event)),
            "provider_signature_invalid",
        )
        request_id = event["request_id"]
        path = self._path(request_id, "request")
        _check_file(path)
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            req = self._request(request_id, p, key)
            _check_file(self._path(request_id, "admitted"))
            require(
                event["nonce"] == req["nonce"]
                and event["zap_id"] == p["zap_id"]
                and event["zap_version"] == p["zap_version"],
                "provider_scope_mismatch",
            )
            require(
                type(event["decided_at"]) is int
                and req["issued_at"] <= event["decided_at"] < req["expires_at"]
                and event["decided_at"] <= int(time.time())
                and isinstance(event["event_id"], str)
                and OPAQUE.fullmatch(event["event_id"]),
                "invalid_provider_event",
            )
            require(
                event["status"] in ("Pending", "Approved", "Declined"), "invalid_provider_status"
            )
            if event["status"] == "Pending":
                return {"request_id": request_id, "state": "pending"}
            require(
                event["actor_id"] == p["reviewer"]
                or (event["actor_id"] is None and p["approver_mode"] == "verified_single_approver"),
                "named_approver_unverified",
            )
            self._inspect(req)
            current_policy, current_key = self._policy()
            require(
                current_policy == p and hmac.compare_digest(current_key, key),
                "plugin_policy_changed",
            )
            decision = "approved" if event["status"] == "Approved" else "changes_requested"
            reservation = self._path(request_id, "event")
            identity = digest({k: v for k, v in event.items() if k != "decided_at"})
            stamp = {
                "event_identity": identity,
                "first_event_sha256": digest(event),
                "request_id": request_id,
            }

            if os.path.lexists(reservation):
                require(
                    _read_private(reservation, 1024).get("event_identity") == identity,
                    "conflicting_provider_event",
                )
            else:
                _record(reservation, stamp)
            receipt = release_gate.record_release(
                self.directory,
                self.organization,
                **{k: req[k] for k in BIND},
                decision=decision,
                provider="zapier_slack",
            )
            return {
                "request_id": request_id,
                "state": decision,
                "release_receipt_sha256": digest(receipt),
            }

    def import_ready(self) -> Json:
        """Drain signed files from the customer's private, preconfigured return transport."""
        self._policy()
        inbox = self.directory / "zapier-decisions"
        if not os.path.lexists(inbox):
            return {"processed": 0, "held": 0}
        _check_directory(inbox / "entry.json")
        processed = held = 0
        for path in sorted(inbox.glob("za-*.json")):
            match = re.fullmatch(r"(za-[a-f0-9]{32})\.(pending|approved|declined)\.json", path.name)
            if not match:
                continue
            request_id, status = match.groups()
            marker = self._path(request_id, "import-" + status)
            try:
                envelope = _read_private(path, 16_384)
                message_hash = digest(envelope)
                prior = _read_private(marker, 1024) if os.path.lexists(marker) else None
                if prior and prior["envelope_sha256"] == message_hash:
                    continue
                event = envelope.get("event")
                require(
                    isinstance(event, dict)
                    and event.get("request_id") == request_id
                    and event.get("status") == status.capitalize(),
                    "return_filename_mismatch",
                )
                self.consume(envelope)
                if not prior:
                    _record(marker, {"envelope_sha256": message_hash})
                processed += 1
            except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
                held += 1
                continue
            if processed >= 25:
                break
        return {"processed": processed, "held": held}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--organization", required=True)
    parser.add_argument("--config", type=Path, required=True)
    actions = parser.add_subparsers(dest="operation", required=True)
    actions.add_parser("check-config")
    issue = actions.add_parser("issue")
    for name in ("run-id", "run-revision", "artifact-sha", "inspection-ref"):
        issue.add_argument("--" + name, required=True)
    issue.add_argument("--ttl-seconds", type=int, default=900)
    selector = actions.add_parser("flush").add_mutually_exclusive_group(required=True)
    selector.add_argument("--request-id")
    selector.add_argument("--all", action="store_true")
    actions.add_parser("import").add_argument("--input", type=Path, required=True)
    actions.add_parser("import-ready")
    args = parser.parse_args()
    try:
        bridge = ApprovalBridge(args.directory, args.organization, args.config)
        if args.operation == "check-config":
            value = bridge.check()
        elif args.operation == "issue":
            value = bridge.issue(
                run_id=args.run_id,
                run_revision=args.run_revision,
                artifact_sha=args.artifact_sha,
                inspection_ref=args.inspection_ref,
                ttl_seconds=args.ttl_seconds,
            )
        elif args.operation == "flush":
            hook = os.environ.get("OPENTOUR_ZAPIER_APPROVAL_HOOK_URL", "")
            value = bridge.flush_ready(hook) if args.all else bridge.flush(args.request_id, hook)
        elif args.operation == "import-ready":
            value = bridge.import_ready()
        else:
            require(args.input.is_absolute(), "private_signed_decision_required")
            value = bridge.consume(_read_private(args.input, 16_384))
        print(canonical(value))
        return 0
    except ApprovalError as error:
        print(str(error), file=sys.stderr)
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        print("approval_unavailable", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
