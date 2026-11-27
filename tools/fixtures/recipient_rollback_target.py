"""Exercise activation and rollback on a private generated recipient target."""

from __future__ import annotations

import argparse
import hmac
import os
import re
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from zipfile import BadZipFile

from opentour_cell.pilot import repair_bundle as bundle
from opentour_cell.pilot.intake_mcp import require_ref
from opentour_cell.pilot.operating_point import canonical, digest
from opentour_cell.pilot.readout import parse_manifest
from opentour_cell.pilot.recipient_delivery import LocalRecipientDelivery
from opentour_cell.pilot.release_outbox import ApprovedRelease, ReleaseSelection

CONFIG = "opentour.fixture.recipient-test-target.v1"
ACTION = "opentour.fixture.recipient-test-change.v1"
STATE_FILE = "test-target-state.json"


def _read(path: Path) -> bundle.Json:
    return parse_manifest(bundle._read(path, 64 * 1024))


def _key(root: Path, name: str) -> bytes:
    bundle._require(
        isinstance(name, str) and Path(name).name == name and name != STATE_FILE,
        "test_operator_key_unavailable",
    )
    stored = _read(root / name)
    key = stored.get("key")
    bundle._require(
        set(stored) == {"key"}
        and isinstance(key, str)
        and bool(re.fullmatch(r"[0-9a-f]{64}", key)),
        "test_operator_key_unavailable",
    )
    assert isinstance(key, str)
    return bytes.fromhex(key)


def _decision(
    spec: bundle.Json, members: dict[str, bytes], grant: bundle.Json, query: bundle.Json, route: str
) -> str:
    bundle._require(
        "features" in query and set(query) <= {"features", "incumbent"},
        "decision_time_features_only",
    )
    incumbent = bundle._decision(query["incumbent"]) if "incumbent" in query else None
    return digest(
        bundle._predictions(
            route, spec, members, grant, [bundle._object(query["features"])], [incumbent]
        )[0]
    )


def _change(
    request: bundle.Json,
    key: bytes,
    scope: bundle.Json,
    release: ApprovedRelease,
    state: bundle.Json,
    versions: dict[str, str],
    decisions: dict[str, str],
) -> tuple[bundle.Json, bundle.Json | None]:
    bundle._require(set(request) == {"claim", "seal_sha256"}, "signed_test_change_required")
    claim = bundle._object(request["claim"])
    binding = {
        "schema_version": ACTION,
        "target_ref": scope["target_ref"],
        "tenant": release.tenant,
        "engagement": release.engagement,
        "release_ref": release.reference,
        "receiver_receipt_ref": scope["receiver_receipt_ref"],
        "artifact_sha256": release.artifact_sha256,
        "operator": scope["operator"],
    }
    bundle._require(
        set(claim)
        == set(binding)
        | {"operation", "expected_version", "target_version", "expires_at", "request_ref"}
        and all(claim[k] == value for k, value in binding.items()),
        "test_change_scope_mismatch",
    )
    seal = request["seal_sha256"]
    bundle._require(
        isinstance(seal, str)
        and hmac.compare_digest(seal, hmac.digest(key, canonical(claim).encode(), "sha256").hex()),
        "invalid_test_operator_signature",
    )
    bundle._require(
        type(claim["expires_at"]) is int
        and time.time() < claim["expires_at"] <= release.expires_at,
        "test_change_expired",
    )
    direction = {
        "activate_test": ("fallback", "winner"),
        "rollback_test": ("winner", "fallback"),
    }
    route = direction.get(claim["operation"])
    bundle._require(
        route is not None
        and (claim["expected_version"], claim["target_version"])
        == tuple(versions[k] for k in route),
        "test_change_route_mismatch",
    )
    assert route is not None
    request_ref = require_ref(claim["request_ref"])
    command_sha = digest(claim)
    previous = state["actions"].get(request_ref)
    if previous is not None:
        bundle._require(previous["claim_sha256"] == command_sha, "test_change_conflict")
        return bundle._object(previous["receipt"]), None
    bundle._require(state["active_version"] == claim["expected_version"], "test_target_changed")
    receipt = {
        "target_ref": scope["target_ref"],
        "action_ref": "test-change-" + command_sha[:48],
        "active_version": claim["target_version"],
        "decision_sha256": decisions[route[1]],
        "effects": len(state["actions"]) + 1,
    }
    state["active_version"] = claim["target_version"]
    state["actions"][request_ref] = {"claim_sha256": command_sha, "receipt": receipt}
    return receipt, state


def _publish_state(path: Path, state: bundle.Json, check_current: Callable[[], None]) -> None:
    """Replace the complete state and action receipt under the target's directory lock."""
    fd, temporary = tempfile.mkstemp(prefix=".test-route-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(bundle._bytes(state))
            stream.flush()
            os.fsync(stream.fileno())
        check_current()
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def run(configuration: Path, request_file: Path | None) -> bundle.Json:
    root = configuration.parent
    scope = _read(configuration)
    bundle._require(
        set(scope)
        == {
            "schema_version",
            "target_ref",
            "operator",
            "key_file",
            "recipient_grant",
            "release",
            "receiver_receipt_ref",
            "query",
            "baseline",
        }
        and scope["schema_version"] == CONFIG
        and require_ref(scope["target_ref"]).startswith("test-target-")
        and configuration.name != STATE_FILE,
        "generated_test_target_required",
    )
    require_ref(scope["operator"])
    binding = dict(bundle._object(scope["release"]))
    release = ApprovedRelease(selection=ReleaseSelection(**binding.pop("selection")), **binding)
    bundle._require(release.tenant.startswith("generated-"), "generated_test_target_required")
    path = Path(scope["recipient_grant"])
    receiver = LocalRecipientDelivery(path)
    grant = bundle._grant(path)
    custody = bundle._withdrawal_path(grant).parent
    bundle._require(
        not root.is_relative_to(custody) and not custody.is_relative_to(root),
        "independent_test_target_required",
    )
    with bundle._current(path, grant), bundle._directory_lock(root):
        slot, claim = receiver._scope(release, grant)
        receipt = receiver._receipt(slot, release, grant, claim)
        bundle._require(
            receipt["receipt_ref"] == scope["receiver_receipt_ref"],
            "test_recipient_receipt_mismatch",
        )
        spec, members, _ = bundle._load(slot / "artifact.zip", grant)
        routes = ("fallback", "winner")
        bundle._require(
            all(spec[k]["kind"] == "rule" for k in routes)
            and spec["winner"]["version"] == release.artifact_sha256,
            "compatible_test_rule_required",
        )
        versions = {k: spec[k]["version"] for k in routes}
        decisions = {k: _decision(spec, members, grant, scope["query"], k) for k in routes}
        baseline = {"version": versions["fallback"], "decision_sha256": decisions["fallback"]}
        bundle._require(scope["baseline"] == baseline, "test_incumbent_did_not_replay")
        state_path = root / STATE_FILE
        state = (
            _read(state_path)
            if os.path.lexists(state_path)
            else {
                "scope_sha256": digest(scope),
                "active_version": baseline["version"],
                "actions": {},
            }
        )
        bundle._require(
            set(state) == {"scope_sha256", "active_version", "actions"}
            and state["scope_sha256"] == digest(scope)
            and state["active_version"] in versions.values()
            and isinstance(state["actions"], dict),
            "test_target_state_changed",
        )
        if request_file is None:
            active = next(k for k in routes if versions[k] == state["active_version"])
            bundle._require(
                time.time() < min(grant["expires_at"], release.expires_at),
                "test_recipient_grant_expired",
            )
            return {
                "target_ref": scope["target_ref"],
                "active_version": versions[active],
                "decision_sha256": decisions[active],
                "effects": len(state["actions"]),
            }
        request = _read(request_file)
        key = _key(root, scope["key_file"])
        result, updated = _change(request, key, scope, release, state, versions, decisions)
        deadline = min(request["claim"]["expires_at"], grant["expires_at"])
        if updated is not None:

            def check_current() -> None:
                bundle._require(
                    _read(configuration) == scope
                    and _key(root, scope["key_file"]) == key
                    and bundle._grant(path) == grant
                    and receiver._receipt(slot, release, grant, claim) == receipt
                    and time.time() < deadline,
                    "test_change_authority_changed",
                )

            _publish_state(state_path, updated, check_current)
        else:
            bundle._require(time.time() < deadline, "test_recipient_grant_expired")
        return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--configuration", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect")
    inspect.set_defaults(request=None)
    commands.add_parser("change").add_argument("--request", type=Path, required=True)
    args = parser.parse_args()
    try:
        print(canonical(run(args.configuration, args.request)))
    except (BadZipFile, KeyError, OSError, TypeError, ValueError) as exc:
        print(
            str(exc) if isinstance(exc, bundle.BundleError) else "test_target_unavailable",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
