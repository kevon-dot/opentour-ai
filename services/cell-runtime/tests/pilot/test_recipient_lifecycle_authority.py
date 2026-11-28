"""Check the worker adapter's boundary with the pinned platform verifier."""

from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from opentour_cell.pilot import recipient_lifecycle_authority as adapter
from opentour_cell.pilot.recipient_lifecycle import REPLAY_ISSUER, REPLAY_PURPOSE, LifecycleBinding
from test_repair_bundle import write


@pytest.fixture
def binding() -> LifecycleBinding:
    return LifecycleBinding(
        "generated-tenant",
        "generated-engagement",
        "generated-workflow",
        "generated-approval",
        "generated-proposal",
        "a" * 64,
        "b" * 64,
        "generated-recipient",
        "generated-destination",
        "release-" + "c" * 48,
        "received-" + "d" * 48,
        "e" * 64,
    )


def test_factory_holds_without_current_pinned_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(adapter, "import_module", lambda _: pytest.fail("registry was absent"))
    assert adapter.open_lifecycle_authority(tmp_path.resolve()) is None

    write(tmp_path / adapter.REGISTRY_FILE, {"unverified": True})

    def unapproved(_registry):
        raise ValueError("root_pin_not_approved")

    monkeypatch.setattr(
        adapter, "import_module", lambda _: SimpleNamespace(PilotVerifier=unapproved)
    )
    assert adapter.open_lifecycle_authority(tmp_path.resolve()) is None


def test_observations_and_policy_use_distinct_platform_purposes(tmp_path, monkeypatch, binding):
    now = datetime.now(UTC)
    raw_policy = {
        "profile_version": "1.0",
        "engagement_id": binding.engagement,
        "binding": asdict(binding),
        "issued_at": (now - timedelta(seconds=1)).isoformat(),
        "not_after": (now + timedelta(minutes=10)).isoformat(),
        "evaluation_only": True,
        "replay_key_id": "generated-recipient-key",
        "rollback": None,
    }
    policy_path = tmp_path / f"recipient-lifecycle-{binding.release_ref}.policy.json"
    write(policy_path, raw_policy)
    registry_path = tmp_path / adapter.REGISTRY_FILE
    write(registry_path, {"generated_registry": True})
    purposes = []
    observation = (
        b'{"signatures":[{"key_id":"generated-recipient-key",'
        b'"purpose":"opentour.pilot.recipient-replay.v1","signer_role":"recipient_replay_issuer"}]}'
    )

    class FixtureVerifier:
        def __init__(self, supplied):
            assert supplied == registry_path.read_bytes()

        def verify(self, raw, purpose):
            purposes.append(purpose)
            if raw == policy_path.read_bytes() and purpose == adapter.POLICY_PURPOSE:
                return dict(raw_policy)
            if raw == observation and purpose == REPLAY_PURPOSE:
                return {"generated_observation": True}
            raise ValueError("signature_invalid")

    monkeypatch.setattr(
        adapter, "import_module", lambda _: SimpleNamespace(PilotVerifier=FixtureVerifier)
    )
    authority = adapter.open_lifecycle_authority(tmp_path.resolve())
    assert authority is not None
    policy = authority.current_policy(binding)
    assert policy is not None and policy.binding == binding and policy.evaluation_only
    assert policy.replay_key_id == "generated-recipient-key"
    verified = authority.verify(observation, REPLAY_PURPOSE)
    assert verified.signer_role == REPLAY_ISSUER and verified.key_id == policy.replay_key_id
    with pytest.raises(ValueError, match="signature_invalid"):
        authority.verify(observation.replace(b"recipient-key", b"substituted-key"), REPLAY_PURPOSE)
    raw_policy["binding"]["destination_ref"] = "foreign-destination"
    assert authority.current_policy(binding) is None
    assert purposes == [
        adapter.POLICY_PURPOSE,
        REPLAY_PURPOSE,
        REPLAY_PURPOSE,
        adapter.POLICY_PURPOSE,
    ]
    write(registry_path, {"replacement_registry": True})
    with pytest.raises(ValueError, match="lifecycle_registry_changed"):
        authority.verify(observation, REPLAY_PURPOSE)
    registry_path.unlink()
    with pytest.raises(ValueError, match="lifecycle_registry_changed"):
        authority.current_policy(binding)
