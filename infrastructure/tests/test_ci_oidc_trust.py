"""Bootstrap deliberately grants no production cloud/payment identity."""

import json
from pathlib import Path

import pytest

from tools.ci.policy import production_identity_allowed, workflow_errors

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "event,repository,environment",
    [
        ("pull_request", "generated/fork", "production"),
        ("pull_request", "kevon-dot/opentour-ai", "production"),
        ("push", "kevon-dot/opentour-ai", "unapproved"),
        ("push", "kevon-dot/opentour-ai", "production"),
    ],
)
def test_bootstrap_never_grants_production_identity(event, repository, environment):
    policy = json.loads((ROOT / ".github/ci-policy.json").read_text())
    assert not production_identity_allowed(
        {"event_name": event, "repository": repository, "environment": environment}, policy
    )


def test_actual_workflow_has_no_credential_or_oidc_route():
    value = json.loads((ROOT / ".github/workflows/ci.yml").read_text())
    assert not workflow_errors(value)
    assert all(j["permissions"] == {"contents": "read"} for j in value["jobs"].values())


def test_production_profile_requires_its_future_owner():
    with pytest.raises(ValueError):
        production_identity_allowed({}, {"production_oidc_profiles": [{"role": "not-admitted"}]})
