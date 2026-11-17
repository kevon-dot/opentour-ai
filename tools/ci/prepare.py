"""Install only hash-pinned public CI prerequisites into this checkout's cache.

The initial interpreter may be the runner's system Python. Actual checks always
use the manifest-pinned interpreter. No cloud resource or package service starts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import socket
import subprocess
import tarfile
import tempfile
import urllib.error
import urllib.request
from http.client import HTTPMessage, HTTPResponse, IncompleteRead
from pathlib import Path
from typing import IO, cast

ROOT = Path(__file__).resolve().parents[2]
MAX_DOWNLOAD = 134_217_728
DOWNLOAD_ATTEMPTS = 3
RETRYABLE_HTTP_STATUS = frozenset({408, 429, 500, 502, 503, 504})


def _retryable_network_error(error: Exception) -> bool:
    if isinstance(error, urllib.error.HTTPError):
        error.close()
        return error.code in RETRYABLE_HTTP_STATUS
    reason = error.reason if isinstance(error, urllib.error.URLError) else error
    return isinstance(
        reason, (TimeoutError, ConnectionResetError, ConnectionAbortedError, IncompleteRead)
    ) or (isinstance(reason, socket.gaierror) and reason.errno == socket.EAI_AGAIN)


class _HttpsRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        if not newurl.startswith("https://"):
            fp.close()
            raise ValueError("Insecure prerequisite redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open_artifact(url: str, *, timeout: int) -> HTTPResponse:
    return cast(
        HTTPResponse, urllib.request.build_opener(_HttpsRedirects).open(url, timeout=timeout)
    )


def _download_attempt(url: str, expected: str, staged: Path) -> None:
    size = 0
    checksum = hashlib.sha256()
    with _open_artifact(url, timeout=60) as response, staged.open("xb") as file:
        if not response.geturl().startswith("https://"):
            raise ValueError("Insecure prerequisite redirect")
        expected_size = response.length
        while True:
            chunk = response.read(65_536)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_DOWNLOAD:
                raise ValueError("Prerequisite exceeds download bound")
            checksum.update(chunk)
            file.write(chunk)
    if expected_size is not None and size < expected_size:
        # HTTPResponse.read(size) can return EOF silently on a short fixed-length body.
        raise IncompleteRead(b"", expected_size - size)
    if checksum.hexdigest() != expected:
        raise ValueError("Prerequisite digest mismatch")


def download(url: str, expected: str, destination: Path) -> None:
    if not url.startswith("https://") or len(expected) != 64:
        raise ValueError("Unpinned prerequisite")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if os.path.lexists(destination):
        raise FileExistsError("Prerequisite destination already exists")
    for attempt in range(DOWNLOAD_ATTEMPTS):
        with tempfile.TemporaryDirectory(
            prefix="artifact-attempt-", dir=destination.parent
        ) as directory:
            staged = Path(directory) / "artifact"
            try:
                _download_attempt(url, expected, staged)
            except (OSError, IncompleteRead) as error:
                if not _retryable_network_error(error) or attempt + 1 == DOWNLOAD_ATTEMPTS:
                    raise
                continue
            # The staging directory is on the destination filesystem. Only the
            # verified file becomes visible and a concurrent writer is preserved.
            os.link(staged, destination)
            return


def extract(archive: Path, destination: Path) -> None:
    if not hasattr(tarfile, "data_filter"):
        raise ValueError("System Python lacks safe archive extraction")
    destination.mkdir(parents=True, exist_ok=False)
    with tarfile.open(archive) as bundle:
        members = bundle.getmembers()
        if len(members) > 50_000 or sum(m.size for m in members) > 1_073_741_824:
            raise ValueError("Archive exceeds extraction bound")
        bundle.extractall(destination, members=members, filter="data")


def prepare(root: Path) -> list[str]:
    manifest = json.loads((root / "tools/toolchain.json").read_text())
    target = platform.system() + "-" + platform.machine()
    if target not in manifest["supported_platforms"]:
        raise ValueError("Unsupported CI platform")
    base = root / ".toolchain/ci"
    base.mkdir(parents=True, exist_ok=True)
    paths: list[str] = []
    names = {"python": "python3.12", "node": "node", "uv": "uv", "pnpm": "pnpm", "just": "just"}
    for tool, name in names.items():
        artifact = manifest["tools"][tool]["artifacts"][target]
        with tempfile.TemporaryDirectory(prefix="download-", dir=base) as temporary:
            archive = Path(temporary) / "archive.tar"
            download(artifact["url"], artifact["sha256"], archive)
            directory = base / (tool + "-" + artifact["sha256"][:12])
            extract(archive, directory)
        candidates = [p for p in directory.rglob(name) if p.is_file()]
        if len(candidates) != 1:
            raise ValueError("Prerequisite executable is ambiguous")
        paths.append(str(candidates[0].parent))
    return paths


def postgres(root: Path) -> str:
    item = json.loads((root / ".github/ci-policy.json").read_text())["postgres"]
    base = root / ".toolchain/ci"
    base.mkdir(parents=True, exist_ok=True)
    prefix = base / ("postgres-" + item["version"])
    with tempfile.TemporaryDirectory(prefix="postgres-source-", dir=base) as temporary:
        archive = Path(temporary) / "postgres.tar.bz2"
        download(item["url"], item["sha256"], archive)
        source = Path(temporary) / "source"
        extract(archive, source)
        folders = list(source.iterdir())
        if len(folders) != 1 or not folders[0].is_dir():
            raise ValueError("Unexpected PostgreSQL source archive")
        commands = [
            [
                "./configure",
                "--prefix=" + str(prefix),
                "--without-readline",
                "--without-zlib",
                "--without-icu",
            ],
            ["make", "-j2"],
            ["make", "install"],
        ]
        for command in commands:
            # Public build output is withheld. A failure emits only a safe category.
            result = subprocess.run(command, cwd=folders[0], capture_output=True, timeout=600)
            if result.returncode:
                raise ValueError("Pinned PostgreSQL compilation failed")
    return str(prefix / "bin")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--postgres", action="store_true")
    args = parser.parse_args()
    try:
        paths = [postgres(ROOT)] if args.postgres else prepare(ROOT)
        destination = os.environ.get("GITHUB_PATH")
        if destination:
            with Path(destination).open("a") as file:
                file.write("\n".join(paths) + "\n")
        print(
            json.dumps(
                {
                    "status": "PASS",
                    "prerequisites": "postgres" if args.postgres else "toolchain",
                    "platform": platform.system() + "-" + platform.machine(),
                }
            )
        )
        return 0
    except (OSError, ValueError, KeyError, subprocess.SubprocessError, tarfile.TarError):
        print(json.dumps({"status": "BLOCKED", "reason": "CI_PREREQUISITE_UNAVAILABLE"}))
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
