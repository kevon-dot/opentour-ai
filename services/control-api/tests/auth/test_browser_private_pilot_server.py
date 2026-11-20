"""Offline startup proof for the separate private browser listener process."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
import ssl
import stat
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import httpx2 as httpx
import pytest
import uvicorn
from opentour_control.auth import browser_private_pilot_server as server
from test_browser_private_listener import tls_fixture as tls_fixture

PREFIX = "OPENTOUR_PRIVATE_BROWSER_"
METADATA_URI = "http://169.254.170.2/v4/generated-fixture"
SUBNET = "10.0.10.0/24"
LISTEN_ADDRESS = "10.0.10.20"


def _network(**overrides: object) -> dict[str, object]:
    return {
        "NetworkMode": "awsvpc",
        "IPv4Addresses": [LISTEN_ADDRESS],
        "IPv4SubnetCIDRBlock": SUBNET,
        **overrides,
    }


def _reply(body: bytes, *, status: int = 200, **headers: str) -> httpx.Response:
    return httpx.Response(
        status,
        headers={"content-type": "application/json", **headers},
        stream=httpx.ByteStream(body),
    )


def test_reads_only_own_container_metadata_and_approved_address() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert str(request.url) == METADATA_URI
        assert request.headers["Accept-Encoding"] == "identity"
        assert "authorization" not in request.headers
        return _reply(json.dumps({"Networks": [_network()], "UnrelatedField": True}).encode())

    approved = server._approved_subnets(json.dumps([SUBNET, "10.0.11.0/24"]))
    result = asyncio.run(
        server._ecs_listener_ipv4(METADATA_URI, approved, transport=httpx.MockTransport(respond))
    )
    assert str(result) == LISTEN_ADDRESS


@pytest.mark.parametrize(
    "uri",
    [
        "http://169.254.169.254/v4/generated-fixture",
        "http://169.254.170.2:8080/v4/generated-fixture",
        "http://169.254.170.2/v4/generated-fixture/task",
        "http://169.254.170.2/v4/generated-fixture?target=foreign",
        "http://169.254.170.2/v4/%2F",
    ],
)
def test_wrong_metadata_address_is_refused_before_transport(uri: str) -> None:
    respond = Mock(side_effect=AssertionError("unexpected transport"))
    approved = server._approved_subnets(json.dumps([SUBNET]))
    with pytest.raises(ValueError, match="Invalid ECS container metadata endpoint"):
        asyncio.run(
            server._ecs_listener_ipv4(uri, approved, transport=httpx.MockTransport(respond))
        )
    respond.assert_not_called()


@pytest.mark.parametrize(
    "networks",
    [
        [_network(), _network()],
        [_network(NetworkMode="bridge")],
        [_network(IPv4Addresses=[LISTEN_ADDRESS, "10.0.10.21"])],
        [_network(IPv4SubnetCIDRBlock="10.0.0.0/16")],
        [_network(IPv4Addresses=["10.0.11.20"])],
        [_network(IPv4Addresses=["10.0.10.255"])],
    ],
)
def test_disallows_ambiguous_or_unapproved_eni(networks: list[dict[str, object]]) -> None:
    approved = server._approved_subnets(json.dumps([SUBNET]))
    transport = httpx.MockTransport(
        lambda _request: _reply(json.dumps({"Networks": networks}).encode())
    )
    with pytest.raises(ValueError):
        asyncio.run(server._ecs_listener_ipv4(METADATA_URI, approved, transport=transport))


@pytest.mark.parametrize(
    ("body", "status", "headers"),
    [
        (b"", 302, {"location": "http://example.invalid"}),
        (b" " * 32769, 200, {}),
        (b"{}", 200, {"content-encoding": "gzip"}),
        (b'{"Networks":[],"Networks":[]}', 200, {}),
    ],
)
def test_metadata_transport_refuses_redirect_and_unusable_bytes(
    body: bytes, status: int, headers: dict[str, str]
) -> None:
    approved = server._approved_subnets(json.dumps([SUBNET]))
    respond = Mock(side_effect=lambda _request: _reply(body, status=status, **headers))
    with pytest.raises(ValueError):
        asyncio.run(
            server._ecs_listener_ipv4(
                METADATA_URI, approved, transport=httpx.MockTransport(respond)
            )
        )
    assert respond.call_count == 1


def test_metadata_timeout_is_total(monkeypatch: pytest.MonkeyPatch) -> None:
    async def wait(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1)
        raise AssertionError("deadline was not applied")

    monkeypatch.setattr(server, "_METADATA_TIMEOUT", 0.01)
    approved = server._approved_subnets(json.dumps([SUBNET]))
    with pytest.raises(ValueError, match="ECS container metadata unavailable"):
        asyncio.run(
            server._ecs_listener_ipv4(METADATA_URI, approved, transport=httpx.MockTransport(wait))
        )


@pytest.mark.parametrize(
    "value", ["[]", '["10.0.10.2/24"]', '["10.0.10.0/24","10.0.10.0/24"]', '["8.8.8.0/24"]']
)
def test_requires_canonical_distinct_private_subnets(value: str) -> None:
    with pytest.raises(ValueError):
        server._approved_subnets(value)


def test_task_metadata_is_opt_in_fargate_startup_only(monkeypatch: pytest.MonkeyPatch) -> None:
    resolve = AsyncMock(return_value=ipaddress.IPv4Address(LISTEN_ADDRESS))
    monkeypatch.setattr(server, "_ecs_listener_ipv4", resolve)
    env = {
        PREFIX + "LISTEN_HOST": LISTEN_ADDRESS,
        PREFIX + "LISTEN_CIDRS": json.dumps([SUBNET]),
        "AWS_EXECUTION_ENV": "AWS_ECS_FARGATE",
        "ECS_CONTAINER_METADATA_URI_V4": METADATA_URI,
    }
    assert str(server._listener_host(env, allow_metadata=True)) == LISTEN_ADDRESS
    resolve.assert_not_awaited()
    env[PREFIX + "LISTEN_HOST"] = "ecs-task-ipv4"
    with pytest.raises(ValueError):
        server._listener_host(env, allow_metadata=False)
    with pytest.raises(ValueError):
        server._listener_host({**env, "AWS_EXECUTION_ENV": "AWS_ECS_EC2"}, allow_metadata=True)
    resolve.assert_not_awaited()
    assert str(server._listener_host(env, allow_metadata=True)) == LISTEN_ADDRESS
    resolve.assert_awaited_once_with(METADATA_URI, frozenset({ipaddress.IPv4Network(SUBNET)}))


@pytest.fixture
def deployment(tls_fixture: Any, tmp_path: Path) -> dict[str, str]:
    _, stage, _, stage_pair, *_ = tls_fixture
    approved = replace(
        stage["approved"],
        stage_arn="arn:aws:apigateway:us-east-2::/restapis/fixtureapi/stages/dev",
    )
    binding = asdict(approved)
    readback = stage["readback"]
    documents = {
        "BINDING": binding,
        "READBACK": {
            "binding": binding,
            "verified_at": readback.verified_at.isoformat(),
            "valid_until": readback.valid_until.isoformat(),
        },
    }
    env = {
        "STAGE_LEAF_FILE": str(stage_pair[0]),
        "SERVER_CERT_FILE": str(stage["server_certfile"]),
        "SERVER_KEY_FILE": str(stage["server_keyfile"]),
        "LISTEN_HOST": "10.0.10.20",
        "LISTEN_PORT": "8443",
    }
    for name, document in documents.items():
        content = json.dumps(document).encode()
        path = tmp_path / f"{name.lower()}.json"
        path.write_bytes(content)
        env[f"{name}_FILE"] = str(path)
        env[f"{name}_SHA256"] = hashlib.sha256(content).hexdigest()
    return {PREFIX + key: value for key, value in env.items()}


@pytest.fixture
def launch(deployment: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> tuple[Mock, Mock]:
    for name, value in deployment.items():
        monkeypatch.setenv(name, value)
    runtime = Mock(close=Mock(return_value=True))
    configure = Mock(return_value=(runtime, "unused"))
    monkeypatch.setattr(server, "configure_auth_runtime", configure)
    return runtime, configure


@pytest.mark.parametrize(
    ("bind_source", "arguments"),
    [("explicit", ()), ("fargate", ()), ("fargate", ("--readiness-checkpoint",))],
)
def test_private_launcher_loads_real_reviewed_tls_configuration(
    bind_source: str,
    arguments: tuple[str, ...],
    launch: tuple[Mock, Mock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, configure = launch
    loaded: list[uvicorn.Config] = []
    resolve = AsyncMock(return_value=ipaddress.IPv4Address(LISTEN_ADDRESS))
    monkeypatch.setattr(server, "_ecs_listener_ipv4", resolve)
    build = Mock(wraps=server.build_private_browser_pilot_listener)
    monkeypatch.setattr(server, "build_private_browser_pilot_listener", build)
    if bind_source == "fargate":
        monkeypatch.setenv(PREFIX + "LISTEN_HOST", "ecs-task-ipv4")
        monkeypatch.setenv(PREFIX + "LISTEN_CIDRS", json.dumps([SUBNET]))
        monkeypatch.setenv("AWS_EXECUTION_ENV", "AWS_ECS_FARGATE")
        monkeypatch.setenv("ECS_CONTAINER_METADATA_URI_V4", METADATA_URI)

    def inspect(listener: uvicorn.Server) -> None:
        listener.config.load()
        loaded.append(listener.config)

    monkeypatch.setattr(uvicorn.Server, "run", inspect)
    server.main(arguments)
    (config,) = loaded
    assert (config.host, config.port, config.workers) == ("10.0.10.20", 8443, 1)
    assert config.ssl is not None
    assert config.ssl.verify_mode == ssl.CERT_REQUIRED
    assert config.ssl.cert_store_stats()["x509"] == 1
    assert config.proxy_headers is False
    assert config.timeout_graceful_shutdown == 20
    configure.assert_called_once()
    runtime.close.assert_called_once()
    assert resolve.await_count == (1 if bind_source == "fargate" else 0)
    expected_sink = server._write_readiness_checkpoint if arguments else None
    assert build.call_args.kwargs["readiness_checkpoint"] is expected_sink


@pytest.mark.parametrize(
    ("bind_host", "execution_env"),
    [(LISTEN_ADDRESS, "AWS_ECS_FARGATE"), ("ecs-task-ipv4", "AWS_ECS_EC2")],
)
def test_checkpoint_requires_private_fargate_binding(
    bind_host: str,
    execution_env: str,
    launch: tuple[Mock, Mock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, configure = launch
    monkeypatch.setenv(PREFIX + "LISTEN_HOST", bind_host)
    monkeypatch.setenv("AWS_EXECUTION_ENV", execution_env)
    load = Mock(wraps=server._load_deployment)
    monkeypatch.setattr(server, "_load_deployment", load)

    with pytest.raises(SystemExit, match="^Private browser listener deployment configuration"):
        server.main(["--readiness-checkpoint"])

    load.assert_not_called()
    configure.assert_not_called()


@pytest.fixture
def checkpoint_process(monkeypatch: pytest.MonkeyPatch) -> Mock:
    process = Mock(
        O_WRONLY=os.O_WRONLY,
        O_NONBLOCK=os.O_NONBLOCK,
        O_CLOEXEC=os.O_CLOEXEC,
        path=os.path,
    )
    process.open.return_value = 91
    monkeypatch.setattr(server, "os", process)
    return process


@pytest.mark.parametrize(
    ("empty", "session_invalid", "lookup", "response"),
    [
        (True, True, "EMPTY", "SESSION_INVALID_ASGI_SENT"),
        (False, False, "UNCONFIRMED", "UNCONFIRMED"),
        (False, True, "UNCONFIRMED", "SESSION_INVALID_ASGI_SENT"),
        (True, False, "EMPTY", "UNCONFIRMED"),
    ],
)
def test_checkpoint_writes_one_fixed_record_to_inherited_pipe(
    empty: bool,
    session_invalid: bool,
    lookup: str,
    response: str,
    checkpoint_process: Mock,
) -> None:
    pipe = SimpleNamespace(st_mode=stat.S_IFIFO, st_dev=12, st_ino=34)
    process = checkpoint_process
    process.fstat.side_effect = [pipe, pipe]

    server._write_readiness_checkpoint(empty, session_invalid)

    process.open.assert_called_once_with(
        "/proc/self/fd/1", os.O_WRONLY | os.O_NONBLOCK | os.O_CLOEXEC
    )
    assert [call.args for call in process.fstat.call_args_list] == [(1,), (91,)]
    process.write.assert_called_once()
    fd, payload = process.write.call_args.args
    assert fd == 91
    assert len(payload) <= 512
    assert payload.count(b"\n") == 1 and payload.endswith(b"\n")
    assert json.loads(payload) == {
        "kind": "control_readiness_checkpoint",
        "listener": "CURRENT_ADMITTED",
        "lookup": lookup,
        "response": response,
    }
    process.close.assert_called_once_with(91)
    process.set_blocking.assert_not_called()


@pytest.mark.parametrize(
    ("stdout_mode", "sink_inode", "open_error", "write_error"),
    [
        (stat.S_IFREG, 34, None, None),
        (stat.S_IFIFO, 35, None, None),
        (stat.S_IFIFO, 34, PermissionError, None),
        (stat.S_IFIFO, 34, None, BlockingIOError),
        (stat.S_IFIFO, 34, None, BrokenPipeError),
    ],
)
def test_checkpoint_does_not_fall_back_when_pipe_is_unavailable(
    stdout_mode: int,
    sink_inode: int,
    open_error: type[OSError] | None,
    write_error: type[OSError] | None,
    checkpoint_process: Mock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    process = checkpoint_process
    process.fstat.side_effect = [
        SimpleNamespace(st_mode=stdout_mode, st_dev=12, st_ino=34),
        SimpleNamespace(st_mode=stat.S_IFIFO, st_dev=12, st_ino=sink_inode),
    ]
    process.open.side_effect = open_error
    process.write.side_effect = write_error

    server._write_readiness_checkpoint(True, True)

    assert process.open.call_count == int(stdout_mode == stat.S_IFIFO)
    assert process.write.call_count == int(write_error is not None)
    assert process.close.call_count == int(stdout_mode == stat.S_IFIFO and open_error is None)
    if process.close.called:
        process.close.assert_called_once_with(91)
    assert capsys.readouterr() == ("", "")


def test_tls_preflight_checks_the_real_mounts_without_starting_runtime(
    launch: tuple[Mock, Mock], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _, configure = launch
    run = Mock()
    monkeypatch.setattr(uvicorn.Server, "run", run)
    server.main(["--check-tls"])
    captured = capsys.readouterr()
    assert captured.out == "Private browser TLS configuration verified\n"
    assert captured.err == ""
    configure.assert_not_called()
    run.assert_not_called()


@pytest.mark.parametrize(
    "change", ["wrong-certificate", "wrong-port", "expired-readback", "wrong-stage"]
)
def test_tls_preflight_refuses_unqualified_mounts_before_runtime(
    change: str,
    deployment: dict[str, str],
    launch: tuple[Mock, Mock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, configure = launch
    if change == "wrong-certificate":
        monkeypatch.setenv(PREFIX + "STAGE_LEAF_FILE", deployment[PREFIX + "SERVER_CERT_FILE"])
    elif change == "wrong-port":
        monkeypatch.setenv(PREFIX + "LISTEN_PORT", "65536")
    elif change == "expired-readback":
        path = Path(deployment[PREFIX + "READBACK_FILE"])
        document = json.loads(path.read_bytes())
        document["verified_at"] = "2020-01-01T00:00:00Z"
        document["valid_until"] = "2020-01-02T00:00:00Z"
        content = json.dumps(document).encode()
        path.write_bytes(content)
        monkeypatch.setenv(PREFIX + "READBACK_SHA256", hashlib.sha256(content).hexdigest())
    else:
        for name in ("BINDING", "READBACK"):
            path = Path(deployment[f"{PREFIX}{name}_FILE"])
            document = json.loads(path.read_bytes())
            binding = document if name == "BINDING" else document["binding"]
            binding["stage_arn"] = binding["stage_arn"].replace("/stages/dev", "/stages/test")
            content = json.dumps(document).encode()
            path.write_bytes(content)
            monkeypatch.setenv(f"{PREFIX}{name}_SHA256", hashlib.sha256(content).hexdigest())
    with pytest.raises(SystemExit, match="^Private browser listener deployment configuration"):
        server.main(["--check-tls"])
    configure.assert_not_called()


@pytest.mark.parametrize(
    "arguments",
    [
        ["--unknown"],
        ["--readiness-checkpoint", "--readiness-checkpoint"],
        ["--check-tls", "--readiness-checkpoint"],
    ],
)
def test_unrecognized_argument_refuses_to_start(
    arguments: list[str], launch: tuple[Mock, Mock]
) -> None:
    _, configure = launch
    with pytest.raises(SystemExit, match="^Invalid private browser listener argument"):
        server.main(arguments)
    configure.assert_not_called()


@pytest.mark.parametrize(
    "change", ["missing-pin", "changed-bytes", "duplicate", "oversize", "bind"]
)
def test_bad_deployment_refuses_before_auth_runtime(
    change: str,
    deployment: dict[str, str],
    launch: tuple[Mock, Mock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, configure = launch
    path = Path(deployment[PREFIX + "READBACK_FILE"])
    if change == "missing-pin":
        monkeypatch.delenv(PREFIX + "READBACK_SHA256")
    elif change == "bind":
        monkeypatch.setenv(PREFIX + "LISTEN_HOST", "0.0.0.0")
    else:
        content = path.read_bytes()
        changed = {
            "changed-bytes": content + b" ",
            "duplicate": b'{"binding":{},' + content[1:],
            "oversize": b" " * 8193,
        }[change]
        path.write_bytes(changed)
        if change != "changed-bytes":
            monkeypatch.setenv(PREFIX + "READBACK_SHA256", hashlib.sha256(changed).hexdigest())
    with pytest.raises(SystemExit, match="^Private browser listener deployment configuration"):
        server.main()
    configure.assert_not_called()


@pytest.mark.parametrize("change", ["different-binding", "expired-readback", "wrong-certificate"])
def test_pinned_but_unqualified_tls_setup_refuses_and_drains_runtime(
    change: str,
    deployment: dict[str, str],
    launch: tuple[Mock, Mock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, configure = launch
    if change == "wrong-certificate":
        monkeypatch.setenv(PREFIX + "STAGE_LEAF_FILE", deployment[PREFIX + "SERVER_CERT_FILE"])
    else:
        path = Path(deployment[PREFIX + "READBACK_FILE"])
        document = json.loads(path.read_bytes())
        if change == "different-binding":
            document["binding"]["client_certificate_id"] = "different-generated-certificate"
        else:
            document["verified_at"] = "2020-01-01T00:00:00Z"
            document["valid_until"] = "2020-01-02T00:00:00Z"
        content = json.dumps(document).encode()
        path.write_bytes(content)
        monkeypatch.setenv(PREFIX + "READBACK_SHA256", hashlib.sha256(content).hexdigest())
    run = Mock()
    monkeypatch.setattr(uvicorn.Server, "run", run)
    with pytest.raises(SystemExit, match="^Private browser listener deployment configuration"):
        server.main()
    configure.assert_called_once()
    run.assert_not_called()
    runtime.close.assert_called_once()
