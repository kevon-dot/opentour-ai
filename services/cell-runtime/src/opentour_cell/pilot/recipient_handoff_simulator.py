"""Run a committed artifact handoff against an enrolled local generated recipient."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sqlite3
import stat
import subprocess
import sys
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from zipfile import BadZipFile

from opentour_cell.pilot import repair_bundle
from opentour_cell.pilot.intake_mcp import require_ref
from opentour_cell.pilot.local_handoff_permissions import handoff_outbox
from opentour_cell.pilot.onboarding_mcp import _read_private
from opentour_cell.pilot.operating_point import canonical
from opentour_cell.pilot.recipient_delivery import LocalRecipientDelivery, dispatch_local_release
from opentour_cell.pilot.release_outbox import (
    ApprovedRelease,
    ReceiverObservation,
    ReleaseSelection,
)
from opentour_cell.pilot.training import LocalTrainingStore

SCHEMA = "opentour.pilot.recipient-handoff-simulator.v1"
LOST_ACK = 75
RECIPE_SHA256 = "87f7d63df654397008d8b7141dd02c1480974b1662ff8b6ae9484f960438c4e2"


def _trigger_release(recipe: Path, trigger: Path) -> str:
    # A public installed recipe is data. Its named actions never select executable code.
    fd = os.open(recipe, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        attrs = os.fstat(source.fileno())
        if not stat.S_ISREG(attrs.st_mode) or attrs.st_mode & 0o022 or attrs.st_size > 4096:
            raise ValueError("invalid_handoff_recipe")
        if hashlib.sha256(source.read(4097)).hexdigest() != RECIPE_SHA256:
            raise ValueError("invalid_handoff_recipe")
    event = _read_private(trigger, 1024)
    if set(event) != {"release_ref"}:
        raise ValueError("invalid_handoff_trigger")
    return require_ref(event["release_ref"])


class _InterruptedReply(LocalRecipientDelivery):
    def _receive_claimed(
        self,
        release: ApprovedRelease,
        offered: Path,
        *,
        sender_current: Callable[[], AbstractContextManager[Callable[[], None]]],
    ) -> str:
        super()._receive_claimed(release, offered, sender_current=sender_current)
        os._exit(LOST_ACK)


class _UnavailableRecipient:
    def __init__(self, clock: Callable[[], int]):
        self.clock = clock

    def lookup(self, release: ApprovedRelease, lookup_ref: str) -> ReceiverObservation:
        return ReceiverObservation(
            "unknown",
            release.reference,
            release.destination_ref,
            release.artifact_sha256,
            self.clock(),
            lookup_ref,
        )


def simulate(
    configuration: Path, release_ref: str, *, lose_ack: bool = False
) -> dict[str, str | None]:
    if (
        re.fullmatch(r"release-[0-9a-f]{48}", release_ref) is None
        or not configuration.is_absolute()
        or configuration.parent.resolve(strict=True) != configuration.parent
    ):
        raise ValueError("invalid_simulator_scope")
    scope = _read_private(configuration, 4096)
    if (
        set(scope) != {"schema_version", "tenant", "actor", "recipient_directory", "selection"}
        or scope["schema_version"] != SCHEMA
    ):
        raise ValueError("invalid_simulator_scope")
    tenant, actor = require_ref(scope["tenant"]), require_ref(scope["actor"])
    recipient = Path(scope["recipient_directory"])
    worker = configuration.parent
    selection = scope["selection"]
    if (
        not tenant.startswith("generated-")
        or not recipient.is_absolute()
        or recipient.resolve() != recipient
        or recipient.is_relative_to(worker)
        or worker.is_relative_to(recipient)
        or not isinstance(selection, dict)
        or set(selection) != {"workflow_ref", "proposal_ref", "approval_ref"}
    ):
        raise ValueError("invalid_simulator_scope")
    selected = ReleaseSelection(**selection)
    outbox = handoff_outbox(
        LocalTrainingStore(worker / "training.sqlite3", tenant, existing_only=True)
    )
    state = outbox.status(selected, actor)
    if state is None or state.reference != release_ref:
        raise ValueError("release_unavailable")
    if state.stage != "delivered":
        receiver_type = _InterruptedReply if lose_ack else LocalRecipientDelivery
        try:
            receiver = receiver_type(recipient / "recipient.json")
        except (OSError, repair_bundle.BundleError):
            state = outbox.reconcile(selected, actor, _UnavailableRecipient(outbox.clock))
        else:
            if state.stage == "delivery_unconfirmed":
                state = outbox.reconcile(selected, actor, receiver)
            else:
                try:
                    state = dispatch_local_release(
                        outbox, selected, actor, receiver, recipient / "offered.zip"
                    )
                except (repair_bundle.BundleError, BadZipFile, subprocess.SubprocessError):
                    # Preserve a durable unresolved claim if package verification or staging fails.
                    state = outbox.status(selected, actor)
                    if state is None or state.reference != release_ref:
                        raise ValueError("release_unavailable") from None
    return {
        "schema_version": SCHEMA,
        "release_ref": state.reference,
        "state": state.stage,
        "receiver_receipt_ref": state.receiver_receipt_ref,
        "owner_action": "authorized_operator_reconcile"
        if state.stage == "delivery_unconfirmed"
        else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--configuration", required=True, type=Path)
    trigger = parser.add_mutually_exclusive_group(required=True)
    trigger.add_argument("--release-ref")
    trigger.add_argument("--trigger", type=Path)
    parser.add_argument("--recipe", type=Path)
    parser.add_argument("--simulate-lost-ack", action="store_true")
    args = parser.parse_args()
    try:
        if (args.trigger is None) != (args.recipe is None):
            raise ValueError("invalid_handoff_trigger")
        release_ref = (
            _trigger_release(args.recipe, args.trigger)
            if args.trigger is not None
            else args.release_ref
        )
        result = simulate(args.configuration, release_ref, lose_ack=args.simulate_lost_ack)
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        print("private_handoff_unavailable", file=sys.stderr)
        return 2
    print(canonical(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
