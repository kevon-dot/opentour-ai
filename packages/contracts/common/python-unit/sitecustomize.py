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
from typing import Any


def install() -> None:
    if os.environ.get("OPENTOUR_UNIT_GUARD") != "1":
        return
    if getattr(subprocess, "_opentour_unit_guard_pid", None) == os.getpid():
        return
    managed_popen = subprocess.Popen
    signature = inspect.signature(managed_popen)

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
    subprocess._opentour_unit_guard_pid = os.getpid()  # type: ignore[attr-defined]
    subprocess.Popen = guarded_popen  # type: ignore[assignment,misc]


try:
    install()
except BaseException:
    # Python normally warns and continues after a sitecustomize error. A failed
    # guard installation must stop before user code runs instead.
    sys.stderr.write("OpenTour unit startup guard failed; refusing unguarded execution.\n")
    os._exit(2)
