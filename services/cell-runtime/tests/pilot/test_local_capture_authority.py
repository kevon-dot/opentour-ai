"""Exercise real scoped grant resolution using generated private custody."""

import hmac
from dataclasses import replace

import pytest
from opentour_cell.pilot.capture_streams import CapturedAttempt, CaptureError, CaptureStreams
from opentour_cell.pilot.local_capture_authority import PURPOSE, SCHEMA, LocalCaptureAuthority
from opentour_cell.pilot.operating_point import canonical, digest
from test_capture_streams import COMMIT, REQUEST, RESPONSE, SENTINEL, journal, row
from test_intake_mcp import _write_private

KEY = bytes(range(32))


def install(tmp_path):
    fixture, origin = journal(tmp_path / "capture")
    store = fixture.store
    directory = store.path.parent
    authority = LocalCaptureAuthority(directory, store.organization, origin.base.workflow)
    _write_private(directory / "capture-authority-key.json", {"key": KEY.hex()})
    grant = {
        "schema_version": SCHEMA,
        "purpose": PURPOSE,
        "tenant": store.organization,
        "engagement": store.engagement_id,
        "workflow": origin.base.workflow,
        "grant_ref": "generated-capture-permission",
        "policy_revision": "policy-v7",
        "mode": "off",
        "expires_at": 200,
        "recorders": {
            boundary: {"actor": f"recorder-{boundary}", "source": source}
            for boundary, source in (REQUEST, RESPONSE, COMMIT)
        },
        "inspectors": {
            "owner": [REQUEST[0], RESPONSE[0], COMMIT[0]],
            "response-only": [RESPONSE[0]],
        },
    }
    return CaptureStreams(store, authority, lambda: 110), authority, grant


def seal(authority, grant, key=KEY):
    _write_private(
        authority.path,
        {
            "grant": grant,
            "seal_sha256": hmac.digest(key, canonical(grant).encode(), "sha256").hex(),
        },
    )


def test_sealed_opt_in_separates_sources_and_current_viewer(tmp_path):
    capture, authority, grant = install(tmp_path)
    workflow = grant["workflow"]
    attempt = CapturedAttempt("invoice-operation", "a1", "case-a", "family-a", "decision-a", 1)
    with pytest.raises(CaptureError):
        capture.observed_chunk(
            "recorder-provider_response", workflow, attempt, row(RESPONSE, "chunk"), 0, SENTINEL
        )
    seal(authority, grant)
    with pytest.raises(CaptureError, match="decision_capture_disabled"):
        capture.observed_chunk(
            "recorder-provider_response", workflow, attempt, row(RESPONSE, "chunk"), 0, SENTINEL
        )
    seal(authority, grant | {"mode": "metadata_only"})
    with pytest.raises(CaptureError):
        capture.observed_chunk("owner", workflow, attempt, row(RESPONSE, "forged"), 0, SENTINEL)
    with pytest.raises(CaptureError):
        capture.observed_chunk(
            "recorder-provider_response",
            workflow,
            attempt,
            row(REQUEST, "wrong-source"),
            0,
            SENTINEL,
        )
    capture.started("recorder-application_request", workflow, attempt, row(REQUEST, "begin"))
    capture.observed_chunk(
        "recorder-provider_response", workflow, attempt, row(RESPONSE, "chunk"), 0, SENTINEL
    )
    capture.terminal(
        "recorder-provider_response", workflow, attempt, row(RESPONSE, "end"), "finished", 1
    )
    capture.committed(
        "recorder-application_commit",
        workflow,
        attempt,
        row(COMMIT, "commit"),
        "commit-receipt",
        decision_payload_ref="generated-payload",
    )
    view = capture.inspect("owner", workflow, attempt.operation_ref)
    assert view["attempts"][0]["state"] == "finished"
    assert view["committed_decisions"] == 1
    scoped = capture.inspect("response-only", workflow, attempt.operation_ref)
    assert scoped["committed_decisions"] is None and scoped["disputed_decisions"] is None
    assert scoped["attempts"][0]["observed_chunks"] == 1
    with pytest.raises(CaptureError):
        capture.inspect("recorder-application_commit", workflow, attempt.operation_ref)
    assert SENTINEL not in capture.store.path.read_bytes()


def test_wrong_scope_signature_expiry_and_revocation_cannot_be_restored(tmp_path):
    capture, authority, draft = install(tmp_path)
    grant = draft | {"mode": "metadata_only"}
    workflow = authority.workflow
    for bad in (
        grant | {"tenant": "other-tenant"},
        grant | {"engagement": "prior-epoch"},
        grant | {"purpose": "other-use"},
        grant | {"expires_at": 110},
    ):
        seal(authority, bad)
        with pytest.raises(CaptureError):
            capture.inspect("owner", workflow, "unknown-operation")
    seal(authority, grant, key=bytes(reversed(range(32))))
    with pytest.raises(CaptureError):
        capture.inspect("owner", workflow, "unknown-operation")
    seal(authority, grant)
    assert capture.inspect("owner", workflow, "unknown-operation")["attempts"] == []
    marker = digest([authority.tenant, workflow, grant["engagement"], grant["grant_ref"]])
    _write_private(authority.directory / f"capture-revoked-{marker}.json", {"state": "withdrawn"})
    seal(authority, grant)
    with pytest.raises(CaptureError):
        capture.inspect("owner", workflow, "unknown-operation")
    with pytest.raises(CaptureError):
        capture.inspect("owner", "other-workflow", "unknown-operation")


def test_key_rotation_distinguishes_prior_capture_scope(tmp_path):
    _, authority, draft = install(tmp_path)
    grant = draft | {"mode": "metadata_only"}
    seal(authority, grant)
    prior = authority.resolve("owner", authority.workflow, "inspect", None)
    assert prior
    key = bytes(reversed(range(32)))
    _write_private(authority.directory / "capture-authority-key.json", {"key": key.hex()})
    assert authority.resolve("owner", authority.workflow, "inspect", None) is None
    seal(authority, grant, key=key)
    current = authority.resolve("owner", authority.workflow, "inspect", None)
    assert current
    assert prior.revision != current.revision and prior.opaque_key != current.opaque_key
    assert replace(prior, revision=current.revision, opaque_key=current.opaque_key) == current
