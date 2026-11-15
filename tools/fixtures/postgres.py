"""One disposable socket-only PostgreSQL fixture; never uses an existing service."""

from __future__ import annotations

import hashlib
import os
import selectors
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, BinaryIO

VERSION = "18.4"
BINARY_NAMES = ("postgres", "initdb", "psql")


class PrerequisiteMissing(RuntimeError):
    pass


class PostgresFixtureFailure(RuntimeError):
    pass


class DisposablePostgres:
    """Bounded actual PostgreSQL, separate setup owner and runtime connections.

    Caller cannot provide a DB URL, socket, port or data directory. All names and
    SQL are registered generated fixture material. This is not application pooling.
    """

    def __init__(self) -> None:
        self.root: Path | None = None
        self.socket: Path | None = None
        self.process: subprocess.Popen[bytes] | None = None
        self.log: BinaryIO | None = None
        self.binaries: dict[str, str] = {}
        self.cleanup_complete = False
        self.receipt: dict[str, Any] = {
            "kind": "local_integration",
            "fixture_id": "disposable-postgresql-runtime-role",
            "fixture_version": "1",
            "product_acceptance": "NOT_RUN",
            "postgres_version": VERSION,
        }

    def _environment(self) -> dict[str, str]:
        # No HOME/.pgpass, inherited PG* service config, provider credentials or loader injection.
        return {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C", "TZ": "UTC"}

    def _command(self, argv: list[str], *, sql: str | None = None, timeout: int = 15) -> str:
        if sql is not None and len(sql.encode()) > 16_384:
            raise PostgresFixtureFailure("Registered SQL script exceeds fixture input bound")
        process: subprocess.Popen[bytes] | None = None
        try:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE if sql is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self._environment(),
                cwd=self.root,
            )
            if process.stdin is not None:
                process.stdin.write((sql or "").encode())
                process.stdin.close()
            assert process.stdout is not None and process.stderr is not None
            output = bytearray()
            observed_bytes = 0
            deadline = time.monotonic() + timeout
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ, True)
                selector.register(process.stderr, selectors.EVENT_READ, False)
                while selector.get_map():
                    if time.monotonic() >= deadline:
                        raise subprocess.TimeoutExpired(argv[0], timeout)
                    for key, _ in selector.select(timeout=0.05):
                        chunk = os.read(key.fd, 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        observed_bytes += len(chunk)
                        if observed_bytes > 1_048_576:
                            raise PostgresFixtureFailure("PostgreSQL output limit exceeded")
                        if key.data:
                            output.extend(chunk)
            process.wait(timeout=max(0.05, deadline - time.monotonic()))
        except (OSError, subprocess.TimeoutExpired, PostgresFixtureFailure) as exc:
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=3)
                self.receipt.update(helper_aborted=True, owned_helper_pid=process.pid)
            raise PostgresFixtureFailure("Bounded PostgreSQL fixture command failed") from exc
        finally:
            if process is not None:
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()
        if process.returncode:
            # No arbitrary SQL/provider stderr is copied to an evidence record.
            raise PostgresFixtureFailure("PostgreSQL fixture returned a nonzero command status")
        return output.decode("utf-8").strip()

    def qualify_binaries(self) -> None:
        for name in BINARY_NAMES:
            executable = shutil.which(name)
            if executable is None:
                # Explicit installed host profile; never start a package service or download.
                candidate = Path("/opt/homebrew/bin") / name
                executable = str(candidate) if candidate.is_file() else None
            if executable is None:
                raise PrerequisiteMissing("Pinned PostgreSQL fixture tools are unavailable")
            path = Path(executable).resolve(strict=True)
            version = self._command([str(path), "--version"])
            if version not in {
                f"{name} (PostgreSQL) {VERSION}",
                f"{name} (PostgreSQL) {VERSION} (Homebrew)",
            }:
                raise PrerequisiteMissing("PostgreSQL fixture requires exact version " + VERSION)
            self.binaries[name] = str(path)
        self.receipt["binary_sha256"] = {
            name: hashlib.sha256(Path(path).read_bytes()).hexdigest()
            for name, path in self.binaries.items()
        }

    def __enter__(self) -> DisposablePostgres:
        self.qualify_binaries()
        # Short private paths also satisfy PostgreSQL's Unix socket path bound on macOS.
        self.root = Path(tempfile.mkdtemp(prefix="opentour-c03-", dir="/tmp")).resolve()
        self.root.chmod(0o700)
        (self.root / ".owned-c03-fixture").write_text(self.root.name)
        self.socket = self.root / "socket"
        self.socket.mkdir(mode=0o700)
        data = self.root / "data"
        try:
            self._command(
                [
                    self.binaries["initdb"],
                    "-D",
                    str(data),
                    "-U",
                    "c03_owner",
                    "--no-locale",
                    "--encoding=UTF8",
                    "--auth-local=trust",
                    "--auth-host=reject",
                ]
            )
            configuration = [
                "listen_addresses=",
                f"unix_socket_directories={self.socket}",
                "unix_socket_permissions=0700",
                "max_connections=8",
                "shared_buffers=16MB",
                "work_mem=1MB",
                "maintenance_work_mem=16MB",
                "statement_timeout=3000",
                "lock_timeout=2000",
                "idle_in_transaction_session_timeout=5000",
                "timezone=UTC",
                "log_statement=none",
                "logging_collector=off",
            ]
            argv = [self.binaries["postgres"], "-D", str(data)]
            for setting in configuration:
                argv.extend(["-c", setting])
            self.receipt["configuration_sha256"] = hashlib.sha256(
                "\n".join(
                    s for s in configuration if not s.startswith("unix_socket_directories")
                ).encode()
            ).hexdigest()
            self.log = (self.root / "postgres.log").open("wb")
            self.process = subprocess.Popen(
                argv,
                cwd=self.root,
                env=self._environment(),
                stdin=subprocess.DEVNULL,
                stdout=self.log,
                stderr=subprocess.STDOUT,
            )
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise PostgresFixtureFailure("Disposable PostgreSQL exited during startup")
                try:
                    if self.sql("SELECT 1", role="c03_owner") == "1":
                        self.receipt["startup"] = "PASS"
                        return self
                except PostgresFixtureFailure:
                    time.sleep(0.05)
            raise PostgresFixtureFailure("Disposable PostgreSQL startup deadline exceeded")
        except BaseException:
            self.close()
            raise

    def sql(self, script: str, *, role: str = "c03_runtime") -> str:
        if self.socket is None or role not in {"c03_owner", "c03_runtime"}:
            raise PostgresFixtureFailure("Fixture database connection is not available")
        return self._command(
            [
                self.binaries["psql"],
                "-X",
                "-w",
                "-h",
                str(self.socket),
                "-p",
                "5432",
                "-U",
                role,
                "-d",
                "postgres",
                "-v",
                "ON_ERROR_STOP=1",
                "-A",
                "-t",
                "-q",
            ],
            sql=script,
        )

    def close(self) -> None:
        if self.process is not None and self.process.poll() is None:
            # This foreground Popen is the exact process created here, never a discovered service.
            self.process.terminate()
            try:
                self.process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
                self.receipt["forced_stop"] = True
        if self.log is not None:
            self.log.close()
        # A normal smart shutdown waits for backends. A killed/crashed postmaster
        # does not establish that all owned backends have exited; preserve recovery
        # material instead of deleting a possibly live cluster underneath them.
        if self.receipt.get("helper_aborted") or (
            self.process is not None
            and (self.process.returncode != 0 or self.receipt.get("forced_stop"))
        ):
            self.receipt.update(
                cleanup_complete=False,
                recovery_required=True,
                owned_postmaster_pid=self.process.pid if self.process else None,
                retained_private_directory=str(self.root),
            )
            raise PostgresFixtureFailure("Abnormal PostgreSQL termination; owned cluster retained")
        if self.root is not None:
            marker = self.root / ".owned-c03-fixture"
            if (
                self.root.is_symlink()
                or not marker.is_file()
                or marker.read_text() != self.root.name
            ):
                raise PostgresFixtureFailure("Fixture ownership marker changed; cleanup refused")
            try:
                shutil.rmtree(self.root)
            except OSError as exc:
                self.receipt["cleanup_complete"] = False
                raise PostgresFixtureFailure("Owned PostgreSQL temporary cleanup failed") from exc
        self.cleanup_complete = True
        self.receipt["cleanup_complete"] = self.cleanup_complete

    def __exit__(self, *args: object) -> None:
        self.close()
