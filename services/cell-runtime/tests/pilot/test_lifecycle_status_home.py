"""Private Slack composition with generated responses at the canonical verifier boundary."""

from dataclasses import asdict, fields, replace
from datetime import UTC, datetime
from pathlib import Path
from time import time

from opentour_cell.pilot import recipient_lifecycle as lifecycle
from opentour_cell.pilot.local_handoff_permissions import handoff_outbox
from opentour_cell.pilot.operating_point import digest
from opentour_cell.pilot.recipient_delivery import LocalRecipientDelivery
from test_app_home import click, publish
from test_recipient_test_rollback import assert_readback, target_cli
from test_recipient_test_rollback import generated_target as generated_target
from test_repair_bundle import read, write
from test_reviewer_workflow import buttons, restart, screen, text
from test_status_questions import counts


class GeneratedAuthority:
    """Test double for platform signature validation. No keys or registry in the worker."""

    def __init__(self, policy):
        self.policy = policy
        self.responses = {}

    def current_policy(self, binding):
        if self.policy and binding == self.policy.binding:
            return self.policy
        return None

    def verify(self, record, purpose):
        return self.responses[(record, purpose)]

    def observe(self, directory, binding, kind, purpose, record):
        token = digest([purpose, asdict(record)]).encode()
        self.responses[token, purpose] = record
        path = directory / f"recipient-lifecycle-{binding.release_ref}.{kind}.json"
        path.write_bytes(token)
        path.chmod(0o600)
        return path


def test_recipient_replay_and_restored_target_through_private_home(generated_target, monkeypatch):
    sample = generated_target
    h = sample.harness
    actor = h.config["reviewer"]
    directory = h.store.path.parent
    target = read(sample.config)
    receiver = LocalRecipientDelivery(Path(target["recipient_grant"]))
    claim = lifecycle.prepare_recipient_replay_claim(receiver, sample.release)
    assert claim["evidence_revision"] == sample.evidence_revision and claim["replayed_cases"] > 0
    assert claim["cohort_revision"] == sample.cohort_revision
    binding = lifecycle.LifecycleBinding(
        **{f.name: claim[f.name] for f in fields(lifecycle.LifecycleBinding)}
    )
    assert (
        binding.release_ref == sample.release.reference
        and binding.receiver_receipt_ref == sample.receipt_ref
    )
    authority = GeneratedAuthority(
        lifecycle.LifecyclePolicy(
            binding, "generated-recipient-key", True, sample.release.expires_at
        )
    )
    before = counts(h)
    delivered = handoff_outbox(h.store).status(sample.release.selection, actor)
    assert delivered and delivered.stage == "delivered"

    def question():
        click(h, publish(h, monkeypatch, actor), "ot_app_prompt", actor)
        view = list(h.slack.screens)[-1]
        h.form(view, {"command": {"command": {"value": "Did it ship?"}}}, actor)
        h.action(view, "ot_result_open", buttons(screen(h, view))["ot_result_open"], actor)
        return view

    def timeline():
        view = question()
        h.slack.screens[view]["hash"] = "generated-status"
        h.action(view, "ot_release_receipt", buttons(screen(h, view))["ot_release_receipt"], actor)
        assert screen(h, view)["callback_id"] == "ot_handoff_receipt"
        return text(h, view)

    replay_record = lifecycle.VerifiedLifecycleRecord(
        claim, lifecycle.REPLAY_ISSUER, authority.policy.replay_key_id
    )
    replay_path = authority.observe(
        directory, binding, "replay", lifecycle.REPLAY_PURPOSE, replay_record
    )
    assert "Replayed by recipient" not in timeline()
    restart(h, lifecycle_authority=authority)
    replay_rows = timeline()
    assert "Replayed by recipient" in replay_rows and "Evaluation handoff complete" in replay_rows
    assert "Rollback verified" not in replay_rows

    for rejected in (
        replace(replay_record, payload=claim | {"recipient_ref": "other-recipient"}),
        replace(replay_record, payload=claim | {"release_ref": "release-" + "9" * 48}),
        replace(replay_record, signer_role=lifecycle.ROLLBACK_ISSUER),
    ):
        authority.observe(directory, binding, "replay", lifecycle.REPLAY_PURPOSE, rejected)
        assert "Replayed by recipient" not in timeline()
    authority.observe(directory, binding, "replay", lifecycle.REPLAY_PURPOSE, replay_record)

    rollback_policy = lifecycle.RollbackPolicy(
        "generated-observer-key",
        "generated-rollback-authority",
        target["target_ref"],
        sample.incumbent_version,
        int(time()),
        sample.release.expires_at,
    )
    authority.policy = replace(authority.policy, evaluation_only=False, rollback=rollback_policy)
    assert "Activation unconfirmed" in timeline()
    request = sample.config.with_name("signed-change-request.json")
    write(request, sample.envelope())
    target_cli(sample.config, "change", "--request", request)
    assert_readback(
        target_cli(sample.config, "inspect"),
        sample.release.artifact_sha256,
        sample.winner_decision,
        1,
    )
    write(
        request,
        sample.envelope(
            operation="rollback_test",
            expected_version=sample.release.artifact_sha256,
            target_version=sample.incumbent_version,
            request_ref=rollback_policy.authorization_ref,
        ),
    )
    target_cli(sample.config, "change", "--request", request)
    restored = target_cli(sample.config, "inspect")
    observed_at = datetime.now(UTC).replace(microsecond=0).isoformat()
    assert_readback(restored, sample.incumbent_version, sample.incumbent_decision, 2)
    rollback_claim = {
        **asdict(binding),
        "profile_version": "1.0",
        "observed_at": observed_at,
        "issued_at": observed_at,
        "not_after": claim["not_after"],
        "package_sha256": claim["package_sha256"],
        "environment_ref": restored["target_ref"],
        "restored_version": restored["active_version"],
        "rollback_authorization_ref": rollback_policy.authorization_ref,
        "serving_readback_ref": "serving-" + digest(restored)[:48],
    }
    rollback_record = lifecycle.VerifiedLifecycleRecord(
        rollback_claim, lifecycle.ROLLBACK_ISSUER, rollback_policy.observer_key_id
    )
    for rejected in (
        replace(rollback_record, payload=rollback_claim | {"environment_ref": "other-target"}),
        replace(
            rollback_record,
            payload=rollback_claim | {"restored_version": sample.release.artifact_sha256},
        ),
        replace(rollback_record, key_id=replay_record.key_id),
    ):
        authority.observe(directory, binding, "rollback", lifecycle.ROLLBACK_PURPOSE, rejected)
        assert "Rollback verified" not in timeline()
    authority.observe(directory, binding, "rollback", lifecycle.ROLLBACK_PURPOSE, rollback_record)
    restart(h, lifecycle_authority=authority)
    for _ in range(2):
        rows = timeline()
        assert "Rollback verified" in rows and sample.incumbent_version in rows
        assert restored["target_ref"] in rows and "Replayed by recipient" in rows
    replay_path.unlink()
    rows = timeline()
    assert "Rollback verified" in rows and "Replayed by recipient" not in rows
    sample.permission_path.unlink()
    assert "ot_release_receipt" not in buttons(screen(h, question()))
    assert counts(h) == before
    assert target_cli(sample.config, "inspect") == restored
    assert len(list(sample.receipt_path.parent.parent.glob("delivery-*/artifact.zip"))) == 1
