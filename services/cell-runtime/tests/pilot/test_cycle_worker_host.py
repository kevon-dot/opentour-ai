"""OPE-577 package checks: cycle unit hardening, offline model staging, snapshot boundary.

Every model file, wheel and interpreter here is a generated sentinel. No real weights, numeric
runtime or systemd manager is used, so these tests prove the package only, not an installed host.
"""

import hashlib
import json
import os
import pwd
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
HOST = ROOT / "services/cell-runtime/integrations/worker-host"
PROFILE = ROOT / "training/profiles/smollm2_135m_lora_v1.json"
LOCK = ROOT / "training/profiles/smollm2_135m_lora_v1.requirements.txt"
ISOLATED = [sys.executable, "-I", "-S", "-B"]
VOLUME = "/var/lib/opentour-worker"
ANTI_ROLLBACK = "/var/lib/opentour-antirollback"
REVISION = "12fd25f77366fa6b3b4b768ec3050bf629380bac"
OWNER = os.geteuid()


def load(name):
    namespace = {"__name__": "ope577_" + name.removesuffix(".py")}
    exec(compile((HOST / name).read_text(), str(HOST / name), "exec"), namespace)
    return namespace


stage_module = load("stage_open_weight.py")
boundary_module = load("snapshot_boundary.py")


def unit(name):
    sections, current = {}, None
    for raw in (HOST / name).read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            current = sections.setdefault(line.strip("[]"), {})
            continue
        key, _, value = line.partition("=")
        current.setdefault(key, []).append(value)
    return sections


WORKER_HARDENING = {
    "User": ["opentour-worker"],
    "Group": ["opentour-worker"],
    "UMask": ["0077"],
    "StateDirectory": ["opentour-worker opentour-antirollback"],
    "StateDirectoryMode": ["0700"],
    "ProtectSystem": ["strict"],
    "ProtectHome": ["yes"],
    "PrivateTmp": ["yes"],
    "NoNewPrivileges": ["yes"],
    "ProtectProc": ["invisible"],
    "ProcSubset": ["pid"],
    "CapabilityBoundingSet": [""],
    "StandardOutput": ["null"],
    "InaccessiblePaths": ["-/etc/opentour-health"],
    "ReadOnlyPaths": [f"-{VOLUME}/model-base"],
}
CYCLE_COMMAND = (
    "/opt/opentour/python/bin/python3 -I -B /opt/opentour/current/opentour-slack.pyz "
    f"cycle-worker --directory {VOLUME} --organization ${{ORGANIZATION}} "
    f"--config {VOLUME}/cycle.json --interval 300"
)


@pytest.mark.parametrize("name", ["opentour-cycle.service", "opentour-worker.service"])
def test_worker_units_pin_every_hardening_value(name):
    service = unit(name)["Service"]
    for key, value in WORKER_HARDENING.items():
        assert service.get(key) == value, key
    assert service["Restart"] == ["always"]
    assert unit(name)["Install"] == {"WantedBy": ["multi-user.target"]}


def test_cycle_unit_has_exactly_the_reviewed_settings():
    service = unit("opentour-cycle.service")["Service"]
    assert service == {
        **WORKER_HARDENING,
        "EnvironmentFile": ["/etc/opentour-worker/site.env", "-/etc/opentour-worker/cycle.env"],
        "ExecStart": [CYCLE_COMMAND],
        "Restart": ["always"],
        "RestartSec": ["300"],
        "MemoryMax": ["4G"],
        "TasksMax": ["128"],
    }
    # The model route never gates the unit, so rules-only work starts without a staged base.
    assert not {"ExecStartPre", "ConditionPathExists", "AssertPathExists", "Requires"} & set(
        service
    )
    assert unit("opentour-worker.service")["Service"]["EnvironmentFile"] == [
        "/etc/opentour-worker/site.env"
    ]


def test_health_unit_pins_its_separate_identity_and_denials():
    health = unit("opentour-health.service")["Service"]
    for key, value in {
        "User": ["opentour-health"],
        "Group": ["opentour-health"],
        "UMask": ["0077"],
        "ProtectSystem": ["strict"],
        "ProtectHome": ["yes"],
        "PrivateTmp": ["yes"],
        "NoNewPrivileges": ["yes"],
        "CapabilityBoundingSet": [""],
        "InaccessiblePaths": [f"-{VOLUME}", f"-{ANTI_ROLLBACK}"],
    }.items():
        assert health.get(key) == value, key
    assert "StateDirectory" not in health and "EnvironmentFile" not in health


def test_cycle_unit_command_is_the_packaged_cycle_worker_entry_point(tmp_path):
    builder = (ROOT / "tools/build_slack_pilot_kit.py").read_text()
    assert '"cycle-worker": ("cycle_worker", "main"),' in builder
    argv = CYCLE_COMMAND.split()
    options = dict(zip(argv[5::2], argv[6::2], strict=True))
    # The same options reach the real cycle worker; a missing private config holds safely.
    directory = tmp_path.resolve() / "opentour-worker"
    directory.mkdir(mode=0o700)
    held = subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "opentour_cell.pilot.cycle_worker",
            "--directory",
            str(directory),
            "--organization",
            "generated-org",
            "--config",
            str(directory / "cycle.json"),
            "--interval",
            options["--interval"],
            "--once",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert held.returncode == 2
    assert json.loads(held.stderr) == {
        "state": "held",
        "reason": "check_private_configuration_and_source",
    }


def test_pinned_constants_match_the_repository_profile_and_runtime():
    from opentour_cell.pilot import open_weight

    profile, lock = stage_module["check_profile"](PROFILE.read_bytes(), LOCK.read_bytes())
    assert stage_module["REVISION"] == open_weight.REVISION == profile["revision"] == REVISION
    assert stage_module["PROFILE"] == open_weight.PROFILE
    assert hashlib.sha256(PROFILE.read_bytes()).hexdigest() == open_weight.PROFILE_SHA256
    assert set(lock) == {"numpy", "tokenizers"}
    assert profile["files"]["model.safetensors"] == open_weight.BASE_SHA256


@pytest.fixture
def site(tmp_path):
    """A generated release, offline checkpoint, wheels, interpreter, volume and work dir."""
    real = json.loads(PROFILE.read_text())
    source = tmp_path / "offline-checkpoint"
    source.mkdir()
    files = {}
    for name in real["files"]:
        data = f"generated sentinel {name}\n".encode()
        (source / name).write_bytes(data)
        files[name] = hashlib.sha256(data).hexdigest()
    wheels = tmp_path / "verified-wheels"
    wheels.mkdir()
    runtime = dict(real["runtime"])
    lock_lines = ["# generated sentinel lock"]
    for name, tag in (("numpy", "cp312-cp312"), ("tokenizers", "cp39-abi3")):
        data = f"generated sentinel wheel {name}\n".encode()
        version = runtime[name]
        (wheels / f"{name}-{version}-{tag}-manylinux_2_28_x86_64.whl").write_bytes(data)
        runtime[f"{name}_wheel_sha256"] = hashlib.sha256(data).hexdigest()
        lock_lines.append(f"{name}=={version} --hash=sha256:{runtime[f'{name}_wheel_sha256']}")
    profile = dict(real, files=files, runtime=runtime)
    release = tmp_path / "release"

    def write_release(document):
        (release / "training/profiles").mkdir(parents=True, exist_ok=True)
        payloads = {
            "training/profiles/smollm2_135m_lora_v1.json": json.dumps(document).encode(),
            "training/profiles/smollm2_135m_lora_v1.requirements.txt": (
                "\n".join(lock_lines) + "\n"
            ).encode(),
        }
        for name, payload in payloads.items():
            (release / name).write_bytes(payload)
        (release / "SHA256SUMS").write_text(
            "".join(f"{hashlib.sha256(p).hexdigest()}  {n}\n" for n, p in payloads.items())
        )

    write_release(profile)
    python = tmp_path / "model-python/bin/python"
    python.parent.mkdir(parents=True)

    def write_python(**overrides):
        observed = {
            "python": runtime["python"],
            "system": "Linux",
            "machine": "x86_64",
            "numpy": runtime["numpy"],
            "tokenizers": runtime["tokenizers"],
            **overrides,
        }
        python.write_text(f"#!/bin/sh\nprintf '%s\\n' '{json.dumps(observed)}'\n")
        python.chmod(0o755)

    write_python()
    lib = tmp_path / "var/lib"
    volume = lib / "opentour-worker"
    volume.mkdir(parents=True, mode=0o700)
    (volume / "ledger.sqlite3").write_bytes(b"generated rules-only ledger")
    (volume / "cycle.json").write_text('{"generated": true}')
    return {
        "release": release,
        "source": source,
        "wheels": wheels,
        "python": python,
        "volume": volume,
        "work": volume / ".opentour-staging",
        "model": volume / "model-base",
        "profile": profile,
        "write_release": write_release,
        "write_python": write_python,
    }


def staged(site, revision=REVISION, owner=OWNER):
    return stage_module["stage"](
        site["release"],
        site["source"],
        revision,
        site["model"],
        site["wheels"],
        str(site["python"]),
        owner,
    )


def verified(site, owner=OWNER):
    return stage_module["verify"](
        site["release"], site["model"], site["wheels"], str(site["python"]), owner
    )


def cli_arguments(site):
    return [
        "--release",
        str(site["release"]),
        "--model-dir",
        str(site["model"]),
        "--wheel-dir",
        str(site["wheels"]),
        "--python",
        str(site["python"]),
    ]


def rules_only_state_unchanged(site):
    assert (site["volume"] / "ledger.sqlite3").read_bytes() == b"generated rules-only ledger"
    assert (site["volume"] / "cycle.json").read_text() == '{"generated": true}'


def copies(directory, prefix):
    return sorted(p for p in directory.iterdir() if p.name.startswith(prefix + "-"))


def one_held(site):
    (held,) = copies(site["work"], "held")
    return held


def tamper(path, data=b"tampered"):
    """Change a staged file's bytes while keeping its owner and 0444 mode."""
    path.chmod(0o644)
    path.write_bytes(data)
    path.chmod(0o444)


def test_stage_accepts_a_correct_sentinel_set_and_promotes_read_only(site):
    assert staged(site) == {
        "schema": "opentour.open-weight-staging.v1",
        "route": "ready",
        "reason": "verified",
        "profile": "smollm2_135m_lora_v1",
        "checkpoint_revision": REVISION,
        "model_base": "promoted",
        "cleanup": "complete",
    }
    model = site["model"]
    assert sorted(p.name for p in model.iterdir()) == sorted(site["profile"]["files"])
    assert stat.S_IMODE(model.stat().st_mode) == 0o555 and model.stat().st_uid == OWNER
    assert {stat.S_IMODE(p.stat().st_mode) for p in model.iterdir()} == {0o444}
    assert sorted(p.name for p in site["volume"].iterdir()) == [
        ".opentour-staging",
        "cycle.json",
        "ledger.sqlite3",
        "model-base",
    ]
    work = site["work"].stat()
    assert stat.S_IMODE(work.st_mode) == 0o700 and work.st_uid == OWNER
    # Staging, previous and held copies are cleaned out of the root-only work directory.
    assert list(site["work"].iterdir()) == []
    assert verified(site)["route"] == "ready"
    rules_only_state_unchanged(site)


def test_stage_cli_runs_isolated_as_root(site):
    script = str(HOST / "stage_open_weight.py")
    arguments = cli_arguments(site)
    command = [*ISOLATED, script, "stage", *arguments, "--source", str(site["source"])]
    ready = subprocess.run(
        [*command, "--revision", REVISION],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env={},
    )
    if OWNER != 0:
        assert ready.returncode == 2
        assert json.loads(ready.stdout)["reason"] == "installer_root_required"
        assert not site["model"].exists()
        return
    assert ready.returncode == 0, ready.stderr
    assert json.loads(ready.stdout)["route"] == "ready"
    tamper(site["model"] / "config.json")
    held = subprocess.run(
        [*ISOLATED, script, "verify", *arguments],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env={},
    )
    assert held.returncode == 2
    assert json.loads(held.stdout)["reason"] == "staged_model_hash_mismatch"
    assert not site["model"].exists() and one_held(site).is_dir()


@pytest.mark.parametrize("command", ["stage", "verify"])
def test_cli_refuses_to_act_without_root(site, monkeypatch, capsys, command):
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    extra = ["--source", str(site["source"]), "--revision", REVISION] if command == "stage" else []
    assert stage_module["main"]([command, *cli_arguments(site), *extra]) == 2
    assert json.loads(capsys.readouterr().out)["reason"] == "installer_root_required"
    assert not site["model"].exists() and not site["work"].exists()
    rules_only_state_unchanged(site)


def test_verify_holds_a_restored_base_whose_bytes_changed(site):
    """A restore or tamper that keeps owner and modes is still caught by the digests."""
    assert staged(site)["route"] == "ready"
    tamper(site["model"] / "model.safetensors", b"restored older generated weights")
    assert verified(site) == {
        "schema": "opentour.open-weight-staging.v1",
        "route": "held",
        "reason": "staged_model_hash_mismatch",
        "profile": "smollm2_135m_lora_v1",
        "checkpoint_revision": REVISION,
        "model_base": "moved_to_held",
        "cleanup": "complete",
    }
    assert not site["model"].exists() and one_held(site).is_dir()
    rules_only_state_unchanged(site)


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("declared_revision", "checkpoint_revision_mismatch"),
        ("profile_revision", "checkpoint_revision_mismatch"),
        ("unverified_profile", "release_profile_unverified"),
        ("checkpoint_hash", "staged_model_hash_mismatch"),
        ("missing_checkpoint_file", "staged_model_file_required"),
        ("checkpoint_link", "staged_model_file_required"),
        ("missing_runtime", "numeric_runtime_required"),
        ("wrong_runtime", "unqualified_numeric_runtime"),
        ("wheel_hash", "wheel_hash_mismatch"),
        ("missing_wheel", "locked_wheel_required"),
        ("extra_wheel", "unexpected_wheel"),
    ],
)
def test_stage_holds_the_model_route_on_any_mismatch(site, case, reason):
    assert staged(site)["route"] == "ready"  # an earlier good install must also be held
    revision = REVISION
    if case == "declared_revision":
        revision = "0" * 40
    elif case == "profile_revision":
        site["write_release"](dict(site["profile"], revision="1" * 40))
    elif case == "unverified_profile":
        (site["release"] / "SHA256SUMS").write_text("")
    elif case == "checkpoint_hash":
        (site["source"] / "model.safetensors").write_bytes(b"different generated weights")
    elif case == "missing_checkpoint_file":
        (site["source"] / "tokenizer.json").unlink()
    elif case == "checkpoint_link":
        target = site["source"] / "config.json"
        shutil.move(target, site["source"].parent / "elsewhere.json")
        target.symlink_to(site["source"].parent / "elsewhere.json")
    elif case == "missing_runtime":
        site["python"].unlink()
    elif case == "wrong_runtime":
        site["write_python"](numpy="2.0.0")
    elif case == "wheel_hash":
        next(site["wheels"].glob("numpy-*")).write_bytes(b"different wheel")
    elif case == "missing_wheel":
        next(site["wheels"].glob("tokenizers-*")).unlink()
    elif case == "extra_wheel":
        (site["wheels"] / "torch-2.6.0-cp312-cp312-linux_x86_64.whl").write_bytes(b"x")
    result = staged(site, revision)
    assert (result["route"], result["reason"]) == ("held", reason)
    assert not site["model"].exists()
    assert one_held(site).is_dir()
    assert copies(site["work"], "staging") == [] and copies(site["work"], "previous") == []
    rules_only_state_unchanged(site)


def test_a_base_swapped_in_after_promotion_is_held(site, monkeypatch):
    """A worker-identity process replaces model-base between the rename and re-verification."""
    real_rename = os.rename
    planted = site["volume"] / "planted"

    def racing_rename(src, dst, *, src_dir_fd=None, dst_dir_fd=None):
        real_rename(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)
        if src.startswith("staging-") and dst == "model-base":
            # Same filesystem, owner, modes and digests: only the inode identity can tell.
            shutil.copytree(site["model"], planted, copy_function=shutil.copy2)
            real_rename(site["model"], site["volume"] / "stolen")
            real_rename(planted, site["model"])

    monkeypatch.setattr(os, "rename", racing_rename)
    result = staged(site)
    monkeypatch.undo()
    assert (result["route"], result["reason"]) == ("held", "model_base_replaced")
    assert not site["model"].exists()
    assert one_held(site).is_dir()


@pytest.mark.parametrize("swap", ["renamed_away", "replaced", "replaced_by_link"])
def test_a_work_directory_renamed_or_swapped_mid_stage_holds(site, monkeypatch, swap):
    """The worker owns the volume: it renames the work directory entry while root copies."""
    original = stage_module["_copy_verified"]
    moved = site["volume"] / "moved-staging"

    def racing_copy(source_dir, areas, profile, staging):
        result = original(source_dir, areas, profile, staging)
        os.rename(site["work"], moved)
        if swap == "replaced":
            site["work"].mkdir(mode=0o700)
            site["work"].chmod(0o700)
        elif swap == "replaced_by_link":
            site["work"].symlink_to(moved)
        return result

    monkeypatch.setitem(stage_module, "_copy_verified", racing_copy)
    result = staged(site)
    assert (result["route"], result["reason"]) == ("held", "staging_area_replaced")
    assert not site["model"].exists()
    # The staging copy never left the original root-only directory, wherever its entry went.
    assert stat.S_IMODE(moved.lstat().st_mode) == 0o700
    assert copies(moved, "staging") == []
    rules_only_state_unchanged(site)


def test_staging_works_when_the_volume_reports_its_own_device(site, monkeypatch):
    """Simulate the documented layout: the volume is a separate mount with its own st_dev."""
    volume = str(site["volume"])
    real = {name: getattr(os, name) for name in ("stat", "lstat", "fstat")}
    shifted = []

    def inside(path):
        return path == volume or path.startswith(volume + "/")

    def shift(result, path):
        if not inside(path):
            return result
        shifted.append(path)
        fields = list(result[:10])
        fields[2] += 7919
        return os.stat_result(fields)

    def fake_stat(path, *, dir_fd=None, follow_symlinks=True):
        result = real["stat"](path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)
        base = os.readlink(f"/proc/self/fd/{dir_fd}") if dir_fd is not None else ""
        return shift(result, os.path.join(base, os.fspath(path)))

    def fake_lstat(path, *, dir_fd=None):
        return fake_stat(path, dir_fd=dir_fd, follow_symlinks=False)

    def fake_fstat(fd):
        return shift(real["fstat"](fd), os.readlink(f"/proc/self/fd/{fd}"))

    monkeypatch.setattr(os, "stat", fake_stat)
    monkeypatch.setattr(os, "lstat", fake_lstat)
    monkeypatch.setattr(os, "fstat", fake_fstat)
    assert staged(site)["route"] == "ready"
    assert verified(site)["route"] == "ready"
    monkeypatch.undo()
    assert any(path.endswith("/.opentour-staging") for path in shifted)
    assert real["stat"](site["volume"]).st_dev == real["stat"](site["volume"].parent).st_dev


def test_staging_works_with_the_volume_on_a_separate_mount(site, tmp_path):
    """Root only: mount a private tmpfs at the volume path, as the documented layout does."""
    if OWNER != 0 or shutil.which("unshare") is None:
        pytest.skip("needs root and unshare to mount a private tmpfs volume")
    probe = subprocess.run(
        ["unshare", "-m", "--propagation", "private", "true"], capture_output=True, check=False
    )
    if probe.returncode != 0:
        pytest.skip("this host refuses a private mount namespace")
    volume = site["volume"]
    script = str(HOST / "stage_open_weight.py")
    arguments = " ".join(f"'{a}'" for a in cli_arguments(site))
    isolated = " ".join(ISOLATED)
    shell = (
        f"mount -t tmpfs -o mode=0700 tmpfs '{volume}' && "
        f"stat -c %d '{volume}' '{volume.parent}' && "
        f"{isolated} '{script}' stage {arguments} --source '{site['source']}' "
        f"--revision {REVISION} && "
        f"{isolated} '{script}' verify {arguments} && "
        f"stat -c %d '{volume}/.opentour-staging' && "
        f"stat -c '%a %u' '{volume}/.opentour-staging'"
    )
    completed = subprocess.run(
        ["unshare", "-m", "--propagation", "private", "sh", "-c", shell],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    lines = completed.stdout.splitlines()
    volume_dev, parent_dev = lines[0], lines[1]
    assert volume_dev != parent_dev
    assert json.loads(lines[2])["route"] == "ready"
    assert json.loads(lines[3])["route"] == "ready"
    assert lines[4] == volume_dev and lines[5] == "700 0"
    # The private mount is gone: nothing was staged on the underlying directory.
    assert not site["model"].exists()


def plant_unremovable_held_tree(site, depth=40):
    """A worker-owned held copy, deeper than removal allows, as a kept descriptor can grow."""
    top = site["work"] / "held"  # the fixed name the earlier script removed first
    node = top
    for level in range(depth):
        node = node / f"d{level}"
    node.mkdir(parents=True)
    (node / "leaf").write_text("generated")
    if OWNER == 0:
        for path in [top, *top.rglob("*")]:
            os.chown(path, 54321, -1, follow_symlinks=False)
    return top


@pytest.mark.parametrize("via", ["module", "cli"])
def test_hold_moves_the_base_out_even_when_old_held_copies_cannot_be_removed(site, via):
    assert staged(site)["route"] == "ready"
    grown = plant_unremovable_held_tree(site)
    tamper(site["model"] / "config.json")
    if via == "module":
        result = verified(site)
    elif OWNER == 0:
        completed = subprocess.run(
            [*ISOLATED, str(HOST / "stage_open_weight.py"), "verify", *cli_arguments(site)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            env={},
        )
        assert completed.returncode == 2 and completed.stderr == ""
        result = json.loads(completed.stdout)
    else:
        pytest.skip("the CLI acts only as root; the module case covers non-root runners")
    assert (result["route"], result["reason"]) == ("held", "staged_model_hash_mismatch")
    assert (result["model_base"], result["cleanup"]) == ("moved_to_held", "incomplete")
    assert not site["model"].exists()
    assert grown.is_dir()
    (new_held,) = [p for p in copies(site["work"], "held") if p != grown]
    assert sorted(p.name for p in new_held.iterdir()) == sorted(site["profile"]["files"])
    rules_only_state_unchanged(site)


def test_hold_survives_entries_added_during_cleanup_and_staging_still_promotes(site, monkeypatch):
    assert staged(site)["route"] == "ready"
    stale = site["work"] / "held-stale"
    stale.mkdir()
    real_rmdir = os.rmdir

    def growing_rmdir(path, *, dir_fd=None):
        if path == "held-stale":
            (stale / "added-during-removal").write_text("generated")
        real_rmdir(path, dir_fd=dir_fd)

    monkeypatch.setattr(os, "rmdir", growing_rmdir)
    tamper(site["model"] / "tokenizer.json")
    result = verified(site)
    assert (result["route"], result["model_base"], result["cleanup"]) == (
        "held",
        "moved_to_held",
        "incomplete",
    )
    assert not site["model"].exists() and stale.is_dir()
    # A later promotion is not blocked by an unremovable earlier copy either.
    ready = staged(site)
    assert (ready["route"], ready["model_base"], ready["cleanup"]) == (
        "ready",
        "promoted",
        "incomplete",
    )
    assert site["model"].is_dir()


def test_verify_refuses_a_base_not_owned_by_the_installer_or_with_wrong_modes(site):
    assert staged(site)["route"] == "ready"
    # The worker identity cannot create root-owned files, so any other owner is refused.
    fd = os.open(site["model"], os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(ValueError, match="model_base_owner_mismatch"):
            stage_module["verify_model_tree"](fd, site["profile"], OWNER + 1)
    finally:
        os.close(fd)
    if OWNER == 0:  # end to end: a base handed to another identity is held
        os.chown(site["model"] / "config.json", 54321, -1)
        assert verified(site)["reason"] == "model_base_owner_mismatch"
        assert not site["model"].exists()
        assert staged(site)["route"] == "ready"
        os.chown(site["model"], 54321, -1)
        assert verified(site)["reason"] == "model_base_owner_mismatch"
        assert staged(site)["route"] == "ready"
    os.chmod(site["model"], 0o755)
    assert verified(site)["reason"] == "model_base_mode_invalid"
    assert staged(site)["route"] == "ready"
    os.chmod(site["model"] / "tokenizer.json", 0o644)
    assert verified(site)["reason"] == "model_base_mode_invalid"
    assert staged(site)["route"] == "ready"
    os.chmod(site["model"], 0o755)
    (site["model"] / "notes.txt").write_text("unexpected")
    os.chmod(site["model"], 0o555)
    assert verified(site)["reason"] == "unexpected_model_file"
    assert verified(site)["reason"] == "staged_model_file_required"
    assert staged(site)["route"] == "ready"
    assert copies(site["work"], "held") == []


def test_verify_holds_a_linked_base_without_following_it(site):
    assert staged(site)["route"] == "ready"
    outside = site["volume"].parent / "outside-base"
    os.rename(site["model"], outside)
    site["model"].symlink_to(outside)
    assert verified(site)["reason"] == "staged_model_file_required"
    assert not site["model"].is_symlink() and one_held(site).is_symlink()
    assert (outside / "config.json").is_file()


def test_removal_in_the_work_directory_never_follows_links(site):
    outside = site["volume"].parent / "keep"
    outside.mkdir()
    (outside / "file").write_text("keep")
    assert staged(site)["route"] == "ready"
    held = site["work"] / "held"
    held.mkdir()
    (held / "link").symlink_to(outside)
    (held / "nested").mkdir()
    (held / "nested" / "link").symlink_to(outside / "file")
    assert staged(site)["route"] == "ready"
    assert not held.exists()
    assert (outside / "file").read_text() == "keep"


@pytest.mark.parametrize("problem", ["wide_mode", "link", "file", "foreign_owner"])
def test_an_unsafe_work_directory_leaves_the_volume_unchanged(site, problem):
    assert staged(site)["route"] == "ready"
    work = site["work"]
    owner = OWNER
    if problem == "wide_mode":
        work.chmod(0o755)
    elif problem == "link":
        work.rmdir()
        target = site["volume"] / "worker-made"
        target.mkdir(mode=0o700)
        work.symlink_to(target)
    elif problem == "file":
        work.rmdir()
        work.write_text("worker-made")
    elif OWNER == 0:  # a work directory the worker identity created first
        os.chown(work, 54321, -1)
    else:
        owner = OWNER + 1
    result = verified(site, owner)
    assert (result["route"], result["reason"]) == ("unverified", "staging_area_invalid")
    assert site["model"].is_dir()


def make_snapshot(tmp_path):
    snapshot = tmp_path / "fallback/opentour-worker"
    snapshot.mkdir(parents=True, mode=0o700)
    os.chmod(snapshot, 0o700)
    (snapshot / "ledger.sqlite3").write_bytes(b"generated ledger")
    (snapshot / "authorization.json").write_text('{"generated": "grant"}')
    (snapshot / "managed-outputs").mkdir(mode=0o700)
    (snapshot / "managed-outputs/manifest.json").write_text("{}")
    for path in snapshot.rglob("*"):
        path.chmod(0o700 if path.is_dir() else 0o600)
    return snapshot


def screen(snapshot, restore_to=None, owner=OWNER):
    return boundary_module["check"](snapshot, restore_to, owner_uid=owner)


def test_documented_copy_passes_and_careless_copies_of_the_layout_are_refused(tmp_path):
    """Lay out a generated host as the README describes, then screen three copies of it."""
    host = tmp_path / "host"
    layout = boundary_module["boundary"]()
    volume = host / VOLUME.lstrip("/")
    volume.mkdir(parents=True, mode=0o700)
    (volume / "ledger.sqlite3").write_bytes(b"generated ledger")
    (volume / "authorization.json").write_text('{"generated": "grant"}')
    (volume / "model-base").mkdir()
    (volume / "model-base/config.json").write_text("generated")
    (volume / ".opentour-staging/held").mkdir(parents=True)
    for outside in layout["outside"]:
        path = host / outside.lstrip("/")
        if outside.endswith(".env"):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("GENERATED=1")
        else:
            path.mkdir(parents=True, exist_ok=True)
    (host / "etc/opentour-health/credential").write_text("generated")
    (host / "etc/opentour-health/tls.key").write_text("generated")
    for path in host.rglob("*"):
        path.chmod(0o700 if path.is_dir() else 0o600)

    documented = tmp_path / "documented/opentour-worker"
    shutil.copytree(
        volume, documented, ignore=shutil.ignore_patterns("model-base", ".opentour-staging")
    )
    assert screen(documented, VOLUME) == []

    whole_lib = tmp_path / "careless-lib"
    shutil.copytree(host / "var/lib", whole_lib)
    whole = screen(whole_lib)
    assert {
        "model_base_in_snapshot",
        "staging_area_in_snapshot",
        "out_of_boundary_material",
    } <= set(whole)

    with_etc = tmp_path / "careless-etc/opentour-worker"
    shutil.copytree(
        volume, with_etc, ignore=shutil.ignore_patterns("model-base", ".opentour-staging")
    )
    shutil.copytree(host / "etc", with_etc / "etc")
    assert "out_of_boundary_material" in screen(with_etc)


def test_every_path_the_units_hold_secrets_or_state_in_is_outside_the_volume():
    layout = boundary_module["boundary"]()
    assert layout["inside"] == [VOLUME]
    outside = list(layout["outside"])
    cycle = unit("opentour-cycle.service")["Service"]
    health = unit("opentour-health.service")["Service"]
    (health_command,) = health["ExecStart"]
    health_argv = health_command.split()
    secret_paths = [
        health_argv[health_argv.index(flag) + 1]
        for flag in ("--config", "--cert", "--key", "--token")
    ]
    secret_paths += [p.lstrip("-") for p in cycle["EnvironmentFile"]]
    state_paths = ["/var/lib/" + name for name in cycle["StateDirectory"][0].split()]
    for path in secret_paths + [ANTI_ROLLBACK]:
        assert not (path == VOLUME or path.startswith(VOLUME + "/")), path
        assert any(path == o or path.startswith(o + "/") for o in outside), path
    assert state_paths == [VOLUME, ANTI_ROLLBACK]
    readme = (HOST / "README.md").read_text()
    for path in outside:
        assert f"`{path}`" in readme, path
    assert "not yet written (OPE-566)" in readme
    assert "Not in the distributed kit yet." in readme


@pytest.mark.parametrize(
    ("name", "finding"),
    [
        ("cycle.env.bak", "out_of_boundary_material"),
        ("Credential", "out_of_boundary_material"),
        ("site.ENV", "out_of_boundary_material"),
        ("tls.pem", "out_of_boundary_material"),
        ("health-token", "out_of_boundary_material"),
        ("api.key", "out_of_boundary_material"),
        ("client-secret.json", "out_of_boundary_material"),
        ("site\u200b.env", "hidden_character_in_name"),
        ("site\u200b.env", "out_of_boundary_material"),
        ("\uff53\uff49\uff54\uff45.env", "non_normalized_name"),
        ("CRED\u200dENTIAL", "out_of_boundary_material"),
        ("opentour-antirollback", "out_of_boundary_material"),
        ("Model-Base", "model_base_in_snapshot"),
        (".opentour-staging", "staging_area_in_snapshot"),
    ],
)
def test_snapshot_names_that_bypassed_the_first_screen_are_refused(tmp_path, name, finding):
    snapshot = make_snapshot(tmp_path)
    (snapshot / "managed-outputs" / name).write_text("generated")
    (snapshot / "managed-outputs" / name).chmod(0o600)
    assert finding in screen(snapshot)


@pytest.mark.parametrize(
    ("change", "finding"),
    [
        (lambda s: (s / "link").symlink_to("/etc/opentour-health/credential"), "link_in_snapshot"),
        (lambda s: os.link(s / "ledger.sqlite3", s / "ledger-copy"), "hard_link_in_snapshot"),
        (lambda s: os.mkfifo(s / "pipe", 0o600), "special_file_in_snapshot"),
        (lambda s: (s / "ledger.sqlite3").chmod(0o644), "snapshot_permissions_too_wide"),
        (lambda s: (s / "ledger.sqlite3").chmod(0o4700), "special_mode_in_snapshot"),
        (lambda s: (s / "ledger.sqlite3").chmod(0o2600), "special_mode_in_snapshot"),
        (lambda s: (s / "managed-outputs").chmod(0o1700), "special_mode_in_snapshot"),
    ],
)
def test_snapshot_metadata_that_must_not_be_restored_is_refused(tmp_path, change, finding):
    snapshot = make_snapshot(tmp_path)
    change(snapshot)
    assert finding in screen(snapshot)


def test_snapshot_entries_must_belong_to_the_worker_identity(tmp_path):
    snapshot = make_snapshot(tmp_path)
    assert screen(snapshot) == []
    assert "foreign_owner_in_snapshot" in screen(snapshot, owner=OWNER + 1)
    if OWNER == 0:  # one root-owned entry among worker-owned ones
        for path in [snapshot, *snapshot.rglob("*")]:
            os.chown(path, 54321, -1)
        assert screen(snapshot, owner=54321) == []
        os.chown(snapshot / "authorization.json", 0, -1)
        assert screen(snapshot, owner=54321) == ["foreign_owner_in_snapshot"]


def test_restore_target_must_be_exactly_the_worker_volume(tmp_path):
    snapshot = make_snapshot(tmp_path)
    assert screen(snapshot, VOLUME) == []
    for target in (
        tmp_path / "var/lib/opentour-worker",
        Path(VOLUME + "/"),
        Path(VOLUME) / "nested",
        Path(ANTI_ROLLBACK),
        Path("/var/lib/opentour-worker-old"),
    ):
        if os.path.abspath(target) == VOLUME:
            assert screen(snapshot, target) == []
        else:
            assert screen(snapshot, target) == ["restore_target_not_worker_volume"]
    for live in (VOLUME + "/snap", ANTI_ROLLBACK + "/snap", "/etc/opentour-worker/snap"):
        assert "snapshot_overlaps_live_state" in screen(Path(live))


def test_snapshot_cli_screens_with_the_named_worker_identity(tmp_path):
    snapshot = make_snapshot(tmp_path)
    script = str(HOST / "snapshot_boundary.py")
    owner = pwd.getpwuid(OWNER).pw_name

    def cli(*extra):
        completed = subprocess.run(
            [*ISOLATED, script, "check", "--snapshot", str(snapshot), *extra],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env={},
        )
        return completed.returncode, json.loads(completed.stdout)["findings"]

    assert cli("--owner", owner, "--restore-to", VOLUME) == (0, [])
    assert cli("--owner", owner, "--restore-to", str(tmp_path)) == (
        2,
        ["restore_target_not_worker_volume"],
    )
    (snapshot / "Health-Token").write_text("generated")
    (snapshot / "Health-Token").chmod(0o600)
    assert cli("--owner", owner) == (2, ["out_of_boundary_material"])
    assert cli("--owner", "opentour-generated-missing-user") == (2, ["worker_identity_unknown"])
    printed = subprocess.run(
        [*ISOLATED, script, "boundary"],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
        env={},
    )
    assert json.loads(printed.stdout) == boundary_module["boundary"]()
