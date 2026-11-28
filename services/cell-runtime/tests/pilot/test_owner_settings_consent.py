"""Generated private settings consent and the canonical verifier boundary."""

from copy import deepcopy

import pytest
from opentour_cell.pilot import owner_settings as settings
from opentour_cell.pilot import signed_records as records
from opentour_journal.crypto import canonical
from test_owner_settings import Setup
from test_signed_records import kit as kit
from test_signed_records import sign


def _install_generated_registry(setup, kit):
    registry = kit.registry()
    path = setup.customer.owner / settings.ISSUER_REGISTRY
    path.write_bytes(registry)
    path.chmod(0o600)
    kit.verifier(registry).pin_customer_anchor(kit.enrolment(engagement_id=setup.customer.eng))


@pytest.fixture
def generated_consent(kit, monkeypatch, tmp_path):
    # The serialized verifier writer owns the production registry policy. Extend it in
    # this generated fixture only to exercise real ES256 and scope checks end to end.
    purpose = settings.CONSENT_PURPOSE
    monkeypatch.setattr(records, "CONSENT_PURPOSES", records.CONSENT_PURPOSES | {purpose})
    monkeypatch.setattr(records, "RECORD_PURPOSES", records.RECORD_PURPOSES | {purpose})
    monkeypatch.setattr(
        records,
        "REQUIRED_ROLES",
        {
            **records.REQUIRED_ROLES,
            purpose: frozenset({records.CUSTOMER_OWNER, records.OPERATOR_CUSTODIAN}),
        },
    )
    monkeypatch.setattr(
        records,
        "REGISTRY_ROLES",
        {
            **records.REGISTRY_ROLES,
            records.OPERATOR_CUSTODIAN: records.REGISTRY_ROLES[records.OPERATOR_CUSTODIAN]
            | {purpose},
        },
    )
    if purpose not in kit.operator.purposes:
        kit.operator.purposes.append(purpose)
    setup = Setup(tmp_path)
    _install_generated_registry(setup, kit)
    return setup, kit


def _preview(setup, revision=None, *, action="save", policy=None):
    return setup.service.preview_consent(
        setup.policy if policy is None and action == "save" else policy,
        actor=setup.owner,
        workflow=setup.policy["mandate"]["workflow"],
        expected_revision=revision,
        action=action,
    )


def _apply(setup, envelope, workflow=None):
    return setup.service.save_consent(
        envelope,
        actor=setup.owner,
        workflow=workflow or setup.policy["mandate"]["workflow"],
    )


def test_consent_endpoint_rejects_valid_signature_for_another_purpose(tmp_path, kit):
    setup = Setup(tmp_path)
    _install_generated_registry(setup, kit)
    claim = _preview(setup)["payload"]
    envelope = sign(claim, records.ROUTING_POLICY_PURPOSE, kit.customer, kit.operator)
    before = setup.protocol()
    with pytest.raises(settings.SettingsError, match="settings_consent_unverifiable"):
        _apply(setup, envelope)
    assert setup.service.inspect(actor=setup.owner)["current"] is None
    assert setup.protocol() == before


def test_consent_revision_joins_scoped_save_retry_and_signed_pause(generated_consent):
    setup, kit = generated_consent
    draft = _preview(setup)
    payload = draft["payload"]
    baseline = setup.customer.ledger.inspect(setup.customer.mandate)
    assert payload["prior_mandate_revision"] == baseline["mandate_revision"]
    assert payload["workflow"] == setup.policy["mandate"]["workflow"]
    assert payload["engagement_id"] == setup.customer.eng
    assert payload["issued_at"].endswith(".000Z")
    envelope = sign(payload, settings.CONSENT_PURPOSE, kit.customer, kit.operator)
    receipt = _apply(setup, envelope)
    assert receipt["revision"] == settings.repair_bundle._sha(canonical(payload))
    assert receipt["consent_purpose"] == settings.CONSENT_PURPOSE
    equivalent = sign(payload, settings.CONSENT_PURPOSE, kit.customer, kit.operator)
    assert _apply(setup, equivalent) == receipt
    before = setup.protocol()
    with pytest.raises(settings.SettingsError, match="settings_revision_conflict"):
        _preview(setup)
    pause = _preview(setup, receipt["revision"], action="pause")["payload"]
    confirmed = _apply(setup, sign(pause, settings.CONSENT_PURPOSE, kit.customer, kit.operator))
    assert confirmed["state"] == "pause"
    assert (
        setup.service.setup_readback(actor=setup.reviewer, workflow=payload["workflow"])["state"]
        == "paused"
    )
    assert setup.protocol() == before
    with setup.service.ledger._db(write=False) as db:
        assert db.execute("SELECT count(*) FROM owner_settings_revisions").fetchone()[0] == 2


@pytest.mark.parametrize("kind", ["single_signer", "workflow", "prior_mandate", "wrong_actor"])
def test_consent_denies_unapproved_scope_before_catalog_access(generated_consent, kind):
    setup, kit = generated_consent
    payload = deepcopy(_preview(setup)["payload"])
    signers = (kit.customer, kit.operator)
    expected = "settings_consent_scope_mismatch"
    actor = setup.owner
    workflow = payload["workflow"]
    if kind == "single_signer":
        signers = (kit.customer,)
        expected = "settings_consent_unverifiable"
    elif kind == "workflow":
        workflow = "generated-different-workflow"
    elif kind == "prior_mandate":
        payload["prior_mandate_revision"] = "0" * 64
        expected = "settings_mandate_revision_conflict"
    else:
        actor = setup.reviewer
        expected = "settings_custodian_required"
    envelope = sign(payload, settings.CONSENT_PURPOSE, *signers)
    before = setup.protocol()
    with pytest.raises(settings.SettingsError, match=expected):
        setup.service.save_consent(envelope, actor=actor, workflow=workflow)
    assert setup.service.inspect(actor=setup.owner)["current"] is None
    assert setup.protocol() == before
