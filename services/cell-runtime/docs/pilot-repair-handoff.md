# Deliver and repeat an approved repair

The output of one completed pilot belongs to one organization, engagement, agreed audience and policy. A qualifying rule includes its executable mapping. A qualifying model includes the trained adapter and its pinned base, tokenizer, worker and fit references. A held comparison or an owner stop delivers diagnostic evidence. Those two outcomes have no executable route.

## Stage the result

Use the supported kit, Python 3.12.16 and the private owner directory from intake. All paths supplied to the handoff commands must be absolute. Create owner-controlled directories with mode 0700 and use 0600 for every grant, input, key and package. Set the shell umask to 077 before redirecting these commands. Use the actual run ID returned by the worker.

```sh
umask 077
python -I -S -B "$KIT" repair --directory "$PILOT_OWNER" --organization "$ORG" \
  --manifest "$PILOT_OWNER/managed-plan.json" prepare-handoff --run-id "$RUN" \
  > "$PILOT_OWNER/prepared.json"
```

The custodian must have declared `handoff.policy_revision`, `exporter`, `support_owner`, `decision_time_assumptions`, `limitations` and `fallback` in the managed plan. Use `manual_review` for manual fallback. A prior executable config uses `{"kind":"rule","policy_revision":"CURRENT","rule":PRIOR_RULE_OBJECT}` and must exactly reproduce the frozen incumbent. Set `handoff.disposition` to `stop` for an explicit business stop. For development learning rows, explicitly declare `allow_learning_rows:true` and the permitted `learning_features` before running. The source must still authorize training on those records. Protected references and other fields stay out of the learning set.

The preparation receipt supplies `spec_path`, `source_directory`, `spec_sha256`, versions, intake and dataset scope, and a digest-to-file map `local_assets`. The staged spec identifies the exporter and distinct named audience. Its manifest includes the reference sources, decision-time assumptions, support owner, limitations and asset hashes. Keep those descriptive values safe for the approved recipient. A gated team must first inspect and approve the exact revision in Slack. Export and entitlement issuance check the current release receipt again.

## Give each role its own private grant

The custodian provisions `operator.json` and `recipient.json` through the customer's controlled local account. Copy the following values from `prepared.json` verbatim: `organization`, `engagement`, `store_engagement`, `audience`, `policy_revision`, `dataset_id`, `intake_revision`, `spec_sha256` and `local_assets`. Add these fields to both:

```json
{
  "schema_version": "opentour.pilot.handoff-grant.v1",
  "role": "operator",
  "actor": "approved-exporter",
  "state": "active",
  "expires_at": 0,
  "store": "/absolute/owner/training.sqlite3"
}
```

Replace the example actor and expiry with the approved exporter and an actual bounded UTC Unix cutoff. The recipient grant instead has `role:"recipient"` and its `actor` must equal the staged audience. The exporter and recipient must differ. These are OS-controlled files furnished by the custodian. A user-supplied actor string or a copied ZIP cannot authorize access. The same-machine commands recheck the registered source and engagement. A changed scope, revoked source, expired permission or changed pinned asset prevents use.

The ordinary local export includes the permitted executable. It references the evidence, benchmark and learning set by SHA-256. Stage those permitted private files in the recipient environment and map each exact raw-file digest to its owner-only local path. `verify` checks and actually opens these references. Use a durable, private location for each delivered version. Ports, AWS credentials and production writes are unnecessary.

```sh
python -I -S -B "$KIT" bundle --grant "$OPERATOR" export \
  --spec "$SPEC" --source "$SOURCE" --output "$PILOT_OWNER/repair.zip"
python -I -S -B "$KIT" bundle --grant "$RECIPIENT" import \
  --archive "$PILOT_OWNER/repair.zip" --output "$RECIPIENT_DIR/installed.zip"
python -I -S -B "$KIT" bundle --grant "$RECIPIENT" verify --archive "$RECIPIENT_DIR/installed.zip"
```

## Transfer to a separate authorized machine

The owner provisions the recipient's encryption certificate and independently verifies its SHA-256. The recipient keeps the decryption key. The customer separately pins the issuer's public key and private ledger directory on the recipient machine. Use distinct keys for signing and encryption, protected PEM files and local OpenSSL with CMS AES-256-GCM support. RSA signing and RSA recipient certificates are exercised by the qualification tests. Keep signing credentials on the issuing machine. The recipient needs neither the producer's training store nor its adapter directory.

On the operator grant add `seal_assets`, a list of the independently permitted names from `evidence.json`, `benchmark.json` and `learning.json`. For a self-contained encrypted transfer select all three. Also set `recipient_certificate`, `recipient_certificate_sha256` and `portable` with `evidence_access:true`, `owner_public_key`, `owner_key_sha256`, `owner_signing_key` and `expires_at`. Add `learning_access:true` only when that recipient is allowed to read the authorized learning rows. The issued cutoff must fit within the source and operator grants and any model grant. Each encrypted delivery seals its executable as well. Unselected data stays hash-pinned at a private location. A separately authorized local-reference delivery can permit `portable.clear_repair_transfer:true` if its executable may travel in clear.

```sh
python -I -S -B "$KIT" bundle --grant "$OPERATOR" seal \
  --spec "$SPEC" --source "$SOURCE" --output "$PILOT_OWNER/sealed.zip"
python -I -S -B "$KIT" bundle --grant "$OPERATOR" issue-entitlement \
  --spec "$SPEC" --source "$SOURCE" --archive "$PILOT_OWNER/sealed.zip" \
  --output "$PILOT_OWNER/entitlement.json"
```

Move the encrypted ZIP and signed entitlement through the owner-approved channel. Issue the recipient a private grant for the same scope with the returned `spec_sha256`, an expiry within the signed cutoff, and the destination's `recipient_certificate` and `recipient_key` paths. Replace the producer's `store` with `recipient_custody` containing `entitlement`, `owner_public_key`, `owner_key_sha256` and `ledger` on the recipient machine. Set `local_assets` only for independently staged private references still in the manifest. Keep the owner key pin and ledger location independent of the package. Signed scope binds the exact ZIP bytes, enrolled source, recipient, policy, expiry and model grant. For an authorized replacement the issuer sets `portable.supersedes` to the current recipient spec hash. The local enrollment atomically switches the current revision and then refuses the old one.

```sh
python -I -S -B "$KIT" bundle --grant "$RECIPIENT" enroll-recipient
python -I -S -B "$KIT" bundle --grant "$RECIPIENT" import \
  --archive "$RECIPIENT_DIR/sealed.zip" --output "$RECIPIENT_DIR/installed.zip"
```

For a model, provide `model_runtime` with `model_dir` pointing to independently licensed, hash-verified base files, `python` pointing to the qualified numeric interpreter, and `grant` containing the exact scoped original model permission. The same-machine issuer also supplies `model_runtime.authority_bundle` pointing to its private original adapter directory. An offline recipient uses the signed time-bounded model entitlement in place of that path. The recipient's local cache must contain the pinned base and tokenizer. Missing features go to review. A wrong model identity, base, runtime or expired permission stops inference.

## Repeat the result, rehearse fallback and record the witness

`replay` rereads the independent benchmark, executes the winner and its fallback, checks predictions case-by-case and recomputes the frozen per-arm counters. It reports actual arm readout digests, counts and whether fallback was exercised. Use the winner and fallback versions from `prepared.json`. A new private request looks like `{"features":{"description":"generated example"}}`; supply the enrolled feature names and, for an incumbent routing rule, optionally an already obtained `incumbent` v1 decision.

```sh
python -I -S -B "$KIT" bundle --grant "$RECIPIENT" replay --archive "$INSTALLED"
python -I -S -B "$KIT" bundle --grant "$RECIPIENT" predict --archive "$INSTALLED" \
  --version "$WINNER_VERSION" --input "$DECISION"
python -I -S -B "$KIT" bundle --grant "$RECIPIENT" fallback --archive "$INSTALLED" --input "$DECISION"
python -I -S -B "$KIT" bundle --grant "$RECIPIENT" predict --archive "$INSTALLED" \
  --version "$FALLBACK_VERSION" --input "$DECISION"
python -I -S -B "$KIT" bundle --grant "$RECIPIENT" witness --archive "$INSTALLED" \
  --attestation "$PRIVATE_WITNESS_REFERENCE" --output "$RECIPIENT_DIR/witness.json"
```

The named recipient issues the witness after personally repeating the work. Its date, actor, revision, package hash and replay digests remain private. For explicitly permitted materialization also set `materialize_learning:true` on the recipient grant and run `learning --archive "$INSTALLED" --output "$RECIPIENT_DIR/learning.json"`. Empty permission yields an explicit exclusion with zero learning records. Generated exercises do not establish customer acceptance or an authorized rollout.

On withdrawal, remove/disable same-machine grants and use the engagement custody closeout. Notify every prior offline recipient through its agreed route. At each registered machine run `bundle --grant "$RECIPIENT" withdraw-recipient`, retain the local ledger, and account for all files and materialized copies under the retention terms. The ledger disables the whole received engagement/audience scope across restarts. A disconnected machine cannot learn of a remote withdrawal until notified or its signed authority expires. Production releases, traffic changes and paid work require their separate owner records.
