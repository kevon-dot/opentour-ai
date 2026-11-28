"""Customer-operated TLS listener for an isolated health, intake or learning-run connection."""

import argparse
import asyncio
import hmac
import json
import os
import re
import ssl
import stat
import sys
from pathlib import Path
from urllib.parse import urlsplit


def credential(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_mode & 0o077
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
        ):
            raise ValueError("invalid credential")
        value = stream.read(130).removesuffix(b"\n")
        if not re.fullmatch(rb"[A-Za-z0-9_-]{43,128}", value):
            raise ValueError("invalid credential")
        return value


INTAKE_ROUTES = {
    b"POST /v1/discover_sources HTTP/1.1": "discover_sources",
    b"POST /v1/submit_intake HTTP/1.1": "submit_intake",
    b"POST /v1/get_run HTTP/1.1": "get_run",
}
INTAKE_STATUSES = {
    "invalid_arguments": b"400 Bad Request",
    "idempotency_conflict": b"409 Conflict",
    "intake_rate_limited": b"429 Too Many Requests",
}
STATUS_LINES = {
    400: b"400 Bad Request",
    403: b"403 Forbidden",
    409: b"409 Conflict",
    429: b"429 Too Many Requests",
}
MAX_ARGUMENT_BYTES = 4096
MAX_RESPONSE_BYTES = 32768


def authenticated(headers, config, token_path):
    origin = config["origin"].encode("ascii")
    if (
        headers.get(b"host") != origin.removeprefix(b"https://")
        or headers.get(b"origin") != origin
        or headers.get(b"x-opentour-audience") != config["audience"].encode("ascii")
    ):
        return False
    try:
        token = credential(token_path)
    except (OSError, ValueError):
        return False
    return hmac.compare_digest(headers.get(b"authorization", b""), b"Bearer " + token)


def intake_connection(selection, config):
    # The immutable installed zip contains the same resolver and dispatch as mcp-intake.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "opentour-slack.pyz"))
    from opentour_cell.pilot.intake_local import open_intake
    from opentour_cell.pilot.intake_mcp import intake_dispatch

    principal, admission = open_intake(selection)
    if (principal.tenant, principal.actor) != (config["intake_tenant"], config["intake_actor"]):
        raise ValueError("private selection mismatch")
    return intake_dispatch(principal, admission)


def learning_connection(selection, config, *, ledger=None):
    # A separate scoped permission: routes, codes and handlers all come from the installed zip,
    # so the gateway has no hand-typed copy of the learning-run contract.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "opentour-slack.pyz"))
    from opentour_cell.pilot import learning_runs

    principal, service = learning_runs.open_learning_runs(selection, ledger=ledger)
    expected = (config["learning_tenant"], config["learning_actor"], config["learning_client"])
    if (principal.tenant, principal.actor, principal.client) != expected:
        raise ValueError("private selection mismatch")
    statuses = {code: STATUS_LINES[value] for code, value in learning_runs.ERRORS.items()}
    return (
        learning_runs.learning_run_dispatch(principal, service),
        learning_runs.http_routes(),
        statuses,
    )


def unique_object(pairs):
    obj = dict(pairs)
    if len(obj) != len(pairs):
        raise ValueError("duplicate argument")
    return obj


def learning_route(line, routes):
    for pattern, operation in routes:
        match = pattern.fullmatch(line)
        if match is not None:
            return operation, {k: v.decode("ascii") for k, v in match.groupdict().items()}
    return None, {}


async def response(reader, config, token_path, body, call, learning=None):
    request = await reader.readuntil(b"\r\n\r\n")
    lines = request[:-4].split(b"\r\n")
    health = call is None and learning is None and lines[0] == b"GET /v1/health HTTP/1.1"
    operation = INTAKE_ROUTES.get(lines[0]) if call is not None else None
    statuses, path_arguments = INTAKE_STATUSES, {}
    if learning is not None:
        call, routes, statuses = learning
        operation, path_arguments = learning_route(lines[0], routes)
    if not health and operation is None:
        return b"404 Not Found", b""
    headers = {}
    for line in lines[1:]:
        name, sep, value = line.partition(b":")
        name = name.lower()
        if not sep or not re.fullmatch(rb"[a-z0-9-]+", name) or name in headers:
            return b"400 Bad Request", b""
        headers[name] = value.strip(b" \t")
    if b"transfer-encoding" in headers or b"content-encoding" in headers:
        return b"400 Bad Request", b""
    if not authenticated(headers, config, token_path):
        return b"403 Forbidden", b""
    if health or lines[0].startswith(b"GET "):
        # Health and learning-run status reads carry no body.
        if headers.get(b"content-length", b"0") != b"0":
            return b"400 Bad Request", b""
        if health:
            return b"200 OK", body
        arguments = {}
    else:
        if headers.get(b"content-type", b"").lower() != b"application/json":
            return b"415 Unsupported Media Type", b""
        length = headers.get(b"content-length", b"")
        if not re.fullmatch(rb"[1-9][0-9]*", length):
            return b"400 Bad Request", b""
        if len(length) > 4 or int(length) > MAX_ARGUMENT_BYTES:
            return b"413 Content Too Large", b""
        try:
            arguments = json.loads(
                (await reader.readexactly(int(length))).decode("utf-8"),
                object_pairs_hook=unique_object,
            )
            if not isinstance(arguments, dict) or any(
                type(v) not in (str, int) for v in arguments.values()
            ):
                raise ValueError("expected flat argument object")
        except (ValueError, RecursionError):
            return b"400 Bad Request", b""
    if path_arguments.keys() & arguments.keys():
        return b"400 Bad Request", b""
    arguments.update(path_arguments)
    # A credential can rotate while the client sends its body.
    if not authenticated(headers, config, token_path):
        return b"403 Forbidden", b""
    result = call(operation, arguments)
    if result.get("isError"):
        code = result["content"][0]["text"]
        status = statuses.get(code, b"403 Forbidden")
        payload = {"error": code}
    else:
        status, payload = b"200 OK", result["structuredContent"]
    encoded = json.dumps(payload, allow_nan=False, separators=(",", ":")).encode()
    if len(encoded) > MAX_RESPONSE_BYTES:
        return b"503 Service Unavailable", b""
    return status, encoded


async def bind_server(args):
    config = json.loads(args.config.read_text())
    provenance = json.loads((Path(__file__).resolve().parents[2] / "PROVENANCE.json").read_text())
    for key in ("site", "audience", "revision"):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", config[key]):
            raise ValueError("invalid host configuration")
    origin = urlsplit(config["origin"])
    if (
        not origin.hostname
        or config["origin"] != f"https://{origin.netloc}"
        or origin.username is not None
        or not re.fullmatch(r"[a-z0-9.-]+(?::[0-9]+)?", origin.netloc)
        or origin.port == 0
        or config["schema"] != "opentour.worker-host.v1"
        or config["release_version"] != provenance["kit_version"]
        or config["source_commit"] != provenance["source_commit"]
    ):
        raise ValueError("invalid origin or package binding")
    selection = getattr(args, "selection", None)
    learning_selection = getattr(args, "learning_selection", None)
    if selection and learning_selection:
        # Intake and learning-run control are separate permissions with separate credentials.
        raise ValueError("intake and learning-run connections need separate listeners")
    call = intake_connection(selection, config) if selection else None
    learning = learning_connection(learning_selection, config) if learning_selection else None
    body = json.dumps(
        {
            "status": "ready",
            "component": "health-endpoint",
            "site": config["site"],
            "revision": config["revision"],
            "release": provenance["kit_version"],
            "source_commit": provenance["source_commit"],
        }
    ).encode()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(args.cert, args.key)

    async def handle(reader, writer):
        try:
            async with asyncio.timeout(4):
                status, payload = await response(reader, config, args.token, body, call, learning)
                writer.write(
                    b"HTTP/1.1 " + status + b"\r\nCache-Control: no-store\r\nConnection: close"
                    b"\r\nContent-Type: application/json\r\nContent-Length: "
                    + str(len(payload)).encode()
                    + b"\r\n\r\n"
                    + payload
                )
                await writer.drain()
        except (TimeoutError, OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            pass
        finally:
            writer.close()

    return await asyncio.start_server(
        handle,
        args.bind,
        args.port,
        ssl=context,
        ssl_handshake_timeout=4,
        limit=8192,
    )


async def serve(args):
    server = await bind_server(args)
    async with server:
        print(server.sockets[0].getsockname()[1], flush=True)
        await server.serve_forever()


if __name__ == "__main__":
    if sys.version_info[:3] != (3, 12, 16):
        raise SystemExit("Use Python 3.12.16.")
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ("config", "cert", "key", "token"):
        parser.add_argument("--" + option, type=Path, required=True)
    parser.add_argument(
        "--selection",
        type=Path,
        help="private approved intake selection for a separate connector listener",
    )
    parser.add_argument(
        "--learning-selection",
        type=Path,
        help="private owner-authorized learning-run selection for a separate listener",
    )
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8443)
    asyncio.run(serve(parser.parse_args()))
