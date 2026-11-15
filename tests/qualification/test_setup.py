"""Explicit full setup qualification: run on each admitted OS/architecture profile.

Run separately from the bounded contract suite:
    uv run python -m pytest tests/qualification/test_setup.py -q -s
This installs the real frozen dependencies in a disposable clean clone. It never
provisions cloud resources and requires the pinned local tools and package access.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path

from tools import bootstrap


def command(args: list[str], cwd: Path) -> str:
    env = os.environ.copy()
    env.pop("VIRTUAL_ENV", None)
    result = subprocess.run(
        args, cwd=cwd, env=env, capture_output=True, text=True, timeout=1800, check=False
    )
    assert result.returncode == 0, bootstrap.safe_error(result.stderr or result.stdout)
    return result.stdout.strip()


def test_clean_checkout_setup_twice_preserves_user_and_source_files():
    source = bootstrap.ROOT
    assert not command(["git", "status", "--porcelain", "--untracked-files=normal"], source), (
        "Commit the candidate before full qualification; uncommitted source is not accepted."
    )
    candidate = command(["git", "rev-parse", "HEAD"], source)
    with tempfile.TemporaryDirectory(prefix="opentour qualification ") as temporary:
        root = Path(temporary) / "checkout with spaces"
        command(
            ["git", "clone", "--no-hardlinks", "--single-branch", str(source), str(root)], source
        )
        assert command(["git", "rev-parse", "HEAD"], root) == candidate
        tracked = command(["git", "ls-files", "-z"], root).split("\0")
        tracked = [path for path in tracked if path]
        # These generated fixtures model existing user content, never private files.
        (root / "README.md").write_text("Existing user README — sentinel fixture v1\n")
        (root / "LICENSE").write_text("Existing user license — sentinel fixture v1\n")
        (root / "unrelated user file.txt").write_text("Unrelated sentinel fixture v1\n")
        paths = (*tracked, "LICENSE", "unrelated user file.txt")
        before = bootstrap.fingerprints(root, paths)
        receipts = []
        for _ in range(2):
            receipt = json.loads(command(["just", "setup"], root).splitlines()[-1])
            assert receipt["status"] == "PASS"
            assert receipt["kind"] == "local_integration"
            assert receipt["commit_sha"] == candidate
            assert receipt["git_worktree_dirty"] is True  # The three intentional user fixtures.
            assert receipt["product_acceptance"] == "NOT_RUN"
            assert bootstrap.fingerprints(root, paths) == before
            receipts.append(receipt)
        assert receipts[0]["lock_and_manifest_sha256"] == receipts[1]["lock_and_manifest_sha256"]
        checks = [
            ["uv", "run", "--no-sync", "python", "-m", "pytest", "tests/contract", "-q"],
            ["uv", "run", "--no-sync", "ruff", "check", "."],
            [
                "uv", "run", "--no-sync", "mypy", "tools", "services/control-api/src",
                "services/cell-runtime/src", "services/audit-journal/src", "training/src",
            ],
            [
                "pnpm", "exec", "tsc", "--noEmit", "--strict", "--skipLibCheck",
                "--module", "NodeNext", "--moduleResolution", "NodeNext",
                "packages/client-ts/src/index.d.ts",
            ],
        ]
        check_results = [{"command": args, "output": command(args, root)} for args in checks]
        assert bootstrap.fingerprints(root, paths) == before
        print(json.dumps({
            "issue_key": "OPE-122",
            "fixture_id": "clean-checkout-repeat-and-preservation",
            "fixture_version": "1",
            "kind": "local_integration",
            "status": "PASS",
            "commit_sha": candidate,
            "source_candidate_clean": True,
            "intentional_user_fixture_paths": ["README.md", "LICENSE", "unrelated user file.txt"],
            "preserved_files_sha256": hashlib.sha256(
                json.dumps(before, sort_keys=True).encode()
            ).hexdigest(),
            "setup_runs": receipts,
            "checks": check_results,
            "product_acceptance": "NOT_RUN",
        }, sort_keys=True))
