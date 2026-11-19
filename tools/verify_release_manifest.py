"""Verify local OCI content or the GitHub attestation of exact local manifest bytes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from io import BufferedReader, FileIO
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

_INDEX = "application/vnd.oci.image.index.v1+json"
_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
_IMAGE_TYPES = {_INDEX, _MANIFEST}
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_MAX_METADATA_BYTES = 16 * 1024 * 1024
_READ_CHUNK = 1024 * 1024

_GITHUB_CLI_VERSION = "2.91.0"
_GITHUB_REPOSITORY = "kevon-dot/opentour-ai"
_GITHUB_REPOSITORY_ID = "1455972360"
_GITHUB_OIDC_ISSUER = "https://token.actions.githubusercontent.com"
_PROVENANCE_TYPE = "https://slsa.dev/provenance/v1"
_ATTESTATION_RESULT_TYPE = "application/vnd.dev.sigstore.verificationresult+json;version=0.1"


class ReleaseVerificationError(ValueError):
    """Local release content or its attestation could not be verified."""


@dataclass(frozen=True)
class _Descriptor:
    digest: str
    size: int
    media_type: str


def _fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    fields = dict(pairs)
    if len(fields) != len(pairs):
        raise ReleaseVerificationError("Duplicate keys in OCI JSON")
    return fields


def _json_object(data: bytes) -> dict[str, Any]:
    try:
        result: object = json.loads(data.decode("utf-8"), object_pairs_hook=_fields)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReleaseVerificationError("Invalid OCI JSON") from error
    if not isinstance(result, dict):
        raise ReleaseVerificationError("Expected an OCI JSON object")
    return result


@contextmanager
def _directory(path: Path | str, parent: int | None = None) -> Iterator[int]:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    except OSError as error:
        raise ReleaseVerificationError("Missing or unreadable local OCI directory") from error
    try:
        yield fd
    finally:
        os.close(fd)


def _open_file(directory: int | None, name: str) -> BufferedReader:
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except OSError as error:
        raise ReleaseVerificationError("Missing or unreadable OCI file") from error
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise ReleaseVerificationError("OCI content must be a regular file")
    return BufferedReader(FileIO(fd, mode="rb", closefd=True))


def _read_metadata(stream: BufferedReader) -> bytes:
    data = stream.read(_MAX_METADATA_BYTES + 1)
    if len(data) > _MAX_METADATA_BYTES:
        raise ReleaseVerificationError("OCI metadata exceeds the local verification limit")
    return data


def _descriptor(value: object) -> _Descriptor:
    if not isinstance(value, dict):
        raise ReleaseVerificationError("Expected an OCI content descriptor")
    digest, size, media_type = value.get("digest"), value.get("size"), value.get("mediaType")
    if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
        raise ReleaseVerificationError("OCI descriptor needs a canonical SHA-256 digest")
    if type(size) is not int or size < 0 or not isinstance(media_type, str) or not media_type:
        raise ReleaseVerificationError("Invalid OCI descriptor size or media type")
    return _Descriptor(digest, size, media_type)


def _descriptors(value: object) -> list[_Descriptor]:
    if not isinstance(value, list):
        raise ReleaseVerificationError("Expected a list of OCI content descriptors")
    return [_descriptor(item) for item in value]


def _document(data: bytes, media_type: str) -> dict[str, Any]:
    result = _json_object(data)
    version = result.get("schemaVersion")
    if type(version) is not int or version != 2:
        raise ReleaseVerificationError("Unsupported OCI document version")
    if result.get("mediaType", media_type) != media_type:
        raise ReleaseVerificationError("OCI document type does not match its descriptor")
    return result


def _verify_blob(blobs: int, descriptor: _Descriptor, *, document: bool) -> bytes:
    expected = descriptor.digest.removeprefix("sha256:")
    if document and descriptor.size > _MAX_METADATA_BYTES:
        raise ReleaseVerificationError("OCI metadata exceeds the local verification limit")
    with _open_file(blobs, expected) as stream:
        if os.fstat(stream.fileno()).st_size != descriptor.size:
            raise ReleaseVerificationError("OCI blob size mismatch")
        hasher = hashlib.sha256()
        if document:
            data = stream.read(descriptor.size)
            if len(data) != descriptor.size:
                raise ReleaseVerificationError("OCI blob size changed while reading")
            hasher.update(data)
        else:
            data = b""
            remaining = descriptor.size
            while remaining:
                chunk = stream.read(min(remaining, _READ_CHUNK))
                if not chunk:
                    raise ReleaseVerificationError("OCI blob size changed while reading")
                hasher.update(chunk)
                remaining -= len(chunk)
        if stream.read(1):
            raise ReleaseVerificationError("OCI blob size changed while reading")
    if hasher.hexdigest() != expected:
        raise ReleaseVerificationError("OCI blob digest mismatch")
    return data


def verify_oci_layout(layout: Path, expected_digest: str) -> None:
    """Verify one approved SHA-256 image and its locally available OCI content.

    The caller establishes the digest's authority separately. An approved digest can name
    the layout index bytes or a descriptor in its top-level index. This reads local files
    only, and an incomplete export fails even if its descriptors advertise remote blob URLs.
    Each JSON metadata document is limited to 16 MiB. Layers are streamed.
    """
    if _DIGEST.fullmatch(expected_digest) is None:
        raise ReleaseVerificationError("Expected a canonical SHA-256 image digest")
    with _directory(layout) as layout_fd:
        with _open_file(layout_fd, "oci-layout") as stream:
            if _json_object(_read_metadata(stream)).get("imageLayoutVersion") != "1.0.0":
                raise ReleaseVerificationError("Unsupported OCI image layout version")
        with _open_file(layout_fd, "index.json") as stream:
            index = _read_metadata(stream)
        with _directory("blobs", layout_fd) as blobs_fd, _directory("sha256", blobs_fd) as sha_fd:
            _verify_reachable(sha_fd, index, expected_digest)


def _verify_reachable(blobs: int, index: bytes, expected_digest: str) -> None:
    candidates = _descriptors(_document(index, _INDEX).get("manifests"))
    if f"sha256:{hashlib.sha256(index).hexdigest()}" == expected_digest:
        roots = candidates
    else:
        roots = list({item for item in candidates if item.digest == expected_digest})
        if len(roots) != 1:
            raise ReleaseVerificationError("Image digest is missing or ambiguous in the OCI index")
    if not roots:
        raise ReleaseVerificationError("The OCI index has no images")

    pending = [(root, True) for root in roots]
    visited: set[tuple[_Descriptor, bool]] = set()
    while pending:
        desc, document = pending.pop()
        if (desc, document) in visited:
            continue
        if document and desc.media_type not in _IMAGE_TYPES:
            raise ReleaseVerificationError("Unsupported OCI image descriptor type")
        data = _verify_blob(blobs, desc, document=document)
        visited.add((desc, document))
        if not document:
            continue
        doc = _document(data, desc.media_type)
        if desc.media_type == _INDEX:
            children = _descriptors(doc.get("manifests"))
            if not children:
                raise ReleaseVerificationError("The OCI index has no images")
            pending.extend((child, True) for child in children)
        else:
            pending.append((_descriptor(doc.get("config")), False))
            pending.extend((layer, False) for layer in _descriptors(doc.get("layers")))


@dataclass(frozen=True)
class _AttestationPolicy:
    oidc_issuer: str
    workflow_path: str
    source_ref: str
    source_commit: str
    trusted_root_sha256: str

    @property
    def workflow_uri(self) -> str:
        return f"https://github.com/{_GITHUB_REPOSITORY}/{self.workflow_path}@{self.source_ref}"


def _read_attestation_input(path: Path, label: str) -> bytes:
    try:
        with _open_file(None, str(path)) as stream:
            data = stream.read(_MAX_METADATA_BYTES + 1)
    except (OSError, ReleaseVerificationError) as error:
        raise ReleaseVerificationError(f"{label} must be a readable local regular file") from error
    if not data or len(data) > _MAX_METADATA_BYTES:
        raise ReleaseVerificationError(f"{label} is empty or exceeds the local verification limit")
    return data


def _attestation_json(data: bytes, label: str) -> object:
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=_fields)
    except (ValueError, RecursionError) as error:
        raise ReleaseVerificationError(f"Invalid {label} JSON") from error


def _load_attestation_policy(path: Path) -> _AttestationPolicy:
    label = "Approved attestation policy"
    fields = _attestation_json(_read_attestation_input(path, label), label.lower())
    if not isinstance(fields, dict) or set(fields) != set(_AttestationPolicy.__annotations__):
        raise ReleaseVerificationError("Expected the five approved attestation policy fields")
    policy = _AttestationPolicy(**fields)
    if (
        not all(isinstance(value, str) for value in fields.values())
        or policy.oidc_issuer != _GITHUB_OIDC_ISSUER
        or re.fullmatch(r"\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml", policy.workflow_path) is None
        or re.fullmatch(r"refs/(heads|tags)/[A-Za-z0-9_./-]+", policy.source_ref) is None
        or re.fullmatch(r"[0-9a-f]{40}", policy.source_commit) is None
        or re.fullmatch(r"[0-9a-f]{64}", policy.trusted_root_sha256) is None
    ):
        raise ReleaseVerificationError(
            "Invalid approved GitHub issuer, workflow, commit, or root pin"
        )
    return policy


def _run_github_cli(executable: Path, directory: Path, *args: str) -> bytes:
    # Supply fresh local settings and exclude inherited tokens, host and proxy settings.
    environment = {
        "HOME": str(directory),
        "GH_CONFIG_DIR": str(directory / "gh-config"),
        "GH_PROMPT_DISABLED": "1",
        "GH_NO_UPDATE_NOTIFIER": "1",
        "GH_NO_EXTENSION_UPDATE_NOTIFIER": "1",
    }
    try:
        result = subprocess.run(
            [str(executable), *args],
            cwd=directory,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ReleaseVerificationError("GitHub CLI offline verification failed") from error
    if len(result.stdout) > _MAX_METADATA_BYTES:
        raise ReleaseVerificationError("GitHub CLI output exceeds the local verification limit")
    return result.stdout


def _verify_attestation_result(
    output: bytes,
    policy: _AttestationPolicy,
    manifest_sha256: str,
) -> None:
    results = _attestation_json(output, "GitHub CLI verification result")
    if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict):
        raise ReleaseVerificationError("Expected one verified GitHub attestation")
    result = results[0].get("verificationResult")
    if not isinstance(result, dict) or result.get("mediaType") != _ATTESTATION_RESULT_TYPE:
        raise ReleaseVerificationError("Missing GitHub CLI verification result")
    signature = result.get("signature")
    certificate = signature.get("certificate") if isinstance(signature, dict) else None
    expected_claims = {
        "issuer": policy.oidc_issuer,
        "subjectAlternativeName": policy.workflow_uri,
        "sourceRepositoryURI": f"https://github.com/{_GITHUB_REPOSITORY}",
        "sourceRepositoryIdentifier": _GITHUB_REPOSITORY_ID,
        "sourceRepositoryRef": policy.source_ref,
        "sourceRepositoryDigest": policy.source_commit,
        "buildConfigURI": policy.workflow_uri,
        "buildConfigDigest": policy.source_commit,
        "buildSignerURI": policy.workflow_uri,
        "buildSignerDigest": policy.source_commit,
    }
    # Check the authenticated certificate summary, including the repository ID and exact
    # source ref. The 2.91.0 flag check alone compares the source ref case-insensitively.
    if not isinstance(certificate, dict) or any(
        certificate.get(key) != value for key, value in expected_claims.items()
    ):
        raise ReleaseVerificationError(
            "Verified certificate does not match approved GitHub identity"
        )
    statement = result.get("statement")
    if not isinstance(statement, dict) or statement.get("predicateType") != _PROVENANCE_TYPE:
        raise ReleaseVerificationError("Missing verified SLSA provenance statement")
    subjects = statement.get("subject")
    if not isinstance(subjects, list) or not any(
        isinstance(subject, dict)
        and isinstance(subject.get("digest"), dict)
        and subject["digest"].get("sha256") == manifest_sha256
        for subject in subjects
    ):
        raise ReleaseVerificationError("Verified attestation does not name the manifest SHA-256")


def verify_manifest_attestation(
    manifest: Path,
    *,
    bundle: Path,
    trusted_root: Path,
    approved_policy: Path,
    github_cli: Path,
) -> str:
    """Return the SHA-256 of exact local manifest bytes with verified SLSA provenance.

    Supply the policy and pinned-version GitHub CLI executable from a trusted
    source independent of the manifest and bundle. This supports one local Sigstore
    JSON bundle signed by a direct OpenTour GitHub Actions workflow at the approved
    ref and commit. OCI content and release eligibility have separate checks.
    """
    if not github_cli.is_absolute():
        raise ReleaseVerificationError("Expected an absolute path to the approved GitHub CLI")
    policy = _load_attestation_policy(approved_policy)
    root_bytes = _read_attestation_input(trusted_root, "Trusted root")
    if hashlib.sha256(root_bytes).hexdigest() != policy.trusted_root_sha256:
        raise ReleaseVerificationError("Trusted root does not match the approved SHA-256")
    manifest_bytes = _read_attestation_input(manifest, "Manifest")
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    bundle_bytes = _read_attestation_input(bundle, "Attestation bundle")
    if not isinstance(_attestation_json(bundle_bytes, "attestation bundle"), dict):
        raise ReleaseVerificationError("Expected one local Sigstore JSON bundle")
    # Verify the same bytes that were read and hashed, even if the caller's paths change.
    with TemporaryDirectory(prefix="opentour-attestation-") as temporary:
        workspace = Path(temporary).absolute()
        (workspace / "manifest.json").write_bytes(manifest_bytes)
        (workspace / "attestation.sigstore.json").write_bytes(bundle_bytes)
        (workspace / "trusted_root.jsonl").write_bytes(root_bytes)
        version = _run_github_cli(github_cli, workspace, "--version").splitlines()
        if not version or version[0].split()[:3] != [
            b"gh",
            b"version",
            _GITHUB_CLI_VERSION.encode(),
        ]:
            raise ReleaseVerificationError(f"Expected approved GitHub CLI {_GITHUB_CLI_VERSION}")
        output = _run_github_cli(
            github_cli,
            workspace,
            "attestation",
            "verify",
            "./manifest.json",
            "--hostname",
            "github.com",
            "--repo",
            _GITHUB_REPOSITORY,
            "--bundle",
            "./attestation.sigstore.json",
            "--custom-trusted-root",
            "./trusted_root.jsonl",
            "--cert-oidc-issuer",
            policy.oidc_issuer,
            "--cert-identity",
            policy.workflow_uri,
            "--source-digest",
            policy.source_commit,
            "--source-ref",
            policy.source_ref,
            "--signer-digest",
            policy.source_commit,
            "--digest-alg",
            "sha256",
            "--predicate-type",
            _PROVENANCE_TYPE,
            "--format",
            "json",
        )
    _verify_attestation_result(output, policy, manifest_sha256)
    return manifest_sha256


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--oci-layout", type=Path)
    mode.add_argument("--manifest", type=Path, help="Exact local manifest bytes to authenticate")
    parser.add_argument("--image-digest")
    parser.add_argument("--bundle", type=Path, help="Local Sigstore JSON attestation bundle")
    parser.add_argument("--trusted-root", type=Path, help="Local GitHub CLI trusted_root.jsonl")
    parser.add_argument(
        "--approved-policy",
        type=Path,
        help="Independent JSON with oidc_issuer, workflow_path, source_ref, source_commit and "
        "trusted_root_sha256",
    )
    parser.add_argument(
        "--github-cli",
        type=Path,
        help=f"Absolute path to the approved GitHub CLI {_GITHUB_CLI_VERSION} executable",
    )
    args = parser.parse_args()
    attestation_inputs = (args.bundle, args.trusted_root, args.approved_policy, args.github_cli)
    try:
        if args.manifest is not None:
            if args.image_digest is not None or None in attestation_inputs:
                parser.error(
                    "--manifest requires --bundle, --trusted-root, --approved-policy "
                    "and --github-cli"
                )
            digest = verify_manifest_attestation(
                args.manifest,
                bundle=args.bundle,
                trusted_root=args.trusted_root,
                approved_policy=args.approved_policy,
                github_cli=args.github_cli,
            )
            print(f"Verified GitHub attestation for local manifest sha256:{digest}")
        else:
            if args.image_digest is None or any(item is not None for item in attestation_inputs):
                parser.error("--oci-layout requires --image-digest")
            verify_oci_layout(args.oci_layout, args.image_digest)
            print(f"Verified local OCI content for {args.image_digest}")
    except ReleaseVerificationError as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
