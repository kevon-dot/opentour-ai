# OPE-530 evidence: stage timing of the incumbent open-weight recipe

This file replaces the evidence at `b650d7d`, which a cross-session audit returned as CHANGES REQUIRED. It was prepared on 2026-11-28 by a Claude Code subagent. **It has not been independently reviewed.** A reviewer adds that at the exact commit.

## 0. Scope, in one paragraph

This is a stage-time inventory of the **incumbent** `smollm2_135m_lora_v1` recipe on the **generated** engineering rehearsal in `training/profiles/qualify_open_weight.py`. That fixture has 8 training rows of 50–51 tokens, so padding is 0.98%. It has 5 protected rows and 1 unsupported projection. Length bucketing and token-budgeted batches **cannot be exercised** on rows of near-equal length. There is **no real-label, customer or variable-length evidence** here, and none is claimed. **The question in the ticket title, whether faster batches qualify on the same real-label task and budget, stays open.** No candidate recipe was built or timed.

## 1. Run inventory

| Field | Value |
|---|---|
| Registered run | batch `8910bd724775`, 2026-11-28 00:37:56Z to 00:41:10Z, file `runs/measurements.json` |
| Measured git SHA | `0bb0bec4ee9f3c5ef7670bc488171d496b36c411` (tree `b760afa4abadd9e420899bf8cf1d8fc2fe5dd4e9`), branch `claude/ope-530-stage-timing`. The tree was **clean** at start. HEAD was unchanged at finish. Every row carries this SHA and `git_tree_dirty: false`. |
| Row binding | Each of the 41 rows has a unique `run_id` (`<batch>-r<repeat>-<state>-<kind>`), an ISO-8601 UTC `started_at`, `measured_git_sha` and `git_tree_dirty`. The file is a JSON object with a `rows` array. It is rewritten in full on every row and never appended. |
| Product files measured (SHA-256) | `smollm2_135m_lora_v1.json` `4bf04f57…7804c` (equals `PROFILE_SHA256`); `open_weight_worker.py` `01ec4c24…7b20`; `qualify_open_weight.py` `a05a3162…04df`; `services/cell-runtime/…/pilot/open_weight.py` `1c54691e…233d`. None of these was modified. They are byte-identical to the ones at `67ac969c`, the revision behind the earlier evidence. |
| Recipe (incumbent, unchanged) | SmolLM2-135M-Instruct @ `12fd25f77366fa6b3b4b768ec3050bf629380bac`, BF16 safetensors, frozen; rank-4 LoRA on tied lm_head rows A–H; alpha 4; seed 374; 64 full-batch Adam steps; learning rate 0.015; forward batch_size 4; max_tokens 192; BLAS 1 thread. |
| Checkpoint | All 4 profile files match their SHA-256 values: config `8eb740e8…`, model.safetensors `5af571cb…` (269,060,552 B), tokenizer `9ca9acdd…` and tokenizer_config `4ec77d44…`. They are staged **outside the repository**. |
| Runtime | CPython 3.12.16, numpy 2.5.5 and tokenizers 0.21.4. The wheel SHA-256 values `704d8192…` and `51b7eabb…` match `smollm2_135m_lora_v1.requirements.txt`. |
| Machine | KVM guest (lscpu reports hypervisor vendor KVM) inside a Docker container. Intel Xeon @ 2.10 GHz, 4 vCPU (1 thread per core), 15.7 GiB RAM, no swap, Linux 6.18.44 x86_64. The JSON field `virtualization: unknown` only means the DMI vendor file was unreadable. |
| Machine load | **Not idle.** Other sessions' pytest suites were running on the same 4 vCPUs. The 1-minute load average was 2.23 at start and 6.36 at finish. The median at the start of each product phase was 3.0 cold and 3.1 warm, with a range of 2.21–3.61. Load is recorded for every measured process. I waited 20 minutes for the machine to go quiet, and it did not. |
| Cache method | The condition is applied **before every measured process, including each of the 5 product phases**. **Cold:** `POSIX_FADV_DONTNEED` on the staged model files, the model venv and its CPython home. **Warm:** the same files are read through. mincore residency of model.safetensors is recorded at the start of each process: 0.0 in every cold phase and 1.0 in every warm phase (n = 25 each). The host page cache beneath the VM disk is not controllable from the guest. |
| Order | Alternating. Repeats 0, 2 and 4 ran cold first; repeats 1 and 3 ran warm first. |
| Attempted / completed / failed | 41 / 41 / 0: 5 repeats × {cold, warm} × {A product CLI (5 phases), B replica fit, B replica predict, C service fit split}, plus 1 D batch probe (20 trials). None was held or excluded. |
| Parity | All 20 replica outputs equal the product outputs. One artifact `a5367d97…a3ab` across all 10 product fits, one replay output `80a9f895…f09f3d` across all 20 replays. |
| Metric / denominator | 5 generated protected cases: 5 automatic, 0 wrong automatic, operating point met, unsupported projection sent to review, 0 customer acceptances, 0 cents. Identical in all 10 product runs. |
| Missing fields | The generated rehearsal has no verified-label wait, extraction or queue, so those are not observable. Energy was not measured. There are no candidate or paired times, and no real-label or customer quality. |
| Superseded trial (preserved) | `runs/superseded/measurements-cf074e3.json` (batch `cb614d341102`, at `cf074e392a96ed39cc78d1702b372ade00e0f192`, clean tree). It is the same method under heavier, unrecorded contention, before load recording was added. Its summary is `runs/superseded/summary-cf074e3.json`, regenerated by the `0bb0bec` summarizer. It is kept, not used for the decision. Its medians agree within about 3% (see §3). |

## 2. Stage times (ms, n = 5 per condition)

The product path (A) runs the unmodified `qualify_open_weight.py` phases, each a fresh process with its own cache conditioning.

| Stage | Cold median | Cold worst | Warm median | Warm worst | Share of e2e, cold / warm median |
|---|---|---|---|---|---|
| prepare (generation, residual gate, grant) | 74.8 | 80.6 | 61.1 | 71.1 | 1.27% / 1.07% |
| fit phase (whole process) | 2,245.1 | 2,391.0 | 2,175.9 | 2,340.7 | **38.34% / 38.83%** |
| … worker elapsed inside fit | 2,026 | 2,179 | 2,004 | 2,154 | — |
| replay-first | 1,708.9 | 1,976.3 | 1,638.5 | 1,679.5 | — |
| replay-repeat | 1,798.0 | 2,095.0 | 1,628.3 | 1,656.4 | — |
| both fresh replays | 3,495.0 | 4,071.3 | 3,257.2 | 3,308.2 | **58.94% / 58.86%** |
| verify | 74.7 | 78.2 | 60.4 | 63.6 | 1.25% / 1.07% |
| **time to verified verdict** | **6,002.0** | **6,472.7** | **5,603.7** | **5,715.8** | 100% |

Per-run end-to-end times in ms:
- Cold: 5829.6, 6472.7, 5753.1, 6002.0, 6034.4.
- Warm: 5493.7, 5603.7, 5635.0, 5554.5, 5715.8.

Cold costs about 398 ms more than warm at the median. The earlier evidence showed about 64 ms, because it evicted only before the fit.

**Inside the fit worker** (B replica; parity checked against the product):

| Sub-stage | Cold median (worst) | Warm median (worst) |
|---|---|---|
| interpreter start + module load | 41.8 | 35.7 |
| numpy + tokenizers import | 82.5 (92.2) | 49.9 (53.0) |
| SHA-256 of 4 inputs | 258.0 (270.4) | 235.3 (239.4) |
| tokenizer load | 70.2 (77.3) | 64.0 (78.5) |
| **tokenization (8 prompts)** | **2.3 (2.8)** | **1.1 (1.1)** |
| **frozen 30-layer forward** | **1,447.1 (1,584.7)** | **1,446.4 (1,591.5)** |
| **LoRA optimizer, 64 steps** | **14.7 (18.3)** | **10.2 (15.3)** |
| base re-hash (integrity) | 234.0 (242.6) | 231.6 (236.9) |
| replica wall | 2,180.7 (2,346.9) | 2,102.0 (2,264.3) |

Within the fit replica's wall time, the forward pass is 66.36% / 68.83%, hashing is 22.36% / 22.13% and the optimizer is 0.70% / 0.48%.

The predict replica splits the same way: forward 1,002.1 / 956.3 ms, hashing 472.8 / 483.1 ms, tokenization 1.8 / 0.8 ms, scoring 0.5 / 0.2 ms, wall 1,693.4 / 1,607.0 ms.

Service-side fit split (C):
- validation: 2.5 / 1.6 ms
- worker subprocess: 2,166.4 / 2,122.1 ms
- atomic publication: 2.7 / 3.9 ms

**Tokens, padding and throughput.**
- **Fit:** lengths [51, 50, 51, 51, 50, 51, 50, 50]; 404 useful tokens out of 408 padded (0.98% padding).
- **Predict:** lengths [52, 51, 50, 50, 51]; 254 useful out of 259 (1.93% padding).
- **Forward throughput, non-padding:** about 279 tok/s for the fit (cold and warm), and 253 / 266 tok/s for predict.
- **Fit rate:** 3.56 / 3.68 examples/s of fit-phase wall.
- **Peak RSS:** 283.4 / 284.7 MiB.

**Approximate shares of time to verified verdict.** These combine replica sub-stages with product medians.

| Component | Cold | Warm |
|---|---|---|
| Fit phase | 38.34% | 38.83% |
| LoRA optimizer (the actual parameter update) | 0.24% | 0.18% |
| Tokenization across fit + both replays | 0.10% | 0.05% |
| Frozen forward across the three workers | ~57.5% | ~59.9% |
| SHA-256 integrity hashing across the three workers | ~24.0% | ~25.6% |

## 3. The batch-size probe behind the opportunity bound

`harness/batch_probe.py` (committed; run as row `8910bd724775-warm-D_batch_probe`) runs in one warm worker-like process with the profile's address-space limit. It compares the frozen forward over the 8 fit prompts in two layouts, alternating the order over 20 trials:
- **incumbent:** two batches of 4;
- **merged:** one batch of 8, the most a token budget could merge on this fixture.

Hidden states were identical (max absolute difference 0.0).

| | Median | Min | Worst |
|---|---|---|---|
| incumbent (2×4) | 1,588.9 ms | 1,394.9 | 2,143.1 |
| merged (1×8) | 1,525.8 ms | 1,309.3 | 1,689.6 |
| saving per trial | **97.8 ms** | −117.4 | 547.0 |

The probe is noisy under the load described above. Two of 20 trials were slower merged. The large savings coincide with slow incumbent trials.

The **opportunity bound** for the ticket's bounded alternative (cached training-only tokenization plus length-bucketed, token-budgeted batches) on this fixture is an upper estimate. It removes:
- all tokenization;
- all padded forward positions;
- the median merge saving.

| | Cold | Warm |
|---|---|---|
| Removable time | 2.3 + 14.2 + 97.8 = **114.3 ms** | 1.1 + 14.2 + 97.8 = **113.1 ms** |
| Share of median fit wall | **5.09%** | **5.20%** |

The superseded contended run at `cf074e3` gave a 122.3 ms median saving, which puts the bound at 6.04% / 6.15%. **Stated bound: about 5–6% of fit wall time, about 2% of time to verified verdict.** A 20% median fit-time reduction would need about 450 ms per fit. The earlier "~17 ms / ≤0.8%" figure was wrong, because it left out per-batch overhead, and is withdrawn.

## 4. Decision (ticket decision table, priority order)

1. **Invalid evidence or unsafe effects?** No.
   - 41/41 rows completed and parity was 20/20.
   - The verdict, artifact and replay outputs were identical in every run, at 0 cents.
   - The load contention is disclosed. Its effect is noise of a few percent, not a change in which stage dominates: both batches agree.
2. **Source, queue or dataset preparation dominates?** No. Prepare takes 1.07–1.27% of time to verdict. The fixture has no label wait, extraction or queue.
3. **Tokenization repeats or excessive padding dominates the actual fit?** No.
   - Tokenization takes 1–2 ms per worker and padding is 0.98%.
   - The fit is dominated by the frozen forward pass (about 67%) and integrity hashing (about 22%).
   - The bounded alternative's ceiling is about 5–6% of fit wall, so it **cannot reach the 20% median fit-time target**.
4. **Faster, but a slice regresses, cost exceeds the cap, or quality is uncertain?** Not applicable, because no candidate was built.
5. **Owner D4 / step-3 stop.** The objective is time to a verified verdict. The fit is 38–39% of that time, so it does not dominate. The two fresh protected replays (about 59%) do.

**Decision: keep current behavior.** The default `smollm2_135m_lora_v1` recipe is unchanged, and no speed or accuracy claim is made. The new numbers **do not change** the earlier decision. They do correct its supporting figures:
- the cold/warm gap is about 398 ms, not about 64 ms;
- the bound is about 5–6%, not ≤0.8%;
- fit shares are 38.34% / 38.83%.

**Evidence level.** Generated engineering rehearsal, one named CPU backend, a shared and loaded machine. It is not customer evidence and not real-label quality.

**Observation, routed and not decided here.** The largest measured costs are the repeated frozen forward pass and the repeated full SHA-256 of the 269 MB base in each of the three worker processes. Both are verification and integrity controls owned by the OPE-528 and platform owners, not by the fit recipe. No claim is made and no change is authorized.

**Still open.** The ticket title's real-label, variable-length question needs a real-label fixture with a length distribution that bucketing can act on. Until then, this evidence decides only the generated fixture.

## 5. Acceptance mapping

| Requirement | Status |
|---|---|
| Redacted run inventory: date and revision, scope, inputs and config, attempted/completed/failed/held, metric and denominator, stage times, missing fields | Met for the generated fixture (§1) |
| Stage-time the current recipe on the named backend, cold and warm, with a matched baseline | Met: 5 + 5 repeats, per-phase conditioning, distributions in `runs/summary.json` |
| Instrument label wait, extraction, generation, tokenization, queue, fit, protected evaluation, proof publication | Partly met. Label wait, extraction and queue are absent from the fixture and recorded as not observable. |
| Decide from evidence via the table | Met for the generated fixture (§4) |
| Warm/cold, non-padding tok/s, examples/s, peak RAM, total wall, excluded examples | Met for the baseline. Energy was not measured. |
| Real-label task, variable-length datasets, five matched baseline/candidate pairs, 20% gate, tail ratios, noninferiority, critical slices, OPE-528 on untouched real families, adverse cases on an optimized path | **Not met / not applicable.** No candidate was built, and no real-label fixture exists here. |
| Exact-commit independent review | **Not done.** Pending, by a cross-session reviewer. |

## 6. Commands and exit codes

Paths: `$STAGE` is an out-of-repo staging directory holding `model-base/` and `model-python/`. `$WORK` is an out-of-repo work root.

| # | Command (cwd = repository root) | Exit / result |
|---|---|---|
| 1 | `git fetch origin main claude/gracious-thompson-6r8fcc`; `git checkout -b claude/ope-530-stage-timing origin/main`; `git checkout b650d7d -- training/profiles/benchmarks/ope530` | 0, 0, 0 |
| 2 | `just lint` on the imported harness, before the fixes | 1: ruff found 22 errors (21 in summarize.py, 1 F401 in stage_time.py) |
| 3 | `python3 tools/public_content_check.py` with `runs/measurements.jsonl` staged | 1: FAIL, 1 finding `UNREVIEWED_BINARY_OR_TYPE` |
| 4 | uv 0.13.8: `uv lock --check` and `uv sync --frozen --all-packages --no-python-downloads`, for the root and for `training/` | all 0; locks unchanged |
| 5 | pnpm 12.18.0 / Node 24.22.3: `pnpm install --frozen-lockfile --ignore-scripts` | 0 |
| 6 | `just lint` after the fixes (just 1.59.0; ruff plus the three oxlint steps) | 0: PASS, 4/4 steps 0 |
| 7 | `ruff check training/profiles/benchmarks/ope530`; `ruff format --check training/profiles/benchmarks/ope530` | 0; 0 |
| 8 | `$STAGE/model-python/bin/python training/profiles/benchmarks/ope530/harness/stage_time.py --staging $STAGE --work-root $WORK/smoke --out <scratch> --repeats 1 --probe-trials 4` (smoke test, not evidence) | 0, 9 rows |
| 9 | Same command at `cf074e3`, `--repeats 5 --probe-trials 10` | 0, 41 rows (superseded; preserved) |
| 10 | Same command at `0bb0bec`: `--work-root $WORK/main --out training/profiles/benchmarks/ope530/runs/measurements.json --repeats 5 --probe-trials 20` | **0, 41/41 rows, 0 failed** |
| 11 | `$STAGE/model-python/bin/python training/profiles/benchmarks/ope530/harness/summarize.py training/profiles/benchmarks/ope530/runs/measurements.json training/profiles/benchmarks/ope530/runs/summary.json` (and the same for the superseded file) | 0, 0 |
| 12 | `just test-python training/tests` | **0: PASS, 624 collected, 624 passed, 0 failed, 0 skipped** (kind simulated) |
| 13 | `OPENTOUR_QUALIFICATION_BASE=$STAGE/model-base OPENTOUR_TRAINING_PYTHON=$STAGE/model-python/bin/python .venv/bin/python -m pytest -q -rA -p no:cacheprovider services/cell-runtime/tests/pilot/qualified_model_checks.py` | **0: 2 passed, 0 skipped**, using the real staged model |
| 14 | Adverse check: the same command without the two variables | 1: 2 failed, so the suite does not pass vacuously |
| 15 | `python3 tools/public_content_check.py` at the evidence commit | 0: PASS, 0 findings |

Commands 12–14 ran at `0bb0bec`, with only the untracked evidence files present. The evidence commit adds only `.json` and `.md` files under this directory.

## 7. Deviations and limitations

- **Shared, loaded machine.** Other sessions' test suites ran throughout, with a 1-minute load of 2.2–6.4 on 4 vCPUs. Absolute times carry a few percent of noise, and the probe trials vary widely. The order alternated, and load is recorded per process.
- **Host page cache.** It is uncontrolled. "Cold" means the guest page cache was evicted, as verified by mincore. It does not mean a cold device.
- **Reused staging.** The model venv in the staging directory was created earlier with uv 0.11.32. Its interpreter is the toolchain-pinned CPython 3.12.16 tarball, and the hash-locked wheels are installed. The pinned uv 0.13.8 was used for the root and training syncs.
- **Instrumented replica.** The worker sub-stage split comes from `harness/worker_replica.py`, which calls the product worker's own functions and now applies its checks:
  - symlink/regular-file check;
  - fit refuses an over-length training prompt, as the product does, instead of dropping it;
  - adapter-profile check;
  - nonfinite-logit check.
  
  Phase totals come from the unmodified product CLI.
- **Probe scope.** The probe is warm and in-process only. It bounds one mechanism, merging forward batches. It does not implement or time an actual candidate recipe.
- **Generated data only.** The fixture has 8 near-equal-length rows and 0.98% padding. There is no real-label, variable-length, critical-slice or customer evidence.

## Reproduce

Never place weights, interpreters, wheels or work directories under the repository. The harness refuses a `--staging` or `--work-root` inside the checkout. This directory's `.gitignore` also blocks `model-base/`, `model-python/`, `wheels/`, `work-*/`, `*.safetensors` and `*.whl` as a backstop.

1. Choose a staging directory **outside the checkout**, for example `STAGE=$HOME/ope530-stage`.
2. Stage the checkpoint into `$STAGE/model-base/`, and the CPython 3.12.16 venv with the hash-locked wheels into `$STAGE/model-python/`, as described in `docs/pilot-open-weight.md`. Verify every SHA-256 listed in the profile.
3. From the repository root, on a clean tree:

   ```sh
   $STAGE/model-python/bin/python training/profiles/benchmarks/ope530/harness/stage_time.py \
     --staging "$STAGE" --work-root "$HOME/ope530-work-$(date +%s)" \
     --out training/profiles/benchmarks/ope530/runs/measurements.json --repeats 5 --probe-trials 20
   $STAGE/model-python/bin/python training/profiles/benchmarks/ope530/harness/summarize.py \
     training/profiles/benchmarks/ope530/runs/measurements.json \
     training/profiles/benchmarks/ope530/runs/summary.json
   ```

The harness derives the repository from its own location. It records `git rev-parse HEAD` and the dirty-tree flag before writing anything. It needs root, or the right to call `posix_fadvise` on the staged files, for cold eviction.
