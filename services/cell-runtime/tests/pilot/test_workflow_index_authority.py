"""Generated signer and launch fixtures for the production workflow index reader."""

from dataclasses import replace

import pytest
from opentour_cell.pilot import signed_records as sr
from opentour_cell.pilot.intake_mcp import IntakePrincipal
from opentour_cell.pilot.slack_adapter import Settings
from opentour_cell.pilot.workflow_benchmarks import BenchmarkScope
from opentour_cell.pilot.workflow_index import ProposalAccess, WorkflowVisibility
from opentour_cell.pilot.workflow_index_authority import (
    PUBLICATION_PURPOSE,
    REGISTRY_FILE,
    open_workflow_index_scope,
)
from test_signed_records import ENGAGEMENT, Kit, Signer, sign, stamp
from test_signed_records import kit as signer_kit  # noqa: F401

TENANT = "org-sentinel-0001"
ACTOR = "USENTINEL01"
OTHER = "USENTINEL02"
WORKFLOW = "wf-sentinel-01"
FILE = "workflow-index.json"
MEMBER = {
    "roles": ["inspect"],
    "datasets": ["generated-dataset"],
    "preview": False,
    "aggregate": True,
    "expires_at": 2_100_000_000,
}


def _fixture_settings(kit: Kit) -> Settings:
    return Settings(
        TENANT,
        "T00000001",
        "A00000001",
        "C00000001",
        ACTOR,
        OTHER,
        kit.worker,
        {ACTOR: MEMBER},
        False,
        False,
        False,
        "",
        workflow_index_publication_file=FILE,
    )


def _payload(**changes):
    return {
        "profile_version": "1.0",
        "tenant": TENANT,
        "engagement_id": ENGAGEMENT,
        "issued_at": stamp(-15),
        "not_after": stamp(3600),
        "workflows": [
            {
                "workflow_ref": WORKFLOW,
                "label": "Invoices",
                "aliases": ["Purchasing"],
                "directory_viewers": [ACTOR],
            }
        ],
        **changes,
    }


def _write(kit: Kit, filename: str, record: bytes) -> None:
    path = kit.worker / filename
    path.write_bytes(record)
    path.chmod(0o600)


@pytest.fixture
def enrolled(signer_kit: Kit) -> Kit:  # noqa: F811
    kit = signer_kit
    assert PUBLICATION_PURPOSE == sr.WORKFLOW_INDEX_PUBLICATION_PURPOSE
    registry = kit.registry()
    _write(kit, REGISTRY_FILE, registry)
    kit.verifier(registry).pin_customer_anchor(kit.enrolment())
    return kit


def _publish(kit: Kit, payload, *signers: Signer) -> None:
    _write(kit, FILE, sign(payload, PUBLICATION_PURPOSE, *signers))


def test_current_signed_publication_and_separate_proposal_authority(enrolled: Kit) -> None:
    actor = IntakePrincipal(TENANT, ACTOR, "slack")
    cfg = _fixture_settings(enrolled)
    payload = _payload()
    _publish(enrolled, payload, enrolled.customer, enrolled.operator)
    workflow = BenchmarkScope(TENANT, ENGAGEMENT, WORKFLOW)
    proposal = ProposalAccess(
        workflow,
        ACTOR,
        "grant",
        "proposal",
        "revision",
        "pending",
        1_800_000_000,
        2_090_000_000,
        2_100_000_000,
        "open_review",
        2_090_000_000,
    )
    scope = open_workflow_index_scope(cfg, actor, (proposal,))
    assert scope is not None and scope.actor == actor and scope.epoch == ENGAGEMENT
    assert scope.proposals == (proposal,)
    assert [(label.scope, label.label, label.aliases) for label in scope.labels] == [
        (workflow, "Invoices", ("Purchasing",))
    ]
    assert scope.workflows == (WorkflowVisibility(workflow, ACTOR, scope.valid_until),)

    removed = {**payload["workflows"][0], "directory_viewers": [], "label": "Invoice review"}
    new_payload = {**payload, "issued_at": stamp(-5), "workflows": [removed]}
    _publish(enrolled, new_payload, enrolled.customer, enrolled.operator)
    revised = open_workflow_index_scope(cfg, actor, ())
    assert revised is not None and revised.workflows == () and revised.proposals == ()
    assert revised.labels[0].revision != scope.labels[0].revision
    assert revised.labels[0].label == "Invoice review"

    _publish(enrolled, payload, enrolled.customer, enrolled.operator)
    assert open_workflow_index_scope(cfg, actor, ()) is None
    _publish(
        enrolled,
        {**new_payload, "workflows": payload["workflows"]},
        enrolled.customer,
        enrolled.operator,
    )
    assert open_workflow_index_scope(cfg, actor, ()) is None
    _publish(enrolled, new_payload, enrolled.customer, enrolled.operator)
    assert open_workflow_index_scope(cfg, actor, ()) == revised
    assert open_workflow_index_scope(cfg, replace(actor, actor=OTHER), ()) is None
    assert open_workflow_index_scope(cfg, replace(actor, role="intake"), ()) is None
    assert open_workflow_index_scope(cfg, replace(actor, tenant="other-tenant"), ()) is None
    assert (
        open_workflow_index_scope(replace(cfg, workflow_index_publication_file=""), actor, ())
        is None
    )


@pytest.mark.parametrize(
    "adverse",
    [
        "missing_custodian",
        "wrong_custodian",
        "wrong_customer",
        "wrong_tenant",
        "expired",
        "duplicates",
        "embedded_grant",
        "unsafe_file",
        "no_pin",
        "not_enrolled",
    ],
)
def test_invalid_publications_produce_no_index(
    enrolled: Kit, monkeypatch: pytest.MonkeyPatch, adverse: str
) -> None:
    actor = IntakePrincipal(TENANT, ACTOR, "slack")
    cfg = _fixture_settings(enrolled)
    payload = _payload()
    signers = (enrolled.customer, enrolled.operator)
    if adverse == "missing_custodian":
        signers = (enrolled.customer,)
    elif adverse == "wrong_custodian":
        signers = (enrolled.customer, Signer(sr.OPERATOR_CUSTODIAN))
    elif adverse == "wrong_customer":
        signers = (Signer(sr.CUSTOMER_OWNER, registry_revision="1"), enrolled.operator)
    elif adverse == "wrong_tenant":
        payload["tenant"] = "other-tenant"
    elif adverse == "expired":
        payload.update(issued_at=stamp(-120), not_after=stamp(-60))
    elif adverse == "duplicates":
        payload["workflows"] += payload["workflows"]
    elif adverse == "embedded_grant":
        payload["workflows"][0]["proposal_action"] = "open_review"
    elif adverse == "no_pin":
        monkeypatch.setattr(sr, "APPROVED_ROOT_PIN_SHA256", None)
    elif adverse == "not_enrolled":
        payload["engagement_id"] = "missing-customer-anchor"

    _publish(enrolled, payload, *signers)
    if adverse == "unsafe_file":
        path = cfg.directory / FILE
        path.rename(cfg.directory / "elsewhere.json")
        path.symlink_to(cfg.directory / "elsewhere.json")
    assert open_workflow_index_scope(cfg, actor, ()) is None
