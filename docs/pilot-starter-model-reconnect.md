# Reconnect the exact selected starter model

The private benchmark's reconnect detail is available to a current score
viewer who also holds the separate model-custodian grant for that execution.
The installation owner provisions it privately as
`starter-reconnect-SHA256.json`. The suffix is the canonical digest of
`[tenant, viewer actor, execution reference]`.

The sealed grant uses schema `opentour.pilot.starter-model-reconnect-grant.v1`.
It binds the exact tenant, engagement epoch, workflow, current intake run,
benchmark reference and version, evaluation, frozen definition reference and
revision, selected model reference and immutable revision, custodian, inspection
grant revision, grant reference, and expiry. Seal canonical grant JSON with
HMAC-SHA256 using the starter-intake key derived with this schema as the domain.
The Slack view cannot issue the grant. The operator can withdraw it using the
private `starter-reconnect-withdrawn-GRANT_REF.json` marker.

The detail opens only while the exact selected execution is frozen and unscored.
In-flight or restored adapter evidence and completed results suppress reconnect.
The detail points the custodian to setup for the pinned model and inference grant.
It creates no connection, issues no authorization, sends nothing to a provider,
and never starts or reruns an evaluation.
