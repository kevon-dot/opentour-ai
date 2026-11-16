"""Generated identity fixtures. Memory ports prove logic, never PostgreSQL durability."""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import jwt
import psycopg
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from opentour_control.auth.config import AuthError, IdentityConfig, digest
from opentour_control.auth.repository import Observation, SessionRevocationEvent
from opentour_control.auth.session import LocalMembership, SessionService
from opentour_control.auth.workos_provider import WorkOSTokenVerifier

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
ISSUER = "https://identity.example.invalid/"
AUDIENCE = "https://api.example.invalid"


class MemoryRepository:
    def __init__(self):
        self.sessions, self.bindings, self.observations = {}, {}, {}
        self.inbox, self.outbox, self.cursors, self.denials = {}, [], {}, set()
        self.fail_commit = False
        self.load_calls = 0

    def create_session(self, session, binding_sha256):
        if self.fail_commit:
            raise psycopg.OperationalError("synthetic secret must not escape")
        current = self.sessions.get(session.session_id, session)
        if current.ended_at or current.session_id in self.denials:
            raise AuthError("SESSION_ENDED")
        self.sessions[current.session_id] = current
        self.bindings[binding_sha256] = (current.session_id, current.generation)
        return current

    def load_session(self, binding_sha256, now, *, allow_ended=False):
        self.load_calls += 1
        if binding_sha256 not in self.bindings:
            raise AuthError("SESSION_INVALID")
        sid, generation = self.bindings[binding_sha256]
        session = self.sessions[sid]
        if now >= session.expires_at:
            raise AuthError("SESSION_INVALID")
        if session.ended_at or sid in self.denials:
            if allow_ended:
                return session
            raise AuthError("SESSION_ENDED")
        if generation != session.generation:
            raise AuthError("STALE_SESSION_CONTEXT", 409)
        return session

    def end_session(self, session, now):
        if self.fail_commit:
            raise psycopg.OperationalError("synthetic secret must not escape")
        current = self.sessions[session.session_id]
        if current.ended_at:
            return current
        if current.generation != session.generation:
            raise AuthError("STALE_SESSION_CONTEXT", 409)
        ended = replace(
            current,
            ended_at=now,
            generation=current.generation + 1,
            selected_organization_id=None,
            selected_membership_id=None,
        )
        self.sessions[session.session_id] = ended
        self.denials.add(session.session_id)
        return ended

    def clear_selection(self, session, binding_sha256):
        current = self.sessions[session.session_id]
        if current.generation != session.generation:
            raise AuthError("STALE_SESSION_CONTEXT", 409)
        cleared = replace(
            current,
            selected_organization_id=None,
            selected_membership_id=None,
            generation=current.generation + 1,
        )
        self.sessions[current.session_id] = cleared
        self.bindings[binding_sha256] = (cleared.session_id, cleared.generation)
        return cleared

    def observation(self, issuer, membership_id):
        return self.observations.get((issuer, membership_id))

    def require_reconciliation(self, session):
        self.sessions[session.session_id] = replace(session, reconciliation_required=True)

    def record_observation(self, observation):
        key = observation.issuer, observation.membership_id
        previous = self.observations.get(key)
        if previous and previous.locally_denied:
            observation = replace(observation, status="removed", locally_denied=True)
        self.observations[key] = observation
        return observation

    def cursor(self, issuer):
        return self.cursors.get(issuer)

    def commit_batch(self, issuer, expected_cursor, cursor, events):
        if self.cursor(issuer) != expected_cursor:
            raise AuthError("IDENTITY_SYNC_CONFLICT", 409)
        before = copy.deepcopy((self.inbox, self.outbox, self.observations, self.denials))
        try:
            for event in events:
                key = issuer, event.event_id
                if key in self.inbox:
                    if self.inbox[key] != event.body_sha256:
                        raise AuthError("IDENTITY_EVENT_CONFLICT", 409)
                    continue
                self.inbox[key] = event.body_sha256
                if isinstance(event, SessionRevocationEvent):
                    self.denials.add(event.session_id)
                else:
                    self.record_observation(event.observation)
                self.outbox.append(key)
            if self.fail_commit:
                raise psycopg.OperationalError("simulated crash before commit")
            self.cursors[issuer] = cursor
        except Exception:
            self.inbox, self.outbox, self.observations, self.denials = before
            raise


@pytest.fixture(scope="session")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def identity(signing_key):
    config = IdentityConfig(
        ISSUER, AUDIENCE, "https://api.workos.com/sso/jwks/client_fixture", "client_fixture", 86_400
    )
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(signing_key.public_key()))
    public.update(kid="key-a", alg="RS256", use="sig")
    keys = {"keys": [public]}
    fetches = []

    def fetch():
        fetches.append(True)
        return keys

    def token(**changes):
        claims = {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": "user_a",
            "sid": "session_a",
            "org_id": "org_a",
            "iat": int(NOW.timestamp()) - 60,
            "exp": int(NOW.timestamp()) + 600,
            "auth_time": int(NOW.timestamp()) - 120,
        }
        claims.update(changes)
        return jwt.encode(claims, signing_key, algorithm="RS256", headers={"kid": "key-a"})

    repository = MemoryRepository()
    members = {
        "org_a": LocalMembership(
            "user_a", "org_a", "member_a", "provider_a", True, "authority_a", 2
        ),
        "org_b": LocalMembership(
            "user_a", "org_b", "member_b", "provider_b", True, "authority_b", 3
        ),
    }
    authority = SimpleNamespace(current=lambda subject, org: members.get(org))
    observation = Observation(ISSUER, "provider_a", "org_a", "user_a", "active", NOW, NOW)
    repository.record_observation(observation)
    provider = SimpleNamespace(current_membership=lambda member, now: observation)
    verifier = WorkOSTokenVerifier(config, fetch=fetch, clock=lambda: NOW)
    service = SessionService(config, verifier, repository, authority, provider, clock=lambda: NOW)
    context, handle = service.admit(token())
    repository.sessions["session_a"] = replace(
        repository.sessions["session_a"],
        selected_organization_id="org_a",
        selected_membership_id="member_a",
    )
    return SimpleNamespace(
        config=config,
        verifier=verifier,
        repository=repository,
        service=service,
        provider=provider,
        members=members,
        token=token,
        handle=handle,
        keys=keys,
        public=public,
        fetches=fetches,
        context=context,
        now=NOW,
        issuer=ISSUER,
        audience=AUDIENCE,
        binding_digest=digest(handle),
        key=signing_key,
    )
