"""Generated keys and payloads; no configured provider credentials are used."""

from datetime import timedelta

import pytest
from cryptography.fernet import Fernet
from opentour_control.auth.config import AuthError, FernetSealingPort


def test_sealed_payload_scope_expiry_rotation_and_tampering(identity):
    old_key, new_key = Fernet.generate_key(), Fernet.generate_key()
    old = FernetSealingPort((old_key,), 600)
    scope = "session:issuer:subject:sid"
    sealed = old.seal(
        "GENERATED_REFRESH_SECRET", scope, identity.now, identity.now + timedelta(seconds=30)
    )
    assert "GENERATED_REFRESH_SECRET" not in sealed
    rotated = FernetSealingPort((new_key, old_key), 600)
    assert rotated.open(sealed, scope, identity.now) == "GENERATED_REFRESH_SECRET"
    for port, value, binding, now in [
        (rotated, sealed, "session:issuer:other:sid", identity.now),
        (rotated, sealed, scope, identity.now + timedelta(seconds=30)),
        (rotated, sealed, scope, identity.now - timedelta(seconds=1)),
        (FernetSealingPort((new_key,), 600), sealed, scope, identity.now),
        (rotated, "a" + sealed[1:], scope, identity.now),
    ]:
        with pytest.raises(AuthError, match="SESSION_CREDENTIALS_UNAVAILABLE"):
            port.open(value, binding, now)


def test_no_implicit_sealing_key_or_lifetime_extension(identity):
    with pytest.raises(ValueError):
        FernetSealingPort((), 600)
    port = FernetSealingPort((Fernet.generate_key(),), 600)
    with pytest.raises(AuthError):
        port.seal("generated", "scope", identity.now, identity.now + timedelta(seconds=601))
