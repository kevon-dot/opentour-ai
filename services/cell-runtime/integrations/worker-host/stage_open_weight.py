"""Stage and verify the pinned open-weight checkpoint and numeric runtime offline.

Run as root with the release interpreter: ``python3 -I -S -B stage_open_weight.py``.
The standard library is the only dependency. Staging, the previous base and any held base
live in a root-only work directory outside the worker volume, on the same filesystem. The
worker volume is reached only through directory descriptors opened without following links.
Any missing or mismatched file holds the model route by leaving no ``model-base`` in the
volume. Rules-only workflows do not read ``model-base`` and keep running.
"""

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
from pathlib import Path

SCHEMA = "opentour.open-weight-staging.v1"
PROFILE = "smollm2_135m_lora_v1"
REVISION = "12fd25f77366fa6b3b4b768ec3050bf629380bac"
PROFILE_PATH = "training/profiles/" + PROFILE + ".json"
LOCK_PATH = "training/profiles/" + PROFILE + ".requirements.txt"
MODEL_NAME = "model-base"
DIRECTORY_MODE = 0o555
FILE_MODE = 0o444
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_HEX = re.compile(r"[0-9a-f]{64}")
_LOCK_LINE = re.compile(
    r"([A-Za-z0-9][A-Za-z0-9._-]*)==([0-9][A-Za-z0-9.]*) --hash=sha256:([0-9a-f]{64})"
)
# Runs in the numeric interpreter. It reports versions only and reads no model or data.
_PROBE = (
    "import json, platform, numpy, tokenizers; print(json.dumps({"
    "'python': platform.python_version(), 'system': platform.system(), "
    "'machine': platform.machine(), 'numpy': numpy.__version__, "
    "'tokenizers': tokenizers.__version__}))"
)


class Held(ValueError):
    """The model route stays held for this reason."""


class Unverified(ValueError):
    """The script could not act safely; the volume was left unchanged."""


def _sha256_file(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as file:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise Held("regular_file_required")
        return hashlib.file_digest(file, "sha256").hexdigest()


def _release_document(release, name):
    """Read a release file only if it matches the release's verified SHA256SUMS entry."""
    sums = {}
    try:
        for line in (release / "SHA256SUMS").read_text().splitlines():
            digest, sep, entry = line.partition("  ")
            if sep and _HEX.fullmatch(digest):
                sums[entry] = digest
        document = (release / name).read_bytes()
    except OSError as error:
        raise Held("verified_release_required") from error
    if (release / name).is_symlink() or sums.get(name) != hashlib.sha256(document).hexdigest():
        raise Held("release_profile_unverified")
    return document


def check_profile(profile_bytes, lock_bytes):
    """Return the profile and the hash lock after checking that they agree on the runtime."""
    try:
        profile = json.loads(profile_bytes)
        runtime = profile["runtime"]
        files = profile["files"]
        if profile["profile"] != PROFILE:
            raise Held("unsupported_profile")
        if profile["revision"] != REVISION:
            raise Held("checkpoint_revision_mismatch")
        if (
            not isinstance(files, dict)
            or not files
            or any(
                not isinstance(name, str)
                or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name)
                or name.startswith(".")
                or not isinstance(digest, str)
                or not _HEX.fullmatch(digest)
                for name, digest in files.items()
            )
        ):
            raise Held("invalid_profile_files")
        if not isinstance(runtime["python"], str) or len(runtime["platform"].split()) != 2:
            raise Held("invalid_profile")
        expected_lock = {
            "numpy": (runtime["numpy"], runtime["numpy_wheel_sha256"]),
            "tokenizers": (runtime["tokenizers"], runtime["tokenizers_wheel_sha256"]),
        }
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        if isinstance(error, Held):
            raise
        raise Held("invalid_profile") from error
    lock = {}
    for line in lock_bytes.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = _LOCK_LINE.fullmatch(line)
        if match is None or match[1].lower() in lock:
            raise Held("invalid_runtime_lock")
        lock[match[1].lower()] = (match[2], match[3])
    if lock != expected_lock:
        raise Held("runtime_lock_mismatch")
    return profile, lock


def verify_wheels(wheel_dir, lock):
    """Exactly one hash-matching wheel per locked requirement, and nothing else."""
    try:
        entries = sorted(wheel_dir.iterdir())
    except OSError as error:
        raise Held("locked_wheels_required") from error
    wheels = [path for path in entries if path.name.endswith(".whl")]
    for name, (version, digest) in sorted(lock.items()):
        prefix = name.replace("-", "_") + "-" + version + "-"
        matches = [path for path in wheels if path.name.lower().startswith(prefix)]
        if len(matches) != 1:
            raise Held("locked_wheel_required")
        try:
            if _sha256_file(matches[0]) != digest:
                raise Held("wheel_hash_mismatch")
        except OSError as error:
            raise Held("locked_wheel_required") from error
    if len(wheels) != len(lock):
        raise Held("unexpected_wheel")


def verify_runtime(python, profile):
    """Ask the numeric interpreter for its versions with an empty environment."""
    path = Path(python)
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        raise Held("numeric_runtime_required")
    try:
        completed = subprocess.run(
            [str(path), "-I", "-B", "-c", _PROBE],
            capture_output=True,
            env={},
            timeout=120,
            check=False,
        )
        observed = json.loads(completed.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        raise Held("numeric_runtime_required") from error
    runtime = profile["runtime"]
    expected = {
        "python": runtime["python"],
        "system": runtime["platform"].split()[0],
        "machine": runtime["platform"].split()[-1],
        "numpy": runtime["numpy"],
        "tokenizers": runtime["tokenizers"],
    }
    if completed.returncode != 0 or observed != expected:
        raise Held("unqualified_numeric_runtime")


def verify_model_tree(directory_fd, profile, owner_uid):
    """Exactly the pinned files, owned by the installer, read-only, with every digest."""
    info = os.fstat(directory_fd)
    if info.st_uid != owner_uid:
        raise Held("model_base_owner_mismatch")
    if stat.S_IMODE(info.st_mode) != DIRECTORY_MODE:
        raise Held("model_base_mode_invalid")
    names = sorted(os.listdir(directory_fd))
    expected = sorted(profile["files"])
    if not set(expected) <= set(names):
        raise Held("staged_model_file_required")
    if names != expected:
        raise Held("unexpected_model_file")
    for name, digest in profile["files"].items():
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
        except OSError as error:
            raise Held("staged_model_file_required") from error
        with os.fdopen(fd, "rb") as file:
            entry = os.fstat(fd)
            if not stat.S_ISREG(entry.st_mode) or entry.st_nlink != 1:
                raise Held("staged_model_file_required")
            if entry.st_uid != owner_uid:
                raise Held("model_base_owner_mismatch")
            if stat.S_IMODE(entry.st_mode) != FILE_MODE:
                raise Held("model_base_mode_invalid")
            if hashlib.file_digest(file, "sha256").hexdigest() != digest:
                raise Held("staged_model_hash_mismatch")


def _exists_at(directory_fd, name):
    try:
        os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _remove_at(directory_fd, name, depth=0):
    """Remove an entry of a root-only directory without following any link."""
    try:
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(info.st_mode):
        os.unlink(name, dir_fd=directory_fd)
        return
    if depth > 32:
        raise OSError("removal_too_deep")
    fd = os.open(name, _DIR_FLAGS, dir_fd=directory_fd)
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise OSError("removal_target_changed")
        os.fchmod(fd, 0o700)
        for entry in os.listdir(fd):
            _remove_at(fd, entry, depth + 1)
    finally:
        os.close(fd)
    os.rmdir(name, dir_fd=directory_fd)


def _sync(*directory_fds):
    for fd in directory_fds:
        os.fsync(fd)


def open_areas(model_dir, work_dir, owner_uid):
    """Open the worker volume and the root-only work directory as descriptors."""
    volume = model_dir.parent
    if (
        model_dir.name != MODEL_NAME
        or not work_dir.is_absolute()
        or work_dir == volume
        or volume in work_dir.parents
    ):
        raise Unverified("staging_area_invalid")
    try:
        volume_fd = os.open(volume, _DIR_FLAGS)
    except OSError as error:
        raise Unverified("worker_volume_required") from error
    try:
        work_fd = os.open(work_dir, _DIR_FLAGS)
    except OSError as error:
        os.close(volume_fd)
        raise Unverified("staging_area_invalid") from error
    work = os.fstat(work_fd)
    if (
        work.st_uid != owner_uid
        or stat.S_IMODE(work.st_mode) != 0o700
        or work.st_dev != os.fstat(volume_fd).st_dev
    ):
        os.close(volume_fd)
        os.close(work_fd)
        raise Unverified("staging_area_invalid")
    return volume_fd, work_fd


def hold(volume_fd, work_fd):
    """Move whatever is at model-base out of the volume into the root-only held slot."""
    if _exists_at(volume_fd, MODEL_NAME):
        _remove_at(work_fd, "held")
        os.rename(MODEL_NAME, "held", src_dir_fd=volume_fd, dst_dir_fd=work_fd)
        _sync(volume_fd, work_fd)


def _result(route, reason):
    return {
        "schema": SCHEMA,
        "route": route,
        "reason": reason,
        "profile": PROFILE,
        "checkpoint_revision": REVISION,
    }


def _checked_release(release, wheel_dir, python):
    profile, lock = check_profile(
        _release_document(release, PROFILE_PATH), _release_document(release, LOCK_PATH)
    )
    verify_wheels(wheel_dir, lock)
    verify_runtime(python, profile)
    return profile


def _verify_installed(volume_fd, profile, owner_uid, expected_identity=None):
    try:
        model_fd = os.open(MODEL_NAME, _DIR_FLAGS, dir_fd=volume_fd)
    except OSError as error:
        raise Held("staged_model_file_required") from error
    try:
        info = os.fstat(model_fd)
        if expected_identity is not None and (info.st_dev, info.st_ino) != expected_identity:
            raise Held("model_base_replaced")
        verify_model_tree(model_fd, profile, owner_uid)
    finally:
        os.close(model_fd)


def verify(release, model_dir, wheel_dir, python, work_dir, owner_uid=0):
    """Verify an installed base and runtime. Any failure holds the model route."""
    try:
        volume_fd, work_fd = open_areas(model_dir, work_dir, owner_uid)
    except Unverified as error:
        return _result("unverified", str(error))
    try:
        try:
            _verify_installed(volume_fd, _checked_release(release, wheel_dir, python), owner_uid)
        except Held as error:
            hold(volume_fd, work_fd)
            return _result("held", str(error))
        return _result("ready", "verified")
    finally:
        os.close(volume_fd)
        os.close(work_fd)


def _copy_verified(source_dir, work_fd, profile):
    """Copy into a fresh root-only staging directory, hashing the bytes actually written."""
    _remove_at(work_fd, "staging")
    os.mkdir("staging", 0o700, dir_fd=work_fd)
    staging_fd = os.open("staging", _DIR_FLAGS, dir_fd=work_fd)
    try:
        for name, digest in profile["files"].items():
            try:
                reader_fd = os.open(source_dir / name, os.O_RDONLY | os.O_NOFOLLOW)
            except OSError as error:
                raise Held("staged_model_file_required") from error
            with os.fdopen(reader_fd, "rb") as reader:
                if not stat.S_ISREG(os.fstat(reader_fd).st_mode):
                    raise Held("staged_model_file_required")
                writer_fd = os.open(
                    name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=staging_fd,
                )
                with os.fdopen(writer_fd, "wb") as writer:
                    sha = hashlib.sha256()
                    while chunk := reader.read(1 << 20):
                        sha.update(chunk)
                        writer.write(chunk)
                    writer.flush()
                    os.fchmod(writer_fd, FILE_MODE)
                    os.fsync(writer_fd)
            if sha.hexdigest() != digest:
                raise Held("staged_model_hash_mismatch")
        os.fchmod(staging_fd, DIRECTORY_MODE)
        os.fsync(staging_fd)
        info = os.fstat(staging_fd)
        return staging_fd, (info.st_dev, info.st_ino)
    except BaseException:
        os.close(staging_fd)
        raise


def stage(release, source_dir, revision, model_dir, wheel_dir, python, work_dir, owner_uid=0):
    """Copy the pinned files from an offline source, verify them, promote, then re-verify."""
    try:
        volume_fd, work_fd = open_areas(model_dir, work_dir, owner_uid)
    except Unverified as error:
        return _result("unverified", str(error))
    try:
        try:
            if revision != REVISION:
                raise Held("checkpoint_revision_mismatch")
            profile = _checked_release(release, wheel_dir, python)
            staging_fd, identity = _copy_verified(source_dir, work_fd, profile)
            try:
                verify_model_tree(staging_fd, profile, owner_uid)
            finally:
                os.close(staging_fd)
            _remove_at(work_fd, "previous")
            if _exists_at(volume_fd, MODEL_NAME):
                os.rename(MODEL_NAME, "previous", src_dir_fd=volume_fd, dst_dir_fd=work_fd)
            os.rename("staging", MODEL_NAME, src_dir_fd=work_fd, dst_dir_fd=volume_fd)
            _sync(volume_fd, work_fd)
            # The volume is worker-writable: check that the promoted inode is still in place.
            _verify_installed(volume_fd, profile, owner_uid, identity)
        except (Held, OSError) as error:
            _remove_at(work_fd, "staging")
            hold(volume_fd, work_fd)
            return _result("held", str(error) if isinstance(error, Held) else "staging_failed")
        _remove_at(work_fd, "previous")
        _remove_at(work_fd, "held")
        _sync(work_fd)
        return _result("ready", "verified")
    finally:
        os.close(volume_fd)
        os.close(work_fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("verify", "stage"):
        command = commands.add_parser(name)
        command.add_argument("--release", type=Path, required=True)
        command.add_argument("--model-dir", type=Path, required=True)
        command.add_argument("--wheel-dir", type=Path, required=True)
        command.add_argument("--work-dir", type=Path, required=True)
        command.add_argument("--python", required=True)
        if name == "stage":
            command.add_argument("--source", type=Path, required=True)
            command.add_argument("--revision", required=True)
    args = parser.parse_args(argv)
    for path in (args.release, args.model_dir, args.wheel_dir, args.work_dir):
        if not path.is_absolute():
            parser.error("use absolute paths")
    if args.model_dir.name != MODEL_NAME:
        parser.error("the model directory must be the worker volume's model-base")
    if os.geteuid() != 0:
        result = _result("unverified", "installer_root_required")
    elif args.command == "stage":
        result = stage(
            args.release,
            args.source,
            args.revision,
            args.model_dir,
            args.wheel_dir,
            args.python,
            args.work_dir,
        )
    else:
        result = verify(args.release, args.model_dir, args.wheel_dir, args.python, args.work_dir)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["route"] == "ready" else 2


if __name__ == "__main__":
    raise SystemExit(main())
