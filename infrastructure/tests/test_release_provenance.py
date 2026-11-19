"""Local OCI release input checks with generated images and no network."""

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from tools import verify_release_manifest
from tools.verify_release_manifest import ReleaseVerificationError, verify_oci_layout

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
