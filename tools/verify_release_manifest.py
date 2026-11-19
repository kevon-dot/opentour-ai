"""Check the local OCI content addressed by an independently approved image digest."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from io import BufferedReader, FileIO
from pathlib import Path
from typing import Any

_INDEX = "application/vnd.oci.image.index.v1+json"
_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
_IMAGE_TYPES = {_INDEX, _MANIFEST}
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_MAX_METADATA_BYTES = 16 * 1024 * 1024
_READ_CHUNK = 1024 * 1024


class ReleaseVerificationError(ValueError):
    """A local image cannot be verified against its expected content address."""


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


def _open_file(directory: int, name: str) -> BufferedReader:
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--oci-layout", required=True, type=Path)
    parser.add_argument("--image-digest", required=True)
    args = parser.parse_args()
    try:
        verify_oci_layout(args.oci_layout, args.image_digest)
    except ReleaseVerificationError as error:
        parser.error(str(error))
    print(f"Verified local OCI content for {args.image_digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
