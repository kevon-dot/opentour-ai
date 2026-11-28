# GPU training lock and trainer image (OPE-579)

This directory holds the GPU dependency stack for the EVE QLoRA trainer
([OPE-558](https://linear.app/opentourai/issue/OPE-558)). It is kept apart from everything the
control workspace installs.

| File | Purpose |
| --- | --- |
| `pyproject.toml` | Exact pins for the GPU stack. Virtual project (`package = false`), wheels only. |
| `uv.lock` | The GPU training lock. Linux x86_64 only. Every artifact carries a SHA-256. |
| `trainer.Dockerfile` | Trainer image built only from `uv.lock`, with hashes, as a non-root user. |
| `.dockerignore` | Admits only `pyproject.toml` and `uv.lock` into the image build context. |
| `eve-bake.hcl`, `infrastructure/images/eve-trainer.Dockerfile` | Build the runnable Linux/amd64 EVE trainer, its supervisor and the private worker exchange on the locked runtime. |

## Package the runnable trainer on CPU

From the repository root, run `docker buildx bake -f training/gpu/eve-bake.hcl --load eve`.
The resulting `opentour/eve-trainer:local` image runs the installed trainer module in isolated
Python mode as UID 10001. `docker run --rm --network none opentour/eve-trainer:local --help`
lists `serve`, `fit`, `status`, `pause`, `resume`, `stop` and `replay`. Supply the pinned
weights and authorized input files as read-only mounts at runtime.

The **Package EVE trainer on CPU** workflow runs two generated CPU proofs in the same Linux
image. One compares uninterrupted training with a checkpoint and fresh replay. The other drives
the installed exchange through pause, supervisor restart, resume, completion and cancellation.
The workflow publishes the tested trainer to GHCR and attaches the small checksummed receipts.
Pull the immutable manifest in `registry-ref.txt` and verify the local Docker image ID against
`image-id.txt`. The manifest digest and the image configuration digest identify different objects.

## Connect the installed worker

On a qualified host, the installed Slack adapter consumes `eve-learning-selection.json` in its
private worker directory. This is the normal sealed owner learning-run selection for Slack. The
signed `eve-spend-mandate.json` and `pilot-issuer-registry.json` bind the reviewer, current
plan, benchmark, paid execution, host, model and image. The execution must already be reserved
in the existing dispatch ledger. A missing signer, unqualified route or missing reservation
holds the proposal. The installed cycle worker refreshes a live permit every five seconds.
The authority and Slack credentials stay in the worker process.

Mount only the worker's owner-private `eve-exchange` directory read-write at `/exchange`,
the pinned base at `/model` and the development rows and protected membership fingerprints at
`/inputs` read-only. Name each input `<sha256>.json`. Give UID 10001 a separate private
read-write `/work`. Mounts must be separately addressable and the exchange must have the
same effective owner in each process. Run this entry in the GPU container with networking
disabled and the verified configuration digest supplied by the host:

```sh
serve --exchange /exchange --model-dir /model --inputs /inputs --work /work --image-id "$IMAGE_ID"
```

Enable the existing Slack `channel_aggregates` setting for the authorized private channel.
The optional `learning_thread_ts` selects its existing progress thread. Current Slack approval
admits the fit. Pause becomes confirmed when the trainer writes a checkpoint. The worker
keeps showing the same run across restart. Paused hardware continues to consume the host
allocation. The GPU rental needs an independent provider-side deletion at its cutoff.
A model release requires the separate benchmark and release decision.

## Isolation rules

- This project is **not** a member of the root uv workspace. uv finds `training/` first, a
  non-workspace project, so this directory resolves as its own root. The root `pyproject.toml`
  lists only the control services in `[tool.uv.workspace].members`.
- `just setup`, `tools/bootstrap.py`, the root `uv.lock` and `training/uv.lock` do not reference
  this project. CUDA, torch and the `nvidia-*` / `cuda-*` wheels never enter the control
  workspace. `tests/contract/test_gpu_training_lock.py` checks this.
- Do **not** run `uv sync`, `uv pip install` or `pip install` for this project on a developer
  machine or in the control workspace. Install it only inside `trainer.Dockerfile`.
- Resolving is allowed. To change a pin, edit `pyproject.toml` and run, from this directory,
  with uv 0.13.8 (the `tools/toolchain.json` pin) and a CPython 3.12.16 interpreter:
  `uv lock --no-python-downloads --python <python3.12.16>`. Resolution reads package metadata
  only. A pin change invalidates every earlier qualification record for this lock.

## Single writer

The single writer of `training/gpu/pyproject.toml`, `training/gpu/uv.lock` and
`training/gpu/trainer.Dockerfile` is the **OPE-579 lane**. The OPE-579 owner must name the
person who holds that lane in Linear. Until then, nobody else changes these files. Other
issues request pin changes from that lane. They do not edit the lock in parallel.

**Status:** no person is named yet. OPE-579 item 1 ("a named single writer") stays open until
the OPE-579 owner names one. This file does not satisfy that item.

## Pinned stack

| Component | Pin | Notes |
| --- | --- | --- |
| Python | 3.12.16 | python-build-standalone 20261110, the `tools/toolchain.json` artifact and SHA-256 |
| torch | 2.14.1 | PyPI Linux x86_64 wheel, built against CUDA 13.0 |
| transformers | 5.23.0 | |
| peft | 0.21.3 | |
| bitsandbytes | 0.50.2 | Wheel ships `libbitsandbytes_cuda130.so` |
| accelerate | 1.15.0 | |
| safetensors | 0.8.0 | |
| tokenizers | 0.23.3 | |
| huggingface-hub | 2.4.0 | Set to offline mode in the image. Network isolation is enforced by the operator. |
| CUDA runtime wheels | `cuda-toolkit` 13.0.3.0, `nvidia-cuda-runtime` 13.0.96, `nvidia-cublas` 13.1.1.3, `nvidia-cudnn-cu13` 9.24.0.43, `nvidia-nccl-cu13` 2.30.7, `nvidia-nvjitlink` 13.0.88, `cuda-bindings` 13.0.3, `triton` 3.8.0 | Full set and hashes in `uv.lock` |
| Base image | `nvidia/cuda:13.0.3-base-ubuntu24.04` linux/amd64 `sha256:97d085a7423ee18ec483a2878b9be2c976dc4ba908aef96518beb00e1899dcc4` | Index `sha256:7c7413a56200486f71f181cad9310f6fd31b6bb21816ade15fc9c1e1e927a5c1`. Checked on 2026-11-27 against registry-1.docker.io: the index, manifest and config bytes re-hashed to their digests, and Docker Hub reports the same digests. Uses the `base` flavour, not `runtime`, so the image has no second cuBLAS or NCCL. Its config history adds only `cuda-cudart-13-0` 13.0.96 and `cuda-compat-13-0`. Which library copies actually load is unverified until qualification item 10 runs. |

The lock uses `exclude-newer = 2026-11-16T00:00:00Z` so transitive pins re-resolve to the
same set.

The CUDA wheels come from PyPI. The CUDA wheel index `download.pytorch.org` was refused by the
resolver environment's network proxy (`CONNECT` 403) on 2026-11-27. PyPI's torch 2.14.1 Linux
x86_64 wheel is the CUDA 13.0 build. It declares `cuda-toolkit==13.0.3` and the `nvidia-*-cu13`
wheels pinned here.

## Supported driver range

- **Supported:** NVIDIA Linux driver **580.65.06 or newer**, the CUDA 13.0 minimum. The driver
  must report CUDA 13.0 or higher in `nvidia-smi`.
- **Not supported:** drivers below 580.65.06, including the 535/550/565/570/575 data-center
  branches. The base image's own `NVIDIA_REQUIRE_CUDA` allows those branches through CUDA
  forward compatibility. The trainer image overrides it with `NVIDIA_REQUIRE_CUDA=cuda>=13.0`
  and no brand exceptions, so the NVIDIA container toolkit refuses to start it on an older
  driver. The image also deletes the base image's `/usr/local/cuda/compat` forward-compatibility
  `libcuda`. The operator must not set `NVIDIA_DISABLE_REQUIRE`, which skips the check.
- **Unverified: the toolkit check may not run in CDI mode.** `NVIDIA_REQUIRE_CUDA` is checked
  by the NVIDIA container toolkit's legacy injection path (`nvidia-container-cli`). When the
  toolkit injects the GPU through CDI (Container Device Interface) instead, the check may not
  run. This has not been verified for any toolkit version or on the OPE-565 host. The backstop
  is the driver version recorded in qualification item 3: a run on a driver below 580.65.06
  fails qualification, whatever the container toolkit allowed.
- **GPU:** one 80 GB NVIDIA data-center GPU, Ampere (sm_80) or newer. CUDA 13 no longer
  supports Volta and older GPUs.
- No upper driver bound is asserted. A newer driver is qualified only on the host where it was
  recorded.

## What qualification on the OPE-565 host must record

Qualification runs on the approved no-AWS 80 GB host from
[OPE-565](https://linear.app/opentourai/issue/OPE-565), using this image built from this lock at
a recorded commit. Each run records:

1. **Identity.** Image digest, the `training/gpu/uv.lock` SHA-256, the repository commit, and
   the reviewed OPE-558 trainer commit.
2. **Device.** GPU model, memory and compute capability from `nvidia-smi` and
   `torch.cuda.get_device_properties`. Record the host ID only in redacted form.
3. **Driver.** Driver version and the CUDA version it reports. Also record
   `torch.version.cuda` and `torch.backends.cudnn.version()`, and confirm the driver is in the
   supported range. Also record whether the container toolkit injected the GPU in legacy or
   CDI mode. This record is the backstop if the toolkit did not enforce `NVIDIA_REQUIRE_CUDA`.
4. **Checkpoint.** The pinned `eve-esa/EVE-Instruct` revision
   `b4965c4acbb27299078e4f97171530644fcc839f` and the `model.safetensors` SHA-256
   `150815d5b113536a83f24099528794686055675953cfcc14cdc6dbdadd86edfc`.
5. **Peak memory.** `torch.cuda.max_memory_allocated` and `max_memory_reserved` for the fit and
   for replay, plus wall time.
6. **Bounded fit.** One fit of 8–16 steps on authorized fixtures within the host's timeout and
   spend cap. Record the step count, finite loss, the trainable module selection, the trainable
   parameter count, nonzero adapter-weight updates and base integrity.
7. **Fresh-process replay.** Reload the adapter in a new process. Replay must reproduce
   generation within the stated tolerance, with the adapter both disabled and enabled.
8. **Route order: NF4 QLoRA first, bf16 LoRA fallback.** Try 4-bit NF4 QLoRA (bitsandbytes,
   bf16 compute) first. Try bf16 LoRA only if the 4-bit kernels fail compatibility. Record the
   fallback as a separate route, with the NF4 failure that triggered it.
9. **Runtime isolation.** The container runs with `--network none`, as UID 10001, with the
   weights mounted read-only. The image cannot enforce this. Its offline variables only stop
   the Hub and package clients from trying to connect. The operator enforces
   `--network none`, and the record must show it. The image sets `PYTHONSAFEPATH=1`. Python
   then never puts the working directory or a script's own directory on `sys.path`, so a
   writable mount such as `/run/opentour` cannot shadow the venv, even under `-w`. The OPE-558
   trainer must therefore run as an installed module (`python -m`), not as a script that
   imports files next to it.
10. **Loaded CUDA libraries.** During the fit and the replay, read `/proc/self/maps` in the
    training process after the first CUDA operation. Record the resolved path and version of
    each loaded `libcudart`, `libcublas` and `libnccl`. Each path must be under
    `/opt/venv/lib/python3.12/site-packages/`, inside a locked `nvidia-*` wheel. A path under
    `/usr/local/cuda` or `/usr/lib/x86_64-linux-gnu` fails qualification. Also record the
    `libcuda` path the NVIDIA container toolkit injected and any `/usr/local/cuda/compat` path.
    A compat path would mean CUDA forward compatibility was used, which this lock does not
    support. Recent NVIDIA container toolkits can select the base image's `cuda-compat-13-0`
    automatically when the host driver is older than it. The image now deletes
    `/usr/local/cuda/compat` and requires `cuda>=13.0` with no brand exceptions, so neither
    should happen. Still, record which `libcuda` the process actually loaded, from
    `/proc/self/maps`. Also record that the container started with `NVIDIA_REQUIRE_CUDA` in
    force and `NVIDIA_DISABLE_REQUIRE` unset. Qualification does not support use of the compat
    `libcuda`.

## Follow-ups for other owners

- **Dependency scan.** The OSV scan in `tools/ci/run.py` (`dependency_scan_command`) covers
  only `uv.lock`, `training/uv.lock` and `pnpm-lock.yaml`. It does not scan
  `training/gpu/uv.lock`. Adding `--lockfile=training/gpu/uv.lock` is a follow-up for the
  owner of `tools/ci/run.py`. The file's history does not name an owner, so confirm it. AGENTS.md
  assigns command wrappers to OPE-123. This lane does not edit that file. The contract test
  forbids any reference to `training/gpu` in `tools/ci/run.py` and the `justfile`, with one
  exception: the exact string `"--lockfile=training/gpu/uv.lock"` in `tools/ci/run.py`, so this
  follow-up stays possible.
- **Toolchain pointer.** `tools/toolchain.json` (OPE-122/OPE-123) could name this lock.
  `gpu_qualification.status` stays `NOT_RUN`.

## Evidence limits

- **Component evidence only.** A CPU-only, tokenizer-only, import-only, lock-resolution or
  simulated check proves only that one component works. That includes this lock resolving, the
  contract test passing, the `.so` listing of a wheel, and the image's default version report.
  None of them is GPU qualification, an EVE fit or replay, or customer evidence.
- `tools/toolchain.json` `gpu_qualification.status` stays `NOT_RUN` until the OPE-565 host
  records the items above.
- The runnable image packages the OPE-558 trainer recovered and independently reviewed at
  `d9943f31074c50b1f4aa3733f5f17cdd837e39ce`. Its EVE 24B qualification requires the approved
  GPU, a fresh Slack training approval and the measured evidence above.
