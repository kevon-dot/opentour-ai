"""Generated event replay tests. Actual SQL atomicity is qualified separately."""

from dataclasses import replace
from types import SimpleNamespace

import psycopg
import pytest
from opentour_control.auth.config import AuthError
from opentour_control.auth.identity_sync import IdentitySynchronizer, ProviderEvent


def synchronizer(identity, events):
    return IdentitySynchronizer(
        identity.config,
        identity.repository,
        SimpleNamespace(page=lambda after: events),
        identity.provider,
        clock=lambda: identity.now,
    )


def event(kind="organization_membership.updated", event_id="event_a", digest="a" * 64):
    return ProviderEvent(event_id, kind, digest, "provider_a", "user_a", "identity_org_a")


def test_remove_then_late_active_stays_denied(identity):
    sync = synchronizer(identity, [event("organization_membership.deleted")])
    assert sync.poll_once() == 1
    sync.events = SimpleNamespace(page=lambda after: [event(event_id="event_b")])
    sync.poll_once()
    observed = identity.repository.observation(identity.issuer, "provider_a")
    assert observed.status == "removed" and observed.locally_denied
    with pytest.raises(AuthError, match="MEMBERSHIP_INACTIVE"):
        identity.service.authorization_context(identity.token(), identity.handle)


def test_duplicate_and_conflicting_event_digest(identity):
    sync = synchronizer(identity, [event()])
    sync.poll_once()
    sync.poll_once()
    assert len(identity.repository.inbox) == len(identity.repository.outbox) == 1
    sync.events = SimpleNamespace(page=lambda after: [event(digest="b" * 64)])
    with pytest.raises(AuthError, match="IDENTITY_EVENT_CONFLICT"):
        sync.poll_once()


def test_crash_before_cursor_commit_replays_once(identity):
    sync = synchronizer(identity, [event()])
    identity.repository.fail_commit = True
    with pytest.raises(psycopg.OperationalError):
        sync.poll_once()
    assert not identity.repository.inbox and identity.repository.cursor(identity.issuer) is None
    identity.repository.fail_commit = False
    sync.poll_once()
    sync.poll_once()
    assert identity.repository.cursor(identity.issuer) == "event_a"
    assert len(identity.repository.outbox) == 1


def test_current_fetch_unknown_does_not_advance_cursor(identity):
    def unavailable(*args):
        raise AuthError("IDENTITY_FRESHNESS_UNAVAILABLE", 503)

    identity.provider.current_membership = unavailable
    with pytest.raises(AuthError):
        synchronizer(identity, [event()]).poll_once()
    assert not identity.repository.inbox and identity.repository.cursor(identity.issuer) is None


@pytest.mark.parametrize("status", ["pending_identity", "reconciliation_unknown", "removed"])
def test_unconfirmed_identity_never_grants(identity, status):
    prior = identity.repository.observation(identity.issuer, "provider_a")
    identity.provider.current_membership = lambda *args: replace(prior, status=status)
    synchronizer(identity, [event()]).poll_once()
    with pytest.raises(AuthError):
        identity.service.authorization_context(identity.token(), identity.handle)


def test_conflicting_scope_fails_batch(identity):
    with pytest.raises(AuthError, match="IDENTITY_EVENT_SCOPE_CONFLICT"):
        synchronizer(identity, [replace(event(), subject="user_b")]).poll_once()
    assert not identity.repository.inbox


def test_revoked_session_cannot_be_recreated(identity):
    revoked = replace(event("session.revoked"), resource_id="session_a")
    synchronizer(identity, [revoked]).poll_once()
    with pytest.raises(AuthError, match="SESSION_ENDED"):
        identity.service.admit(identity.token())


def test_webhook_wakeup_never_applies_payload(identity):
    sync = synchronizer(identity, [])
    sync.wake_after_verified_webhook()
    assert sync.wakeup.is_set() and not identity.repository.inbox
