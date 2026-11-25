# Local supervised decision adapter

An eligible pilot team can fit a private adapter to route a finite set of labels on its own CPU. The supported checkpoint is HuggingFaceTB/SmolLM2-135M-Instruct at revision `12fd25f77366fa6b3b4b768ec3050bf629380bac`. The profile `smollm2_135m_lora_v1` runs all 30 pretrained transformer layers, keeps the original BF16 weights read-only, and learns a rank-four update on the tied language-model output projection for the approved decision codes. Each label maps to one code from A through H. The owner keeps the adapter and model files locally. [Publisher model card](https://huggingface.co/HuggingFaceTB/SmolLM2-135M-Instruct/blob/12fd25f77366fa6b3b4b768ec3050bf629380bac/README.md)

## Stage the model once

Use a private directory on Linux x86-64. Copy `config.json`, `model.safetensors`, `tokenizer.json` and `tokenizer_config.json` from that exact publisher revision into the engagement's `model-base/`. The complete BF16 checkpoint is 269,060,552 bytes. Verify every SHA-256 listed in `training/profiles/smollm2_135m_lora_v1.json`, including the license reference if retaining a copy. The publisher identifies the model as Apache-2.0. Record customer permission for the model, training records, destination and intended use separately. [Pinned files](https://huggingface.co/HuggingFaceTB/SmolLM2-135M-Instruct/tree/12fd25f77366fa6b3b4b768ec3050bf629380bac) · [Apache License 2.0](https://www.apache.org/licenses/LICENSE-2.0)

Stage a CPython 3.12.16 environment with the two Linux x86-64 wheels named by the profile. The local tokenizer API needs the Rust tokenizer wheel. Keep the two approved wheels in a private directory. Install them offline with the checked-in hash lock.

```sh
uv venv --offline --no-python-downloads --python /private/cpython-3.12.16/bin/python /private/model-python
uv pip install --python /private/model-python/bin/python --offline --no-index \
  --find-links /private/verified-wheels --only-binary :all: --no-deps --require-hashes \
  -r training/profiles/smollm2_135m_lora_v1.requirements.txt
export OPENTOUR_TRAINING_PYTHON=/private/model-python/bin/python
```

The worker supports CPython 3.12.16, NumPy 2.5.5 and tokenizers 0.21.4. It reads the tokenizer from disk, fixes BLAS to one thread and uses no provider connection. A missing file, a different revision or an incompatible runtime holds the model route. The rules and supplied-output workflows can still be used independently.

## Approve the fit

Use the managed pilot workflow to enroll the decision-time fields, permitted labels and rights. The owner approves one residual decision, training scope, destination, trainer, payer, expiry and time cap. Local incremental compute cost must be zero. An existing rule which meets the owner’s operating limits can complete the engagement before this step.

The fit accepts two to 32 distinct training cases, one to eight approved feature fields, and two to eight labels. The separate protected cohort contributes only hashed case IDs, related-case family IDs and content fingerprints to the fitting process. All training labels must be in the granted vocabulary. The tokenizer applies the frozen ChatML decision template with a limit of 192 tokens per case. Out-of-schema inference and oversized inference prompts go to manual review.

The atomic customer artifact includes `adapter.json`, with both FP32 LoRA tensors, and `manifest.json`, with the exact base, worker, adapter, grant, training, protected membership, residual and resource evidence. The immutable model identity is `artifact_sha256`. A retry of the same completed request returns the original manifest. A changed request requires a new fit. Withdrawal stops the active local child and writes a durable denial for the bound run. The handoff additionally checks current owner custody before using an exported model reference.

## Reproduce the engineering qualification

From the repository checkout, give the runner a new private directory and the exact local interpreter and model. This command exercises the generated simple winner without fitting, an authorized model fit, two fresh replays, fallback, deadline, withdrawal, corrupt artifacts and grant failures.

```sh
/private/model-python/bin/python training/tests/training/test_public_model_qualification.py \
  --model-dir /private/engagement/model-base \
  --numeric-python /private/model-python/bin/python \
  --wheel-dir /private/verified-wheels \
  --work-dir /private/empty-generated-qualification
```

The generated rehearsal trains on eight labeled support-routing examples. Two independent replays score five separate examples and an unsupported projection. The source contains original Transformers 4.49.0 / CPU PyTorch 2.6.0 reference logits for two prompts and compares the local frozen forward pass against them. The runner writes `qualification-result.json` with actual loss, parameter changes, memory, elapsed time, artifact hashes, wrong-automatic count, fallback and the result against an 80% minimum coverage and 5% maximum wrong-automatic rate. Its customer acceptance count is zero. A customer decision requires a customer-owned protected cohort and owner receipt.

To inspect the individual steps, run `training/profiles/qualify_open_weight.py` with `PYTHONPATH=services/cell-runtime/src`, the same three paths, and run each phase in its own process, in this order, `prepare`, `fit`, `replay-first`, `replay-repeat` and `verify`. Keep the staged publisher weights, input records and generated private evidence outside the source repository.


### Integrated qualification with the staged model

Run the ordinary service tests on every checkout. On the authorized machine, set
`OPENTOUR_QUALIFICATION_BASE` to the independently verified base directory and
`OPENTOUR_TRAINING_PYTHON` to the approved numeric interpreter. Then run the
dedicated qualification suite. It requires both resources and exercises genuine
fitting under the standing mandate and a disconnected recipient's replay.

```sh
.venv/bin/python -m pytest -q services/cell-runtime/tests/pilot/qualified_model_checks.py
```
