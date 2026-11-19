"""Inherited default-unit guard, loaded by Python before config or helper code.

This standard-library-only preload enforces ordinary test discipline. It is not
an operating-system sandbox for hostile native code or deliberate -I/-S bypass.
"""

from __future__ import annotations

import inspect
import os
import socket
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any


def install() -> None:
    if os.environ.get("OPENTOUR_UNIT_GUARD") != "1":
        return
    if getattr(subprocess, "_opentour_unit_guard_pid", None) == os.getpid():
        return
    managed_popen = subprocess.Popen
    signature = inspect.signature(managed_popen)
    connect = socket.socket.connect
    bind = socket.socket.bind
    accept = socket.socket.accept
    loopback_target = os.environ.get("OPENTOUR_LOOPBACK_TARGET")
    loopback_supervised = (
        loopback_target is not None
        and loopback_target == os.environ.get("OPENTOUR_TEST_TARGET")
        and sys.orig_argv[1:4] == ["-m", "tools.fixtures.python_postgres", loopback_target]
    )

    def network_denied(*args: Any, **kwargs: Any) -> Any:
        raise OSError("Network access denied by unit/contract command boundary")

    def detached_denied(*args: Any, **kwargs: Any) -> Any:
        raise OSError("Detached child creation denied by unit/contract command boundary")

    def guarded_popen(*args: Any, **kwargs: Any) -> Any:
        options = signature.bind_partial(*args, **kwargs).arguments
        if (
            options.get("start_new_session")
            or options.get("preexec_fn") is not None
            or options.get("process_group", -1) not in (None, -1)
            or options.get("creationflags", 0)
        ):
            detached_denied()
        return managed_popen(*args, **kwargs)

    @contextmanager
    def supervised_loopback() -> Iterator[dict[str, Any]]:
        """Admit only ephemeral listeners owned by this supervised process."""
        if not loopback_supervised or socket.socket.connect is not network_denied:
            raise RuntimeError("Loopback supervisor requires the inactive network guard")
        listeners: dict[int, socket.socket] = {}
        bound: list[socket.socket] = []
        observation = {"accepted_connections": 0, "cleanup_complete": False}

        def stream(sock: socket.socket) -> bool:
            return sock.family == socket.AF_INET and sock.type == socket.SOCK_STREAM

        def owns(address: Any) -> bool:
            if (
                not isinstance(address, tuple)
                or len(address) != 2
                or address[0] != "127.0.0.1"
                or type(address[1]) is not int
            ):
                return False
            listener = listeners.get(address[1])
            return listener is not None and listener.fileno() >= 0

        def loopback_bind(sock: socket.socket, address: Any) -> None:
            if not stream(sock) or address != ("127.0.0.1", 0) or len(bound) >= 128:
                network_denied()
            bind(sock, address)
            listeners[sock.getsockname()[1]] = sock
            bound.append(sock)

        def loopback_connect(sock: socket.socket, address: Any) -> None:
            if not stream(sock) or not owns(address):
                network_denied()
            connect(sock, address)

        def loopback_getaddrinfo(
            host: Any, port: Any, family: int = 0, type: int = 0, proto: int = 0, flags: int = 0
        ) -> list[tuple[int, int, int, str, tuple[str, int]]]:
            if (
                not owns((host, port))
                or family not in (0, socket.AF_INET)
                or type not in (0, socket.SOCK_STREAM)
                or proto not in (0, socket.IPPROTO_TCP)
                or flags != 0
            ):
                network_denied()
            # The numeric endpoint requires no system resolver or DNS.
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (host, port))]

        def loopback_accept(sock: socket.socket) -> tuple[socket.socket, Any]:
            peer, address = accept(sock)
            if stream(sock) and listeners.get(sock.getsockname()[1]) is sock:
                if address[0] != "127.0.0.1":
                    peer.close()
                    network_denied()
                observation["accepted_connections"] += 1
            return peer, address

        socket.socket.bind = loopback_bind
        socket.socket.connect = loopback_connect
        socket.socket.accept = loopback_accept
        socket.getaddrinfo = loopback_getaddrinfo
        subprocess._opentour_owns_loopback = owns  # type: ignore[attr-defined]
        try:
            yield observation
        finally:
            del subprocess._opentour_owns_loopback  # type: ignore[attr-defined]
            socket.socket.bind = bind
            socket.socket.connect = network_denied
            socket.socket.accept = accept
            socket.getaddrinfo = network_denied
            remaining = [sock for sock in bound if sock.fileno() >= 0]
            observation["cleanup_complete"] = not remaining
            for sock in remaining:
                sock.close()

    for name in ("connect", "connect_ex", "sendto", "sendmsg"):
        if hasattr(socket.socket, name):
            setattr(socket.socket, name, network_denied)
    for name in (
        "getaddrinfo",
        "gethostbyname",
        "gethostbyname_ex",
        "gethostbyaddr",
        "getnameinfo",
    ):
        setattr(socket, name, network_denied)
    for name in ("setsid", "setpgid"):
        if hasattr(os, name):
            setattr(os, name, detached_denied)
    # The trusted command supervisor alone uses this saved capability for bounded
    # process groups. Ordinary inherited helper code receives the guarded API.
    subprocess._opentour_managed_popen = managed_popen  # type: ignore[attr-defined]
    subprocess._opentour_supervised_loopback = supervised_loopback  # type: ignore[attr-defined]
    subprocess._opentour_unit_guard_pid = os.getpid()  # type: ignore[attr-defined]
    subprocess.Popen = guarded_popen  # type: ignore[assignment,misc]


try:
    install()
except BaseException:
    # Python normally warns and continues after a sitecustomize error. A failed
    # guard installation must stop before user code runs instead.
    sys.stderr.write("OpenTour unit startup guard failed; refusing unguarded execution.\n")
    os._exit(2)
