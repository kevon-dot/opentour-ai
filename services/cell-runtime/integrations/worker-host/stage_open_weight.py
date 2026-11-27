"""Stage and verify the pinned open-weight checkpoint and numeric runtime offline.

Run as the installer with the release interpreter: ``python3 -I -S -B stage_open_weight.py``.
The standard library is the only dependency. Any missing or mismatched file holds the
model route by leaving no ``model-base`` in the worker volume. Rules-only workflows do not
read ``model-base`` and keep running.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

SCHEMA = "opentour.open-weight-staging.v1"
PROFILE = "smollm2_135m_lora_v1"
REVISION = "12fd25f77366fa6b3b4b768ec3050bf629380bac"
PROFILE_PATH = "training/profiles/" + PROFILE + ".json"
LOCK_PATH = "training/profiles/" + PROFILE + ".requirements.txt"
HELD_NAME = "model-base.held"
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
    except (ValueError, KeyError, TypeError) as error:
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
    """Ask the numeric interpreter for its versions without site customisation from the env."""
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


def verify_model_dir(model_dir, profile):
    """The directory holds exactly the pinned files, each a regular file with its digest."""
    if model_dir.is_symlink() or not model_dir.is_dir():
        raise Held("staged_model_file_required")
    names = sorted(entry.name for entry in model_dir.iterdir())
    expected = sorted(profile["files"])
    if not set(expected) <= set(names):
        raise Held("staged_model_file_required")
    if names != expected:
        raise Held("unexpected_model_file")
    for name, digest in profile["files"].items():
        source = model_dir / name
        if source.is_symlink() or not source.is_file():
            raise Held("staged_model_file_required")
        if _sha256_file(source) != digest:
            raise Held("staged_model_hash_mismatch")


def _remove_tree(path):
    if not path.exists() and not path.is_symlink():
        return
    if path.is_symlink() or not path.is_dir():
        path.unlink()
        return
    for directory, _dirs, _files in os.walk(path):
        os.chmod(directory, 0o700)
    shutil.rmtree(path)


def hold(model_dir):
    """Take the model route out of service: the cycle then finds no staged base."""
    if model_dir.exists() or model_dir.is_symlink():
        held = model_dir.with_name(HELD_NAME)
        _remove_tree(held)
        os.rename(model_dir, held)


def _copy_verified(source_dir, staging, profile):
    os.mkdir(staging, 0o700)
    for name, digest in profile["files"].items():
        source = source_dir / name
        if source.is_symlink() or not source.is_file():
            raise Held("staged_model_file_required")
        target = staging / name
        fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as reader, open(target, "xb") as writer:
            sha = hashlib.sha256()
            while chunk := reader.read(1 << 20):
                sha.update(chunk)
                writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        if sha.hexdigest() != digest:
            raise Held("staged_model_hash_mismatch")
        os.chmod(target, 0o444)
    os.chmod(staging, 0o555)


def _result(route, reason, profile_name=PROFILE):
    return {
        "schema": SCHEMA,
        "route": route,
        "reason": reason,
        "profile": profile_name,
        "checkpoint_revision": REVISION,
    }


def verify(release, model_dir, wheel_dir, python):
    """Verify an installed base and runtime. Any failure holds the model route."""
    try:
        profile, lock = check_profile(
            _release_document(release, PROFILE_PATH), _release_document(release, LOCK_PATH)
        )
        verify_wheels(wheel_dir, lock)
        verify_runtime(python, profile)
        verify_model_dir(model_dir, profile)
    except Held as error:
        hold(model_dir)
        return _result("held", str(error))
    return _result("ready", "verified")


def stage(release, source_dir, revision, model_dir, wheel_dir, python):
    """Copy the pinned files from an offline source, verify them, then promote atomically."""
    staging = model_dir.with_name(".model-base.staging")
    previous = model_dir.with_name(".model-base.previous")
    try:
        if revision != REVISION:
            raise Held("checkpoint_revision_mismatch")
        profile, lock = check_profile(
            _release_document(release, PROFILE_PATH), _release_document(release, LOCK_PATH)
        )
        verify_wheels(wheel_dir, lock)
        verify_runtime(python, profile)
        _remove_tree(staging)
        _copy_verified(source_dir, staging, profile)
        verify_model_dir(staging, profile)
    except (Held, OSError) as error:
        _remove_tree(staging)
        hold(model_dir)
        reason = str(error) if isinstance(error, Held) else "staging_copy_failed"
        return _result("held", reason)
    _remove_tree(previous)
    if model_dir.exists() or model_dir.is_symlink():
        os.rename(model_dir, previous)
    os.rename(staging, model_dir)
    _remove_tree(previous)
    _remove_tree(model_dir.with_name(HELD_NAME))
    return _result("ready", "verified")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("verify", "stage"):
        command = commands.add_parser(name)
        command.add_argument("--release", type=Path, required=True)
        command.add_argument("--model-dir", type=Path, required=True)
        command.add_argument("--wheel-dir", type=Path, required=True)
        command.add_argument("--python", required=True)
        if name == "stage":
            command.add_argument("--source", type=Path, required=True)
            command.add_argument("--revision", required=True)
    args = parser.parse_args(argv)
    for path in (args.release, args.model_dir, args.wheel_dir):
        if not path.is_absolute():
            parser.error("use absolute paths")
    if args.model_dir.name != "model-base":
        parser.error("the model directory must be the worker volume's model-base")
    if args.command == "stage":
        result = stage(
            args.release, args.source, args.revision, args.model_dir, args.wheel_dir, args.python
        )
    else:
        result = verify(args.release, args.model_dir, args.wheel_dir, args.python)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["route"] == "ready" else 2


if __name__ == "__main__":
    raise SystemExit(main())
