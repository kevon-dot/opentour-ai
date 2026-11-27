"""OPE-577 package checks: cycle unit hardening, offline model staging, snapshot boundary.

Every model file, wheel and interpreter here is a generated sentinel. No real weights, numeric
runtime or systemd manager is used, so these tests prove the package only, not an installed host.
"""

import hashlib
import json
import os
import shutil
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


HARDENING = (
    "User",
    "Group",
    "UMask",
    "StateDirectory",
    "StateDirectoryMode",
    "ProtectSystem",
    "ProtectHome",
    "PrivateTmp",
    "NoNewPrivileges",
    "CapabilityBoundingSet",
    "StandardOutput",
    "InaccessiblePaths",
    "ReadOnlyPaths",
)


def test_cycle_unit_matches_worker_hardening_and_identity():
    cycle, worker, health = (
        unit(n)["Service"]
        for n in ("opentour-cycle.service", "opentour-worker.service", "opentour-health.service")
    )
    for key in HARDENING:
        assert cycle.get(key) == worker.get(key), key
    assert cycle["User"] == cycle["Group"] == ["opentour-worker"]
    assert health["User"] == ["opentour-health"] != cycle["User"]
    assert cycle["UMask"] == ["0077"] and cycle["StateDirectoryMode"] == ["0700"]
    assert cycle["ProtectSystem"] == ["strict"] and cycle["CapabilityBoundingSet"] == [""]
    assert cycle["StateDirectory"] == ["opentour-worker opentour-antirollback"]
    assert cycle["InaccessiblePaths"] == ["-/etc/opentour-health"]
    assert cycle["ReadOnlyPaths"] == [f"-{VOLUME}/model-base"]
    # The health identity reaches neither the restorable volume nor the anti-rollback state.
    assert health["InaccessiblePaths"] == [f"-{VOLUME}", f"-{ANTI_ROLLBACK}"]
    assert unit("opentour-cycle.service")["Install"] == {"WantedBy": ["multi-user.target"]}


def test_cycle_unit_reads_configuration_and_secrets_from_outside_the_snapshot():
    cycle = unit("opentour-cycle.service")["Service"]
    assert cycle["EnvironmentFile"] == [
        "/etc/opentour-worker/site.env",
        "-/etc/opentour-worker/cycle.env",
    ]
    assert unit("opentour-worker.service")["Service"]["EnvironmentFile"] == [
        "/etc/opentour-worker/site.env"
    ]
    assert "Environment" not in cycle and "LoadCredential" not in cycle
    for path in cycle["EnvironmentFile"] + cycle["InaccessiblePaths"]:
        assert not path.lstrip("-").startswith(VOLUME)
    assert not ANTI_ROLLBACK.startswith(VOLUME + "/")
    # The model route never gates the unit, so rules-only work starts without a staged base.
    assert not {"ExecStartPre", "ConditionPathExists", "AssertPathExists", "Requires"} & set(cycle)
    assert cycle["Restart"] == ["always"]


def test_cycle_unit_command_is_the_packaged_cycle_worker_entry_point(tmp_path):
    (command,) = unit("opentour-cycle.service")["Service"]["ExecStart"]
    argv = command.split()
    assert argv[:5] == [
        "/opt/opentour/python/bin/python3",
        "-I",
        "-B",
        "/opt/opentour/current/opentour-slack.pyz",
        "cycle-worker",
    ]
    options = dict(zip(argv[5::2], argv[6::2], strict=True))
    assert options == {
        "--directory": VOLUME,
        "--organization": "${ORGANIZATION}",
        "--config": f"{VOLUME}/cycle.json",
        "--interval": "300",
    }
    builder = (ROOT / "tools/build_slack_pilot_kit.py").read_text()
    assert '"cycle-worker": ("cycle_worker", "main"),' in builder
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
    """A generated release, offline checkpoint source, wheel directory and interpreter."""
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
        profiles = release / "training/profiles"
        profiles.mkdir(parents=True, exist_ok=True)
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
    volume = tmp_path / "var/lib/opentour-worker"
    volume.mkdir(parents=True, mode=0o700)
    (volume / "ledger.sqlite3").write_bytes(b"generated rules-only ledger")
    (volume / "cycle.json").write_text('{"generated": true}')
    return {
        "release": release,
        "source": source,
        "wheels": wheels,
        "python": python,
        "volume": volume,
        "model": volume / "model-base",
        "profile": profile,
        "write_release": write_release,
        "write_python": write_python,
    }


def staged(site, revision=REVISION):
    return stage_module["stage"](
        site["release"],
        site["source"],
        revision,
        site["model"],
        site["wheels"],
        str(site["python"]),
    )


def verified(site):
    return stage_module["verify"](
        site["release"], site["model"], site["wheels"], str(site["python"])
    )


def rules_only_state_unchanged(site):
    assert (site["volume"] / "ledger.sqlite3").read_bytes() == b"generated rules-only ledger"
    assert (site["volume"] / "cycle.json").read_text() == '{"generated": true}'


def test_stage_accepts_a_correct_sentinel_set_and_promotes_read_only(site):
    assert staged(site) == {
        "schema": "opentour.open-weight-staging.v1",
        "route": "ready",
        "reason": "verified",
        "profile": "smollm2_135m_lora_v1",
        "checkpoint_revision": REVISION,
    }
    model = site["model"]
    assert sorted(p.name for p in model.iterdir()) == sorted(site["profile"]["files"])
    assert model.stat().st_mode & 0o777 == 0o555
    assert {p.stat().st_mode & 0o777 for p in model.iterdir()} == {0o444}
    assert sorted(p.name for p in site["volume"].iterdir()) == [
        "cycle.json",
        "ledger.sqlite3",
        "model-base",
    ]
    assert verified(site)["route"] == "ready"
    rules_only_state_unchanged(site)


def test_stage_cli_runs_isolated_with_the_standard_library_only(site):
    arguments = [
        "--release",
        str(site["release"]),
        "--model-dir",
        str(site["model"]),
        "--wheel-dir",
        str(site["wheels"]),
        "--python",
        str(site["python"]),
    ]
    script = str(HOST / "stage_open_weight.py")
    ready = subprocess.run(
        [
            *ISOLATED,
            script,
            "stage",
            *arguments,
            "--source",
            str(site["source"]),
            "--revision",
            REVISION,
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env={},
    )
    assert ready.returncode == 0, ready.stderr
    assert json.loads(ready.stdout)["route"] == "ready"
    os.chmod(site["model"], 0o755)
    (site["model"] / "config.json").chmod(0o644)
    (site["model"] / "config.json").write_bytes(b"tampered")
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
    assert not site["model"].exists() and (site["volume"] / "model-base.held").is_dir()


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
    assert not (site["volume"] / ".model-base.staging").exists()
    rules_only_state_unchanged(site)


def test_verify_holds_an_installed_base_with_an_extra_or_missing_file(site):
    assert staged(site)["route"] == "ready"
    os.chmod(site["model"], 0o755)
    (site["model"] / "notes.txt").write_text("unexpected")
    assert verified(site)["reason"] == "unexpected_model_file"
    assert not site["model"].exists()
    assert verified(site)["reason"] == "staged_model_file_required"
    # Re-staging from the verified offline source is the only way back to the model route.
    assert staged(site)["route"] == "ready"
    assert not (site["volume"] / "model-base.held").exists()


def test_boundary_lists_disjoint_inside_and_outside_paths_matching_the_units():
    printed = subprocess.run(
        [*ISOLATED, str(HOST / "snapshot_boundary.py"), "boundary"],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
        env={},
    )
    layout = json.loads(printed.stdout)
    assert layout["inside"] == [VOLUME]
    assert f"{VOLUME}/model-base" in layout["outside"]
    assert ANTI_ROLLBACK in layout["outside"]
    for path in layout["outside"]:
        assert path == f"{VOLUME}/model-base" or not path.startswith(VOLUME + "/")
    cycle = unit("opentour-cycle.service")["Service"]
    referenced = [p.lstrip("-") for p in cycle["EnvironmentFile"] + cycle["InaccessiblePaths"]]
    assert all(any(r.startswith(o) for o in layout["outside"]) for r in referenced)
    readme = (HOST / "README.md").read_text()
    for path in layout["outside"]:
        assert f"`{path}`" in readme, path


def make_snapshot(tmp_path):
    snapshot = tmp_path / "fallback/opentour-worker"
    snapshot.mkdir(parents=True, mode=0o700)
    os.chmod(snapshot, 0o700)
    (snapshot / "ledger.sqlite3").write_bytes(b"generated ledger")
    (snapshot / "authorization.json").write_text('{"generated": "grant"}')
    for path in snapshot.iterdir():
        path.chmod(0o600)
    return snapshot


def test_clean_snapshot_is_accepted_and_restore_keeps_out_of_volume_authority(tmp_path):
    host = tmp_path / "host"
    volume = host / "var/lib/opentour-worker"
    anchors = host / "var/lib/opentour-antirollback"
    secrets = host / "etc/opentour-health"
    for directory in (volume, anchors, secrets):
        directory.mkdir(parents=True, mode=0o700)
    (secrets / "credential").write_text("generated-old-credential")
    (anchors / "generated-anchor").write_text("revision-1")
    (volume / "ledger.sqlite3").write_bytes(b"generated ledger")
    snapshot = make_snapshot(tmp_path)
    assert boundary_module["check"](snapshot, volume) == []
    # After the snapshot the custodian rotates the credential and advances anti-rollback state.
    (secrets / "credential").write_text("generated-new-credential")
    (anchors / "generated-anchor").write_text("revision-2")
    shutil.rmtree(volume)
    shutil.copytree(snapshot, volume)
    assert (secrets / "credential").read_text() == "generated-new-credential"
    assert (anchors / "generated-anchor").read_text() == "revision-2"
    assert not (volume / "model-base").exists()


@pytest.mark.parametrize(
    ("change", "finding"),
    [
        (lambda s: (s / "credential").write_text("x"), "out_of_boundary_material"),
        (
            lambda s: (s / "nested").mkdir() or (s / "nested/site.env").write_text("x"),
            "out_of_boundary_material",
        ),
        (lambda s: (s / "opentour-antirollback").mkdir(), "out_of_boundary_material"),
        (lambda s: (s / "model-base").mkdir(), "model_base_in_snapshot"),
        (lambda s: (s / "link").symlink_to("/etc/opentour-health/credential"), "link_in_snapshot"),
        (lambda s: os.link(s / "ledger.sqlite3", s / "ledger-copy"), "hard_link_in_snapshot"),
        (lambda s: os.mkfifo(s / "pipe", 0o600), "special_file_in_snapshot"),
        (lambda s: (s / "ledger.sqlite3").chmod(0o644), "snapshot_permissions_too_wide"),
    ],
)
def test_snapshot_with_out_of_boundary_material_is_refused(tmp_path, change, finding):
    snapshot = make_snapshot(tmp_path)
    change(snapshot)
    assert finding in boundary_module["check"](snapshot)
    refused = subprocess.run(
        [*ISOLATED, str(HOST / "snapshot_boundary.py"), "check", "--snapshot", str(snapshot)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env={},
    )
    assert refused.returncode == 2 and finding in json.loads(refused.stdout)["findings"]


def test_restore_target_must_be_the_worker_volume_and_not_live_state(tmp_path):
    snapshot = make_snapshot(tmp_path)
    lib = tmp_path / "var/lib"
    assert boundary_module["check"](snapshot, lib / "opentour-antirollback") == [
        "restore_target_not_worker_volume"
    ]
    inside = lib / "opentour-antirollback/opentour-worker"
    inside.mkdir(parents=True)
    copy = inside / "snap"
    shutil.copytree(snapshot, copy)
    assert "snapshot_overlaps_live_state" in boundary_module["check"](
        copy, lib / "opentour-antirollback/opentour-worker"
    )
