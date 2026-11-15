"""Actual Docker context privacy regression; no image pull or provider call required."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from tools import bootstrap


def test_container_context_excludes_undeclared_synthetic_files():
    root = bootstrap.ROOT
    allowed = [
        line[1:] for line in (root / ".dockerignore").read_text().splitlines()
        if line.startswith("!")
    ]
    sentinels = [
        "private-root.txt", "services/private-records.txt",
        "services/control-api/private-records.txt",
        "services/control-api/src/opentour_control/private-records.txt",
        "training/private-records.txt", "training/src/opentour_training/private-records.txt",
        "packages/client-ts/private-records.txt", "apps/private-records.txt",
        "apps/web/private-records.txt", "containers/private-records.txt",
    ]
    with tempfile.TemporaryDirectory(prefix="opentour context ") as temporary:
        base = Path(temporary)
        context = base / "context"
        context.mkdir()
        for relative in [".dockerignore", *allowed]:
            path = context / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(root / relative, path)
        for relative in sentinels:
            path = context / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("GENERATED SENTINEL: not customer data\n")
        output = base / "copied"
        args = [
            "docker", "buildx", "build", "--network", "none", "--output",
            f"type=local,dest={output}", "--file", "-", str(context),
        ]
        result = subprocess.run(
            args, input="FROM scratch\nCOPY . /\n", capture_output=True,
            text=True, timeout=120, check=False,
        )
        assert result.returncode == 0, bootstrap.safe_error(result.stderr)
        actual = sorted(
            str(path.relative_to(output)) for path in output.rglob("*") if path.is_file()
        )
        assert actual == sorted(allowed)
        assert not any((output / relative).exists() for relative in sentinels)
        for relative in allowed:
            assert (output / relative).read_bytes() == (root / relative).read_bytes()
        print(json.dumps({
            "issue_key": "OPE-122", "kind": "local_integration",
            "fixture_id": "docker-context-synthetic-sentinels", "fixture_version": "1",
            "status": "PASS", "sentinels_rejected": len(sentinels),
            "allowed_files": actual,
            "dockerignore_sha256": hashlib.sha256(
                (root / ".dockerignore").read_bytes()
            ).hexdigest(),
            "product_acceptance": "NOT_RUN",
        }, sort_keys=True))
