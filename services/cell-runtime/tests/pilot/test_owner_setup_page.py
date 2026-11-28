"""Generated owner token, private page and actual settings consent transaction."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime
from html import unescape
from urllib.parse import urlencode

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from opentour_cell.pilot import owner_settings, owner_setup_page
from opentour_cell.pilot.readout import parse_manifest
from opentour_control.auth.workos_provider import WorkOSTokenVerifier
from test_owner_settings import save
from test_owner_settings_consent import generated_consent as generated_consent
from test_signed_records import kit as kit
from test_signed_records import sign

_ORIGIN = "https://owner.example.test"
_ISSUER = "https://identity.example.test"
_SUBJECT = "generated-owner-subject"
_OWNER = "generated-owner-role"


class Page:
    def __init__(self, setup, kit):
        self.setup, self.kit = setup, kit
        self.directory = setup.customer.owner
        self.binding = self.directory / owner_setup_page._CONFIG_FILE
        authority = parse_manifest(setup.authority.read_bytes())
        save(setup.authority, {**authority, "workflow_owner": _OWNER})
        self.config = {
            "schema_version": "opentour.pilot.owner-setup-gateway.v1",
            "origin": _ORIGIN,
            "identity": {
                "issuer": _ISSUER,
                "audience": "https://audience.example.test",
                "jwks_url": f"{_ISSUER}/keys",
                "client_id": "generated-client",
                "credential_max_lifetime_seconds": 300,
            },
            "subject": _SUBJECT,
            "organization": setup.customer.org,
            "engagement": setup.customer.eng,
            "workflow": setup.policy["mandate"]["workflow"],
            "owner": _OWNER,
            "custodian": setup.owner,
            "authority_file": setup.authority.name,
            "connection_file": setup.policy["connection_file"],
            "connection_revision": setup.policy["agreement"]["connection_revision"],
        }
        save(self.binding, self.config)
        (self.directory / owner_setup_page._CSRF_FILE).write_bytes(b"g" * 32)
        (self.directory / owner_setup_page._CSRF_FILE).chmod(0o600)
        self.private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        jwk = jwt.algorithms.RSAAlgorithm.to_jwk(self.private.public_key(), as_dict=True)
        jwk["kid"] = "generated-key"
        binding = owner_setup_page._Binding.read(self.directory)
        verifier = WorkOSTokenVerifier(binding.identity, fetch=lambda: {"keys": [jwk]})
        self.app = owner_setup_page._app(self.directory, binding, verifier)
        self.token = self.issue()

    def issue(self, **updates):
        now = int(datetime.now(UTC).timestamp())
        return jwt.encode(
            {
                "iss": _ISSUER,
                "sub": _SUBJECT,
                "sid": "generated-session",
                "aud": self.config["identity"]["audience"],
                "iat": now - 1,
                "auth_time": now - 1,
                "exp": now + 180,
                **updates,
            },
            self.private,
            algorithm="RS256",
            headers={"kid": "generated-key"},
        )

    def request(self, *, form=None, token=True, origin=_ORIGIN, extras=(), query=b""):
        headers = [(b"host", b"owner.example.test"), *extras]
        if token is not False:
            headers.append(
                (b"authorization", f"Bearer {self.token if token is True else token}".encode())
            )
        if form is not None:
            headers.extend(
                [
                    (b"content-type", b"application/x-www-form-urlencoded"),
                    (b"origin", origin.encode()),
                ]
            )
        body = b"" if form is None else urlencode(form).encode()
        path = owner_setup_page.PATH
        events = []

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(event):
            events.append(event)

        asyncio.run(
            self.app(
                {
                    "type": "http",
                    "method": "GET" if form is None else "POST",
                    "scheme": "http",
                    "path": path,
                    "raw_path": path.encode(),
                    "query_string": query,
                    "headers": headers,
                },
                receive,
                send,
            )
        )
        return events[0]["status"], dict(events[0]["headers"]), events[1]["body"].decode()

    def form(self, page, *, operation="save", base="initial"):
        csrf = re.search('name="csrf_token" value="([a-f0-9]{64})"', page)[1]
        pair = re.search('<option value="([a-f0-9]{64})"', page)
        policy = json.loads(json.dumps(self.setup.policy)) if operation in {"save", "renew"} else {}
        if policy:
            del policy["connection_file"]
            del policy["agreement"]["connection_revision"]
            for key in ("trace_source", "correction_source", "agreement_revision"):
                del policy["mandate"][key]
        return {
            "action": "preview",
            "csrf_token": csrf,
            "expected_revision": base,
            "operation": operation,
            "policy": json.dumps(policy) if policy else "",
            "source_pair": pair[1] if pair is not None and policy else "",
        }

    def submission(self, form, preview, *signers):
        match = re.search('<textarea readonly rows="10">(.*?)</textarea>', preview, re.S)
        payload = json.loads(unescape(match[1]))
        return {
            "action": "submit",
            "csrf_token": form["csrf_token"],
            "signed_packet": sign(payload, owner_settings.CONSENT_PURPOSE, *signers).decode(),
        }


@pytest.fixture
def page(generated_consent):
    return Page(*generated_consent)


def test_signed_in_owner_selects_catalog_and_commits_exact_dual_signed_settings(page):
    status, headers, original = page.request()
    assert status == 200
    assert headers[b"cache-control"] == b"private, no-store"
    assert b"frame-ancestors 'none'" in headers[b"content-security-policy"]
    assert "Independent corrections" in original and page.token not in original
    form = page.form(original)
    preview_status, _, preview = page.request(form=form)
    assert preview_status == 200
    assert form["source_pair"] in preview
    assert page.setup.service.inspect(actor=page.setup.owner)["current"] is None
    owner = page.kit.customer
    custodian = page.kit.operator
    assert page.request(form=page.submission(form, preview, owner))[0] == 403
    submit = page.submission(form, preview, owner, custodian)
    status, _, result = page.request(form=submit)
    current = page.setup.service.inspect(actor=page.setup.owner)["current"]
    assert status == 200 and "Saved settings" in result and current["revision"] in result
    assert 'href="/owner/setup">Review current settings</a>' in result
    assert "<form" not in result and "source_pair" not in result
    assert current["workflow_owner"] == _OWNER and current["actor"] == page.setup.owner
    assert current["binding"]["pair_revision"] == form["source_pair"]
    assert page.request(form=submit)[0] == 200
    assert page.request(form=form)[0] == 409
    assert "resources/read" not in str(page.setup.protocol())


def test_current_browser_identity_and_form_controls_deny_without_catalog_disclosure(page):
    before = page.setup.protocol()
    for denied in (
        page.request(token=False, extras=((b"x-authenticated-subject", _SUBJECT.encode()),)),
        page.request(token=page.issue(sub="generated-other")),
        page.request(token=page.issue(iss="https://other.example.test")),
        page.request(token=page.issue(iat=1, auth_time=1, exp=2)),
        page.request(query=b"tenant=generated-other"),
    ):
        assert denied[0] == 403
        assert "source_pair" not in denied[2]
    assert page.setup.protocol() == before
    form = page.form(page.request()[2])
    before = page.setup.protocol()
    for denied in (
        page.request(form=form, origin="https://other.example.test"),
        page.request(form=form, token=page.issue(sid="generated-new-session")),
        page.request(form={**form, "organization": "generated-other"}),
    ):
        assert denied[0] == 403
    assert page.setup.protocol() == before
    assert page.request(form={**form, "source_pair": "a" * 64})[0] == 403
    assert page.setup.service.inspect(actor=page.setup.owner)["current"] is None


def test_gateway_binding_change_before_commit_rolls_back_settings(page, monkeypatch):
    form = page.form(page.request()[2])
    _, _, preview = page.request(form=form)
    submit = page.submission(form, preview, page.kit.customer, page.kit.operator)
    live = owner_settings.OwnerSettings._live

    def rotate(service, policy, *, discover):
        result = live(service, policy, discover=discover)
        if not discover:
            save(page.binding, {**page.config, "subject": "generated-new-owner"})
        return result

    monkeypatch.setattr(owner_settings.OwnerSettings, "_live", rotate)
    assert page.request(form=submit)[0] == 403
    assert page.setup.service.inspect(actor=page.setup.owner)["current"] is None
    with page.setup.service.ledger._db(write=False) as db:
        assert db.execute("SELECT count(*) FROM owner_settings_revisions").fetchone()[0] == 0
        prior, _ = page.setup.service.ledger._mandate(db, page.setup.customer.mandate)
        assert prior["withdrawn"] is None
