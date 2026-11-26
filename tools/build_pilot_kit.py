"""Build the offline pilot kit from tracked blobs at the current Git commit."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from zipfile import ZIP_STORED, ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parents[1]
CELL = "services/cell-runtime"
KIT_VERSION = "1.0.0-rc.3"
PYTHON = (3, 12, 16)
MODULES = (
    "opentour_cell/__init__.py",
    "opentour_cell/pilot/__init__.py",
    "opentour_cell/pilot/brief.py",
    "opentour_cell/pilot/operating_point.py",
    "opentour_cell/pilot/readout.py",
)


def _git(*args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(ROOT), *args])


def _archive(files: dict[str, bytes], *, executable: str = "", prefix: bytes = b"") -> bytes:
    data = io.BytesIO()
    data.write(prefix)
    with ZipFile(data, "w", compression=ZIP_STORED) as archive:
        for name, payload in sorted(files.items()):
            entry = ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            entry.create_system = 3
            entry.external_attr = (0o100755 if name == executable else 0o100644) << 16
            archive.writestr(entry, payload)
    return data.getvalue()


def _kit(revision: str) -> bytes:
    def blob(path: str) -> bytes:
        return _git("show", f"{revision}:{path}")

    if blob("tools/build_pilot_kit.py") != Path(__file__).read_bytes():
        raise ValueError("Use the build script exactly as committed at HEAD.")
    modules = {name: blob(f"{CELL}/src/{name}") for name in MODULES}
    version = f"OpenTour offline pilot kit {KIT_VERSION} ({revision})"
    modules["__main__.py"] = f"""import sys

if sys.version_info[:3] != {PYTHON!r}:
    print("This kit requires Python 3.12.16.", file=sys.stderr)
    raise SystemExit(2)
if sys.argv[1:] == ["--version"]:
    print({version!r})
    raise SystemExit(0)

from opentour_cell.pilot.readout import main
raise SystemExit(main())
""".encode()
    files = {
        "opentour-pilot.pyz": _archive(modules, prefix=b"#!/usr/bin/env python3\n"),
        "example.json": blob(f"{CELL}/examples/pilot-evaluation.json"),
        "README.md": blob(f"{CELL}/docs/pilot-kit.md"),
        "manifest-guide.md": blob(f"{CELL}/docs/pilot-readout.md"),
        "PROVENANCE.json": (
            json.dumps(
                {
                    "kit_version": KIT_VERSION,
                    "source_repository": "kevon-dot/opentour-ai",
                    "source_commit": revision,
                    "python_version": "3.12.16",
                    "artifact_kind": "release_candidate",
                    "example_kind": "generated_fixture",
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode(),
    }
    with tempfile.TemporaryDirectory() as temporary:
        working = Path(temporary)
        for name in ("opentour-pilot.pyz", "example.json"):
            (working / name).write_bytes(files[name])
        subprocess.run(
            [
                sys.executable,
                "-I",
                "-S",
                "-B",
                "opentour-pilot.pyz",
                "--report-dir",
                "example-readout",
                "example.json",
            ],
            cwd=working,
            check=True,
        )
        for name in ("brief.md", "aggregate.json"):
            member = f"example-readout/{name}"
            files[member] = (working / member).read_bytes()
    files["SHA256SUMS"] = "".join(
        f"{hashlib.sha256(payload).hexdigest()}  {name}\n"
        for name, payload in sorted(files.items())
    ).encode()
    return _archive(files, executable="opentour-pilot.pyz")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="new ZIP path for the release candidate")
    args = parser.parse_args()
    if sys.version_info[:3] != PYTHON:
        parser.exit(2, "Build with Python 3.12.16.\n")
    try:
        revision = _git("rev-parse", "--verify", "HEAD^{commit}").decode().strip()
        artifact = _kit(revision)
        with tempfile.TemporaryDirectory(dir=args.output.parent) as scratch:
            complete = Path(scratch) / "candidate.zip"
            complete.write_bytes(artifact)
            os.link(complete, args.output)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        parser.exit(2, f"Unable to build candidate: {exc}\n")
    print(f"{hashlib.sha256(artifact).hexdigest()}  {args.output.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
