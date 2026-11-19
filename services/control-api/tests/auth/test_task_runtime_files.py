"""Generated task-volume fixtures for the runtime file initializer."""

from __future__ import annotations

import base64
import hashlib
import os
import stat
from pathlib import Path
from unittest.mock import Mock

import pytest
from opentour_control.auth import task_runtime_files as runtime_files

PREFIX = "OPENTOUR_RUNTIME_FILES_"
type FileSet = tuple[tuple[str, str, bytes], ...]
BFF = (("BFF_BINDING", "binding.json", b'{"fixture":"bff"}\n'),)
LISTENER = (
    ("STAGE_BINDING", "binding.json", b'{"fixture":"stage"}\n'),
    ("STAGE_READBACK", "readback.json", b'{"fixture":"readback"}\n'),
    ("STAGE_LEAF", "stage.pem", b"generated-stage-leaf\n"),
    ("SERVER_CERT", "server.pem", b"generated-server-certificate\n"),
    ("SERVER_KEY", "server.key", b"generated-server-key\n"),
)


def _payload_env(field: str, content: bytes) -> dict[str, str]:
    return {
        f"{PREFIX}{field}_B64": base64.b64encode(content).decode("ascii"),
        f"{PREFIX}{field}_SHA256": hashlib.sha256(content).hexdigest(),
    }


def _configure(
    monkeypatch: pytest.MonkeyPatch, profile: str = "bff", files: FileSet = BFF
) -> None:
    monkeypatch.setenv(PREFIX + "PROFILE", profile)
    for field, _path, data in files:
        for key, value in _payload_env(field, data).items():
            monkeypatch.setenv(key, value)


def _refused(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as caught:
        runtime_files.main()
    assert caught.value.code == "Task runtime files are unavailable"
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__
    assert capsys.readouterr() == ("", "")


@pytest.fixture(autouse=True)
def volume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in os.environ.copy():
        if name.startswith(PREFIX):
            monkeypatch.delenv(name)
    mount = tmp_path.resolve()
    mount.chmod(0o700)
    monkeypatch.setattr(runtime_files, "_MOUNT", mount)
    monkeypatch.setattr(runtime_files, "_RUN_UID", os.geteuid())
    return mount


@pytest.mark.parametrize(("profile", "files"), [("bff", BFF), ("private-listener", LISTENER)])
def test_publishes_only_exact_private_files(
    profile: str, files: FileSet, volume: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(monkeypatch, profile, files)
    monkeypatch.setenv("OPENTOUR_UNRELATED_FIXTURE", "ignored")

    runtime_files.main()

    current = volume / "current"
    assert set(volume.iterdir()) == {current}
    info = current.stat()
    assert info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o700
    assert set(current.iterdir()) == {current / path for _field, path, _data in files}
    for _field, name, data in files:
        path = current / name
        info = path.lstat()
        assert stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o400
        assert info.st_uid == os.geteuid() and info.st_nlink == 1
        assert path.read_bytes() == data


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("PROFILE", None),
        ("PROFILE", "wrong-profile"),
        ("BFF_BINDING_B64", None),
        ("BFF_BINDING_B64", "!private-generated-detail!"),
        ("BFF_BINDING_SHA256", None),
        ("BFF_BINDING_SHA256", "f" * 63),
        ("BFF_BINDING_SHA256", hashlib.sha256(b"another fixture").hexdigest()),
        ("STAGE_READBACK_B64", "unexpected fixture"),
    ],
)
def test_refuses_invalid_configuration_before_writer_runs(
    key: str,
    value: str | None,
    volume: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure(monkeypatch)
    if value is None:
        monkeypatch.delenv(PREFIX + key)
    else:
        monkeypatch.setenv(PREFIX + key, value)
    write = Mock(side_effect=AssertionError("invalid configuration reached the writer"))
    monkeypatch.setattr(runtime_files, "_write_files", write)

    _refused(capsys)

    write.assert_not_called()
    assert not list(volume.iterdir())


def test_refuses_oversize_bff_and_incomplete_listener_without_partial_publication(
    volume: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with monkeypatch.context() as bff:
        _configure(bff, files=(("BFF_BINDING", "binding.json", b"x" * 16385),))
        _refused(capsys)
        assert not list(volume.iterdir())
    with monkeypatch.context() as listener:
        _configure(listener, "private-listener", LISTENER[:-1])
        _refused(capsys)
        assert not list(volume.iterdir())


@pytest.mark.parametrize("admission", ["uid", "mount-mode"])
def test_requires_runtime_owner_and_private_mount_mode(
    admission: str,
    volume: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure(monkeypatch)
    if admission == "uid":
        monkeypatch.setattr(runtime_files, "_RUN_UID", os.geteuid() + 1)
    else:
        volume.chmod(0o755)

    _refused(capsys)
    assert not list(volume.iterdir())


def test_cleans_a_partial_write_and_accepts_a_fresh_retry(
    volume: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _configure(monkeypatch, "private-listener", LISTENER)
    sync = os.fsync
    calls = 0

    def interrupted(fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("generated private write failure detail")
        sync(fd)

    with monkeypatch.context() as failure:
        failure.setattr(runtime_files.os, "fsync", interrupted)
        _refused(capsys)
    assert calls == 2
    assert not list(volume.iterdir())

    runtime_files.main()
    assert set((volume / "current").iterdir()) == {
        volume / "current" / name for _field, name, _data in LISTENER
    }


def test_exact_replay_keeps_inode_and_rejects_replacement_bytes(
    volume: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _configure(monkeypatch)
    runtime_files.main()
    path = volume / "current" / "binding.json"
    original = path.stat()

    runtime_files.main()
    assert path.stat().st_ino == original.st_ino
    assert path.stat().st_mtime_ns == original.st_mtime_ns

    _configure(monkeypatch, files=(("BFF_BINDING", "binding.json", b'{"fixture":"new"}\n'),))
    _refused(capsys)
    assert path.read_bytes() == BFF[0][2]
    assert path.stat().st_ino == original.st_ino


@pytest.mark.parametrize("dirty_name", ["current", ".staged-leftover", "unrelated"])
def test_never_overwrites_dirty_mount_entries(
    dirty_name: str,
    volume: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure(monkeypatch)
    dirty = volume / dirty_name
    dirty.write_bytes(b"existing fixture")
    original = dirty.stat().st_ino

    _refused(capsys)
    assert set(volume.iterdir()) == {dirty}
    assert dirty.read_bytes() == b"existing fixture"
    assert dirty.stat().st_ino == original


@pytest.mark.parametrize("damage", ["extra-file", "missing-file", "directory-mode", "file-mode"])
def test_existing_current_must_keep_its_exact_private_shape(
    damage: str, volume: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _configure(monkeypatch)
    runtime_files.main()
    current = volume / "current"
    path = current / "binding.json"
    if damage == "extra-file":
        (current / "extra.json").write_bytes(b"extra")
    elif damage == "missing-file":
        path.unlink()
    elif damage == "directory-mode":
        current.chmod(0o755)
    else:
        path.chmod(0o600)
    before = {item.name: item.stat() for item in current.iterdir()}
    mode = current.stat().st_mode

    _refused(capsys)
    assert current.stat().st_mode == mode
    assert {item.name: item.stat() for item in current.iterdir()} == before


@pytest.mark.parametrize("position", ["mount", "current", "file", "hard-link"])
def test_refuses_links_even_when_the_target_bytes_match(
    position: str, volume: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _configure(monkeypatch)
    outside = volume.with_name(volume.name + "-outside")
    outside.mkdir(mode=0o700)
    target = outside / "binding.json"
    if position != "mount":
        target.write_bytes(BFF[0][2])
        target.chmod(0o400)
    current = volume / "current"
    if position == "mount":
        link = volume / "mount-link"
        link.symlink_to(outside, target_is_directory=True)
        monkeypatch.setattr(runtime_files, "_MOUNT", link)
    elif position == "current":
        link = current
        link.symlink_to(outside, target_is_directory=True)
    else:
        current.mkdir(mode=0o700)
        link = current / "binding.json"
        if position == "file":
            link.symlink_to(target)
        else:
            link.hardlink_to(target)

    inode = link.lstat().st_ino
    _refused(capsys)
    assert link.lstat().st_ino == inode
    if position == "mount":
        assert not list(outside.iterdir())
    else:
        assert target.read_bytes() == BFF[0][2]
