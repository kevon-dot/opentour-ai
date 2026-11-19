"""Generated local OCI content and offline attestation policy tests."""

import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tools import verify_release_manifest
from tools.verify_release_manifest import (
    ReleaseVerificationError,
    verify_manifest_attestation,
    verify_oci_layout,
)

_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
_INDEX = "application/vnd.oci.image.index.v1+json"


def _json(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode()


def _blob(root: Path, data: bytes, media_type: str) -> dict[str, Any]:
    digest = hashlib.sha256(data).hexdigest()
    (root / "blobs" / "sha256" / digest).write_bytes(data)
    return {"digest": f"sha256:{digest}", "size": len(data), "mediaType": media_type}


def _index(root: Path, entries: list[dict[str, Any]]) -> str:
    contents = _json({"schemaVersion": 2, "manifests": entries})
    (root / "index.json").write_bytes(contents)
    return f"sha256:{hashlib.sha256(contents).hexdigest()}"


@pytest.fixture
def layout(tmp_path: Path) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    (tmp_path / "blobs" / "sha256").mkdir(parents=True)
    (tmp_path / "oci-layout").write_bytes(_json({"imageLayoutVersion": "1.0.0"}))
    config = _blob(tmp_path, b"{}", "application/vnd.oci.image.config.v1+json")
    layer = _blob(tmp_path, b"generated", "application/vnd.oci.image.layer.v1.tar")
    manifest = _blob(
        tmp_path,
        _json({"schemaVersion": 2, "mediaType": _MANIFEST, "config": config, "layers": [layer]}),
        _MANIFEST,
    )
    _index(tmp_path, [manifest])
    return tmp_path, manifest, layer


def test_verify_exact_manifest_and_all_its_local_content(
    layout: tuple[Path, dict[str, Any], dict[str, Any]],
) -> None:
    root, manifest, _ = layout
    verify_oci_layout(root, manifest["digest"])


def test_verify_nested_index_and_root_digest(
    layout: tuple[Path, dict[str, Any], dict[str, Any]],
) -> None:
    root, manifest, _ = layout
    child = _blob(root, _json({"schemaVersion": 2, "manifests": [manifest]}), _INDEX)
    root_digest = _index(root, [child])
    verify_oci_layout(root, child["digest"])
    verify_oci_layout(root, root_digest)


def test_modified_layer_with_unchanged_size_fails(
    layout: tuple[Path, dict[str, Any], dict[str, Any]],
) -> None:
    root, manifest, layer = layout
    (root / "blobs" / "sha256" / layer["digest"][7:]).write_bytes(b"different")
    with pytest.raises(ReleaseVerificationError, match="digest mismatch"):
        verify_oci_layout(root, manifest["digest"])


def test_missing_local_layer_is_not_accepted(
    layout: tuple[Path, dict[str, Any], dict[str, Any]],
) -> None:
    root, manifest, layer = layout
    (root / "blobs" / "sha256" / layer["digest"][7:]).unlink()
    with pytest.raises(ReleaseVerificationError, match="Missing or unreadable"):
        verify_oci_layout(root, manifest["digest"])


def test_manifest_blob_must_be_selected_from_the_index(
    layout: tuple[Path, dict[str, Any], dict[str, Any]],
) -> None:
    root, manifest, _ = layout
    unrelated = _blob(
        root,
        _json({"schemaVersion": 2, "manifests": [manifest], "annotations": {"test": "unlisted"}}),
        _INDEX,
    )
    with pytest.raises(ReleaseVerificationError, match="missing or ambiguous"):
        verify_oci_layout(root, unrelated["digest"])


def test_index_refuses_conflicting_descriptors_for_same_digest(
    layout: tuple[Path, dict[str, Any], dict[str, Any]],
) -> None:
    root, manifest, _ = layout
    _index(root, [manifest, dict(manifest, size=manifest["size"] + 1)])
    with pytest.raises(ReleaseVerificationError, match="missing or ambiguous"):
        verify_oci_layout(root, manifest["digest"])


def test_refuses_noncanonical_digest_and_symlink_content(
    layout: tuple[Path, dict[str, Any], dict[str, Any]],
) -> None:
    root, manifest, layer = layout
    with pytest.raises(ReleaseVerificationError, match="canonical SHA-256"):
        verify_oci_layout(root, manifest["digest"] + "\n")
    layer_path = root / "blobs" / "sha256" / layer["digest"][7:]
    original = layer_path.rename(root / "outside-blob-directory")
    layer_path.symlink_to(original)
    with pytest.raises(ReleaseVerificationError, match="Missing or unreadable"):
        verify_oci_layout(root, manifest["digest"])


def test_refuses_duplicate_index_keys_even_when_index_bytes_match(
    layout: tuple[Path, dict[str, Any], dict[str, Any]],
) -> None:
    root, _, _ = layout
    good_index = (root / "index.json").read_bytes()
    ambiguous_index = b'{"schemaVersion":2,' + good_index[1:]
    (root / "index.json").write_bytes(ambiguous_index)
    digest = f"sha256:{hashlib.sha256(ambiguous_index).hexdigest()}"
    with pytest.raises(ReleaseVerificationError, match="Duplicate keys"):
        verify_oci_layout(root, digest)


@pytest.mark.parametrize("extra_bytes", [-1, 0], ids=["index", "manifest"])
def test_bounds_index_and_manifest_metadata_reads(
    layout: tuple[Path, dict[str, Any], dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    extra_bytes: int,
) -> None:
    root, manifest, _ = layout
    index_size = (root / "index.json").stat().st_size
    assert manifest["size"] > index_size
    monkeypatch.setattr(verify_release_manifest, "_MAX_METADATA_BYTES", index_size + extra_bytes)
    with pytest.raises(ReleaseVerificationError, match="local verification limit"):
        verify_oci_layout(root, manifest["digest"])


def test_refuses_symlink_blob_directory(
    layout: tuple[Path, dict[str, Any], dict[str, Any]],
) -> None:
    root, manifest, _ = layout
    blob_directory = root / "blobs" / "sha256"
    original = blob_directory.rename(root / "outside-blob-directory")
    blob_directory.symlink_to(original, target_is_directory=True)
    with pytest.raises(ReleaseVerificationError, match="Missing or unreadable local OCI directory"):
        verify_oci_layout(root, manifest["digest"])


@dataclass(frozen=True)
class _AttestationInputs:
    manifest: Path
    bundle: Path
    trusted_root: Path
    policy: Path
    github_cli: Path
    output: dict[str, Any]

    def verify(self) -> str:
        return verify_manifest_attestation(
            self.manifest,
            bundle=self.bundle,
            trusted_root=self.trusted_root,
            approved_policy=self.policy,
            github_cli=self.github_cli,
        )


@pytest.fixture
def attestation(tmp_path: Path) -> _AttestationInputs:
    manifest = tmp_path / "generated-manifest.json"
    manifest.write_bytes(_json({"id": "generated-manifest"}))
    bundle = tmp_path / "generated-bundle.json"
    bundle.write_bytes(_json({"generated": "bundle fixture"}))
    trusted_root = tmp_path / "generated-root.jsonl"
    trusted_root.write_bytes(b'{"generated":"root fixture"}\n')
    policy = tmp_path / "generated-policy.json"
    commit = "1" * 40
    ref = "refs/heads/generated-ref"
    workflow_path = ".github/workflows/generated-workflow.yml"
    repo_uri = "https://github.com/kevon-dot/opentour-ai"
    workflow_uri = f"{repo_uri}/{workflow_path}@{ref}"
    issuer = "https://token.actions.githubusercontent.com"
    policy.write_bytes(
        _json(
            {
                "oidc_issuer": issuer,
                "workflow_path": workflow_path,
                "source_ref": ref,
                "source_commit": commit,
                "trusted_root_sha256": hashlib.sha256(trusted_root.read_bytes()).hexdigest(),
            }
        )
    )
    output = {
        "verificationResult": {
            "mediaType": "application/vnd.dev.sigstore.verificationresult+json;version=0.1",
            "signature": {
                "certificate": {
                    "issuer": issuer,
                    "subjectAlternativeName": workflow_uri,
                    "sourceRepositoryURI": repo_uri,
                    "sourceRepositoryIdentifier": "1455972360",
                    "sourceRepositoryRef": ref,
                    "sourceRepositoryDigest": commit,
                    "buildConfigURI": workflow_uri,
                    "buildConfigDigest": commit,
                    "buildSignerURI": workflow_uri,
                    "buildSignerDigest": commit,
                }
            },
            "statement": {
                "predicateType": "https://slsa.dev/provenance/v1",
                "subject": [
                    {"digest": {"sha256": hashlib.sha256(manifest.read_bytes()).hexdigest()}}
                ],
            },
        }
    }
    return _AttestationInputs(
        manifest, bundle, trusted_root, policy, tmp_path / "approved-github-cli", output
    )


def _stub_github_cli(
    attestation: _AttestationInputs,
    monkeypatch: pytest.MonkeyPatch,
    *,
    version: bytes = b"gh version 2.91.0 (generated fixture)\n",
    verify_exit: int = 0,
    verify_output: bytes | None = None,
) -> list[list[str]]:
    calls: list[list[str]] = []
    original_manifest = attestation.manifest.read_bytes()

    def run(
        arguments: list[str], *, cwd: Path, env: dict[str, str], **options: object
    ) -> subprocess.CompletedProcess[bytes]:
        calls.append(arguments)
        assert arguments[0] == str(attestation.github_cli)
        assert "GH_TOKEN" not in env
        assert "GITHUB_TOKEN" not in env
        assert "HTTPS_PROXY" not in env
        assert env["GH_PROMPT_DISABLED"] == env["GH_NO_UPDATE_NOTIFIER"] == "1"
        assert options["check"] is True
        if arguments[1:] == ["--version"]:
            return subprocess.CompletedProcess(arguments, 0, version)
        assert arguments[1:3] == ["attestation", "verify"]
        assert (cwd / arguments[3]).read_bytes() == original_manifest
        flags = dict(zip(arguments[4::2], arguments[5::2], strict=True))
        assert flags == {
            "--hostname": "github.com",
            "--repo": "kevon-dot/opentour-ai",
            "--bundle": "./attestation.sigstore.json",
            "--custom-trusted-root": "./trusted_root.jsonl",
            "--cert-oidc-issuer": "https://token.actions.githubusercontent.com",
            "--cert-identity": (
                "https://github.com/kevon-dot/opentour-ai/.github/workflows/"
                "generated-workflow.yml@refs/heads/generated-ref"
            ),
            "--source-digest": "1" * 40,
            "--source-ref": "refs/heads/generated-ref",
            "--signer-digest": "1" * 40,
            "--digest-alg": "sha256",
            "--predicate-type": "https://slsa.dev/provenance/v1",
            "--format": "json",
        }
        assert (cwd / flags["--bundle"]).read_bytes() == attestation.bundle.read_bytes()
        assert (
            cwd / flags["--custom-trusted-root"]
        ).read_bytes() == attestation.trusted_root.read_bytes()
        output = _json([attestation.output]) if verify_output is None else verify_output
        if verify_exit:
            raise subprocess.CalledProcessError(verify_exit, arguments, output=output)
        return subprocess.CompletedProcess(arguments, 0, output)

    monkeypatch.setenv("GH_TOKEN", "generated-ignore")
    monkeypatch.setenv("GITHUB_TOKEN", "generated-ignore")
    monkeypatch.setenv("HTTPS_PROXY", "generated-ignore")
    monkeypatch.setattr(subprocess, "run", run)
    return calls


def test_accepts_an_exact_verified_attestation_result(
    attestation: _AttestationInputs, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _stub_github_cli(attestation, monkeypatch)
    assert attestation.verify() == hashlib.sha256(attestation.manifest.read_bytes()).hexdigest()
    assert len(calls) == 2


@pytest.mark.parametrize(
    ("claim", "actual"),
    [
        ("sourceRepositoryIdentifier", None),
        ("sourceRepositoryIdentifier", "123456789"),
        ("sourceRepositoryIdentifier", 1455972360),
        ("sourceRepositoryURI", "https://github.com/other/repo"),
        ("sourceRepositoryRef", "refs/heads/Generated-ref"),
        ("sourceRepositoryDigest", "2" * 40),
        ("issuer", "https://unapproved.invalid"),
        ("subjectAlternativeName", "https://unapproved.invalid"),
        ("buildConfigURI", "https://unapproved.invalid"),
        ("buildConfigDigest", "2" * 40),
        ("buildSignerURI", "https://unapproved.invalid"),
        ("buildSignerDigest", "2" * 40),
    ],
)
def test_rejects_a_verified_attestation_for_an_unapproved_identity(
    attestation: _AttestationInputs, monkeypatch: pytest.MonkeyPatch, claim: str, actual: object
) -> None:
    certificate = attestation.output["verificationResult"]["signature"]["certificate"]
    certificate[claim] = actual
    _stub_github_cli(attestation, monkeypatch)
    with pytest.raises(ReleaseVerificationError, match="does not match approved GitHub identity"):
        attestation.verify()


def test_rejects_replacing_local_manifest_bytes(
    attestation: _AttestationInputs, monkeypatch: pytest.MonkeyPatch
) -> None:
    attestation.manifest.write_bytes(_json({"id": "different-manifest"}))
    _stub_github_cli(attestation, monkeypatch)
    with pytest.raises(ReleaseVerificationError, match="does not name the manifest SHA-256"):
        attestation.verify()


def test_rejects_replacing_the_trusted_root_before_verification(
    attestation: _AttestationInputs, monkeypatch: pytest.MonkeyPatch
) -> None:
    attestation.trusted_root.write_bytes(b'{"generated":"different root"}\n')
    calls = _stub_github_cli(attestation, monkeypatch)
    with pytest.raises(ReleaseVerificationError, match="root does not match the approved SHA-256"):
        attestation.verify()
    assert not calls


def test_rejects_an_unqualified_cli_version(
    attestation: _AttestationInputs, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _stub_github_cli(attestation, monkeypatch, version=b"gh version 2.91.1\n")
    with pytest.raises(ReleaseVerificationError, match="Expected approved GitHub CLI"):
        attestation.verify()
    assert len(calls) == 1


def test_rejects_a_cryptographic_verification_failure(
    attestation: _AttestationInputs, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_github_cli(
        attestation, monkeypatch, verify_exit=1, verify_output=_json([attestation.output])
    )
    with pytest.raises(ReleaseVerificationError, match="offline verification failed"):
        attestation.verify()


@pytest.mark.parametrize("output", [b"", b"[]", b"[{}, {}]", b'[{"verificationResult":{}}]'])
def test_rejects_missing_or_malformed_verified_output(
    attestation: _AttestationInputs, monkeypatch: pytest.MonkeyPatch, output: bytes
) -> None:
    _stub_github_cli(attestation, monkeypatch, verify_output=output)
    with pytest.raises(
        ReleaseVerificationError, match="verification result|verified GitHub attestation"
    ):
        attestation.verify()
