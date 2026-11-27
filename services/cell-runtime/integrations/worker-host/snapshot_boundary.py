"""Check a worker-volume snapshot against the installed snapshot boundary.

Run as the installer with ``python3 -I -S -B snapshot_boundary.py``; standard library only.
``boundary`` prints the paths inside and outside restorable snapshots. ``check`` refuses a
snapshot copy that carries material which must never be restored: credentials, TLS keys,
environment files, anti-rollback state, a staged model base, links or widened permissions.
It reads only names and metadata, never file contents.
"""

import argparse
import json
import os
import stat
import sys
from pathlib import Path

SCHEMA = "opentour.worker-snapshot-boundary.v1"
VOLUME = "/var/lib/opentour-worker"
ANTI_ROLLBACK = "/var/lib/opentour-antirollback"
# Restorable: the quiescent worker volume, without the re-staged model base.
INSIDE = (VOLUME,)
EXCLUDED_FROM_VOLUME = (
    "model-base",
    "model-base.held",
    ".model-base.staging",
    ".model-base.previous",
)
# Never in a worker snapshot, so a restore cannot bring an old value back.
OUTSIDE = {
    "/etc/opentour-worker/site.env": "root:root 0600; organization and numeric runtime path",
    "/etc/opentour-worker/cycle.env": "root:root 0600; workplace reader credentials",
    "/etc/opentour-health": "root:opentour-health 0750; health manifest, TLS and credential",
    ANTI_ROLLBACK: "opentour-worker:opentour-worker 0700; reserved for anti-rollback state",
    "/opt/opentour": "root:root 0755; interpreter, releases, current link, numeric runtime",
    "/var/lib/opentour-worker/model-base": "root:root 0555, files 0444; re-staged, not restored",
}
FORBIDDEN_NAMES = frozenset(
    {
        "site.env",
        "cycle.env",
        "credential",
        "tls.key",
        "tls.crt",
        "opentour-health",
        "opentour-antirollback",
    }
)


def boundary():
    return {
        "schema": SCHEMA,
        "inside": list(INSIDE),
        "excluded_from_volume": list(EXCLUDED_FROM_VOLUME),
        "outside": dict(OUTSIDE),
    }


def _within(path, root):
    return path == root or root in path.parents


def check(snapshot, restore_to=None):
    """Return the findings that make this snapshot unsafe to restore; empty means acceptable."""
    findings = set()
    root = Path(os.path.abspath(snapshot))
    try:
        info = os.lstat(root)
    except OSError:
        return ["snapshot_required"]
    if not stat.S_ISDIR(info.st_mode):
        return ["snapshot_required"]
    if info.st_mode & 0o077:
        findings.add("snapshot_permissions_too_wide")
    if restore_to is not None:
        # The anti-rollback directory is the volume's sibling on every host layout.
        target = Path(os.path.abspath(restore_to))
        anchors = target.parent / Path(ANTI_ROLLBACK).name
        if target.name != Path(VOLUME).name:
            findings.add("restore_target_not_worker_volume")
        if _within(root, target) or _within(root, anchors) or _within(target, root):
            findings.add("snapshot_overlaps_live_state")
    for directory, dirs, files in os.walk(root):
        here = Path(directory)
        for name in dirs + files:
            path = here / name
            entry = os.lstat(path)
            if here == root and name in EXCLUDED_FROM_VOLUME:
                findings.add("model_base_in_snapshot")
            if name in FORBIDDEN_NAMES or name.endswith(".env"):
                findings.add("out_of_boundary_material")
            if stat.S_ISLNK(entry.st_mode):
                findings.add("link_in_snapshot")
            elif not (stat.S_ISDIR(entry.st_mode) or stat.S_ISREG(entry.st_mode)):
                findings.add("special_file_in_snapshot")
            elif stat.S_ISREG(entry.st_mode) and entry.st_nlink != 1:
                findings.add("hard_link_in_snapshot")
            if entry.st_mode & 0o077 and not stat.S_ISLNK(entry.st_mode):
                findings.add("snapshot_permissions_too_wide")
        dirs[:] = [d for d in dirs if not os.path.islink(here / d)]
    return sorted(findings)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("boundary")
    checker = commands.add_parser("check")
    checker.add_argument("--snapshot", type=Path, required=True)
    checker.add_argument("--restore-to", type=Path)
    args = parser.parse_args(argv)
    if args.command == "boundary":
        print(json.dumps(boundary(), sort_keys=True))
        return 0
    findings = check(args.snapshot, args.restore_to)
    print(json.dumps({"schema": SCHEMA, "accepted": not findings, "findings": findings}))
    return 0 if not findings else 2


if __name__ == "__main__":
    sys.exit(main())
