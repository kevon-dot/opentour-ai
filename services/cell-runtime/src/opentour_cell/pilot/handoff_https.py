"""Run the dedicated HTTPS handoff connection on the customer's worker."""

from __future__ import annotations

import argparse
import http.client
import re
import socket
import ssl
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, fields
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from opentour_cell.pilot import repair_bundle as bundle
from opentour_cell.pilot.handoff_connection import (
    ERROR,
    FAILURES,
    LOOKUP,
    RELEASE,
    TICKET,
    HandoffConnection,
    Json,
    match,
    require,
    revoke_connection,
)
from opentour_cell.pilot.readout import parse_manifest

DISPATCH_PATH = "/v1/handoff/dispatch"
FETCH_PATH = "/v1/handoff/fetch"
READBACK_PATH = "/v1/handoff/readback"
ROUTES = {DISPATCH_PATH, FETCH_PATH, READBACK_PATH}
PRIVATE_ROUTES = {"fetch": FETCH_PATH, "readback": READBACK_PATH}
JSON = "application/json"
ARCHIVE = "application/zip"
MAX_REQUEST = 1024
MAX_READBACK = 8192
IO_TIMEOUT = 8
TRANSPORT_FAILURES = (*FAILURES, http.client.HTTPException)
ServerRequest = socket.socket | tuple[bytes, socket.socket]


@dataclass(frozen=True)
class TLSFiles:
    server_cert: Path
    server_key: Path
    recipient_ca: Path
    recipient_cert: Path
    recipient_key: Path
    server_ca: Path


class HandoffHTTPServer(ThreadingHTTPServer):
    """The dispatch route and recipient routes have separate authentication."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        connection: Path,
        recipient_grant: Path,
        tls: TLSFiles,
        after_receive: Callable[[], None] | None,
    ):
        self.configuration = connection
        self.recipient_grant = recipient_grant
        self.tls = tls
        self.after_receive = after_receive
        self.handoff: HandoffConnection | None = None
        self.requests = threading.BoundedSemaphore(32)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.verify_mode = ssl.CERT_OPTIONAL
        context.load_verify_locations(cadata=bundle._read(tls.recipient_ca, 65536).decode())
        bundle._read(tls.server_cert, 65536)
        bundle._read(tls.server_key, 65536)
        context.load_cert_chain(tls.server_cert, tls.server_key)
        self.tls_context = context
        super().__init__(address, _Handler)

    def activate_connection(self) -> None:
        require(self.handoff is None)
        handoff = HandoffConnection(
            self.configuration, self.recipient_grant, lambda: int(time.time())
        )
        origin = urlsplit(handoff.installation["origin"])
        require((origin.port or 443) == self.server_address[1])
        self.handoff = handoff

    def get_request(self) -> tuple[socket.socket, tuple[str, int]]:
        conn, address = super().get_request()
        try:
            conn.settimeout(IO_TIMEOUT)
            secured = self.tls_context.wrap_socket(
                conn, server_side=True, do_handshake_on_connect=False
            )
        except OSError:
            conn.close()
            raise
        return secured, address

    def process_request(self, request: ServerRequest, client_address: tuple[str, int]) -> None:
        if not self.requests.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.requests.release()
            raise

    def process_request_thread(
        self, request: ServerRequest, client_address: tuple[str, int]
    ) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.requests.release()

    def handle_error(self, request: ServerRequest, client_address: tuple[str, int]) -> None:
        # Request-derived values and recovered connection details stay private.
        pass

    def request_recipient(self, grant: Json, route: str, body: Json) -> bytes:
        target = urlsplit(grant["origin"])
        assert target.hostname is not None
        context = ssl.create_default_context(
            cadata=bundle._read(self.tls.server_ca, 65536).decode()
        )
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        bundle._read(self.tls.recipient_cert, 65536)
        bundle._read(self.tls.recipient_key, 65536)
        context.load_cert_chain(self.tls.recipient_cert, self.tls.recipient_key)
        client = http.client.HTTPSConnection(
            target.hostname, target.port or 443, context=context, timeout=IO_TIMEOUT
        )
        try:
            client.request(
                "POST",
                PRIVATE_ROUTES[route],
                bundle._bytes(body),
                headers={"Content-Type": JSON, "Connection": "close"},
            )
            response = client.getresponse()
            maximum, media = (
                (bundle.MAX_ARCHIVE, ARCHIVE) if route == "fetch" else (MAX_READBACK, JSON)
            )
            length = response.getheader("Content-Length", "")
            require(
                response.status == 200
                and response.getheader("Content-Type") == media
                and response.getheader("Transfer-Encoding") is None
                and response.getheader("Content-Encoding") is None
                and re.fullmatch(r"[1-9][0-9]{0,8}", length)
                and int(length) <= maximum
            )
            data = response.read(int(length))
            require(len(data) == int(length))
            return data
        except http.client.HTTPException:
            raise OSError(ERROR) from None
        finally:
            client.close()


class _Handler(BaseHTTPRequestHandler):
    server: HandoffHTTPServer
    _headers_buffer: list[bytes]
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        pass

    def _reply(
        self,
        status: int,
        payload: bytes,
        media: str = JSON,
        before_send: Callable[[], None] | None = None,
    ) -> None:
        self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", media)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store")
        if before_send is not None:
            try:
                before_send()
            except TRANSPORT_FAILURES:
                # Headers are still buffered. Replace the unpublished archive response.
                self._headers_buffer.clear()
                self.send_error(403)
                return
        self.end_headers()
        self.wfile.write(payload)

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        self._reply(code, bundle._bytes({"error": ERROR}))

    def _header(self, name: str) -> str:
        values = self.headers.get_all(name, [])
        require(len(values) <= 1)
        return values[0] if values else ""

    def _authenticate(self, handoff: HandoffConnection, grant: Json) -> None:
        require(self._header("Host") == urlsplit(grant["origin"]).netloc)
        handoff.check_connection(grant)
        if self.path == DISPATCH_PATH:
            handoff.dispatcher(
                grant, self._header("Authorization"), self._header("X-OpenTour-Audience")
            )
        else:
            require(not self._header("Authorization") and not self._header("X-OpenTour-Audience"))
            require(isinstance(self.connection, ssl.SSLSocket))
            assert isinstance(self.connection, ssl.SSLSocket)
            handoff.recipient_peer(grant, self.connection.getpeercert(binary_form=True))

    def do_POST(self) -> None:
        if self.path not in ROUTES:
            self.send_error(404)
            return
        try:
            handoff = self.server.handoff
            require(handoff is not None)
            assert handoff is not None
            grant = handoff.connection()
            self._authenticate(handoff, grant)
            length = self._header("Content-Length")
            require(
                self._header("Content-Type") == JSON
                and not self._header("Transfer-Encoding")
                and not self._header("Content-Encoding")
                and not self._header("Expect")
                and re.fullmatch(r"[1-9][0-9]{0,3}", length)
                and int(length) <= MAX_REQUEST
            )
            data = self.rfile.read(int(length))
            require(len(data) == int(length))
            body = parse_manifest(data)
            self._authenticate(handoff, grant)
            media = JSON
            before_send = None
            if self.path == DISPATCH_PATH:
                require(set(body) == {"release_ref"})
                result = handoff.dispatch(
                    grant,
                    match(body["release_ref"], RELEASE),
                    self.server.request_recipient,
                    self.server.after_receive,
                )
                payload = bundle._bytes(result)
            elif self.path == FETCH_PATH:
                require(set(body) == {"ticket"})
                payload, before_send = handoff.fetch(grant, match(body["ticket"], TICKET))
                media = ARCHIVE
            else:
                require(set(body) == {"release_ref", "lookup_ref"})
                result = handoff.readback(
                    grant, match(body["release_ref"], RELEASE), match(body["lookup_ref"], LOOKUP)
                )
                payload = bundle._bytes(result)
            self._authenticate(handoff, grant)
        except ConnectionAbortedError:
            self.close_connection = True
            return
        except TRANSPORT_FAILURES:
            self.send_error(403)
            return
        self._reply(200, payload, media, before_send)


def bind_server(
    connection: Path,
    recipient_grant: Path,
    tls: TLSFiles,
    *,
    host: str,
    port: int,
    after_receive: Callable[[], None] | None = None,
) -> HandoffHTTPServer:
    return HandoffHTTPServer((host, port), connection, recipient_grant, tls, after_receive)


def serve(
    connection: Path,
    recipient_grant: Path,
    tls: TLSFiles,
    *,
    host: str,
    port: int,
    after_receive: Callable[[], None] | None = None,
) -> None:
    with bind_server(
        connection, recipient_grant, tls, host=host, port=port, after_receive=after_receive
    ) as server:
        server.activate_connection()
        server.serve_forever()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connection", required=True, type=Path)
    parser.add_argument("--revoke", action="store_true")
    parser.add_argument("--recipient-grant", type=Path)
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    for field in fields(TLSFiles):
        parser.add_argument("--" + field.name.replace("_", "-"), type=Path)
    args = parser.parse_args()
    try:
        files = {field.name: getattr(args, field.name) for field in fields(TLSFiles)}
        options = (args.recipient_grant, args.host, args.port, *files.values())
        if args.revoke:
            require(all(value is None for value in options))
            revoke_connection(args.connection, int(time.time()))
            return 0
        require(all(value is not None for value in options) and 0 < args.port < 65536)
        tls = TLSFiles(**files)
        serve(args.connection, args.recipient_grant, tls, host=args.host, port=args.port)
    except KeyboardInterrupt:
        return 0
    except TRANSPORT_FAILURES:
        print(ERROR, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
