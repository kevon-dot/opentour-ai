"""Finite test-only subprocess binding to the real identity repository, not an HTTP API.

Inputs and outputs stay on private pipes. Only the parent probe emits a sanitized
summary. No existing database URL, provider account or real secret is accepted.
"""

import hashlib
import hmac
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from opentour_control.auth.config import AuthError, FernetSealingPort, digest, timestamp
from opentour_control.auth.repository import PostgresIdentityRepository, Session, connection_factory


def run(config, command):
    now = datetime.fromtimestamp(command.get("now", config["now"]) / 1000, UTC)
    repository = PostgresIdentityRepository(connection_factory(config["dsn"]))
    sealer = FernetSealingPort((config["key"].encode(),), 86_400)
    action = command["action"]

    def scope(session):
        return json.dumps(["credentials", session.issuer, session.subject, session.session_id])

    def current(handle):
        session = repository.load_session(digest(handle), now, allow_ended=True)
        csrf_token = hmac.new(config["key"].encode(), handle.encode(), hashlib.sha256).hexdigest()
        if session.ended_at:
            return {
                "handle": handle,
                "csrfToken": csrf_token,
                "ended": True,
                "generation": session.generation,
            }
        private = json.loads(
            sealer.open(repository.read_credentials(session, now), scope(session), now)
        )
        return {
            "handle": handle,
            "ended": False,
            "reconciliationRequired": session.reconciliation_required,
            "csrfToken": csrf_token,
            "credentials": private["credentials"],
            "identity": {
                "actor": {"kind": "human", "subject": session.subject, "issuer": session.issuer},
                "session_id": session.session_id,
                "session_generation": session.generation,
                "identity_checked_at": timestamp(now),
                "authentication_time": private["claims"]["authenticationTime"],
                "assurance_level": "unknown",
            },
        }

    if action == "create_flow":
        flow = command["flow"]
        state = digest(flow["state"] + "." + flow["nonce"])
        expires = datetime.fromtimestamp(flow["expiresAt"] / 1000, UTC)
        sealed = sealer.seal(json.dumps(flow), state + ":" + flow["browserDigest"], now, expires)
        repository.create_auth_transaction(state, flow["browserDigest"], sealed, now, expires)
        return None
    if action == "consume_flow":
        state = digest(command["state"])
        sealed = repository.consume_auth_transaction(state, command["browser"], now)
        return json.loads(sealer.open(sealed, state + ":" + command["browser"], now))
    if action in {"admit", "admit_unknown"}:
        import secrets

        claims = command["claims"]
        if (
            claims["issuer"] != config["issuer"]
            or claims["subject"] != command["credentials"]["userId"]
        ):
            raise AuthError("SESSION_INVALID")
        expires = now + timedelta(days=1)
        session = Session(
            claims["issuer"],
            claims["sessionId"],
            claims["subject"],
            0,
            None,
            None,
            False,
            now,
            expires,
            None,
            expires,
        )
        handle = secrets.token_urlsafe(32)
        sealed = sealer.seal(
            json.dumps({"credentials": command["credentials"], "claims": claims}),
            scope(session),
            now,
            expires,
        )
        repository.create_session(session, digest(handle), sealed_payload=sealed)
        if action == "admit_unknown":
            raise AuthError("SESSION_ADMISSION_UNCONFIRMED", 503)
        return current(handle)
    if action == "current":
        return current(command["handle"])
    if action in {"replace", "end", "end_unknown", "reconcile"}:
        session = repository.load_session(
            digest(command["handle"]), now, allow_ended=action in {"end", "end_unknown"}
        )
        if session.generation != command["generation"] and session.ended_at is None:
            raise AuthError("STALE_SESSION_CONTEXT", 409)
        if action in {"end", "end_unknown"}:
            ended = repository.end_session(session, now)
            if action == "end_unknown":
                raise AuthError("SESSION_END_UNCONFIRMED", 503)
            return {"sessionId": ended.session_id, "generation": ended.generation}
        if action == "reconcile":
            repository.require_reconciliation(session)
            return None
        claims = command["claims"]
        if (claims["issuer"], claims["subject"], claims["sessionId"]) != (
            session.issuer,
            session.subject,
            session.session_id,
        ):
            raise AuthError("SESSION_INVALID")
        sealed = sealer.seal(
            json.dumps({"credentials": command["credentials"], "claims": claims}),
            scope(session),
            now,
            session.expires_at,
        )
        repository.replace_credentials(session, sealed, now)
        return None
    if action == "inspect":
        with repository.connect() as connection:
            row = connection.execute(
                "SELECT count(*) AS count, bool_and(sealed_payload NOT LIKE %s) AS sealed "
                "FROM opentour_identity.session_credentials",
                ("%SENTINEL%",),
            ).fetchone()
            return row
    raise AuthError("TEST_OPERATION_INVALID", 422)


if __name__ == "__main__":
    try:
        config = json.loads(Path(sys.argv[1]).read_text())
        raw = sys.stdin.buffer.read(65_537)
        if len(raw) > 65_536:
            raise AuthError("TEST_INPUT_LIMIT", 422)
        print(json.dumps({"value": run(config, json.loads(raw))}))
    except AuthError as error:
        print(json.dumps({"error": {"code": error.code, "status": error.status}}))
    except Exception:
        print(json.dumps({"error": {"code": "TEST_PORT_FAILED", "status": 503}}))
