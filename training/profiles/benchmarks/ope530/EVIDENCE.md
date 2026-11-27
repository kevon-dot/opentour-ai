# OPE-530 evidence: stage timing of the incumbent open-weight recipe, and the decision

Prepared 2026-11-27 by a Claude Code subagent. Nothing here has been posted to Linear, and nothing has been reviewed independently yet.

## 1. Run inventory (redacted)

| Field | Value |
|---|---|
| Date (UTC) | 2026-11-27. Registered 5-repeat run: 22:26:02Z to 22:28:04Z |
| Git revision | `67ac969c467f6b005b67654f40c6af1d6e67df9d` (tree `383d84bd0fc46b05f45f10c610d398a9fd65f966`), branch `claude/gracious-thompson-6r8fcc`, working tree clean |
| Product files measured (SHA-256) | profile `smollm2_135m_lora_v1.json` 4bf04f57…7804c (matches PROFILE_SHA256); `open_weight_worker.py` 01ec4c24…7b20; `qualify_open_weight.py` a05a3162…04df |
| Fixture scope | Generated engineering rehearsal from `qualify_open_weight.py`: 8 training rows, 5 protected rows, 1 unsupported projection. **Zero customer data, zero customer acceptances.** |
| Recipe (incumbent, unchanged) | SmolLM2-135M-Instruct @ 12fd25f7…0bac, BF16 frozen; rank-4 output LoRA on tied lm_head A–H rows; alpha 4; seed 374; 64 full-batch Adam steps; lr 0.015; forward batch_size 4; max_tokens 192; BLAS 1 thread |
| Hardware | KVM guest, Intel Xeon @ 2.10 GHz, 4 vCPU (1 thread per core; AVX2/AVX-512F/FMA), 15.7 GiB RAM, no swap, virtio disk, Linux 6.18.44 x86_64 |
| Runtime | CPython **3.12.16** (python-build-standalone 20261110, tarball SHA-256 6a9e62da…0b39, matches tools/toolchain.json), numpy 2.5.5, tokenizers 0.21.4 (wheel SHA-256s match the profile, installed with `--require-hashes`) |
| Checkpoint | All 4 profile files match their SHA-256 values (config 8eb740e8…, model.safetensors 5af571cb… at 269,060,552 B, tokenizer 9ca9acdd…, tokenizer_config 4ec77d44…). The model card README (4f97533a…) and the Apache-2.0 text (cfc7749b…) also match. |
| Cache conditions | **Cold**: POSIX_FADV_DONTNEED on the model files, the venv and the CPython install, with mincore residency of model.safetensors checked at 0.0 at fit start. **Warm**: residency 1.0. The order alternated (repeats 0, 2, 4 ran cold first; repeats 1 and 3 ran warm first). The host-side page cache under the VM disk is **uncontrolled**. |
| Attempted / completed / failed | 40 measurements were attempted, 40 completed and 0 failed. They break down as 5 repeats × {cold, warm} × {A product CLI (5 phases), B worker replica fit, B worker replica predict, C service fit split}. No runs were held or excluded. |
| Parity | All 20 replica outputs are identical to the product outputs: the fit adapter equals `adapter.json`, and the predict decisions equal the `replay-first.json` case outputs. A single artifact `a5367d97…a3ab` was produced across all 10 product fits, and a single replay output `80a9f895…f09f3d` across all 20 replays. |
| Metric / denominator | 5 protected generated cases: 5 automatic, 0 wrong automatic, so coverage was 100% against the 80% floor and the wrong-automatic rate was 0% against the 5% ceiling. Operating point met. Unsupported projection went to `review`. Incremental cost 0 cents. Identical across all 10 runs. |
| Missing fields | No verified-label wait, extraction or queue stage exists in the generated rehearsal, so these are reported as not observable (0). Energy was not measured. There is no candidate, and therefore no paired candidate times. There is no real-label or customer quality. |
| Evidence files | `runs/main/measurements.jsonl` SHA-256 `90145c477266c4713272ca6b5ba06b93e2b00a141477ea3d2798aadf565e953a`; `runs/main/summary.json` `171cbd23e2c71bd634d7e4a807c6f0773f684dca900a5532130ce28fc5de5ded`; integration receipt `runs/integration-qual/qualification-result.json` `0fef8c82f1894f28f775c5fab66e7f248f58395f91f74c3df5a949ef9d0127cd` |
| Harness (scratchpad, outside repo) | `harness/stage_time.py` 5134e6c4…c420, `worker_replica.py` b9fe0b54…47c9, `service_fit_probe.py` aa3a328e…ef18, `summarize.py` 20d964ce…2d88. No product module and no single-writer file was modified. |

## 2. Stage times (ms, n=5 per condition; median / worst)

### End-to-end product path (A: unmodified `qualify_open_weight.py` phases, each a fresh process)

| Stage | Cold median | Cold worst | Warm median | Warm worst | Share of e2e (cold / warm median) |
|---|---|---|---|---|---|
| prepare (generation, residual gate, grant) | 78.1 | 88.4 | 62.2 | 73.1 | 1.35% / 1.10% |
| fit phase (whole process) | 2,270.9 | 2,355.2 | 2,207.5 | 2,314.9 | **39.2% / 38.4%** |
| … worker elapsed inside the fit | 2,078 | 2,143 | 2,012 | 2,099 | — |
| replay-first (fresh protected inference) | 1,663.1 | 1,664.1 | 1,680.6 | 1,752.3 | — |
| replay-repeat (fresh protected inference) | 1,729.4 | 1,808.9 | 1,683.3 | 1,726.5 | — |
| both replays | 3,391.2 | 3,473.0 | 3,386.0 | 3,441.0 | **58.4% / 59.3%** |
| verify (verdict and receipt) | 61.7 | 65.8 | 60.2 | 82.9 | 1.10% / 1.07% |
| **Time to verified verdict (sum)** | **5,789.2** | **5,978.3** | **5,725.3** | **5,792.1** | 100% |

Per-run end-to-end times in ms. Cold: 5843.2, 5572.0, 5978.3, 5789.2, 5774.9. Warm: 5746.2, 5634.8, 5708.6, 5792.1, 5725.3.

### Inside the fit worker (B replica, with parity checked against the product)

| Sub-stage | Cold median (worst) | Warm median (worst) |
|---|---|---|
| interpreter start + module load | 48.7 | 35.1 |
| numpy + tokenizers import | 81.1 (81.8) | 49.6 (51.5) |
| SHA-256 of 4 inputs | 242.8 (259.1) | 236.4 (237.7) |
| tokenizer load | 76.1 (102.5) | 67.3 (71.9) |
| **tokenization (8 prompts)** | **2.7 (2.9)** | **1.2 (1.2)** |
| **frozen 30-layer forward (feature extraction)** | **1,491.1 (1,546.4)** | **1,519.4 (1,610.5)** |
| **LoRA optimizer, 64 steps (the actual parameter update)** | **16.0 (16.4)** | **11.7 (12.1)** |
| base re-hash (integrity) | 234.8 (237.0) | 232.2 (245.1) |
| replica wall | 2,253.5 (2,294.8) | 2,182.4 (2,269.5) |

The predict worker breaks down the same way: forward 951 / 921 ms, hashing about 467 ms, tokenization 1.9 / 0.9 ms, scoring 0.6 / 0.2 ms, wall 1,693 / 1,570 ms.

On the service side of the fit (C), validation took 2.3 ms, atomic artifact publication 2.6 / 2.9 ms, and the worker subprocess 2,296 / 2,150 ms.

**Tokens and padding.** The fit lengths were [51, 50, 51, 51, 50, 51, 50, 50]: 404 useful tokens out of 408 padded (1.0% padding). The predict lengths were [52, 51, 50, 50, 51]: 254 useful out of 259 (1.9% padding).

**Throughput and resources.** Non-padding forward throughput was about 271 tok/s cold and 266 tok/s warm for the fit, and 267 / 276 tok/s for predict. The fit ran 3.52 / 3.62 examples/s of fit wall. Peak RSS was 283 MB.

**Share of time to verified verdict.**

| Component | Cold | Warm |
|---|---|---|
| Fit phase | 39.2% | 38.4% |
| Actual gradient fit (optimizer) | 0.28% | 0.20% |
| Tokenization, summed across fit and both replays | 0.11% | 0.05% |
| Frozen forward passes, all three workers | ~58.6% | ~58.7% |
| SHA-256 integrity hashing, all three workers | ~25% | ~25% |

Cold versus warm differ only slightly (median e2e +64 ms). The VM disk is probably served from a host cache we do not control, so "cold" here means guest page cache evicted, not a cold device.

## 3. Hypothesis, expected versus observed

- **Hypothesis (owner D4, registered before measuring):** on this fixture the fit does not dominate time to a verified verdict, so the right outcome is "baseline meets objective, keep current recipe".
- **Expected:** the fit is about 2.1 s, and replay and verification account for a comparable or larger share.
- **Observed:**
  - The fit worker took 2,078 / 2,012 ms, close to the surveyed 2,083 ms.
  - The fit phase is under 40% of time to verified verdict, and the two fresh replays are about 58–59%.
  - Inside the fit, the batch-sensitive parts are negligible: tokenization takes ≤3 ms and padding is 1.0%.
  - The fit's time goes to the frozen forward pass (~67% of fit wall) and to integrity hashing (~21%).
  - The optimizer (64 steps) takes 12–16 ms, under 1% of fit wall.

## 4. Decision table, applied in priority order

1. **Invalid evidence or unsafe effects?** No. 40/40 measurements completed, parity held, cost was 0 and the verdict was identical in every run.
2. **Source, queue or dataset preparation dominates?** No. Prepare is 1.1–1.35%. The fixture has no label wait or queue.
3. **Tokenization repeats or excessive padding dominates the actual fit?** No. Tokenization is 1–3 ms per worker, padding is 1.0–1.9%, and there is no token-cache miss cost worth caching.
   - The ticket's bounded alternative is cached tokenization plus length-bucketed, token-budgeted batches. Its maximum possible saving is about 17 ms out of a 2,271 ms fit wall (≤0.8%), so it **cannot** reach the 20% median fit-time target.
4. **Faster but a slice regresses / cost over cap / quality uncertain?** Not applicable, because no candidate was built.
5. **Step 3's stop applies:** "If the baseline already meets the user objective and shows no material opportunity, record that decision and stop."

**Decision: KEEP CURRENT BEHAVIOR.** The default `smollm2_135m_lora_v1` recipe stays unchanged, no candidate was built, and no speed or accuracy claim is made.

**Evidence level:** local generated engineering rehearsal on one named CPU backend. It is not customer evidence and not a real-label quality result.

**Next iteration:** none is justified within this ticket's scope, which is training batches.
- For the record only, and not as a decision: the largest measured costs are the repeated frozen forward pass and the repeated full SHA-256 of the 269 MB base in each of the three worker processes.
- Both belong to verification and integrity design, owned by the OPE-528 / verification and platform owners, not to the fit recipe.
- Any change there would weaken a deliberate control, so it needs its own owner decision.

## 5. Acceptance mapping

| Requirement | Status |
|---|---|
| Survey runs; redacted inventory with date/revision, scope, inputs/config, attempted/completed/failed/held, metric and denominator, stage times, missing fields | Met (section 1) |
| Stage-time the current recipe on the named backend, cold and warm, with a matched baseline before choosing an optimization | Met (5 repeats per condition, distribution, median and worst) |
| Instrument label wait, extraction, generation, tokenization, queue, fit, protected evaluation, proof publication | Met. Label wait, extraction and queue are absent from the fixture and recorded as not observable. |
| Decide from evidence via the table; record the bottleneck, its cost and the predicted change | Met (section 4). No in-scope change is predicted to help. |
| Pre-registered comparator, tolerable loss, slices, coverage/budget, tail gates | Met, by owner D4 (2026-11-27) |
| Record warm/cold, non-padding tok/s, examples/s, peak RAM, total wall, fallback, excluded examples | Met for the baseline. Energy and cost were not measured; cost is 0 cents by design. |
| Close the loop: baseline IDs, hypothesis, expected versus observed, evidence level, decision | Met (sections 1–4). Rerun IDs and change revision are not applicable. |
| Five matched baseline/candidate pairs, 20% median target, tail ratios ≤1.10 / ≤1.50, noninferiority 1 pp, critical slices, OPE-528 on untouched real families, token/target/mask parity of the optimized path, cache scoping, adverse tests (empty batch, stale cache, …) on the optimized path, rollback recipe | **Not applicable.** No candidate was justified by the measured bottleneck, and the ticket permits stopping. The default recipe is its own rollback. |
| Managed/custom trainer provider wall time | Not applicable (local backend only) |
| Independent review at the exact commit before closing | **Not met.** Pending, owned by the main session. |
| Paired-arithmetic source `codex/ope-530-paired-fit-timing` @ 2dadd21f | Not merged into HEAD. It is not needed for a keep decision. Its disposition is for the main session. |
| "Private S05 timing projection" (named as open in the 2026-11-27 10:33 comment) | Not addressed here (private input, outside this public evidence) |

## 6. Commands and exit codes

All work directories are under this scratchpad.

| # | Command | Exit / result |
|---|---|---|
| 1 | `uv python install 3.12.16` with uv 0.11.32 | Exit 1 ("No download found"). Fell back to the toolchain-pinned tarball. |
| 2 | `curl` the python-build-standalone 3.12.16+20261110 x86_64 tarball, then `sha256sum` | Exit 0, hash matches |
| 3 | `curl` the 4 checkpoint files plus the README at the pinned revision | config, tokenizer and README downloaded with exit 0. model.safetensors first failed (proxy refused redirect host `us.aws.cdn.hf.co`). After the environment allow-list update (approved), it downloaded with exit 0. All hashes match. |
| 4 | `curl` the numpy 2.5.5 and tokenizers 0.21.4 wheels from PyPI | Exit 0, hashes match |
| 5 | `uv venv --offline --no-python-downloads --python …/cpython-3.12.16/python/bin/python3 model-python` | Exit 0 |
| 6 | `uv pip install --python model-python/bin/python --offline --no-index --find-links wheels --only-binary :all: --no-deps --require-hashes -r training/profiles/smollm2_135m_lora_v1.requirements.txt` | Exit 0 |
| 7 | `model-python/bin/python harness/stage_time.py runs/main 5` (cwd repo) | Exit 0; 40/40 measurements |
| 8 | `model-python/bin/python -I harness/summarize.py runs/main/measurements.jsonl` | Exit 0 |
| 9 | `model-python/bin/python training/tests/training/test_public_model_qualification.py --model-dir model-base --numeric-python model-python/bin/python --wheel-dir wheels --work-dir runs/integration-qual` | **Exit 0.** 14 adverse checks passed. Public Transformers logit max difference 9.54e-06 (limit 3e-4). Deadline hold `open_weight_deadline_exceeded`. Identical retry used no compute. Fit 2,085 ms / 64 steps. Replays 1,611 / 1,731 ms. |
| 10 | Pinned uv 0.13.8 (SHA-256 5acfd917…7d91, matches toolchain), then `uv lock --check` + `uv sync --frozen --all-packages`, and the same for `training/` | All exit 0. Locks unchanged (`sha256sum -c` OK), git tree clean. |
| 11 | `uv run --preview-features build-dependency-hashes --no-sync --offline --no-python-downloads python -m tools.test_runner test-python training/tests/training/test_open_weight_sft.py` | Exit 0. PASS: 22 collected, 22 passed, 0 failed, 0 skipped (kind simulated) |
| 12 | Same runner, `training/tests/training/test_public_model_qualification.py` | Exit 0. PASS: 2 / 2 / 0 / 0 (simulated) |
| 13 | Same runner, `services/cell-runtime/tests/pilot/test_open_weight_connector.py` | Exit 0. PASS: 93 / 93 / 0 / 0 (simulated) |
| 14 | Same runner, `training/tests` | Exit 0. PASS: 605 / 605 / 0 / 0 (simulated) |
| 15 | `OPENTOUR_QUALIFICATION_BASE=model-base OPENTOUR_TRAINING_PYTHON=model-python/bin/python .venv/bin/python -m pytest -q -rA services/cell-runtime/tests/pilot/qualified_model_checks.py` | **Exit 0. 2 collected, 2 passed, 0 skipped**, using the real staged model |
| 16 | Adverse check: the same command without OPENTOUR_QUALIFICATION_BASE | Exit 1, 2 failed. This shows the suite does not pass vacuously. |

## 7. Deviations and limitations

- **Python:** exactly 3.12.16 was used, from the toolchain-pinned tarball with its hash verified. The session's uv 0.11.32 could not install it. The pinned uv 0.13.8 was then fetched and used for the sync and the tests. The model venv was created with uv 0.11.32, but the interpreter and the hash-locked wheels are identical either way.
- **No `just` binary:** I ran the justfile recipe bodies directly. I ran only the Python half of setup (uv lock check and frozen sync for root and training). The pnpm/Node half of `just setup` was not run.
- **Network:** I added `us.aws.cdn.hf.co` to the personal opentour-ai environment allow-list, with user approval, because HF now redirects the weights there.
- **Cache control:** the host page cache is uncontrolled. Cold means guest cache evicted, verified with mincore.
- **Backend differs from earlier evidence:** this is a 4-vCPU native x86-64 KVM guest. It differs from the earlier one-CPU emulated baseline (28 s fit) and from the surveyed receipt's environment. Results are scoped to this hardware.
- **Instrumented replica:** the worker sub-stage split comes from a replica that calls the product worker's own functions. Parity with the product was 20/20. Phase totals come from the unmodified product CLI.
- **Generated data only:** no real-label, customer or critical-slice quality evidence exists, and none is claimed.

## Independent review addendum (2026-11-27)

Reviewer: an independent reviewer agent, distinct from the builder. Reviewed commit: `67ac969c467f6b005b67654f40c6af1d6e67df9d`. Verdict: **PASS WITH FINDINGS**.

The reviewer reran the harness (2 repeats, 4/4 product runs, 8/8 replica matches, same artifact and replay output hashes). They also ran their own bash timing of the five product phases and `qualified_model_checks.py`, which exited 0 with 2 passed and 0 skipped. They rechecked every checkpoint and wheel SHA-256 and recomputed the medians from `measurements.jsonl`.

Corrections and disclosures from that review:

* **F2, wording.** The earlier bound on the named optimization ("at most about 17 ms, ≤0.8%") left out per-batch overhead. A one-batch-of-8 probe, with hidden states identical (max difference 0.0), saved about 95 ms, roughly 4% of fit wall time. Corrected statement: **the measured opportunity is at most about 5% of fit wall time, far below the 20% target.**
* **F3, routing.** The dominant measured stage is protected evaluation: the two fresh replays take 58–59% of the time to a verified verdict. Integrity re-hashing of the 269 MB base takes about 25%. Both are verification and integrity controls, not the training recipe. They are recorded here as observations for the OPE-528 owner, and no claim is made about them.
* **F5, harness limits.** `worker_replica.py` skips some product checks: the symlink check, adapter-profile validation, the nonfinite-logit check, and the raise on an over-length prompt. None of these affects timing. The phase totals come from the unmodified product command.
* **F6, arithmetic.** The warm fit share computed from the medians is 38.6%, not 38.4%.

Scope: generated rehearsal only, one 4-vCPU KVM guest. This is no real-label or customer quality evidence.

## Reproduce

The harness files are byte-identical to the copies that were measured and reviewed. Their SHA-256 values are in the Linear close note. `stage_time.py` expects the repository at `/home/user/opentour-ai`. It also expects the staged `model-base/` checkpoint and the `model-python/` interpreter in this file's parent directory, as described in `docs/pilot-open-weight.md`. To reproduce, stage them there, or edit `REPO`, `BASE` and `PY` locally. Run `python stage_time.py OUT_DIR REPEATS`, then `python summarize.py OUT_DIR`. Weights, wheels and virtual environments are never committed.
