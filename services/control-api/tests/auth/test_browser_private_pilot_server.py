"""Offline startup proof for the separate private browser listener process."""

from __future__ import annotations

import hashlib
import json
import ssl
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
import uvicorn
from opentour_control.auth import browser_private_pilot_server as server
from test_browser_private_listener import tls_fixture as tls_fixture

PREFIX = "OPENTOUR_PRIVATE_BROWSER_"


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


def test_private_launcher_loads_real_reviewed_tls_configuration(
    launch: tuple[Mock, Mock], monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, configure = launch
    loaded: list[uvicorn.Config] = []

    def inspect(listener: uvicorn.Server) -> None:
        listener.config.load()
        loaded.append(listener.config)

    monkeypatch.setattr(uvicorn.Server, "run", inspect)
    server.main()
    (config,) = loaded
    assert (config.host, config.port, config.workers) == ("10.0.10.20", 8443, 1)
    assert config.ssl is not None
    assert config.ssl.verify_mode == ssl.CERT_REQUIRED
    assert config.ssl.cert_store_stats()["x509"] == 1
    assert config.proxy_headers is False
    assert config.timeout_graceful_shutdown == 20
    configure.assert_called_once()
    runtime.close.assert_called_once()


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
