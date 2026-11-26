"""Customer-operated TLS listener for a single authenticated health resource."""

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


def response(request, config, token_path, body):
    lines = request[:-4].split(b"\r\n")
    if lines[0] != b"GET /v1/health HTTP/1.1":
        return b"404 Not Found", b""
    headers = {}
    for line in lines[1:]:
        name, sep, value = line.partition(b":")
        name = name.lower()
        if not sep or not re.fullmatch(rb"[a-z0-9-]+", name) or name in headers:
            return b"400 Bad Request", b""
        headers[name] = value.strip(b" \t")
    if b"transfer-encoding" in headers or headers.get(b"content-length", b"0") != b"0":
        return b"400 Bad Request", b""
    origin = config["origin"].encode("ascii")
    if (
        headers.get(b"host") != origin.removeprefix(b"https://")
        or headers.get(b"origin") != origin
        or headers.get(b"x-opentour-audience") != config["audience"].encode("ascii")
    ):
        return b"403 Forbidden", b""
    try:
        token = credential(token_path)
    except (OSError, ValueError):
        return b"403 Forbidden", b""
    if not hmac.compare_digest(headers.get(b"authorization", b""), b"Bearer " + token):
        return b"403 Forbidden", b""
    return b"200 OK", body


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
                data = await reader.readuntil(b"\r\n\r\n")
                status, payload = response(data, config, args.token, body)
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
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8443)
    asyncio.run(serve(parser.parse_args()))
