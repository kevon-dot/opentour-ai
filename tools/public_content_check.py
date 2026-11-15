"""Fail-closed public source/artifact screening; never print rejected payloads.

This is an engineering disclosure gate, not proof that arbitrary text is anonymous.
Independent source review is still required. Only generated technical fixtures belong
in this repository; binary exceptions are individually reviewed content hashes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from collections.abc import Iterable
from pathlib import Path
from typing import Any

MAX_FILE_BYTES = 4_194_304
TEXT_SUFFIXES = {
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".cjs",
    ".mjs",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".lock",
    ".md",
    ".txt",
    ".css",
    ".html",
    ".svg",
    ".sh",
    ".sql",
    ".tf",
    ".hcl",
    ".ini",
    ".cfg",
    ".example",
    ".d.ts",
}
TEXT_NAMES = {
    "AGENTS.md",
    "README.md",
    "LICENSE",
    "CODEOWNERS",
    "justfile",
    "Dockerfile",
    ".gitignore",
    ".dockerignore",
    ".node-version",
    ".python-version",
}
PRIVATE_PATH = re.compile(
    r"(?i)(?:^|[/_. -])(?:contacts?|prospects?|leads?|customer-data|private-data|"
    r"knowledge-base|warehouse|conversations?|credentials|storage-state)(?:[/_. -]|$)"
)
EMAIL = re.compile(rb"[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
SECRET_PATTERNS = (
    re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(rb"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b"),
    re.compile(rb"\b(?:sk_live_|rk_live_|sk-proj-)[A-Za-z0-9_-]{12,}\b"),
    re.compile(rb"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----"),
    re.compile(rb"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(rb"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
    re.compile(rb"https?://[^\s/:@]+:[^\s/@]+@"),
    re.compile(
        rb"""(?i)["']?(?:aws_secret_access_key|api[_-]?key|client_secret|access_token)["']?\s*[:=]\s*["']?[A-Za-z0-9+/=_-]{20,}"""
    ),
)
CONVERSATION = re.compile(rb"""["']role["']\s*:\s*["'](?:user|assistant|system)["']""")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def finding(path: str, rule: str) -> dict[str, str]:
    # A filename can itself contain a credential or personal address.
    return {"path_sha256": digest(path.encode()), "rule": rule}


def approved_email(value: bytes) -> bool:
    domain = value.rsplit(b"@", 1)[1].lower()
    return domain in {b"example.com", b"example.org", b"example.net"} or domain.endswith(
        (b".invalid", b".test", b".example", b".localhost", b".noreply.github.com")
    )


def inspect_bytes(
    path: str, data: bytes, policy: dict[str, Any], *, binary_content: bool = False
) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    if any(not approved_email(value) for value in EMAIL.findall(path.encode())) or any(
        pattern.search(path.encode()) for pattern in SECRET_PATTERNS
    ):
        issues.append(finding(path, "SENSITIVE_PATH"))
    if PRIVATE_PATH.search(path):
        issues.append(finding(path, "PRIVATE_PATH"))
    if len(data) > MAX_FILE_BYTES:
        return issues + [finding(path, "FILE_TOO_LARGE")]
    content_hash = digest(data)
    approved_matches = (
        set() if binary_content else set(policy.get("approved_synthetic_matches", {}).get(path, []))
    )
    binary_hashes = policy.get("reviewed_binary_sha256", {})
    if binary_hashes.get(path) == content_hash or (
        binary_content
        and Path(path).suffix in {".png", ".woff2"}
        and content_hash in binary_hashes.values()
    ):
        return issues
    if (
        b"\0" in data
        or Path(path).suffix.lower() not in TEXT_SUFFIXES | {".dockerfile"}
        and (Path(path).name not in TEXT_NAMES and not Path(path).name.startswith(".env.example"))
    ):
        return issues + [finding(path, "UNREVIEWED_BINARY_OR_TYPE")]
    try:
        data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return issues + [finding(path, "NON_UTF8_CONTENT")]
    if any(
        digest(match.group()) not in approved_matches
        for pattern in SECRET_PATTERNS
        for match in pattern.finditer(data)
    ):
        issues.append(finding(path, "CREDENTIAL_PATTERN"))
    if any(
        not approved_email(value) and digest(value) not in approved_matches
        for value in EMAIL.findall(data)
    ):
        issues.append(finding(path, "PERSONAL_OR_BUSINESS_EMAIL"))
    if len(CONVERSATION.findall(data)) >= 2:
        issues.append(finding(path, "RAW_CONVERSATION"))
    if b"data:application/" + b"pdf;base64," in data or (
        b"<send_user_" + b"message_question_reply>" in data
    ):
        issues.append(finding(path, "RAW_EXPORT"))
    return issues


def load_policy(root: Path) -> dict[str, Any]:
    value = json.loads((root / ".github/public-content-policy.json").read_text())
    if (
        not isinstance(value, dict)
        or set(value)
        != {
            "schema_version",
            "technical_roots",
            "root_files",
            "reviewed_binary_sha256",
            "approved_synthetic_matches",
        }
        or value["schema_version"] != "opentour.public-content.v1"
    ):
        raise ValueError("Invalid public-content policy")
    for field in ("technical_roots", "root_files"):
        if not isinstance(value[field], list) or not all(isinstance(x, str) for x in value[field]):
            raise ValueError("Invalid public-content allowlist")
    if not isinstance(value["reviewed_binary_sha256"], dict) or any(
        not isinstance(k, str)
        or Path(k).suffix not in {".png", ".woff2"}
        or not isinstance(v, str)
        or not re.fullmatch(r"[a-f0-9]{64}", v)
        for k, v in value["reviewed_binary_sha256"].items()
    ):
        raise ValueError("Invalid reviewed binary digest")
    if not isinstance(value["approved_synthetic_matches"], dict) or any(
        not isinstance(k, str)
        or not k.startswith(("tests/", "tools/tests/"))
        or not k.endswith(".py")
        or not isinstance(v, list)
        or any(not isinstance(item, str) or not re.fullmatch(r"[a-f0-9]{64}", item) for item in v)
        for k, v in value["approved_synthetic_matches"].items()
    ):
        raise ValueError("Invalid generated-test sentinel exceptions")
    return value


def scan_paths(
    root: Path, paths: Iterable[str], policy: dict[str, Any], *, artifacts: bool = False
) -> dict[str, Any]:
    root_is_link = root.is_symlink()
    root = root.resolve()
    records: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    if root_is_link:
        failures.append(finding("", "UNSAFE_PATH"))
    for relative in sorted(set(paths)):
        file = root / relative
        if Path(relative).is_absolute() or ".." in Path(relative).parts or file.is_symlink():
            failures.append(finding(relative, "UNSAFE_PATH"))
            continue
        if not file.resolve().is_relative_to(root) or not file.is_file():
            failures.append(finding(relative, "MISSING_OR_ESCAPING_FILE"))
            continue
        if (
            not artifacts
            and relative not in policy["root_files"]
            and (Path(relative).parts[0] not in policy["technical_roots"])
        ):
            failures.append(finding(relative, "UNAPPROVED_SOURCE_PATH"))
        if file.stat().st_size > MAX_FILE_BYTES:
            failures.append(finding(relative, "FILE_TOO_LARGE"))
            continue
        data = file.read_bytes()
        failures.extend(inspect_bytes(relative, data, policy, binary_content=artifacts))
        records.append(
            {"path_sha256": digest(relative.encode()), "size": len(data), "sha256": digest(data)}
        )
    if not records:
        failures.append(finding("", "NO_FILES"))
    return {
        "schema_version": "opentour.public-content-result.v1",
        "status": "FAIL" if failures else "PASS",
        "files_scanned": len(records),
        "covered_source_sha256": digest(json.dumps(records, sort_keys=True).encode()),
        "findings": failures,
        "records": records,
        "limitation": "Pattern/allowlist screening does not replace independent source review",
    }


def tracked_paths(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True, timeout=15
    )
    return [p for p in result.stdout.decode("utf-8").split("\0") if p]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--artifacts", type=Path)
    args = parser.parse_args()
    try:
        policy = load_policy(args.root)
        base = args.artifacts if args.artifacts is not None else args.root
        paths = (
            [str(p.relative_to(base)) for p in base.rglob("*") if p.is_file() or p.is_symlink()]
            if args.artifacts
            else tracked_paths(base)
        )
        result = scan_paths(base, paths, policy, artifacts=args.artifacts is not None)
    except (OSError, ValueError, subprocess.SubprocessError):
        result = {"status": "FAIL", "findings": [{"rule": "CONTENT_CHECK_UNAVAILABLE"}]}
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
