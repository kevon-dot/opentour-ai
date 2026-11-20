"""Private browser workload listener TLS configuration, with no public route mount."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import re
import ssl
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import uvicorn
from cryptography import x509
from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from cryptography.x509.oid import ExtendedKeyUsageOID, SignatureAlgorithmOID
from uvicorn._types import ASGI3Application, ASGIReceiveCallable, ASGISendCallable, Scope
from uvicorn.protocols.http.h11_impl import H11Protocol
from uvicorn.server import ServerState

UVICORN_VERSION = "0.54.0"
PRIVATE_NETWORKS = tuple(
    ipaddress.IPv4Network(cidr) for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)


class PrivateListenerUnavailable(ValueError):
    """An approved and verified singleton stage binding is required."""


@dataclass(frozen=True)
class StageClientBinding:
    stage_arn: str
    client_certificate_id: str
    client_certificate_sha256: str
    backend_server_name: str


@dataclass(frozen=True)
class ReviewedStageReadback:
    """Provider readback supplied only by the trusted deployment composition root."""

    binding: StageClientBinding
    verified_at: datetime
    valid_until: datetime


def _unavailable() -> PrivateListenerUnavailable:
    return PrivateListenerUnavailable("PRIVATE_BROWSER_LISTENER_UNAVAILABLE")


def _assert_tls_material(
    approved: StageClientBinding, stage_leaf_pem: bytes, server_certfile: Path, now: datetime
) -> x509.Certificate:
    # The trust bytes loaded into OpenSSL are reconstructed from this one parsed leaf.
    if (
        re.fullmatch(
            rb"-----BEGIN CERTIFICATE-----[\r\n]+[A-Za-z0-9+/=\r\n]+-----END CERTIFICATE-----",
            stage_leaf_pem.strip(),
        )
        is None
    ):
        raise _unavailable()
    leaf = x509.load_pem_x509_certificate(stage_leaf_pem)
    if (
        leaf.fingerprint(hashes.SHA256()).hex() != approved.client_certificate_sha256
        or not leaf.not_valid_before_utc <= now < leaf.not_valid_after_utc
    ):
        raise _unavailable()
    if len(leaf.extensions) == 0:
        # API Gateway's native REST stage certificate omits X.509 extensions.
        public_key = leaf.public_key()
        if (
            leaf.version != x509.Version.v3
            or not isinstance(public_key, RSAPublicKey)
            or public_key.key_size != 2048
            or leaf.signature_algorithm_oid != SignatureAlgorithmOID.RSA_WITH_SHA256
        ):
            raise _unavailable()
    elif leaf.extensions.get_extension_for_class(x509.BasicConstraints).value.ca:
        raise _unavailable()
    leaf.verify_directly_issued_by(leaf)
    try:
        usage = leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    except x509.ExtensionNotFound:
        usage = None
    if usage is not None and set(usage) != {ExtendedKeyUsageOID.CLIENT_AUTH}:
        raise _unavailable()
    try:
        key_usage = leaf.extensions.get_extension_for_class(x509.KeyUsage).value
    except x509.ExtensionNotFound:
        key_usage = None
    if key_usage is not None and (not key_usage.digital_signature or key_usage.key_cert_sign):
        raise _unavailable()
    server = x509.load_pem_x509_certificates(server_certfile.read_bytes())[0]
    server_names = server.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    if (
        server.fingerprint(hashes.SHA256()) == leaf.fingerprint(hashes.SHA256())
        or not server.not_valid_before_utc <= now < server.not_valid_after_utc
        or approved.backend_server_name not in server_names.get_values_for_type(x509.DNSName)
    ):
        raise _unavailable()
    try:
        server_usage = server.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    except x509.ExtensionNotFound:
        server_usage = None
    if server_usage is not None and ExtendedKeyUsageOID.SERVER_AUTH not in server_usage:
        raise _unavailable()
    return leaf


def prepare_private_browser_listener_tls(
    *,
    approved: StageClientBinding,
    readback: ReviewedStageReadback | None,
    stage_leaf_pem: bytes,
    server_certfile: Path,
    server_keyfile: Path,
    host: str,
    port: int,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> tuple[ssl.SSLContext, datetime, datetime]:
    """Verify the reviewed TLS material without binding a socket."""
    try:
        now = clock()
        bind = ipaddress.IPv4Address(host)
        if (
            uvicorn.__version__ != UVICORN_VERSION
            or readback is None
            or approved != readback.binding
            or not all(
                (approved.stage_arn, approved.client_certificate_id, approved.backend_server_name)
            )
            or now.utcoffset() is None
            or not readback.verified_at <= now < readback.valid_until
            or (str(bind) != "127.0.0.1" and not any(bind in block for block in PRIVATE_NETWORKS))
            or type(port) is not int
            or not 1024 <= port <= 65535
        ):
            raise _unavailable()
        leaf = _assert_tls_material(approved, stage_leaf_pem, server_certfile, now)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.verify_mode = ssl.CERT_REQUIRED
        context.load_cert_chain(certfile=server_certfile, keyfile=server_keyfile)
        context.load_verify_locations(cadata=leaf.public_bytes(serialization.Encoding.PEM).decode())
        if context.cert_store_stats()["x509"] != 1:
            raise _unavailable()
    except (
        InvalidSignature,
        OSError,
        TypeError,
        UnsupportedAlgorithm,
        ValueError,
        x509.ExtensionNotFound,
    ):
        raise _unavailable() from None

    return context, readback.verified_at, min(readback.valid_until, leaf.not_valid_after_utc)


def _pin_peer_protocol(client_certificate_sha256: str) -> type[asyncio.Protocol]:
    peer_digest = bytes.fromhex(client_certificate_sha256)

    class PinnedStageProtocol(asyncio.Protocol):
        def __init__(
            self,
            config: uvicorn.Config,
            server_state: ServerState,
            app_state: dict[str, object],
            _loop: asyncio.AbstractEventLoop | None = None,
        ) -> None:
            self._config = config
            self._server_state = server_state
            self._app_state = app_state
            self._loop = _loop
            self._http: H11Protocol | None = None

        def connection_made(self, transport: asyncio.BaseTransport) -> None:
            if not isinstance(transport, asyncio.Transport):
                transport.close()
                return
            tls = transport.get_extra_info("ssl_object")
            try:
                peer = (
                    tls.getpeercert(binary_form=True)
                    if isinstance(tls, (ssl.SSLObject, ssl.SSLSocket))
                    else None
                )
            except (OSError, ValueError):
                peer = None
            if not peer or hashlib.sha256(peer).digest() != peer_digest:
                transport.abort()
                return
            self._http = H11Protocol(self._config, self._server_state, self._app_state, self._loop)
            self._http.connection_made(transport)

        def data_received(self, data: bytes) -> None:
            if self._http is not None:
                self._http.data_received(data)

        def eof_received(self) -> None:
            if self._http is not None:
                self._http.eof_received()

        def pause_writing(self) -> None:
            if self._http is not None:
                self._http.pause_writing()

        def resume_writing(self) -> None:
            if self._http is not None:
                self._http.resume_writing()

        def connection_lost(self, exc: Exception | None) -> None:
            if self._http is not None:
                self._http.connection_lost(exc)
                self._http = None

    return PinnedStageProtocol


def build_private_browser_listener(
    app: ASGI3Application,
    *,
    approved: StageClientBinding,
    readback: ReviewedStageReadback | None,
    stage_leaf_pem: bytes,
    server_certfile: Path,
    server_keyfile: Path,
    host: str,
    port: int,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> uvicorn.Config:
    """Build the isolated TLS listener. No route is chosen or mounted here."""
    context, verified_at, valid_until = prepare_private_browser_listener_tls(
        approved=approved,
        readback=readback,
        stage_leaf_pem=stage_leaf_pem,
        server_certfile=server_certfile,
        server_keyfile=server_keyfile,
        host=host,
        port=port,
        clock=clock,
    )

    async def private_app(
        scope: Scope, receive: ASGIReceiveCallable, send: ASGISendCallable
    ) -> None:
        if scope["type"] != "http":
            return
        try:
            now = clock()
            verified = now.utcoffset() is not None and verified_at <= now < valid_until
        except (TypeError, ValueError):
            verified = False
        if scope["scheme"] != "https" or not verified:
            await send({"type": "http.response.start", "status": 403, "headers": []})
            await send({"type": "http.response.body", "body": b""})
            return
        await app(scope, receive, send)

    return uvicorn.Config(
        private_app,
        host=host,
        port=port,
        ssl_context_factory=lambda _config, _default: context,
        interface="asgi3",
        http=_pin_peer_protocol(approved.client_certificate_sha256),
        ws="none",
        lifespan="off",
        proxy_headers=False,
        forwarded_allow_ips="",
        access_log=False,
    )
