# Publish a completed starter evaluation to its original benchmark

First publish the existing freeze with the starter benchmark worker. Run the separately authorized evaluation through the installed evaluator. After its score commits, the evaluator can reconcile that exact execution and issue a private receipt. The original benchmark worker can then adopt it.

```sh
python -m opentour_cell.pilot.starter_evaluation_worker --invocation "$invocation" --publish-completion
python -m opentour_cell.pilot.starter_benchmark_worker --adopt-completion "$private_settings" "$starter_run_ref"
```

Both commands are safe to retry. Publication opens only the selected execution and its two existing prediction receipts. It verifies the frozen population, independent arm receipts, operating-point definition and score before signing. This step can recover after the evaluator committed and the process stopped before issuing a receipt. Missing predictions within a scored arm retain their original treatment. Missing arm receipts, conflicting results and unknown executions withhold publication.

## Customer-local provisioning

The custodian supplies a private, separately sealed publication grant in `starter-publication-<digest(run_ref)>.json`. The payload is `{"grant": {...}, "seal_sha256": "..."}`. Use the canonical JSON and digest functions from `operating_point`. The grant seal is HMAC-SHA256 of canonical grant bytes with a key derived by HMAC-SHA256 of the grant schema name under the installation's starter intake key. Issuing the invocation alone only authenticates an evaluator.

The exact grant fields are recorded in `starter_completion._FIELDS` and exercised through generated provisioning in `test_starter_completion.py`. They bind the original run and correlation reference, canonical admitted work digest, tenant, epoch, workflow, source revision, benchmark reference/version, frozen reference/revision, named evaluator and its original invocation and execution grant, named materialization worker, custodian, grant reference, expiry and public key hash. The custodian selects the original evaluation intent for that benchmark. A manual rerun has its own intent and result.

The evaluator uses a separate owner-private signing key at `starter-completion-signing.pem`. The materializer verifies the signature with the pinned `starter-completion-public.pem`. Provision a dedicated RSA signing pair for this purpose. Both workers need the existing bounded local OpenSSL executable. Files reside in the private installation, under its existing local ownership controls.

The immutable signed receipt is `starter-completion-<digest(benchmark_ref)>.json`. It contains the publication grant digest, benchmark reference and original result reference/digest. The benchmark ledger accepts the same frozen definition and source revision and returns the existing reference/version through authorized `get_run`. Losing the original authority or pinning another execution prevents adoption. A mismatching receipt requires reconciliation by the custodian. The worker preserves any previously published benchmark history. Creating the private `starter-publication-withdrawn-<grant_ref>.json` marker revokes further publication and adoption.

The conformance tests use generated sources, separate worker identities and a pre-existing local CPU fixture. Deployment and installed customer evidence have separate owners.
