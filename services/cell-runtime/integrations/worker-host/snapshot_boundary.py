"""Screen a worker-volume snapshot copy against the installed snapshot boundary.

Run as the installer with ``python3 -I -S -B snapshot_boundary.py``; standard library only.
``boundary`` prints the paths inside and outside restorable snapshots. ``check`` is a
best-effort screen of entry names and metadata. It never reads file contents, so it cannot
prove that a snapshot is free of secrets. It refuses names that look like credentials, keys,
environment files or anti-rollback state, the staged model base and the root-only staging
area (both stay in the volume but out of snapshots), links, special files, hard
links, special mode bits, widened permissions, entries not owned by the worker identity and
any restore target other than the exact worker volume.
"""

import argparse
import json
import os
import pwd
import stat
import sys
import unicodedata
from pathlib import Path

SCHEMA = "opentour.worker-snapshot-boundary.v1"
VOLUME = "/var/lib/opentour-worker"
ANTI_ROLLBACK = "/var/lib/opentour-antirollback"
WORKER = "opentour-worker"
INSIDE = (VOLUME,)
EXCLUDED_FROM_VOLUME = ("model-base", ".opentour-staging")
# Never in a worker snapshot, so a restore cannot bring an old value back.
OUTSIDE = {
    "/etc/opentour-worker/site.env": "root:root 0600; organization and numeric runtime path",
    "/etc/opentour-worker/cycle.env": "root:root 0600; workplace reader credentials",
    "/etc/opentour-health": "root:opentour-health 0750; health manifest, TLS and credential",
    ANTI_ROLLBACK: "opentour-worker:opentour-worker 0700; reserved, not yet written (OPE-566)",
    "/var/lib/opentour-worker/.opentour-staging": "root:root 0700; staging, previous, held base",
    "/opt/opentour": "root:root 0755; interpreter, releases, current link, numeric runtime",
    "/var/lib/opentour-worker/model-base": "root:root 0555, files 0444; re-staged, not restored",
}
LIVE_STATE = (VOLUME, ANTI_ROLLBACK, "/etc", "/opt/opentour")
FORBIDDEN_NAMES = frozenset(
    {"credential", "credentials", "opentour-health", "opentour-antirollback"}
)
FORBIDDEN_FRAGMENTS = (".env", "token", "credential", "secret", "password", "passwd", "apikey")
FORBIDDEN_SUFFIXES = (".pem", ".key", ".crt", ".p12", ".pfx", ".der", ".jks", ".keystore")
# Control, format (including zero-width), private-use, surrogate and unassigned characters.
_HIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Co", "Cs", "Cn", "Zl", "Zp"})


def boundary():
    return {
        "schema": SCHEMA,
        "inside": list(INSIDE),
        "excluded_from_volume": list(EXCLUDED_FROM_VOLUME),
        "outside": dict(OUTSIDE),
    }


def _within(path, root):
    return path == root or root in path.parents


def _name_findings(name):
    findings = set()
    visible = "".join(c for c in name if unicodedata.category(c) not in _HIDDEN_CATEGORIES)
    if visible != name:
        findings.add("hidden_character_in_name")
    folded = unicodedata.normalize("NFKC", visible).casefold()
    if folded != visible.casefold():
        findings.add("non_normalized_name")
    if folded == "model-base":
        findings.add("model_base_in_snapshot")
    if folded == ".opentour-staging":
        findings.add("staging_area_in_snapshot")
    if (
        folded in FORBIDDEN_NAMES
        or folded.endswith(FORBIDDEN_SUFFIXES)
        or folded == "env"
        or any(fragment in folded for fragment in FORBIDDEN_FRAGMENTS)
    ):
        findings.add("out_of_boundary_material")
    return findings


def _entry_findings(entry, owner_uid):
    findings = set()
    mode = entry.st_mode
    if stat.S_ISLNK(mode):
        return {"link_in_snapshot"}
    if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
        findings.add("special_file_in_snapshot")
    if stat.S_ISREG(mode) and entry.st_nlink != 1:
        findings.add("hard_link_in_snapshot")
    if mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX):
        findings.add("special_mode_in_snapshot")
    if mode & 0o077:
        findings.add("snapshot_permissions_too_wide")
    if entry.st_uid != owner_uid:
        findings.add("foreign_owner_in_snapshot")
    return findings


def check(snapshot, restore_to=None, *, owner_uid):
    """Return the screen's findings; empty means nothing the screen looks for was found."""
    root = Path(os.path.abspath(snapshot))
    findings = set()
    if any(_within(root, Path(live)) for live in LIVE_STATE):
        findings.add("snapshot_overlaps_live_state")
    if restore_to is not None and Path(os.path.abspath(restore_to)) != Path(VOLUME):
        findings.add("restore_target_not_worker_volume")
    try:
        info = os.lstat(root)
    except OSError:
        return sorted(findings | {"snapshot_required"})
    if not stat.S_ISDIR(info.st_mode):
        return sorted(findings | {"snapshot_required"})
    findings |= _entry_findings(info, owner_uid)
    for directory, dirs, files in os.walk(root):
        here = Path(directory)
        for name in dirs + files:
            findings |= _name_findings(name)
            findings |= _entry_findings(os.lstat(here / name), owner_uid)
        dirs[:] = [d for d in dirs if not os.path.islink(here / d)]
    return sorted(findings)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("boundary")
    checker = commands.add_parser("check")
    checker.add_argument("--snapshot", type=Path, required=True)
    checker.add_argument("--restore-to", type=Path)
    checker.add_argument("--owner", default=WORKER, help="worker identity that owns the volume")
    args = parser.parse_args(argv)
    if args.command == "boundary":
        print(json.dumps(boundary(), sort_keys=True))
        return 0
    try:
        owner_uid = pwd.getpwnam(args.owner).pw_uid
    except KeyError:
        findings = ["worker_identity_unknown"]
    else:
        findings = check(args.snapshot, args.restore_to, owner_uid=owner_uid)
    print(json.dumps({"schema": SCHEMA, "accepted": not findings, "findings": findings}))
    return 0 if not findings else 2


if __name__ == "__main__":
    sys.exit(main())
