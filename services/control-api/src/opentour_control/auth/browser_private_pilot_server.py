"""Run the reviewed dev-stage browser listener on a separate private task."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import stat
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import psycopg
import uvicorn

from opentour_control.api.server import configure_auth_runtime

from .browser_private_listener import (
    PRIVATE_NETWORKS,
    ReviewedStageReadback,
    StageClientBinding,
    prepare_private_browser_listener_tls,
)
from .browser_private_pilot import PrivateBrowserPilotPathProfile
from .browser_private_pilot_listener import (
    build_private_browser_pilot_listener,
    is_private_browser_dev_stage,
)
from .config import AuthError
from .runtime_server import serve_auth_runtime

_PREFIX = "OPENTOUR_PRIVATE_BROWSER_"
_DOCUMENT_LIMIT = 8192
_TLS_FILE_LIMIT = 65536
_BINDING_FIELDS = {
    "stage_arn",
    "client_certificate_id",
    "client_certificate_sha256",
    "backend_server_name",
}
_UNAVAILABLE = "Private browser listener deployment configuration is missing or invalid"


@dataclass(frozen=True)
class _Deployment:
    approved: StageClientBinding
    readback: ReviewedStageReadback
    stage_leaf_pem: bytes
    server_certfile: Path
    server_keyfile: Path
    host: str
    port: int


def _setting(env: Mapping[str, str], suffix: str) -> str:
    value = env.get(_PREFIX + suffix)
    if not value:
        raise ValueError("Missing private deployment setting")
    return value


def _mount(env: Mapping[str, str], suffix: str, limit: int) -> Path:
    path = Path(_setting(env, suffix))
    if not path.is_absolute():
        raise ValueError("Expected an absolute deployment mount path")
    details = path.lstat()
    if not stat.S_ISREG(details.st_mode) or not 0 < details.st_size <= limit:
        raise ValueError("Expected a bounded regular deployment file")
    return path


def _read_mount(path: Path, limit: int) -> bytes:
    with path.open("rb") as stream:
        content = stream.read(limit + 1)
    if not 0 < len(content) <= limit:
        raise ValueError("Deployment file exceeds its input bound")
    return content


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    document = dict(pairs)
    if len(document) != len(pairs):
        raise ValueError("Repeated deployment field")
    return document


def _pinned_document(env: Mapping[str, str], name: str) -> object:
    expected = _setting(env, f"{name}_SHA256")
    document = _read_mount(_mount(env, f"{name}_FILE", _DOCUMENT_LIMIT), _DOCUMENT_LIMIT)
    if hashlib.sha256(document).hexdigest() != expected:
        raise ValueError("Deployment document digest does not match")
    parsed: object = json.loads(document.decode("utf-8"), object_pairs_hook=_unique)
    return parsed


def _closed(value: object, fields: set[str]) -> dict[str, object]:
    if not isinstance(value, dict) or value.keys() != fields:
        raise ValueError("Invalid deployment document fields")
    return value


def _text(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[!-~]{1,512}", value):
        raise ValueError("Invalid deployment document value")
    return value


def _binding(value: object) -> StageClientBinding:
    document = _closed(value, _BINDING_FIELDS)
    return StageClientBinding(
        stage_arn=_text(document["stage_arn"]),
        client_certificate_id=_text(document["client_certificate_id"]),
        client_certificate_sha256=_text(document["client_certificate_sha256"]),
        backend_server_name=_text(document["backend_server_name"]),
    )


def _readback_time(value: object) -> datetime:
    instant = datetime.fromisoformat(_text(value))
    if instant.utcoffset() is None:
        raise ValueError("Reviewed readback requires an explicit timezone")
    return instant


def _load_deployment(env: Mapping[str, str]) -> _Deployment:
    approved = _binding(_pinned_document(env, "BINDING"))
    observed = _closed(_pinned_document(env, "READBACK"), {"binding", "verified_at", "valid_until"})
    host = ipaddress.IPv4Address(_setting(env, "LISTEN_HOST"))
    if not any(host in network for network in PRIVATE_NETWORKS):
        raise ValueError("Expected the task's private IPv4 address")
    port = _setting(env, "LISTEN_PORT")
    if not re.fullmatch(r"[1-9][0-9]{3,4}", port):
        raise ValueError("Invalid private listener port")
    return _Deployment(
        approved=approved,
        readback=ReviewedStageReadback(
            binding=_binding(observed["binding"]),
            verified_at=_readback_time(observed["verified_at"]),
            valid_until=_readback_time(observed["valid_until"]),
        ),
        stage_leaf_pem=_read_mount(
            _mount(env, "STAGE_LEAF_FILE", _TLS_FILE_LIMIT), _TLS_FILE_LIMIT
        ),
        server_certfile=_mount(env, "SERVER_CERT_FILE", _TLS_FILE_LIMIT),
        server_keyfile=_mount(env, "SERVER_KEY_FILE", _TLS_FILE_LIMIT),
        host=str(host),
        port=int(port),
    )


def main(argv: Sequence[str] = ()) -> None:
    if argv and tuple(argv) != ("--check-tls",):
        raise SystemExit("Invalid private browser listener argument")
    try:
        deployment = _load_deployment(os.environ)
        if argv:
            if not is_private_browser_dev_stage(deployment.approved.stage_arn):
                raise ValueError("Expected the exact dev stage")
            prepare_private_browser_listener_tls(
                approved=deployment.approved,
                readback=deployment.readback,
                stage_leaf_pem=deployment.stage_leaf_pem,
                server_certfile=deployment.server_certfile,
                server_keyfile=deployment.server_keyfile,
                host=deployment.host,
                port=deployment.port,
            )
            print("Private browser TLS configuration verified")
            return
        runtime, _ = configure_auth_runtime(os.environ)
    except (AuthError, OSError, ValueError, RecursionError, psycopg.Error):
        raise SystemExit(_UNAVAILABLE) from None

    def configure() -> uvicorn.Config:
        try:
            return build_private_browser_pilot_listener(
                runtime.browser_state,
                approved=deployment.approved,
                readback=deployment.readback,
                stage_leaf_pem=deployment.stage_leaf_pem,
                server_certfile=deployment.server_certfile,
                server_keyfile=deployment.server_keyfile,
                host=deployment.host,
                port=deployment.port,
                path_profile=PrivateBrowserPilotPathProfile.DEV,
            )
        except ValueError:
            raise SystemExit(_UNAVAILABLE) from None

    serve_auth_runtime(runtime, configure)


if __name__ == "__main__":
    main(sys.argv[1:])
