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

> **Grant migration.** A kit built from main at or after commit 09ad8a8 refuses any grant that does not carry `"engagement_ledger_required": true`. Before you deploy such a kit, re-mint the grants with that field. An existing install upgraded across that release must re-mint its authorization before the worker resumes, or the worker holds. Follow OPE-543 and OPE-569 for the grant migration and its format.

## Install the cycle worker

> **Not in the distributed kit yet.** `opentour-cycle.service`, `stage_open_weight.py` and `snapshot_boundary.py` are not in the kit's worker-host files. Adding them to the kit builder's worker-host asset list is a handoff to OPE-369. Until that ships, take these three files from a verified source checkout at the release's exact `source_commit` from PROVENANCE.json. Confirm that the checkout's HEAD commit matches, then copy the files into a root-owned directory such as `/opt/opentour/worker-host-<source_commit>`. Set `HOST_FILES` to that directory in the commands below. Once the kit ships them, use `HOST_FILES=/opt/opentour/current/integrations/worker-host`.

`opentour-cycle.service` runs the packaged `cycle-worker` entry point: discovery, the low-cost repair and, only when the standing mandate allows it, the local model fit. It uses the opentour-worker identity, the same volume and the same sandbox as `opentour-worker.service`. It has no access to the health identity's files. Install it only after the worker is authorized.

1. As opentour-worker, write `/var/lib/opentour-worker/cycle.json`, the workplace connection and the standing agreement by following pilot-autonomous-cycle.md in the kit. The config must be in the volume directory itself. Pin the workplace reader at a root-owned path under /opt/opentour that no group or other user can write. Keep Slack credentials and model weights out of the config.
2. Put the reader's `OPENTOUR_WORKPLACE_*` credentials in `/etc/opentour-worker/cycle.env`, owned by root, mode 0600. systemd reads it before it drops privileges. Only the cycle unit loads this file. Never place credentials in the volume.
3. Add `OPENTOUR_TRAINING_PYTHON=/opt/opentour/model-python/bin/python` to `/etc/opentour-worker/site.env`. Leave it set even when the model is not staged. A missing runtime holds only the model route.
4. Stage and verify the runtime and base model offline, as described in the next section. You can skip this step for rules-only use.
5. Copy `$HOST_FILES/opentour-cycle.service` to /etc/systemd/system. Run `systemctl daemon-reload`, then `systemctl enable --now opentour-cycle`. The worker runs one bounded cycle every 300 seconds. It restarts 300 seconds after any failure. A held cycle writes only a safe reason to the journal. Standard output stays off, like the managed worker.

The cycle unit is capped at `MemoryMax=4G` and `TasksMax=128`. That covers the profile's 3 GiB address-space limit for the fit child, the worker and the tokenizer threads. These values are not measured. Re-measure them on the customer host and record the result privately.

Both worker units set `ProtectProc=invisible` and `ProcSubset=pid` (systemd 247 or later; Linux 5.8 or later for `invisible`). This hides other users' processes only.

**Both worker units are one trust domain.** They run as the same opentour-worker uid. Either process can read the other's environment, including the cycle.env credentials. With the Yama setting `kernel.yama.ptrace_scope=0`, the default on RHEL and Debian, either process can also ptrace the other or write its memory with `process_vm_writev`. `NoNewPrivileges`, the empty `CapabilityBoundingSet` and `ProtectProc=invisible` do not prevent that. Either process can rewrite cycle.json, the grants and the mandates in the shared volume, and can delete or reset `/var/lib/opentour-antirollback`. Anti-rollback state kept in that directory therefore defends against a snapshot restore only, not against a hostile worker process. Real separation needs a different uid or an anchor outside this host's worker identity. This package does not change the uid layout. The system user name `opentour-signer` is reserved for a later worker-attested signing slice: that user will hold the signing key file, and the worker will reach it only through a narrow signing socket. It is not built here. Until it exists, worker records stay `locally_keyed`.

`ProcSubset=pid` also hides /proc/cpuinfo, /proc/meminfo and /proc/stat. NumPy, OpenBLAS and tokenizers have not been run under these units. Check that the numeric runtime starts and fits under systemd in the real-host smoke before relying on the model route.

The model base path is fixed. The worker always reads `<directory>/model-base`, which is `/var/lib/opentour-worker/model-base`; site.env cannot move it. Both worker units mount it read-only. The `-` prefix on `ReadOnlyPaths` means systemd mounts nothing if model-base is missing when the unit starts. A process running as opentour-worker could then create its own model-base. `verify` refuses any base that root does not own. The worker also re-checks every pinned digest before each fit, so a base with different bytes holds the model route. No unit waits for a staged model, so rules-only workflows start and keep running without one.

## Stage the numeric runtime and base model offline

Obtain the four checkpoint files listed in training/profiles/smollm2_135m_lora_v1.json from publisher revision `12fd25f77366fa6b3b4b768ec3050bf629380bac`. Obtain the two Linux x86-64 wheels named in the hash lock through the custodian's approved channel. Keep the checkpoint in a root-only directory such as /opt/opentour/staging/checkpoint. Keep the wheels in /opt/opentour/staging/wheels. Build the runtime offline from the release's lock, using the commands in pilot-open-weight.md with `/opt/opentour/model-python` as the environment.

The script keeps its staging copy, the previous base and any held base in `/var/lib/opentour-worker/.opentour-staging`. It creates that directory owned by root, mode 0700, so it works when the volume is its own filesystem. The worker owns the volume, so it can rename that entry but cannot read or change its contents. The script refuses a work directory that is a link or is not root-owned 0700, and leaves the volume unchanged. If the worker created one first, remove it as root. The script holds the work directory open, works only through that descriptor and re-checks its identity before each step. If the entry is renamed or replaced mid-run, the model route is held. As root, stop both worker units and run:

```sh
/opt/opentour/python/bin/python3 -I -S -B "$HOST_FILES/stage_open_weight.py" stage \
  --release /opt/opentour/current --source /opt/opentour/staging/checkpoint \
  --revision 12fd25f77366fa6b3b4b768ec3050bf629380bac \
  --model-dir /var/lib/opentour-worker/model-base \
  --wheel-dir /opt/opentour/staging/wheels --python /opt/opentour/model-python/bin/python
```

The script uses only the standard library and refuses to run unless it runs as root. It reads the profile and lock only if they match the release's SHA256SUMS and the SHA-256 values pinned in the script itself: the profile digest that open_weight.py and the numeric worker enforce, and the digest of the checked-in hash lock. A release that is only self-consistent, with other checkpoint digests, is held as `release_profile_unpinned`. `--revision` is the operator's statement of what was downloaded. The pinned file digests are the real check, not that argument. It checks the pinned revision, then confirms that the lock and profile agree on NumPy 2.5.5 and tokenizers 0.21.4 and their wheel digests. It requires exactly one matching wheel per locked package and no other wheels. It asks the runtime interpreter for its Python, platform and package versions.

It then copies each checkpoint file into the root-only work directory, refusing links. It hashes the bytes it writes and fsyncs each file. It sets the directory to mode 0555 and each file to 0444, owned by root. Promotion is two renames through directory descriptors opened without following links. First any current base moves into the work directory, then the staging copy moves into the volume. Both directories are fsynced. The script then reopens model-base and checks that it is the same directory it staged, with the same owner, modes and digests. A base swapped in after the rename holds the route. Output is one JSON line. `route:"ready"` means exit code 0. `route:"held"` with a safe `reason` means exit code 2. `route:"unverified"` means the script did not act, for example when not run as root or when the work directory is unsafe, and also exits 2. Every result also reports `model_base` (`promoted`, `verified`, `moved_to_held`, `absent`, `move_failed` or `unchanged`) and `cleanup` (`complete`, `incomplete` or `not_run`). The script reports an I/O failure as one of these results, never as a traceback. `move_failed` means the failing base could not be moved out. In that case stop the worker units and remove model-base as root.

Any wrong revision, missing or extra file, hash mismatch, wrong owner or mode, missing wheel, or missing or different runtime holds the model route. The script first moves any existing model-base into the work directory under a fresh `held-` name, and it does not promote the new copy. Only then does it try to remove earlier staging, previous and held copies. A held copy can be a worker-owned tree that cannot be removed. That cleanup is best effort and reports `cleanup:"incomplete"` without blocking the hold or a later promotion. Remove leftover copies as root with the worker units stopped. The worker then finds no base and holds only the model branch. The rule, supplied-output and managed workflows keep working. To return to the model route, correct the source and run `stage` again. Run `verify` with the same `--release`, `--model-dir`, `--wheel-dir` and `--python` after every upgrade or restore, and before relying on the model route.

`verify` holds on any failure, including one caused by its own arguments. A wrong `--wheel-dir`, `--python` or `--release` moves a correct model-base to a `held-` copy, and the next failed run may remove that copy. Before re-staging after a held result, check that the reason matches a real problem and re-check every argument against this guide.

A running unit keeps its read-only mount of the directory that was at model-base when it started. Moving or replacing model-base takes effect only after a restart, so stop the worker units before `stage` or `verify` and start them afterwards. Otherwise run `systemctl restart opentour-cycle opentour-worker` right after.

The script does not prove that the interpreter's installed packages came from the staged wheels. The hash-locked offline install proves that. Between runs of `verify`, the worker identity controls its own volume. The digest check the worker runs before each fit is the last guard. A local generated run proves the package only. It is not a customer-host installation. The repository test that stages onto a separately mounted tmpfs volume runs only as root and is skipped on non-root runners such as CI. The simulated separate-device test runs everywhere.

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

A restorable worker snapshot is a copy of the quiescent volume `/var/lib/opentour-worker` (opentour-worker:opentour-worker, mode 0700). It includes SQLite journals, source grants, manifest, cycle config, inputs and receipts. It does not include `model-base` or `.opentour-staging`; leave both out of the copy and re-stage the base after a restore. Nothing outside the volume enters a snapshot, so a restore cannot bring back an old value:

| Outside the snapshot | Owner and mode | Contents |
| --- | --- | --- |
| `/etc/opentour-worker/site.env` | root:root 0600 | Organization and numeric runtime path |
| `/etc/opentour-worker/cycle.env` | root:root 0600 | Workplace reader credentials |
| `/etc/opentour-health` | root:opentour-health 0750 | Health manifest, TLS chain and key, health credential |
| `/var/lib/opentour-antirollback` | opentour-worker:opentour-worker 0700 | Reserved for anti-rollback state; not yet written (OPE-566) |
| `/var/lib/opentour-worker/.opentour-staging` | root:root 0700 | Staging copy, previous base and held base; in the volume, never in a snapshot |
| `/opt/opentour` | root:root 0755 | Interpreter, releases, current link, numeric runtime, offline staging |
| `/var/lib/opentour-worker/model-base` | root:root 0555, files 0444 | Verified base model, re-staged rather than restored |

Both worker units create `/var/lib/opentour-antirollback` as a second systemd state directory. It is reserved for the worker's anti-rollback state: the highest verified issuer-registry revision and each engagement's pinned customer anchor. Nothing writes it yet. The authority-record verifier (OPE-566) defines its format. Never copy it into a snapshot, never restore it, and never reset it during an upgrade or revert. The health unit cannot read it.

Stop the cycle unit, the managed worker and all writers and admission clients. Copy the volume without model-base and .opentour-staging to a private encrypted fallback, keeping owner and permissions. Before restoring, run:

```sh
/opt/opentour/python/bin/python3 -I -S -B "$HOST_FILES/snapshot_boundary.py" check \
  --snapshot /private/fallback/opentour-worker --restore-to /var/lib/opentour-worker
```

This is a best-effort screen of entry names and metadata. It never reads file contents, so it cannot prove a snapshot holds no secrets. It refuses names that look like credentials, tokens, secrets, keys, certificates, environment files, anti-rollback state or a model base. It matches case-insensitively after Unicode NFKC normalization and refuses zero-width and other hidden characters. It also refuses links, special files, hard links, setuid, setgid and sticky bits, group or other permissions, entries not owned by opentour-worker, and any restore target except exactly `/var/lib/opentour-worker`. `--restore-to` is required. A directory or entry it cannot list or examine, for example after a permission or I/O error, is reported as `unreadable_entry` and refuses the snapshot. `snapshot_boundary.py boundary` prints the layout as JSON.

The package keeps this boundary through the layout, the unit sandboxes and the screen. A restore cannot bring back old health credentials, TLS keys, environment credentials or anti-rollback state, and it cannot change which base model is verified. It can still bring back older authority recorded inside the volume. That includes a grant file, model grant or standing mandate that was withdrawn or replaced after the snapshot. The package does not detect that. Before resuming, check the current grants, mandate and engagement against the custodian's current authority. Delete or re-withdraw anything revoked since the snapshot. Rotate the workplace credential if a revocation depends on it.

Restore the volume with opentour-worker as the owner of its top directory. If that owner is wrong, systemd re-owns everything in the state directory to opentour-worker on the next unit start, including `model-base` and `.opentour-staging`. This fails safe. `verify` and `stage` refuse a work area that root does not own, so the script changes nothing, and the worker's digest check before each fit still applies. To recover, stop the worker units. As root, remove `.opentour-staging` or restore its root ownership and mode 0700. Restore root ownership of model-base or remove it, then run `stage` again from the verified offline source.

Retain the previous verified kit and digest and record the exact volume path. If the upgrade crosses main commit 09ad8a8, re-mint the authorization with `"engagement_ledger_required": true` before the worker or cycle unit resumes, as described in OPE-543 and OPE-569. Otherwise the worker holds. Before an upgrade record a queued run ID. Restart on the new package, retrieve that same run and check that one attempt produced one result. To revert, stop writers, move the current volume aside privately, restore the compatible fallback at the identical absolute path, switch the package link back, run `verify` on the model route and check the current grants and engagement before resuming. Run one worker iteration and look up the original result. Count jobs/attempts and source reads before admitting anything else.

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
