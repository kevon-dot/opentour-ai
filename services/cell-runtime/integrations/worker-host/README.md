# Install a customer-owned managed worker and health endpoint

The host custodian owns the approved Linux machine, private volume, source grant, pseudonymous site ID, credential, DNS, TLS and route. The installer verifies the release, sets up the two service identities and records the installation measurements. Record both operators by name in the customer's private installation record. Record only the pseudonymous site, release/source SHA, connector revision, configuration revision, outcome and duration in a shared receipt.

1. With Python 3.12.16 installed at /opt/opentour/python/bin/python3, verify the candidate ZIP SHA-256 against the independently supplied digest. Extract into a fresh root-owned directory under /opt/opentour/releases. Run `sha256sum -c SHA256SUMS` there and check PROVENANCE.json. Point /opt/opentour/current to this exact directory. Only the installer can change releases or this link.
2. Create separate locked system users and groups named opentour-worker and opentour-health, with no login shell. Mount the approved persistent volume at /var/lib/opentour-worker with owner opentour-worker and mode 0700. Under that identity prepare managed-plan.json using pilot-managed-mcp.md in the kit. For a service install, use the authorization command below. Store private inputs, grants, ledger and credentials on this volume or in the approved private secret store. Use umask 077. Set ORGANIZATION to the exact authorized organization in /etc/opentour-worker/site.env (root-owned, 0600).
3. Create root-owned /etc/opentour-health with group opentour-health and mode 0750. Copy manifest.example.json there as manifest.json, copy source_commit and kit_version from the verified PROVENANCE.json into source_commit and release_version respectively, and set the pseudonymous tenant site, configuration revision, unique health audience and approved HTTPS origin (explicit port if nondefault). Install a matching TLS certificate chain and private key accessible only to the service and installer. Pin the health connector contract as opentour.worker-host.v1 in the private installation record. Generate a dedicated credential with at least 32 random bytes, encoded as unpadded base64url. Install it via the local secret store as credential, owned by opentour-health, mode 0600. The health identity has no worker-volume access.
4. Install the two service units in /etc/systemd/system and run `systemctl daemon-reload` and `systemctl enable --now opentour-worker opentour-health`. Install the autonomous cycle unit separately, as described in [Install the cycle worker](#install-the-cycle-worker). The worker runs one managed iteration every ten seconds including after failure. Provision the route exclusively for exact GET /v1/health to the service's TLS listener on 127.0.0.1:8443. The customer's ingress must verify upstream TLS and preserve the approved Host. For direct TLS on an approved interface configure the service bind/port explicitly. Never proxy a generic worker, reader, model or stdio endpoint.
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

## Install the cycle worker

`opentour-cycle.service` runs the packaged `cycle-worker` entry point: discovery, the low-cost repair and, only when the standing mandate allows it, the local model fit. It uses the opentour-worker identity, the same volume and the same sandbox as `opentour-worker.service`. It has no access to the health identity's files. Install it only after the worker is authorized.

1. As opentour-worker, write `/var/lib/opentour-worker/cycle.json`, the workplace connection and the standing agreement by following pilot-autonomous-cycle.md in the kit. The config must be in the volume directory itself. Pin the workplace reader at a root-owned path under /opt/opentour that no group or other user can write. Keep Slack credentials and model weights out of the config.
2. Put the reader's `OPENTOUR_WORKPLACE_*` credentials in `/etc/opentour-worker/cycle.env`, owned by root, mode 0600. systemd reads it before it drops privileges. Only the cycle unit loads this file. Never place credentials in the volume.
3. Add `OPENTOUR_TRAINING_PYTHON=/opt/opentour/model-python/bin/python` to `/etc/opentour-worker/site.env`. Leave it set even when the model is not staged. A missing runtime holds only the model route.
4. Stage and verify the runtime and base model offline, as described in the next section. You can skip this step for rules-only use.
5. Copy `opentour-cycle.service` to /etc/systemd/system. Run `systemctl daemon-reload`, then `systemctl enable --now opentour-cycle`. The worker runs one bounded cycle every 300 seconds. It restarts 300 seconds after any failure. A held cycle writes only a safe reason to the journal. Standard output stays off, like the managed worker.

The model base path is fixed. The worker always reads `<directory>/model-base`, which is `/var/lib/opentour-worker/model-base`; site.env cannot move it. Both worker units mount it read-only. No unit waits for a staged model, so rules-only workflows start and keep running without one.

## Stage the numeric runtime and base model offline

Obtain the four checkpoint files listed in training/profiles/smollm2_135m_lora_v1.json from publisher revision `12fd25f77366fa6b3b4b768ec3050bf629380bac`. Obtain the two Linux x86-64 wheels named in the hash lock through the custodian's approved channel. Keep the checkpoint in a root-only directory such as /opt/opentour/staging/checkpoint. Keep the wheels in /opt/opentour/staging/wheels. Build the runtime offline from the release's lock, using the commands in pilot-open-weight.md with `/opt/opentour/model-python` as the environment. Then, as root and with the worker units stopped:

```sh
/opt/opentour/python/bin/python3 -I -S -B /opt/opentour/current/integrations/worker-host/stage_open_weight.py stage \
  --release /opt/opentour/current --source /opt/opentour/staging/checkpoint \
  --revision 12fd25f77366fa6b3b4b768ec3050bf629380bac \
  --model-dir /var/lib/opentour-worker/model-base --wheel-dir /opt/opentour/staging/wheels \
  --python /opt/opentour/model-python/bin/python
```

The script uses only the standard library. It reads the profile and lock only if they match the release's SHA256SUMS. It checks the pinned revision, then confirms that the lock and profile agree on NumPy 2.5.5 and tokenizers 0.21.4 and their wheel digests. It requires exactly one matching wheel per locked package and no other wheels. It asks the runtime interpreter for its Python, platform and package versions. Then it copies each checkpoint file, refusing links, and checks every SHA-256. The verified copy replaces model-base in one rename: directory mode 0555, files mode 0444, owned by root. Output is one JSON line. `route:"ready"` means exit code 0. `route:"held"` with a safe `reason` means exit code 2.

Any wrong revision, missing or extra file, hash mismatch, missing wheel or missing or different runtime holds the model route. The script moves any existing model-base to `model-base.held` and does not promote the new copy. The worker then finds no base and holds only the model branch. The rule, supplied-output and managed workflows keep working. To return to the model route, correct the source and run `stage` again. Run `verify` with the same `--release`, `--model-dir`, `--wheel-dir` and `--python` after every upgrade or restore, and before relying on the model route. The worker also re-checks the runtime and every checkpoint digest itself before each fit.

The script does not prove that the interpreter's installed packages came from the staged wheels. The hash-locked offline install proves that. A local generated run proves the package only. It is not a customer-host installation.

## Installed witness

After the unit boots under systemd on the customer's host and one normal cycle iteration completes, record the installation. Name the host custodian and installer only in the customer's private installation record. The shared installed-witness record holds only these fields:

| Field | Value |
| --- | --- |
| `site` | Pseudonymous site ID from the health manifest |
| `release_version` and `source_commit` | `kit_version` and `source_commit` from the verified PROVENANCE.json |
| `connector_revision` | The pinned workplace connection revision used by the iteration |
| `configuration_revision` | The `revision` from the installed health manifest |

Keep unit status, cycle outcomes, run IDs, staging output, paths, timings and all customer data in the private record.

## Snapshot and restore

A restorable worker snapshot is a copy of the quiescent volume `/var/lib/opentour-worker` (opentour-worker:opentour-worker, mode 0700). It includes SQLite journals, source grants, manifest, cycle config, inputs and receipts. It does not include `model-base`, `model-base.held` or the staging directories; re-stage the base after a restore. Nothing outside the volume enters a snapshot, so a restore cannot bring back an old value:

| Outside the snapshot | Owner and mode | Contents |
| --- | --- | --- |
| `/etc/opentour-worker/site.env` | root:root 0600 | Organization and numeric runtime path |
| `/etc/opentour-worker/cycle.env` | root:root 0600 | Workplace reader credentials |
| `/etc/opentour-health` | root:opentour-health 0750 | Health manifest, TLS chain and key, health credential |
| `/var/lib/opentour-antirollback` | opentour-worker:opentour-worker 0700 | Reserved for anti-rollback state |
| `/opt/opentour` | root:root 0755 | Interpreter, releases, current link, numeric runtime, offline staging |
| `/var/lib/opentour-worker/model-base` | root:root 0555, files 0444 | Verified base model, re-staged rather than restored |

Both worker units create `/var/lib/opentour-antirollback` as a second systemd state directory. It is the worker's own anti-rollback state: the highest verified issuer-registry revision and each engagement's pinned customer anchor. The authority-record verifier defines its format. Never copy it into a snapshot, never restore it, never reset it during an upgrade or revert. The health unit cannot read it.

Stop the cycle unit, the managed worker and all writers and admission clients. Copy the volume without model-base to a private encrypted fallback, keeping owner and permissions. Before restoring, run:

```sh
/opt/opentour/python/bin/python3 -I -S -B /opt/opentour/current/integrations/worker-host/snapshot_boundary.py check \
  --snapshot /private/fallback/opentour-worker --restore-to /var/lib/opentour-worker
```

It refuses a snapshot that contains credentials, TLS material, environment files, anti-rollback state, a model base, links, special files, hard links or group- or other-readable entries. It also refuses a restore target that is not the worker volume or that overlaps live state. `snapshot_boundary.py boundary` prints the same layout as JSON.

The package enforces this boundary through the layout, the unit sandboxes and the check. A restore cannot bring back old health credentials, TLS keys, environment credentials or anti-rollback state, and it cannot change which base model is verified. It can still bring back older authority recorded inside the volume. That includes a grant file, model grant or standing mandate that was withdrawn or replaced after the snapshot. Before resuming, check the current grants, mandate and engagement against the custodian's current authority. Delete or re-withdraw anything revoked since the snapshot. Rotate the workplace credential if a revocation depends on it.

Retain the previous verified kit and digest and record the exact volume path. Before an upgrade record a queued run ID. Restart on the new package, retrieve that same run and check that one attempt produced one result. To revert, stop writers, move the current volume aside privately, restore the compatible fallback at the identical absolute path, switch the package link back, run `verify` on the model route and check the current grants and engagement before resuming. Run one worker iteration and look up the original result. Count jobs/attempts and source reads before admitting anything else.

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
