"""Explicit generated HTTPS handoff qualification; never installed-provider evidence.

This filename is intentionally outside ordinary pytest discovery. The serialized
loopback supervisor registration owns network qualification and CI inclusion.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import json
import secrets
import ssl
import subprocess
import sys
import threading
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path

import pytest
from opentour_cell.pilot import handoff_connection, handoff_https
from opentour_cell.pilot.operating_point import canonical, digest
from test_recipient_handoff_simulator import authorize, initial_release
from test_repair_bundle import read, write


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Certificates:
    """Separate generated server, recipient authentication and custody identities."""

    def __init__(self, root: Path):
        self.root = root
        root.mkdir(mode=0o700)
        for name in ("server-ca", "recipient-ca"):
            self.openssl(
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-days",
                "1",
                "-subj",
                "/CN=Generated " + name,
                "-keyout",
                name + ".key",
                "-out",
                name + ".crt",
            )
        self.leaf("server", "server-ca", "serverAuth", "subjectAltName=IP:127.0.0.1\n")
        self.leaf("recipient", "recipient-ca", "clientAuth")
        self.leaf("replacement", "recipient-ca", "clientAuth")
        for path in root.iterdir():
            path.chmod(0o600)
        self.files = handoff_https.TLSFiles(
            server_cert=root / "server.crt",
            server_key=root / "server.key",
            recipient_ca=root / "recipient-ca.crt",
            recipient_cert=root / "recipient.crt",
            recipient_key=root / "recipient.key",
            server_ca=root / "server-ca.crt",
        )

    def openssl(self, *args: str, input: bytes | None = None) -> bytes:
        return subprocess.run(
            ["openssl", *args],
            cwd=self.root,
            input=input,
            check=True,
            capture_output=True,
            timeout=20,
        ).stdout

    def leaf(self, name: str, ca: str, purpose: str, extra: str = "") -> None:
        write(self.root / (name + ".ext"), (extra + "extendedKeyUsage=" + purpose + "\n").encode())
        self.openssl(
            "req",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-subj",
            "/CN=Generated " + name,
            "-keyout",
            name + ".key",
            "-out",
            name + ".csr",
        )
        self.openssl(
            "x509",
            "-req",
            "-in",
            name + ".csr",
            "-CA",
            ca + ".crt",
            "-CAkey",
            ca + ".key",
            "-CAcreateserial",
            "-days",
            "1",
            "-extfile",
            name + ".ext",
            "-out",
            name + ".crt",
        )
        (self.root / (name + ".key")).chmod(0o600)

    def fingerprint(self, name: str) -> str:
        return sha(ssl.PEM_cert_to_DER_cert((self.root / (name + ".crt")).read_text()))

    def same_key_client(self, key: Path) -> None:
        write(self.root / "custody-auth.key", key.read_bytes())
        self.openssl(
            "req",
            "-new",
            "-key",
            "custody-auth.key",
            "-passin",
            "pass:",
            "-subj",
            "/CN=Generated distinct certificate same custody key",
            "-out",
            "custody-auth.csr",
        )
        write(self.root / "custody-auth.ext", b"extendedKeyUsage=clientAuth\n")
        self.openssl(
            "x509",
            "-req",
            "-in",
            "custody-auth.csr",
            "-CA",
            "recipient-ca.crt",
            "-CAkey",
            "recipient-ca.key",
            "-CAcreateserial",
            "-days",
            "1",
            "-extfile",
            "custody-auth.ext",
            "-out",
            "custody-auth.crt",
        )
        (self.root / "custody-auth.crt").chmod(0o600)

    def client(self, identity: str | None = None) -> ssl.SSLContext:
        context = ssl.create_default_context(cafile=self.files.server_ca)
        if identity:
            context.load_cert_chain(
                self.root / (identity + ".crt"), self.root / (identity + ".key")
            )
        return context


class Tenant:
    def __init__(self, root: Path, name: str):
        self.item, _, _, _ = initial_release(root, name.replace("-", ""))
        self.worker = self.item.outbox.store.path.parent
        self.certificates = Certificates(root / "transport")
        self.token = secrets.token_urlsafe(32)
        self.connection = self.worker / "generated-handoff-connection.json"
        self.offer = self.worker / ("handoff-package-" + self.item.release_ref + ".zip")
        self.package_sha256 = sha(self.item.offer.read_bytes())
        write(self.offer, self.item.offer.read_bytes())
        self.item.offer.unlink()  # The recipient must actually fetch through HTTPS.
        custody_certificate = Path(read(self.item.receiver.grant_file)["recipient_certificate"])
        self.custody_certificate = custody_certificate
        assert (
            custody_certificate.read_bytes() != self.certificates.files.recipient_cert.read_bytes()
        )
        self.body = {
            "schema_version": "opentour.pilot.handoff-connection.v1",
            "tenant": self.item.permit.tenant,
            "engagement": self.item.permit.engagement,
            "origin": "https://127.0.0.1:443",
            "audience": "generated-handoff-audience",
            "dispatcher_actor": "dispatcher",
            "dispatcher_token_sha256": sha(self.token.encode()),
            "recipient": self.item.permit.recipient,
            "destination_ref": self.item.permit.destination_ref,
            "recipient_tls_sha256": self.certificates.fingerprint("recipient"),
            "recipient_signer_sha256": sha(custody_certificate.read_bytes()),
            "connection_ref": "generated-overlapping-connection",
            "expires_at": int(time.time()) + 300,
        }
        self.seal()

    def seal(self, **changes) -> None:
        self.body.update(changes)
        write(
            self.connection,
            {
                "grant": self.body,
                "seal_sha256": hmac.new(
                    self.item.key,
                    canonical(self.body).encode(),
                    hashlib.sha256,
                ).hexdigest(),
            },
        )

    @contextmanager
    def listen(self, *, after_receive=None):
        with handoff_https.bind_server(
            self.connection,
            self.item.receiver.grant_file,
            self.certificates.files,
            host="127.0.0.1",
            port=0,
            after_receive=after_receive,
        ) as server:
            self.port = server.server_address[1]
            self.seal(origin=f"https://127.0.0.1:{self.port}")
            server.activate_connection()
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                yield self
            finally:
                server.shutdown()
                thread.join(timeout=10)
                assert not thread.is_alive(), "generated listener did not stop"

    def post(self, operation, body, *, identity=None, token=None, audience=None):
        context = self.certificates.client(identity)
        connection = http.client.HTTPSConnection(
            "127.0.0.1", self.port, context=context, timeout=10
        )
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        if audience is not None:
            headers["X-OpenTour-Audience"] = audience
        try:
            connection.request(
                "POST", "/v1/handoff/" + operation, json.dumps(body).encode(), headers
            )
            response = connection.getresponse()
            payload = response.read()
            return response.status, json.loads(payload) if payload else None
        finally:
            connection.close()

    def dispatch(self, release_ref=None, **kwargs):
        return self.post(
            "dispatch",
            {"release_ref": release_ref or self.item.release_ref},
            token=kwargs.pop("token", self.token),
            audience=kwargs.pop("audience", self.body["audience"]),
            **kwargs,
        )

    def unchanged(self) -> None:
        assert self.item.slots == []
        with self.item.outbox.store._db() as db:
            assert db.execute("SELECT count(*) FROM release_outbox").fetchone()[0] == 1
            assert db.execute("SELECT stage FROM release_outbox").fetchone()[0] == "queued"

    def one_effect(self) -> bytes:
        (slot,) = self.item.slots
        installed = slot / "artifact.zip"
        assert sha(installed.read_bytes()) == self.package_sha256
        receipt_bytes = (slot / "receipt.json").read_bytes()
        receipt = json.loads(receipt_bytes)
        signature = base64.b64decode(receipt.pop("receipt_signature"), validate=True)
        assert (receipt["tenant"], receipt["engagement"], receipt["release_ref"]) == (
            self.item.permit.tenant,
            self.item.permit.engagement,
            self.item.release_ref,
        )
        assert (
            receipt["destination_ref"],
            receipt["artifact_sha256"],
            receipt["package_sha256"],
        ) == (
            self.item.permit.destination_ref,
            self.item.permit.artifact_sha256,
            self.package_sha256,
        )
        public = self.certificates.root / "custody-public.pem"
        sealed = self.certificates.root / "custody-signature"
        write(
            public,
            self.certificates.openssl(
                "x509", "-in", str(self.custody_certificate), "-pubkey", "-noout"
            ),
        )
        write(sealed, signature)
        self.certificates.openssl(
            "dgst",
            "-sha256",
            "-verify",
            str(public),
            "-signature",
            str(sealed),
            input=(canonical(receipt) + "\n").encode(),
        )
        with self.item.outbox.store._db() as db:
            assert db.execute("SELECT count(*) FROM release_outbox").fetchone()[0] == 1
        return receipt_bytes


def opaque(result, release_ref, state):
    code, body = result
    assert code == 200
    assert body["release_ref"] == release_ref and body["state"] == state
    assert set(body) <= {
        "schema_version",
        "release_ref",
        "state",
        "receiver_receipt_ref",
        "owner_action",
    }
    return body


def test_two_tenants_lost_ack_restart_three_triggers_one_effect(tmp_path):
    first, second = (Tenant(tmp_path / name, name) for name in ("alpha", "beta"))
    assert first.body["connection_ref"] == second.body["connection_ref"]
    assert first.item.release_ref != second.item.release_ref
    assert first.item.permit.destination_ref != second.item.permit.destination_ref
    with ExitStack() as stack:
        stack.enter_context(first.listen())
        stack.enter_context(second.listen())
        for item, foreign in ((first, second), (second, first)):
            denied = item.dispatch(foreign.item.release_ref)
            assert denied == item.dispatch("release-" + "0" * 48)
            assert denied[0] == 403
            assert item.dispatch(token=foreign.token)[0] == 403
            own_destination = item.body["destination_ref"]
            item.seal(destination_ref=foreign.body["destination_ref"])
            assert item.dispatch()[0] == 403
            item.seal(destination_ref=own_destination)
            item.unchanged()
    for item in (first, second):
        committed = []

        def lose_reply(current=item, observations=committed):
            observations.append(current.one_effect())
            raise ConnectionAbortedError("generated lost reply after durable recipient write")

        with item.listen(after_receive=lose_reply):
            with pytest.raises(
                (http.client.RemoteDisconnected, ConnectionResetError, ssl.SSLEOFError)
            ):
                item.dispatch()
        assert len(committed) == 1
        assert (
            item.item.outbox.status(item.item.selection, "dispatcher").stage
            == "delivery_unconfirmed"
        )
        item.offer.unlink()
        with item.listen():
            delivered = opaque(item.dispatch(), item.item.release_ref, "delivered")
            assert opaque(item.dispatch(), item.item.release_ref, "delivered") == delivered
        assert item.one_effect() == committed[0]
        assert not item.offer.exists() and not item.item.offer.exists()
    for item, foreign in ((first, second), (second, first)):
        (own_slot,), (foreign_slot,) = item.item.slots, foreign.item.slots
        receipt = own_slot / "receipt.json"
        original = receipt.read_bytes()
        write(receipt, (foreign_slot / "receipt.json").read_bytes())
        query = {"release_ref": item.item.release_ref, "lookup_ref": "lookup-" + "a" * 32}
        with item.listen():
            assert item.post("readback", query, identity="recipient")[0] == 403
            write(receipt, original)
            assert item.post("readback", query, identity="recipient")[0] == 200
        assert item.one_effect() == original


def test_dispatcher_connection_scope_rotation_revocation_and_expiry(tmp_path):
    item = Tenant(tmp_path, "connection")
    with item.listen():
        for kwargs in ({"token": "GENERATED_BAD_TOKEN"}, {"audience": "generated-foreign"}):
            assert item.dispatch(**kwargs)[0] == 403
            item.unchanged()
        for field, value in (
            ("tenant", "foreign"),
            ("destination_ref", "foreign"),
            ("actor", "dispatcher"),
        ):
            code, _ = item.post(
                "dispatch",
                {"release_ref": item.item.release_ref, field: value},
                token=item.token,
                audience=item.body["audience"],
            )
            assert code in {400, 403}
            item.unchanged()
        expiry = item.body["expires_at"]
        item.seal(expires_at=int(time.time()) - 1)
        assert item.dispatch()[0] == 403
        item.unchanged()
        item.seal(expires_at=expiry)
        old_token = item.token
        item.token = secrets.token_urlsafe(32)
        item.seal(dispatcher_token_sha256=sha(item.token.encode()))
        assert item.dispatch(token=old_token)[0] == 403
        item.unchanged()
        marker = item.worker / (
            "handoff-connection-revoked-"
            + digest(
                [
                    item.body["tenant"],
                    item.body["engagement"],
                    item.body["connection_ref"],
                ]
            )
            + ".json"
        )
        revoked = subprocess.run(
            [
                sys.executable,
                "-m",
                "opentour_cell.pilot.handoff_https",
                "--connection",
                str(item.connection),
                "--revoke",
            ],
            capture_output=True,
            timeout=20,
        )
        assert revoked.returncode == 0
        assert marker.exists()
        assert item.dispatch()[0] == 403
        item.unchanged()
        # New explicit private grant keeps the old revocation marker intact.
        item.seal(connection_ref="generated-renewed-connection")
        opaque(item.dispatch(), item.item.release_ref, "delivery_unconfirmed")
        opaque(item.dispatch(), item.item.release_ref, "delivered")
        assert item.dispatch(token=old_token)[0] == 403
        assert marker.exists()
    item.one_effect()


def test_recipient_key_is_separate_and_current_for_private_routes(tmp_path):
    item = Tenant(tmp_path, "recipient-auth")
    query = {"release_ref": item.item.release_ref, "lookup_ref": "lookup-" + "a" * 32}
    with item.listen():
        for operation, body in (
            ("fetch", {"ticket": "generated-unknown-ticket"}),
            ("readback", query),
        ):
            assert (
                item.post(operation, body, token=item.token, audience=item.body["audience"])[0]
                == 403
            )
            assert item.post(operation, body, identity="replacement")[0] == 403
        assert (
            item.post("dispatch", {"release_ref": item.item.release_ref}, identity="recipient")[0]
            == 403
        )
        item.unchanged()
        item.seal(recipient_tls_sha256=item.certificates.fingerprint("replacement"))
        assert item.post("readback", query, identity="recipient")[0] == 403
        assert item.post("readback", query, identity="replacement")[0] == 200
        item.unchanged()
        item.seal(recipient_tls_sha256=item.certificates.fingerprint("recipient"))
        opaque(item.dispatch(), item.item.release_ref, "delivery_unconfirmed")
        opaque(item.dispatch(), item.item.release_ref, "delivered")
    item.one_effect()


def test_unverifiable_independent_receipt_never_resends(tmp_path):
    item = Tenant(tmp_path, "unknown")
    with item.listen():
        opaque(item.dispatch(), item.item.release_ref, "delivery_unconfirmed")
    original = item.one_effect()
    (slot,) = item.item.slots
    receipt_path = slot / "receipt.json"
    write(receipt_path, json.loads(original) | {"receipt_signature": "GENERATED_INVALID_SIGNATURE"})
    corrupted = receipt_path.read_bytes()
    item.offer.unlink()
    with item.listen():
        for _ in range(3):
            held = opaque(item.dispatch(), item.item.release_ref, "delivery_unconfirmed")
            assert held["receiver_receipt_ref"] is None
            assert held["owner_action"]
            assert item.item.slots == [slot] and receipt_path.read_bytes() == corrupted
        write(receipt_path, original)
        opaque(item.dispatch(), item.item.release_ref, "delivered")
    assert item.one_effect() == original and not item.offer.exists()


@pytest.mark.parametrize("expired", [False, True])
def test_current_worker_permission_denies_delivery(tmp_path, expired):
    item = Tenant(tmp_path, "worker-permission")
    with item.listen():
        if expired:
            authorize(
                item.item.outbox, item.item.permit, item.item.key, expires_at=int(time.time()) - 1
            )
        else:
            write(item.worker / "handoff-permission-key.json", {"key": secrets.token_hex(32)})
        assert item.dispatch()[0] == 403
        assert item.item.slots == []


def test_different_client_certificate_cannot_reuse_custody_signing_key(tmp_path):
    item = Tenant(tmp_path, "same-key")
    key = Path(read(item.item.receiver.grant_file)["recipient_key"])
    item.certificates.same_key_client(key)
    identity = item.certificates.root / "custody-auth.crt"
    assert identity.read_bytes() != item.custody_certificate.read_bytes()
    custody_public = item.certificates.openssl(
        "x509", "-in", str(item.custody_certificate), "-pubkey", "-noout"
    )
    tls_public = item.certificates.openssl("x509", "-in", str(identity), "-pubkey", "-noout")
    assert tls_public == custody_public  # Real same key, different valid client certificate.
    item.seal(recipient_tls_sha256=item.certificates.fingerprint("custody-auth"))
    with item.listen():
        code, _ = item.post(
            "readback",
            {"release_ref": item.item.release_ref, "lookup_ref": "lookup-" + "b" * 32},
            identity="custody-auth",
        )
        assert code == 403
        assert item.item.slots == []
        assert item.item.outbox.status(item.item.selection, "dispatcher").stage == "queued"


@pytest.mark.parametrize("change", ["permission", "clock"])
def test_final_fetch_publication_rechecks_current_authority(tmp_path, monkeypatch, change):
    item = Tenant(tmp_path, "publication-" + change)
    original_fetch = handoff_connection.HandoffConnection.fetch
    observed = observe_published_fetch(monkeypatch)
    read_payload = []

    def after_payload_read(connection, conn, ticket):
        result = original_fetch(connection, conn, ticket)
        read_payload.append(sha(result[0]))
        if change == "permission":
            authorize(
                item.item.outbox,
                item.item.permit,
                item.item.key,
                expires_at=int(time.time()) - 1,
            )
        else:
            later = connection.clock() + 60
            connection.clock = lambda: later
        return result

    monkeypatch.setattr(handoff_connection.HandoffConnection, "fetch", after_payload_read)
    with item.listen():
        opaque(item.dispatch(), item.item.release_ref, "delivery_unconfirmed")
    assert read_payload == [item.package_sha256]
    assert len(observed) == 1 and observed[0][0:2] == (403, "application/json")
    assert item.item.slots == []
    with item.item.outbox.store._db() as db:
        assert db.execute("SELECT count(*) FROM release_outbox").fetchone()[0] == 1
        assert (
            db.execute("SELECT stage FROM release_outbox").fetchone()[0] == "delivery_unconfirmed"
        )


def observe_published_fetch(monkeypatch):
    """Observe actual TLS headers after the production pre-publication callback."""
    observed = []
    original = handoff_https._Handler.end_headers

    def end_headers(handler):
        if handler.path == handoff_https.FETCH_PATH:
            headers = b"".join(handler._headers_buffer).decode("ascii")
            lines = headers.split("\r\n")
            fields = dict(line.split(": ", 1) for line in lines[1:] if ": " in line)
            observed.append(
                (int(lines[0].split()[1]), fields["Content-Type"], int(fields["Content-Length"]))
            )
        return original(handler)

    monkeypatch.setattr(handoff_https._Handler, "end_headers", end_headers)
    return observed


def test_successful_fetch_ticket_is_single_use_for_same_recipient(tmp_path, monkeypatch):
    item = Tenant(tmp_path, "single-use")
    fetched = []
    original = handoff_connection.HandoffConnection.fetch
    published = observe_published_fetch(monkeypatch)

    def capture(connection, conn, ticket):
        result = original(connection, conn, ticket)
        fetched.append(ticket)
        return result

    monkeypatch.setattr(handoff_connection.HandoffConnection, "fetch", capture)
    with item.listen():
        opaque(item.dispatch(), item.item.release_ref, "delivery_unconfirmed")
        assert len(fetched) == 1
        assert published == [(200, "application/zip", item.offer.stat().st_size)]
        item.offer.unlink()
        for _ in range(3):
            assert item.post("fetch", {"ticket": fetched[0]}, identity="recipient")[0] == 403
        assert len(published) == 4
        assert all(row[0:2] == (403, "application/json") for row in published[1:])
        opaque(item.dispatch(), item.item.release_ref, "delivered")
    item.one_effect()


def test_mutated_sender_archive_is_not_published_over_https(tmp_path, monkeypatch):
    item = Tenant(tmp_path, "mutated-archive")
    published = observe_published_fetch(monkeypatch)
    write(item.offer, b"GENERATED_MUTATED_SENDER_ARCHIVE")
    with item.listen():
        opaque(item.dispatch(), item.item.release_ref, "delivery_unconfirmed")
    assert len(published) == 1 and published[0][0:2] == (403, "application/json")
    assert item.item.slots == []
    with item.item.outbox.store._db() as db:
        assert db.execute("SELECT count(*) FROM release_outbox").fetchone()[0] == 1
        assert (
            db.execute("SELECT stage FROM release_outbox").fetchone()[0] == "delivery_unconfirmed"
        )
