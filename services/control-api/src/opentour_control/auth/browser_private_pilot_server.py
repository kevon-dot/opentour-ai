"""Run the reviewed dev-stage browser listener on a separate private task."""

from __future__ import annotations

import asyncio
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

import httpx2 as httpx
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
_METADATA_LIMIT = 32768
_METADATA_TIMEOUT = 2.0
_ECS_METADATA_URI = re.compile(r"http://169\.254\.170\.2/v4/[A-Za-z0-9_-]{1,128}")
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


def _approved_subnets(raw: str) -> frozenset[ipaddress.IPv4Network]:
    if len(raw) > 2048:
        raise ValueError("Invalid approved listener subnets")
    values: object = json.loads(raw)
    if not isinstance(values, list) or not 1 <= len(values) <= 32:
        raise ValueError("Invalid approved listener subnets")
    networks = []
    for value in values:
        network = ipaddress.IPv4Network(_text(value))
        if str(network) != value or not any(network.subnet_of(block) for block in PRIVATE_NETWORKS):
            raise ValueError("Expected canonical private listener subnets")
        networks.append(network)
    if len(set(networks)) != len(networks):
        raise ValueError("Duplicate approved listener subnets")
    return frozenset(networks)


async def _ecs_listener_ipv4(
    uri: str,
    subnets: frozenset[ipaddress.IPv4Network],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ipaddress.IPv4Address:
    if _ECS_METADATA_URI.fullmatch(uri) is None:
        raise ValueError("Invalid ECS container metadata endpoint")
    body = bytearray()
    try:
        async with (
            asyncio.timeout(_METADATA_TIMEOUT),
            httpx.AsyncClient(
                trust_env=False,
                follow_redirects=False,
                timeout=_METADATA_TIMEOUT,
                transport=transport,
            ) as client,
            client.stream(
                "GET", uri, headers={"Accept": "application/json", "Accept-Encoding": "identity"}
            ) as response,
        ):
            media = response.headers.get("content-type", "").partition(";")[0].strip().lower()
            encoding = response.headers.get("content-encoding", "identity").strip().lower()
            if response.status_code != 200 or media != "application/json" or encoding != "identity":
                raise ValueError("Invalid ECS container metadata response")
            async for chunk in response.aiter_raw():
                if len(chunk) > _METADATA_LIMIT - len(body):
                    raise ValueError("ECS container metadata exceeds its input bound")
                body.extend(chunk)
    except (TimeoutError, httpx.HTTPError):
        raise ValueError("ECS container metadata unavailable") from None
    document: object = json.loads(body.decode("utf-8"), object_pairs_hook=_unique)
    networks = document.get("Networks") if isinstance(document, dict) else None
    if not isinstance(networks, list) or len(networks) != 1:
        raise ValueError("Expected one ECS container network")
    network = networks[0]
    if not isinstance(network, dict) or network.get("NetworkMode") != "awsvpc":
        raise ValueError("Expected an ECS awsvpc container network")
    addresses = network.get("IPv4Addresses")
    if not isinstance(addresses, list) or len(addresses) != 1:
        raise ValueError("Expected one ECS container IPv4 address")
    cidr = _text(network.get("IPv4SubnetCIDRBlock"))
    subnet = ipaddress.IPv4Network(cidr)
    host = ipaddress.IPv4Address(_text(addresses[0]))
    if (
        str(subnet) != cidr
        or subnet not in subnets
        or host not in subnet
        or host in (subnet.network_address, subnet.broadcast_address)
    ):
        raise ValueError("ECS container address is outside the approved listener subnet")
    return host


def _listener_host(env: Mapping[str, str], *, allow_metadata: bool) -> ipaddress.IPv4Address:
    requested = _setting(env, "LISTEN_HOST")
    if requested != "ecs-task-ipv4":
        return ipaddress.IPv4Address(requested)
    if not allow_metadata or env.get("AWS_EXECUTION_ENV") != "AWS_ECS_FARGATE":
        raise ValueError("ECS listener address requires an explicitly configured Fargate startup")
    subnets = _approved_subnets(_setting(env, "LISTEN_CIDRS"))
    return asyncio.run(_ecs_listener_ipv4(env.get("ECS_CONTAINER_METADATA_URI_V4", ""), subnets))


def _load_deployment(env: Mapping[str, str], *, allow_metadata: bool) -> _Deployment:
    approved = _binding(_pinned_document(env, "BINDING"))
    observed = _closed(_pinned_document(env, "READBACK"), {"binding", "verified_at", "valid_until"})
    port = _setting(env, "LISTEN_PORT")
    if not re.fullmatch(r"[1-9][0-9]{3,4}", port) or not 1024 <= int(port) <= 65535:
        raise ValueError("Invalid private listener port")
    host = _listener_host(env, allow_metadata=allow_metadata)
    if not any(host in network for network in PRIVATE_NETWORKS):
        raise ValueError("Expected the task's private IPv4 address")
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


def _write_readiness_checkpoint(empty_lookup: bool, session_invalid: bool) -> None:
    # Fixed, short records fit in an atomic nonblocking pipe write.
    record = json.dumps(
        {
            "kind": "control_readiness_checkpoint",
            "listener": "CURRENT_ADMITTED",
            "lookup": "EMPTY" if empty_lookup else "UNCONFIRMED",
            "response": "SESSION_INVALID_ASGI_SENT" if session_invalid else "UNCONFIRMED",
        },
        separators=(",", ":"),
    ).encode("ascii")
    try:
        stdout = os.fstat(1)
        if not stat.S_ISFIFO(stdout.st_mode):
            return
        fd = os.open("/proc/self/fd/1", os.O_WRONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            sink = os.fstat(fd)
            if stat.S_ISFIFO(sink.st_mode) and os.path.samestat(stdout, sink):
                os.write(fd, record + b"\n")
        finally:
            os.close(fd)
    except OSError:
        return


def main(argv: Sequence[str] = ()) -> None:
    arguments = tuple(argv)
    check_tls = arguments == ("--check-tls",)
    readiness_checkpoint = arguments == ("--readiness-checkpoint",)
    if arguments and not (check_tls or readiness_checkpoint):
        raise SystemExit("Invalid private browser listener argument")
    try:
        if readiness_checkpoint and (
            os.environ.get(_PREFIX + "LISTEN_HOST") != "ecs-task-ipv4"
            or os.environ.get("AWS_EXECUTION_ENV") != "AWS_ECS_FARGATE"
        ):
            raise ValueError("Readiness checkpoint requires the private Fargate task")
        deployment = _load_deployment(os.environ, allow_metadata=not check_tls)
        if (check_tls or readiness_checkpoint) and not is_private_browser_dev_stage(
            deployment.approved.stage_arn
        ):
            raise ValueError("Expected the exact dev stage")
        if check_tls:
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
                readiness_checkpoint=_write_readiness_checkpoint if readiness_checkpoint else None,
            )
        except ValueError:
            raise SystemExit(_UNAVAILABLE) from None

    serve_auth_runtime(runtime, configure)


if __name__ == "__main__":
    main(sys.argv[1:])
