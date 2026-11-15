"""Generated adverse disclosure fixtures; no real secret or contact data."""

import json
from pathlib import Path

import pytest

from tools.public_content_check import digest, inspect_bytes, load_policy, scan_paths

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("kind", ["credential", "prospect", "conversation", "binary", "path"])
def test_private_payload_is_refused_without_echo(tmp_path, kind):
    address = "generated-owner" + "@" + "sentinel-corporation.com"
    key = "AK" + "IA" + "Z" * 16
    role = "role"
    data = {
        "credential": key,
        "prospect": "name,email\nGenerated," + address,
        "conversation": json.dumps(
            [
                {role: "user", "content": "Generated private input"},
                {role: "assistant", "content": "Generated answer"},
            ]
        ),
        "binary": "\0unreviewed",
        "path": "Generated safe text",
    }[kind].encode()
    name = address + ".txt" if kind == "path" else "candidate.txt"
    file = tmp_path / name
    file.write_bytes(data)
    result = scan_paths(tmp_path, [name], load_policy(ROOT), artifacts=True)
    if kind == "path":
        # Filenames themselves must not disclose an address or credential either.
        assert address not in json.dumps(result)
    assert result["status"] == "FAIL"
    for forbidden in (address, key, "Generated private input"):
        assert forbidden not in json.dumps(result)


def test_generated_technical_fixture_passes(tmp_path):
    (tmp_path / "fixture.json").write_text(
        json.dumps(
            {
                "organization_id": "simulated:org-a",
                "email": "generated@example.invalid",
                "kind": "simulated",
                "product_acceptance": "NOT_RUN",
            }
        )
    )
    assert (
        scan_paths(tmp_path, ["fixture.json"], load_policy(ROOT), artifacts=True)["status"]
        == "PASS"
    )


def test_unapproved_source_path_and_private_copy_fail(tmp_path):
    folder = tmp_path / "warehouse"
    folder.mkdir()
    (folder / "export.md").write_text("Generated content")
    result = scan_paths(tmp_path, ["warehouse/export.md"], load_policy(ROOT))
    assert result["status"] == "FAIL"
    assert {x["rule"] for x in result["findings"]} >= {"PRIVATE_PATH", "UNAPPROVED_SOURCE_PATH"}


def test_symlink_traversal_missing_and_empty_never_pass(tmp_path):
    (tmp_path / "escape.txt").symlink_to("/etc/hosts")
    for names in (["escape.txt"], ["../outside.txt"], ["absent.txt"], []):
        assert scan_paths(tmp_path, names, load_policy(ROOT), artifacts=True)["status"] == "FAIL"


def test_directory_and_root_symlinks_fail_artifact_inventory(tmp_path):
    folder = tmp_path / "outside"
    folder.mkdir()
    (folder / "payload.txt").write_text("Bearer " + "SENTINEL" * 5)
    base = tmp_path / "artifacts"
    base.mkdir()
    (base / "safe.txt").write_text("Generated content")
    (base / "linked").symlink_to(folder, target_is_directory=True)
    paths = [str(p.relative_to(base)) for p in base.rglob("*") if p.is_file() or p.is_symlink()]
    assert scan_paths(base, paths, load_policy(ROOT), artifacts=True)["status"] == "FAIL"
    assert (
        scan_paths(base / "linked", ["payload.txt"], load_policy(ROOT), artifacts=True)["status"]
        == "FAIL"
    )


def test_unaccompanied_credential_assignment_is_refused():
    payload = ("AWS_SECRET_" + "ACCESS_KEY=" + "Q" * 40).encode()
    assert inspect_bytes("output.txt", payload, load_policy(ROOT))


def test_reviewed_fake_match_exception_is_exact_and_never_applies_to_logs():
    policy = load_policy(ROOT)
    sentinel = ("AK" + "IA" + "Y" * 16).encode()
    path = "tests/generated_test.py"
    policy["approved_synthetic_matches"][path] = [digest(sentinel)]
    assert not inspect_bytes(path, sentinel, policy)
    assert inspect_bytes(path, sentinel.replace(b"Y", b"Z"), policy)
    assert inspect_bytes("tests/different_test.py", sentinel, policy)
    assert inspect_bytes(path, sentinel, policy, binary_content=True)


def test_binary_exception_requires_exact_reviewed_digest():
    policy = load_policy(ROOT)
    data = b"\0generated-png"
    policy["reviewed_binary_sha256"] = {"apps/fixture.png": digest(data)}
    assert not inspect_bytes("apps/fixture.png", data, policy)
    assert inspect_bytes("apps/fixture.png", data + b"changed", policy)


def test_artifact_or_log_secret_blocks_publication(tmp_path):
    (tmp_path / "command.log.txt").write_text("Bearer " + "SENTINEL" * 5)
    report = scan_paths(tmp_path, ["command.log.txt"], load_policy(ROOT), artifacts=True)
    assert report["status"] == "FAIL"
    assert "SENTINEL" not in json.dumps(report)


def test_oversized_content_and_invalid_utf8_fail(tmp_path):
    for content in (b"a" * 4_194_305, b"\xff"):
        (tmp_path / "payload.txt").write_bytes(content)
        assert (
            scan_paths(tmp_path, ["payload.txt"], load_policy(ROOT), artifacts=True)["status"]
            == "FAIL"
        )
