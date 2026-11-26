"""Rebuild and operate a committed, generated offline kit in isolated directories."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from zipfile import ZipFile


def test_kit_rebuild_sample_integrity_and_isolated_replay(tmp_path):
    root = Path(__file__).resolve().parents[4]
    cell = Path("services/cell-runtime")
    src = cell / "src/opentour_cell"
    files = (
        Path("tools/build_pilot_kit.py"),
        src / "__init__.py",
        src / "pilot/__init__.py",
        src / "pilot/readout.py",
        src / "pilot/brief.py",
        src / "pilot/operating_point.py",
        cell / "examples/pilot-evaluation.json",
        cell / "docs/pilot-kit.md",
        cell / "docs/pilot-readout.md",
    )
    repo = tmp_path / "source"
    for path in files:
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / path, repo / path)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "--no-gpg-sign",
            "-qm",
            "Generated test snapshot",
        ],
        cwd=repo,
        check=True,
    )
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    command = [sys.executable, "-I", "-S", "-B"]
    first, second = tmp_path / "first.zip", tmp_path / "second.zip"
    for output in (first, second):
        subprocess.run(command + [str(files[0]), str(output)], cwd=repo, check=True)
    assert first.read_bytes() == second.read_bytes()
    operator = tmp_path / "operator"
    with ZipFile(first) as archive:
        archive.extractall(operator)
        assert json.loads(archive.read("PROVENANCE.json"))["source_commit"] == revision
        checks = archive.read("SHA256SUMS").decode().splitlines()
        assert len(checks) == len(archive.namelist()) - 1
        for line in checks:
            digest, name = line.split("  ")
            assert hashlib.sha256(archive.read(name)).hexdigest() == digest
    executable = command + ["opentour-pilot.pyz"]
    assert revision in subprocess.check_output(
        executable + ["--version"],
        cwd=operator,
        text=True,
        env={},
    )
    for destination in ("one", "two"):
        subprocess.run(
            executable + ["--report-dir", destination, "example.json"],
            cwd=operator,
            env={},
            check=True,
        )
        for name in ("brief.md", "aggregate.json"):
            assert (operator / destination / name).read_bytes() == (
                operator / "example-readout" / name
            ).read_bytes()
    bad = subprocess.run(
        executable + ["--report-dir", "invalid", "-"],
        cwd=operator,
        env={},
        input=b"{}",
        capture_output=True,
        check=False,
    )
    assert bad.returncode == 2 and bad.stdout == b"" and not (operator / "invalid").exists()
