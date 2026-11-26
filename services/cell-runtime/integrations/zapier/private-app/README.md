# Private OpenTour intake app — 0.1.0

This is the **unpublished OPE-437 app package** for the existing worker's three
reference-plane operations. It is separate from the Slack approval recipe and
the worker ZIP. Building or testing this directory grants no publication,
installation, source access, live Zap activation or customer acceptance.

The authenticated connection fixes the owner-approved HTTPS origin, audience,
scoped intake credential and opaque connection alias. No action accepts a URL,
credential, principal or tenant override. Keep credentials in the Zapier
connection. Only the worker resolves source permissions and admission; the
alias is a label, not evidence of tenancy or permission.

| Action | Existing worker request | Output boundary |
| --- | --- | --- |
| Discover Authorized Sources | `POST /v1/discover_sources`, optional integer `limit` 1–50 and opaque `cursor` | One page of source or owner-bound feed revision metadata; opaque `next_cursor` or null |
| Submit Authorized Intake | `POST /v1/submit_intake`, one historical, starter or live shape | Opaque run/state/correlation or held reason and optional successor feed |
| Get Authorized Run | `POST /v1/get_run`, exact `run_ref` | Bound run/workflow state, next-action/reason codes and optional benchmark revision |

The connection test uses discovery with a one-item limit. It sends no approval
post and admits no run. This app defines no scheduled trigger or approval action.

Historical intake requires `pair_ref`, `source_revision`, `correction_revision`,
`policy_revision`, and the original `idempotency_key`. Starter intake requires
`mode=starter`, `policy_resource_ref`, `source_revision`, `setup_policy_revision`,
`idempotency_key` and exactly one `creation_ref` or existing `workflow_ref`.
Live refresh requires `mode=live`, `feed_binding_ref`, `source_revision` and
`idempotency_key`; omit `correction_revision`. Discover each separately bound
decision/outcome feed under its current grant. An inactive or unconfigured feed
stays held; the app never supplies a guessed source or correction.

All inputs are opaque references, not source rows, arbitrary text or evidence.
Unknown input fields and unexpected response fields fail closed. A response is
limited to 32 KiB and the request to 4 KiB, matching the shipped gateway. TLS
certificate verification is enabled, redirects are not followed, and no raw
transport/response error is returned to Zap History. The app does not retry.
On a lost response, preserve the original key and reconcile the same intent;
never create a new key to bypass an unknown outcome. Once a run reference is
known, read it under that same connection. A state or packet grants no release.

## Versions and reproducible local checks

Runtime dependencies and the developer CLI are pinned to `zapier-platform-core`
and `zapier-platform-cli` **19.3.0**, with exact transitive integrity in this
directory's `package-lock.json`. Two registry deprecation notices retain their
warnings with the maintainer contact address removed for the public-content
policy; dependency versions, URLs and integrity are unchanged.
Repository conformance uses Node **24.22.3** and
Python **3.12.16**. Do not modify the root toolchain, lock or bootstrap.

Zapier documents Node **22** for its hosted runtime and allows other local
development versions. This source uses compatible JavaScript; an actual hosted
Node 22 run has **not** been qualified. The package's engine pin records the
repository development runtime. Installed compatibility remains a release gate.

From this directory, with the repository's pinned Node/npm on PATH:

```sh
npm ci --ignore-scripts --no-audit --no-fund --workspaces=false
node --test test/*.test.js
NO_UPDATE_NOTIFIER=1 node node_modules/zapier-platform-cli/src/bin/run validate --without-style
NO_UPDATE_NOTIFIER=1 node node_modules/zapier-platform-cli/src/bin/run build --skip-dep-install --skip-validation
```

`--without-style` runs Zapier's local schema validation without sending the app
definition to its server. It does not establish publishing validation. The
pinned CLI's `--skip-validation` build flag prevents its default remote `/check`
call; run the separate local validation first. Build produces ignored
`build/build.zip` and `build/source.zip`; neither is uploaded.
The dependency installation disables lifecycle scripts. Use a clean isolated
directory and the committed lock when reproducing.

The standalone worker test needs the existing pinned Python test dependencies
on `PYTHONPATH`, `openssl`, and an absolute path to pinned Node:

```sh
python3.12 -B test/worker_contract.py --node /absolute/path/to/pinned/node
```

It builds and extracts the actual worker ZIP, starts fresh isolated interpreters
using its shipped `health.py` and immutable zipapp, and calls all three app
actions across two generated tenants. It uses a generated CA through
`NODE_EXTRA_CA_CERTS`; it never disables TLS verification. It verifies each
tenant's positive path, bilateral foreign-run denial, unknown reference,
swapped/old credential, wrong audience, missing CA, rotation, closed origin,
reconnect/restart and one run per tenant under duplicate intent. Generated
credential and signing files live only in a temporary owner-local directory;
the test closes all gateway processes and removes that directory.

The Node tests additionally exercise all input variants, bounded paging,
metadata-only outputs, invalid field/URL shapes, redirects, malformed or
oversized responses, unknown/rejected outcomes, one request with the original
key, and execution through the pinned SDK. This is generated local software
conformance, not publication, a Zapier-hosted call or installed provider proof.

**CI scope:** none of the existing eleven repository CI jobs runs these package
Node tests, this standalone TLS test, or the package-local CLI validation/build.
Those are explicit additional commands. Existing repository checks can inspect
tracked public content and existing worker tests; green existing jobs must not
be reported as execution of this new app suite. No global job, runner, wrapper,
bootstrap or worker builder was changed, and no skipped dependency test was
added.

## Logging and permitted provider evidence

The SDK normally logs raw HTTP request/response details. This app uses native
HTTPS and the documented `flags.skipHttpPatch=true` to avoid that SDK patch;
it never calls `z.request`, `z.console` or a custom logger. The SDK's password
authentication field keeps the token out of action fields. Local tests confirm
the SDK leaves HTTPS unpatched and errors do not reflect raw response bodies or
credentials. These observations do **not** attest the remote service's entire
logging pipeline. The installed inspector must separately inspect redacted
connection, bundle/task history and error-log samples before hosted qualification.

## Operator handoff, version pin and recovery

Before any provider action, obtain OPE-553's current operator/admin mandate,
separate inspector authority, scope/expiry, second test tenant and revocation
owner. Store exact installed IDs and private receipts with the owner; publish
only redacted references. Do not use unapproved or unrelated connections.

The authorized publisher must bind this reviewed source, app 0.1.0/core 19.3.0,
worker source/package digest, origin/certificate, scope and configuration
revision in the private installation record. Register/publish an invite-only
integration using Zapier's documented process; record the actual app/version
ID and invitation. An independent installer then installs from that invitation
in the second approved test account and creates its own scoped connection.
Record actual run/version/action/connection references in Zap History. None of
those steps was performed while producing this package.

Exercise each action, unknown/foreign reference, swapped connection, revoked
credential, rotation and unreachable origin in the authorized installation.
Inspect input/output samples and logs for credentials or raw records. Record
tenancy/isolation and allowlisted outputs. OPE-435 owns the later combined
ten-profile installed matrix and actual callback/destination witnesses.

For rollback, the authorized operator records the previous app/template/worker
revision and its compatible connection scope before changing any binding.
Fence or pause affected work under that operator's authority, reconcile all
unknown admissions with their original keys, then explicitly repin the previous
qualified app version and compatible worker package. Re-test the connection,
read existing run state, and verify old revoked credentials remain denied.
Do not assume that editing an app or template rewrites existing Zap versions,
that a recreated connection preserves authority, or that a version change makes
an uncertain admission safe to repeat. Attach the actual rollback/reconnection
receipt; this directory supplies instructions, not a rollback witness.

## Sources

- [OPE-437 native acceptance](https://linear.app/opentourai/issue/OPE-437)
- [Worker intake/TLS owner OPE-537](https://linear.app/opentourai/issue/OPE-537)
- [Shared implementation contract](https://linear.app/opentourai/document/first-ten-implementation-contract-intake-evidence-training-and-release-4e23522a139f)
- [Official CLI/runtime/auth/action/build documentation](https://docs.zapier.com/integrations/build-cli/overview)
- [Official API-key authentication guidance](https://docs.zapier.com/integrations/build/apikeyauth)
- [Pinned 19.3.0 schema flag](https://github.com/zapier/zapier-platform/blob/zapier-platform-schema%4019.3.0/packages/schema/lib/schemas/AppFlagsSchema.js)
- [Official private-app installation guidance](https://help.zapier.com/hc/en-us/articles/8496312360461-Use-private-apps-with-Zapier)

See [ACCEPTANCE.md](ACCEPTANCE.md) for the clause-level evidence and unrun gates.
