"""Customer-local owner setup behind an explicitly configured private identity gateway."""

from __future__ import annotations

import argparse
import hmac
import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from opentour_control.auth.config import AuthError, IdentityConfig
from opentour_control.auth.workos_provider import TokenVerifier, VerifiedToken, WorkOSTokenVerifier
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect, Request
from starlette.responses import HTMLResponse
from starlette.routing import Route

from opentour_cell.pilot import owner_settings, repair_bundle, repair_workflow
from opentour_cell.pilot import workplace_context as workplace
from opentour_cell.pilot.operating_point import digest
from opentour_cell.pilot.owner_settings import Json, OwnerSettings, SettingsError
from opentour_cell.pilot.readout import parse_manifest
from opentour_cell.pilot.simple_repair import identifier

PATH = "/owner/setup"
_CONFIG_FILE = "owner-setup-binding.json"
_CSRF_FILE = "owner-setup-csrf.key"
_MAX_FORM = 256 * 1024
_HEADERS = {
    "Cache-Control": "private, no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
        "base-uri 'none'; frame-ancestors 'none'"
    ),
}
_STALE = frozenset({"settings_revision_conflict", "settings_mandate_revision_conflict"})


@dataclass(frozen=True)
class _Binding:
    origin: str
    identity: IdentityConfig
    subject: str
    organization: str
    engagement: str
    workflow: str
    owner: str
    custodian: str
    authority_file: str
    connection_file: str | None
    connection_revision: str | None

    @classmethod
    def read(cls, directory: Path) -> _Binding:
        raw = parse_manifest(repair_bundle._read(directory / _CONFIG_FILE, 8192))
        owner_settings._need(
            set(raw)
            == {
                "schema_version",
                "origin",
                "identity",
                "subject",
                "organization",
                "engagement",
                "workflow",
                "owner",
                "custodian",
                "authority_file",
                "connection_file",
                "connection_revision",
            }
            and raw["schema_version"] == "opentour.pilot.owner-setup-gateway.v1"
            and isinstance(raw["identity"], dict)
            and set(raw["identity"])
            == {"issuer", "audience", "jwks_url", "client_id", "credential_max_lifetime_seconds"},
            "invalid_owner_setup_binding",
        )
        origin = raw["origin"]
        owner_settings._need(isinstance(origin, str), "invalid_owner_setup_binding")
        parts = urlsplit(origin)
        owner_settings._need(
            parts.scheme == "https"
            and parts.hostname is not None
            and not parts.username
            and origin == f"https://{parts.netloc}"
            and origin.isascii()
            and not any(character.isspace() for character in origin),
            "invalid_owner_setup_binding",
        )
        connection = raw["connection_file"]
        revision = raw["connection_revision"]
        owner_settings._need(
            (connection is None and revision is None)
            or (
                isinstance(connection, str)
                and isinstance(revision, str)
                and re.fullmatch(r"[0-9a-f]{64}", revision)
            ),
            "invalid_owner_setup_connection",
        )
        return cls(
            origin,
            IdentityConfig(**raw["identity"]),
            identifier(raw["subject"]),
            identifier(raw["organization"]),
            identifier(raw["engagement"]),
            identifier(raw["workflow"]),
            identifier(raw["owner"]),
            identifier(raw["custodian"]),
            repair_workflow._name(raw["authority_file"]),
            repair_workflow._name(connection) if connection is not None else None,
            revision,
        )


@dataclass(frozen=True)
class _Access:
    binding: _Binding
    token: VerifiedToken = field(repr=False)
    csrf: str = field(repr=False)


class _Page:
    def __init__(self, directory: Path, binding: _Binding, verifier: TokenVerifier):
        self.directory = directory
        self.binding = binding
        self.verifier = verifier

    def _access(self, request: Request) -> _Access:
        binding = _Binding.read(self.directory)
        owner_settings._need(
            binding.origin == self.binding.origin
            and binding.identity == self.binding.identity
            and request.headers.getlist("host") == [urlsplit(binding.origin).netloc],
            "owner_setup_gateway_changed",
        )
        headers = request.headers.getlist("authorization")
        owner_settings._need(
            len(headers) == 1 and re.fullmatch(r"Bearer [A-Za-z0-9_.-]{1,16384}", headers[0]),
            "owner_setup_identity_required",
        )
        token = self.verifier.verify(headers[0][7:], datetime.now(UTC))
        owner_settings._need(
            (token.issuer, token.subject) == (binding.identity.issuer, binding.subject)
            and token.expires_at > datetime.now(UTC),
            "owner_setup_identity_denied",
        )
        key = repair_bundle._read(self.directory / _CSRF_FILE, 32)
        owner_settings._need(len(key) == 32, "owner_setup_csrf_unavailable")
        scope = [
            token.issuer,
            token.subject,
            token.session_id,
            binding.origin,
            binding.organization,
            binding.engagement,
            binding.workflow,
            binding.owner,
            binding.custodian,
            binding.authority_file,
            binding.connection_file,
            binding.connection_revision,
        ]
        proof = hmac.digest(key, json.dumps(scope, separators=(",", ":")).encode(), "sha256").hex()
        return _Access(binding, token, proof)

    def _catalog(self, binding: _Binding, service: OwnerSettings) -> Json | None:
        if binding.connection_file is None:
            return None
        try:
            path = self.directory / binding.connection_file
            cfg = workplace._live(service.store, path, binding.connection_revision)
            if (cfg["scope"]["workflow"], cfg["scope"]["owner_id"]) != (
                binding.workflow,
                binding.custodian,
            ):
                return None
            return workplace.discover_workplace(
                service.store, path, expected_connection_revision=binding.connection_revision
            )
        except (OSError, ValueError, sqlite3.Error):
            return None

    @staticmethod
    def _pair_policy(policy: Json, binding: _Binding, catalog: Json) -> Json:
        mandate = policy.get("mandate")
        agreement = policy.get("agreement")
        owner_settings._need(
            isinstance(mandate, dict) and isinstance(agreement, dict), "invalid_owner_setup_form"
        )
        assert isinstance(mandate, dict) and isinstance(agreement, dict)
        owner_settings._need(
            policy.get("connection_file", binding.connection_file) == binding.connection_file
            and agreement.get("connection_revision", binding.connection_revision)
            == binding.connection_revision,
            "owner_setup_connection_denied",
        )
        for role in workplace.ROLES:
            key = f"{role}_source"
            source = catalog["sources"][role]["source_id"]
            owner_settings._need(mandate.get(key, source) == source, "owner_setup_pair_changed")
            mandate[key] = source
        policy["connection_file"] = binding.connection_file
        agreement["connection_revision"] = binding.connection_revision
        mandate["agreement_revision"] = digest(agreement)
        return policy

    def _operate(self, request: Request, access: _Access, action: dict[str, str]) -> HTMLResponse:
        from opentour_cell.pilot.owner_setup_view import render_owner_setup

        binding = access.binding
        service = OwnerSettings.open_readback(
            self.directory,
            binding.organization,
            binding.engagement,
            authority_file=self.directory / binding.authority_file,
        )
        authority = service._authority(binding.owner)
        owner_settings._need(
            (authority["workflow_owner"], authority["custodian"])
            == (binding.owner, binding.custodian),
            "owner_setup_role_changed",
        )

        def authorize(claim: Json | None = None) -> None:
            owner_settings._need(
                self._access(request) == access and service._authority(binding.owner) == authority,
                "owner_setup_authority_changed",
            )
            if claim is not None and claim["action"] in {"save", "renew"}:
                policy = claim["policy"]
                agreement = policy.get("agreement")
                owner_settings._need(
                    binding.connection_file is not None
                    and policy.get("connection_file") == binding.connection_file
                    and isinstance(agreement, dict)
                    and agreement.get("connection_revision") == binding.connection_revision,
                    "owner_setup_connection_denied",
                )

        preview = receipt = None
        catalog = None
        if action:
            received = action.get("csrf_token", "")
            owner_settings._need(
                request.headers.getlist("origin") == [binding.origin]
                and received.isascii()
                and hmac.compare_digest(received, access.csrf),
                "owner_setup_csrf_denied",
            )
            if action.get("action") == "preview":
                owner_settings._need(
                    action.keys()
                    == {
                        "action",
                        "csrf_token",
                        "operation",
                        "expected_revision",
                        "policy",
                        "source_pair",
                    }
                    and action["operation"] in owner_settings.CONSENT_ACTIONS,
                    "invalid_owner_setup_form",
                )
                base = action["expected_revision"]
                owner_settings._need(
                    base == "initial" or re.fullmatch(r"[0-9a-f]{64}", base),
                    "invalid_owner_setup_revision",
                )
                policy = action["policy"].encode()
                owner_settings._need(len(policy) <= 64 * 1024, "invalid_owner_setup_form")
                candidate = None
                if action["operation"] in {"save", "renew"}:
                    status = service.setup_readback(actor=binding.owner, workflow=binding.workflow)
                    owner_settings._need(
                        (status["revision"] or "initial") == base,
                        "settings_revision_conflict",
                    )
                    catalog = self._catalog(binding, service)
                    owner_settings._need(
                        catalog is not None and catalog["pair_revision"] == action["source_pair"],
                        "owner_setup_pair_changed",
                    )
                    assert catalog is not None
                    candidate = self._pair_policy(parse_manifest(policy), binding, catalog)
                else:
                    owner_settings._need(
                        not policy and not action["source_pair"],
                        "invalid_owner_setup_form",
                    )
                preview = service.preview_consent(
                    candidate,
                    actor=binding.custodian,
                    workflow=binding.workflow,
                    expected_revision=None if base == "initial" else base,
                    action=action["operation"],
                )
                if catalog is not None:
                    owner_settings._need(
                        preview["payload"]["claim"]["binding"]["pair_revision"]
                        == catalog["pair_revision"],
                        "owner_setup_pair_changed",
                    )
            else:
                owner_settings._need(
                    action.get("action") == "submit"
                    and action.keys() == {"action", "csrf_token", "signed_packet"},
                    "invalid_owner_setup_form",
                )
                accepted = service.save_consent(
                    action["signed_packet"].encode(),
                    actor=binding.custodian,
                    workflow=binding.workflow,
                    authorize=authorize,
                )
                receipt = {
                    key: accepted[key]
                    for key in ("action", "state", "revision", "mandate_revision")
                }
        status = service.setup_readback(actor=binding.owner, workflow=binding.workflow)
        if not action:
            catalog = self._catalog(binding, service)
        authorize()
        status["may_preview"] = receipt is None
        status["source_pairs"] = (
            [
                {
                    "id": catalog["pair_revision"],
                    "workflow_label": "Approved workflow traces",
                    "correction_label": "Independent corrections",
                }
            ]
            if catalog is not None
            else []
        )
        return HTMLResponse(
            render_owner_setup(status, csrf_token=access.csrf, preview=preview, receipt=receipt),
            headers=_HEADERS,
        )

    async def handle(self, request: Request) -> HTMLResponse:
        from opentour_cell.pilot.owner_setup_view import render_owner_setup

        action: dict[str, str] = {}
        try:
            owner_settings._need(
                request.scope.get("raw_path") == PATH.encode()
                and not request.scope["query_string"]
                and request.method in {"GET", "POST"},
                "invalid_owner_setup_request",
            )
            access = await run_in_threadpool(self._access, request)
            if request.method == "POST":
                owner_settings._need(
                    request.headers.getlist("content-type")
                    == ["application/x-www-form-urlencoded"],
                    "invalid_owner_setup_form",
                )
                body = bytearray()
                async for chunk in request.stream():
                    owner_settings._need(
                        len(body) + len(chunk) <= _MAX_FORM, "invalid_owner_setup_form"
                    )
                    body.extend(chunk)
                pairs = parse_qsl(
                    body.decode("ascii"),
                    keep_blank_values=True,
                    strict_parsing=True,
                    errors="strict",
                    max_num_fields=6,
                )
                action = dict(pairs)
                owner_settings._need(
                    bool(action) and len(action) == len(pairs),
                    "invalid_owner_setup_form",
                )
            return await run_in_threadpool(self._operate, request, access, action)
        except (AuthError, ValueError, TypeError, OSError, sqlite3.Error, ClientDisconnect) as exc:
            failure, status_code = "denied", 403
            if isinstance(exc, SettingsError):
                if str(exc) in _STALE:
                    failure, status_code = "stale", 409
                elif str(exc) == "settings_preview_expired":
                    failure, status_code = "expired", 409
            return HTMLResponse(render_owner_setup(None, failure=failure), status_code, _HEADERS)


def _app(directory: Path, binding: _Binding, verifier: TokenVerifier) -> Starlette:
    return Starlette(
        routes=[Route(PATH, _Page(directory, binding, verifier).handle, methods=["GET", "POST"])]
    )


def main() -> int:
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--socket-dir", required=True, type=Path)
    args = parser.parse_args()
    try:
        for path in (args.directory, args.socket_dir):
            owner_settings._need(path.is_absolute(), "owner_setup_private_mount_required")
            repair_bundle._private_directory(path)
        binding = _Binding.read(args.directory)
        owner_settings._need(
            len(repair_bundle._read(args.directory / _CSRF_FILE, 32)) == 32,
            "owner_setup_csrf_unavailable",
        )
        socket = args.socket_dir / "owner-setup.sock"
        owner_settings._need(
            not socket.exists() and not socket.is_symlink(), "socket_already_exists"
        )
    except (ValueError, TypeError, OSError):
        parser.exit(2, "The private owner setup gateway configuration is missing or invalid.\n")
    uvicorn.run(
        _app(args.directory, binding, WorkOSTokenVerifier(binding.identity)),
        uds=str(socket),
        access_log=False,
        proxy_headers=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
