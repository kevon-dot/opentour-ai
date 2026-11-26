# Install a customer-owned managed worker and health endpoint

The host custodian owns the approved Linux machine, private volume, source grant, pseudonymous site ID, credential, DNS, TLS and route. The installer verifies the release, sets up the two service identities and records the installation measurements. Record both operators by name in the customer's private installation record. Record only the pseudonymous site, release/source SHA, connector revision, configuration revision, outcome and duration in a shared receipt.

1. With Python 3.12.16 installed at /opt/opentour/python/bin/python3, verify the candidate ZIP SHA-256 against the independently supplied digest. Extract into a fresh root-owned directory under /opt/opentour/releases. Run `sha256sum -c SHA256SUMS` there and check PROVENANCE.json. Point /opt/opentour/current to this exact directory. Only the installer can change releases or this link.
2. Create separate locked system users and groups named opentour-worker and opentour-health, with no login shell. Mount the approved persistent volume at /var/lib/opentour-worker with owner opentour-worker and mode 0700. Under that identity prepare managed-plan.json using pilot-managed-mcp.md in the kit. For a service install, use the authorization command below. Store private inputs, grants, ledger and credentials on this volume or in the approved private secret store. Use umask 077. Set ORGANIZATION to the exact authorized organization in /etc/opentour-worker/site.env (root-owned, 0600).
3. Create root-owned /etc/opentour-health with group opentour-health and mode 0750. Copy manifest.example.json there as manifest.json, copy source_commit and kit_version from the verified PROVENANCE.json into source_commit and release_version respectively, and set the pseudonymous tenant site, configuration revision, unique health audience and approved HTTPS origin (explicit port if nondefault). Install a matching TLS certificate chain and private key accessible only to the service and installer. Pin the health connector contract as opentour.worker-host.v1 in the private installation record. Generate a dedicated credential with at least 32 random bytes, encoded as unpadded base64url. Install it via the local secret store as credential, owned by opentour-health, mode 0600. The health identity has no worker-volume access.
4. Install the two service units in /etc/systemd/system and run `systemctl daemon-reload` and `systemctl enable --now opentour-worker opentour-health`. The worker runs one managed iteration every ten seconds including after failure. Provision the route exclusively for exact GET /v1/health to the service's TLS listener on 127.0.0.1:8443. The customer's ingress must verify upstream TLS and preserve the approved Host. For direct TLS on an approved interface configure the service bind/port explicitly. Never proxy a generic worker, reader, model or stdio endpoint.
5. The independent client verifies the certificate chain and hostname, sends the exact approved Host, Origin (the configured endpoint origin), X-OpenTour-Audience and Authorization bearer credential, and checks the pseudonym, release version, source SHA and configuration revision in the response. Supply secrets through the approved client secret store. The response describes TLS endpoint readiness. Inspect the worker's actual local unit status, queue and a managed fixture separately. Record a blocked installation with route owner and reason if the approved client cannot reach or authenticate the route.
6. Replace the credential using a same-directory atomic rename of a newly generated owner-only file. Remove the file to revoke all health access immediately. Verify denial of the previous value and success of its replacement from a new client connection. Keep credential and TLS material outside worker snapshots so restoring the ledger cannot restore old endpoint authority.

## Authorize the service worker

During step 2, run the managed-workflow guide's inspection with `pilot_dir=/var/lib/opentour-worker` and `grant="$pilot_dir/authorization.json"`. Use that same grant path for the MCP client and worker. As opentour-worker, set `pilot_org` to the organization in site.env, `revision` to the reviewed plan digest, `attestation` to the custodian's private reference and `expires` to the approved Unix expiry time. Authorize the plan with a fresh grant filename:

```sh
/opt/opentour/python/bin/python3 -I -S -B /opt/opentour/current/opentour-slack.pyz workflow-authorize \
  --directory /var/lib/opentour-worker --organization "$pilot_org" \
  --manifest /var/lib/opentour-worker/managed-plan.json \
  --authorization /var/lib/opentour-worker/authorization.json \
  --plan-revision "$revision" --custodian-attestation "$attestation" --expires-at "$expires"
```

This writes the owner-only grant at the path configured in opentour-worker.service.

## Snapshot and restore

Stop the worker and all writers/admission clients. Copy the entire quiescent volume (including SQLite journals, source grants, manifest, inputs and receipts) to a private encrypted fallback with the same owner and permissions. Retain the previous verified kit and digest and record the exact volume path. Before an upgrade record a queued run ID. Restart on the new package, retrieve that same run and check that one attempt produced one result. To revert, stop writers, move the current volume aside privately, restore the compatible fallback at the identical absolute path, switch the package link back and check the current grants and engagement before resuming. Run one worker iteration and look up the original result. Count jobs/attempts and source reads before admitting anything else.

Measure a clean fixture on the target CPU with the private workload class recorded. Record extraction plus checksum time, logical CPU availability, worker peak RSS, unpacked package, private volume and a separate retained snapshot. Allow additional space for the interpreter, OS, prior package, logs and production input growth. Generated rule-only measurements accompany the source candidate and exclude model training.

For the generated rule-only fixture, provision one logical CPU, at least 32 MiB for the worker process and 2 MiB of working disk for the extracted kit, state and a fallback snapshot. The rehearsal measured about 28 MiB worker peak RSS, 0.90 MiB unpacked and 0.18 MiB each for state and fallback. Reserve the downloaded ZIP, previous kit, gateway, interpreter and OS resources separately. These values bound this fixture only.

The supervised test starts a trusted TLS listener from the packaged health code, verifies that the health command rejects a mismatched release before binding, and restarts the packaged worker as a subprocess. Record separately whether the target host booted the actual units and whether an independently authorized installed provider reached the endpoint.

## Scoped intake connector

For an approved intake connection, launch a **separate** instance of health.py on a separate loopback port with the same TLS options and an absolute `--selection /private/tenant/selection.json`. Use the owner-local intake process identity with access to only this connection's private selection and ledger. Keep the health service and its identity isolated. The intake instance requires its own TLS secret directory, intake-only bearer credential, audience and configuration revision. Add `intake_tenant` and `intake_actor` to this instance's private manifest to match the existing owner-selected grant. Neither the TLS listener nor the stdio command provisions grants. Configure the custodian's ingress to preserve the original approved Host and Origin and to route only these three exact POST paths to that port.

| HTTPS operation | JSON body | Result |
| --- | --- | --- |
| /v1/discover_sources | Optional limit (1–50) and scoped cursor | Opaque authorized metadata and next cursor |
| /v1/submit_intake | Existing historical, starter or live intake arguments | Durable receipt or scoped hold |
| /v1/get_run | One run_ref | Scoped workflow, run and any existing benchmark metadata |

Send application/json with Content-Length, the approved Origin, X-OpenTour-Audience and bearer credential over verified TLS. Boundaries are 8 KiB headers, 4 KiB arguments, 32 KiB result and 120 allowed calls per minute per listener. Connection, tenant, actor, grant, file paths and callback URLs never come from request arguments. A scoped failure returns an HTTP error and safe code. A held admission returns the same successful held result as stdio. Retry a lost response with the original intent. The equivalent local command is `python -I -S -B opentour-slack.pyz mcp-intake /private/tenant/selection.json`. These transports use the same private admission ledger. Live binding resolution requires the independently supplied scoped owner resolver. Without that authority or a reader, preserve the existing scoped unavailable or pending outcome.
