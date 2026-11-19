"""Materialize one digest-pinned runtime file set on a private task volume."""

from __future__ import annotations

import base64
import fcntl
import hashlib
import os
import re
import secrets
import stat
import sys
from collections.abc import Mapping
from pathlib import Path

_PREFIX = "OPENTOUR_RUNTIME_FILES_"
_MOUNT = Path("/run/opentour/runtime")
_RUN_UID = 10001
_PROFILES = {
    "bff": (("BFF_BINDING", "binding.json", 16384),),
    "private-listener": (
        ("STAGE_BINDING", "binding.json", 8192),
        ("STAGE_READBACK", "readback.json", 8192),
        ("STAGE_LEAF", "stage.pem", 65536),
        ("SERVER_CERT", "server.pem", 65536),
        ("SERVER_KEY", "server.key", 65536),
        ("RDS_CA", "rds-ca.pem", 65536),
    ),
}
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_UNAVAILABLE = "Task runtime files are unavailable"


def _payloads(env: Mapping[str, str]) -> dict[str, bytes]:
    profile_name = _PREFIX + "PROFILE"
    profile = _PROFILES.get(env.get(profile_name, ""))
    if profile is None:
        raise ValueError(_UNAVAILABLE)
    expected = {profile_name}
    files = {}
    for field, name, max_bytes in profile:
        input_name = _PREFIX + field + "_B64"
        pin_name = _PREFIX + field + "_SHA256"
        expected.update((input_name, pin_name))
        encoded, pin = env.get(input_name, ""), env.get(pin_name, "")
        if (
            not 0 < len(encoded) <= 4 * ((max_bytes + 2) // 3)
            or not re.fullmatch("[a-f0-9]{64}", pin)
        ):
            raise ValueError(_UNAVAILABLE)
        payload = base64.b64decode(encoded, validate=True)
        if not 0 < len(payload) <= max_bytes or hashlib.sha256(payload).hexdigest() != pin:
            raise ValueError(_UNAVAILABLE)
        files[name] = payload
    if any(key.startswith(_PREFIX) and key not in expected for key in env):
        raise ValueError(_UNAVAILABLE)
    return files


def _owner_directory(fd: int) -> None:
    info = os.fstat(fd)
    if info.st_uid != _RUN_UID or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError(_UNAVAILABLE)


def _verify_current(root: int, files: Mapping[str, bytes]) -> None:
    directory = os.open("current", _DIRECTORY_FLAGS, dir_fd=root)
    try:
        _owner_directory(directory)
        if set(os.listdir(directory)) != set(files):
            raise ValueError(_UNAVAILABLE)
        for name, content in files.items():
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            fd = os.open(name, flags, dir_fd=directory)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != _RUN_UID
                    or stat.S_IMODE(info.st_mode) != 0o400
                    or info.st_nlink != 1
                    or info.st_size != len(content)
                    or stream.read(len(content) + 1) != content
                ):
                    raise ValueError(_UNAVAILABLE)
    finally:
        os.close(directory)


def _publish(root: int, files: Mapping[str, bytes]) -> None:
    staging = ".staged-" + secrets.token_hex(16)
    os.mkdir(staging, 0o700, dir_fd=root)
    directory: int | None = None
    published = False
    try:
        directory = os.open(staging, _DIRECTORY_FLAGS, dir_fd=root)
        os.fchmod(directory, 0o700)
        for name, content in files.items():
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            fd = os.open(name, flags, 0o400, dir_fd=directory)
            with os.fdopen(fd, "wb") as stream:
                os.fchmod(stream.fileno(), 0o400)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        os.fsync(directory)
        os.rename(staging, "current", src_dir_fd=root, dst_dir_fd=root)
        published = True
        os.fsync(root)
    finally:
        try:
            if not published:
                if directory is not None:
                    for name in files:
                        try:
                            os.unlink(name, dir_fd=directory)
                        except FileNotFoundError:
                            pass
                os.rmdir(staging, dir_fd=root)
        finally:
            if directory is not None:
                os.close(directory)


def _write_files(files: Mapping[str, bytes]) -> None:
    if os.geteuid() != _RUN_UID or _MOUNT.resolve(strict=True) != _MOUNT:
        raise ValueError(_UNAVAILABLE)
    root = os.open(_MOUNT, _DIRECTORY_FLAGS)
    try:
        _owner_directory(root)
        # Exactly one initializer publishes a complete directory at a time.
        fcntl.flock(root, fcntl.LOCK_EX | fcntl.LOCK_NB)
        existing = os.listdir(root)
        if existing == ["current"]:
            _verify_current(root, files)
        elif existing:
            raise ValueError(_UNAVAILABLE)
        else:
            _publish(root, files)
    finally:
        os.close(root)


def main() -> None:
    try:
        _write_files(_payloads(os.environ))
    except (OSError, RuntimeError, ValueError):
        raise SystemExit(_UNAVAILABLE) from None


if __name__ == "__main__":
    if len(sys.argv) != 1:
        raise SystemExit(_UNAVAILABLE)
    main()
