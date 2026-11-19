"""Enforce the private TLS startup contract using disposable local certificates."""

from __future__ import annotations

import asyncio
import socket
import ssl
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from opentour_control.auth.browser_private_listener import (
    PrivateListenerUnavailable,
    ReviewedStageReadback,
    StageClientBinding,
    build_private_browser_listener,
)

HOST = "browser.fixture.test"


def cert(name: str, now: datetime, *, purpose=None, ca=False, signer=None, expire=False):
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    issuer, signing_key = (subject, key) if signer is None else (signer[0].subject, signer[1])
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=2))
        .not_valid_after(now + timedelta(days=-1 if expire else 2))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False)
    )
    if purpose is not None:
        builder = builder.add_extension(x509.ExtendedKeyUsage([purpose]), critical=False)
    return builder.sign(signing_key, hashes.SHA256()), key


def write_pair(base: Path, name: str, pair):
    cert_path = base / f"{name}.pem"
    cert_path.write_bytes(pair[0].public_bytes(serialization.Encoding.PEM))
    key_path = base / f"{name}.key"
    key_path.write_bytes(
        pair[1].private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


@pytest.fixture
def tls_fixture(tmp_path: Path):
    now = datetime.now(UTC)
    authority = cert("fixture-server-ca", now, ca=True)
    server = cert(HOST, now, purpose=ExtendedKeyUsageOID.SERVER_AUTH, signer=authority)
    stage = cert("fixture-stage", now, purpose=ExtendedKeyUsageOID.CLIENT_AUTH)
    wrong = cert("fixture-stage", now, purpose=ExtendedKeyUsageOID.CLIENT_AUTH)
    descendant = cert(
        "fixture-descendant", now, purpose=ExtendedKeyUsageOID.CLIENT_AUTH, signer=stage
    )
    server_pem, server_key = write_pair(tmp_path, "server", server)
    authority_pem, _ = write_pair(tmp_path, "authority", authority)
    stage_pair = write_pair(tmp_path, "stage", stage)
    wrong_pair = write_pair(tmp_path, "wrong", wrong)
    descendant_pair = write_pair(tmp_path, "descendant", descendant)
    approved = StageClientBinding(
        "arn:aws:apigateway:us-east-2::/restapis/fixture-api/stages/fixture-stage",
        "fixture-stage-certificate",
        stage[0].fingerprint(hashes.SHA256()).hex(),
        HOST,
    )
    readback = ReviewedStageReadback(approved, now, now + timedelta(hours=1))
    return (
        now,
        dict(
            approved=approved,
            readback=readback,
            stage_leaf_pem=stage_pair[0].read_bytes(),
            server_certfile=server_pem,
            server_keyfile=server_key,
            host="127.0.0.1",
            port=8443,
            clock=lambda: now,
        ),
        authority_pem,
        stage_pair,
        wrong_pair,
        descendant_pair,
    )


async def fixture_app(scope, receive, send):
    assert scope["scheme"] == "https"
    await send({"type": "http.response.start", "status": 204, "headers": []})
    await send({"type": "http.response.body", "body": b""})


def assert_refuses(**options):
    with pytest.raises(PrivateListenerUnavailable, match="^PRIVATE_BROWSER_LISTENER_UNAVAILABLE$"):
        build_private_browser_listener(fixture_app, **options)


def test_exact_singleton_context_and_no_public_defaults(tls_fixture):
    _, options, *_ = tls_fixture
    config = build_private_browser_listener(fixture_app, **options)
    config.load()
    assert config.ssl.verify_mode == ssl.CERT_REQUIRED
    assert config.ssl.cert_store_stats()["x509"] == 1
    assert config.ssl.minimum_version == ssl.TLSVersion.TLSv1_2
    assert config.host == "127.0.0.1" and config.uds is None
    assert config.proxy_headers is False and config.access_log is False
    assert config.ws == "none"


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "8.8.8.8", "localhost"])
def test_public_or_implicit_address_cannot_start(tls_fixture, host):
    _, options, *_ = tls_fixture
    assert_refuses(**(options | {"host": host}))


def test_absent_expired_or_changed_readback_cannot_start(tls_fixture):
    now, options, *_ = tls_fixture
    readback = options["readback"]
    for other in (
        None,
        replace(readback, valid_until=now),
        replace(readback, verified_at=now + timedelta(seconds=1)),
        replace(readback, binding=replace(readback.binding, client_certificate_id="wrong-id")),
    ):
        assert_refuses(**(options | {"readback": other}))


def test_unknown_peer_extra_trust_anchors_and_wrong_server_refused(tls_fixture):
    _, options, authority, _, wrong, _ = tls_fixture
    for extra in (b"\n" + authority.read_bytes(), b"\nnot-a-certificate"):
        assert_refuses(**(options | {"stage_leaf_pem": options["stage_leaf_pem"] + extra}))
    assert_refuses(**(options | {"stage_leaf_pem": wrong[0].read_bytes()}))
    assert_refuses(**(options | {"server_keyfile": wrong[1]}))
    approved = replace(options["approved"], backend_server_name="wrong.fixture.test")
    assert_refuses(
        **(
            options
            | {
                "approved": approved,
                "readback": replace(options["readback"], binding=approved),
            }
        )
    )


@pytest.mark.parametrize("variant", ["server-purpose", "authority", "expired", "not-self-signed"])
def test_wrong_client_tls_purpose_or_invalid_client_leaf_refused(tls_fixture, variant):
    now, options, *_ = tls_fixture
    signer = cert("fixture-wrong-signer", now, ca=True) if variant == "not-self-signed" else None
    purpose = (
        ExtendedKeyUsageOID.SERVER_AUTH
        if variant == "server-purpose"
        else ExtendedKeyUsageOID.CLIENT_AUTH
    )
    leaf, _ = cert(
        "fixture-stage",
        now,
        purpose=purpose,
        ca=variant == "authority",
        expire=variant == "expired",
        signer=signer,
    )
    approved = replace(
        options["approved"], client_certificate_sha256=leaf.fingerprint(hashes.SHA256()).hex()
    )
    assert_refuses(
        **(
            options
            | {
                "approved": approved,
                "readback": replace(options["readback"], binding=approved),
                "stage_leaf_pem": leaf.public_bytes(serialization.Encoding.PEM),
            }
        )
    )


def test_real_tls_denies_absent_wrong_descendant_and_plaintext_peers(tls_fixture):
    now, options, authority, stage, wrong, descendant = tls_fixture

    async def exchange(port: int, pair: tuple[Path, Path] | None, *, plaintext=False) -> bytes:
        context = None if plaintext else ssl.create_default_context(cafile=authority)
        if context is not None and pair is not None:
            context.load_cert_chain(*pair)
        try:
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", port, ssl=context, server_hostname=None if plaintext else HOST
            )
            try:
                writer.write(
                    b"POST /fixture HTTP/1.1\r\nHost: browser.fixture.test\r\n"
                    b"X-Forwarded-Proto: http\r\nX-Gateway-Principal: fixture\r\n"
                    b"Connection: close\r\nContent-Length: 0\r\n\r\n"
                )
                await writer.drain()
                return await asyncio.wait_for(reader.read(), timeout=3)
            finally:
                writer.close()
                await writer.wait_closed()
        except (ssl.SSLError, ConnectionError):
            return b""

    async def exercise() -> None:
        tick = now
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
            config = build_private_browser_listener(
                fixture_app, **(options | {"port": port, "clock": lambda: tick})
            )
            server = uvicorn.Server(config)
            task = asyncio.create_task(server.serve(sockets=[sock]))
            try:
                async with asyncio.timeout(3):
                    while not server.started:
                        await asyncio.sleep(0.01)
                assert (await exchange(port, stage)).startswith(b"HTTP/1.1 204")
                assert await exchange(port, None) == b""
                assert await exchange(port, wrong) == b""
                assert await exchange(port, descendant) == b""
                assert await exchange(port, None, plaintext=True) == b""
                tick += timedelta(hours=1)
                assert (await exchange(port, stage)).startswith(b"HTTP/1.1 403")
            finally:
                server.should_exit = True
                await asyncio.wait_for(task, timeout=3)

    asyncio.run(exercise())
