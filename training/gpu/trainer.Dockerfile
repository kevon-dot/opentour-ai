# OPE-579 EVE QLoRA trainer image (GPU training lock only; NOT a bootstrap smoke image).
#
# Build context is training/gpu/ (its own .dockerignore admits only the lock inputs):
#   docker build -f training/gpu/trainer.Dockerfile training/gpu
# Requires BuildKit with Dockerfile frontend 1.6+ (ADD --checksum). No syntax directive is set
# so no unpinned frontend image is pulled.
#
# Build time has network: the pinned Python archive and the locked wheels are fetched and every
# byte is checked against a recorded SHA-256. Run time must have no network. The image cannot
# enforce that: the operator must run it with `--network none`, the weights mounted read-only,
# and an explicit GPU grant, for example
#   docker run --rm --network none --gpus device=0 --read-only --tmpfs /tmp \
#     -v "$EVE_MODEL_DIR:/models/eve:ro" -v "$RUN_DIR:/run/opentour" <image> ...
# The image never installs into, and is never built inside, the control workspace.
#
# Building this image does not qualify it. GPU qualification (README.md) runs on the approved
# OPE-565 host and is recorded separately. The OPE-558 trainer code is not in this image yet:
# it lands after its exact-commit review (OPE-579 item 4).

# NVIDIA CUDA 13.0.3 *base*, Ubuntu 24.04, linux/amd64 manifest. Verified 2026-11-27 against
# registry-1.docker.io: tag 13.0.3-base-ubuntu24.04 -> index
# sha256:7c7413a56200486f71f181cad9310f6fd31b6bb21816ade15fc9c1e1e927a5c1 (sha256 of the bytes
# served), whose linux/amd64 entry is the digest below (manifest bytes re-hashed to match).
# The base flavour is used instead of runtime so that the image has no second CUDA stack: runtime
# also installs cuda-libraries (cuBLAS, cuSPARSE, ...) and libnccl2 2.28.3 in a default linker
# directory, which could shadow the locked wheels (NCCL 2.30.7) in a process that loads them
# without torch. Per its config history, base adds only cuda-cudart-13-0 13.0.96 (the same
# version as the locked nvidia-cuda-runtime wheel), cuda-compat-13-0, and an ld.so.conf entry
# for /usr/local/cuda/lib64. Which copy of each library a process loads is recorded on the
# OPE-565 host (README qualification item 10); it is not proven here.
# Both images are written into FROM by digest, with no ARG, so `--build-arg` cannot swap them.

# Same uv build as tools/toolchain.json bootstrap_images.uv.
FROM ghcr.io/astral-sh/uv:0.13.8@sha256:5cc1805e2426484abca4ad46ea783bab281fdb225fd82d8ce6d66a81254bac85 AS uv

FROM docker.io/nvidia/cuda:13.0.3-base-ubuntu24.04@sha256:97d085a7423ee18ec483a2878b9be2c976dc4ba908aef96518beb00e1899dcc4 AS build
COPY --from=uv /uv /usr/local/bin/uv
# CPython 3.12.16 from tools/toolchain.json (python-build-standalone 20261110, Linux-x86_64).
ADD --checksum=sha256:6a9e62dad033790aef722dbd370e0ce76ce17b73627ba3ae679f537049350b39 \
    https://github.com/astral-sh/python-build-standalone/releases/download/20261110/cpython-3.12.16%2B20261110-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz \
    /tmp/cpython.tar.gz
RUN mkdir -p /opt/python \
 && tar -xzf /tmp/cpython.tar.gz -C /opt/python --strip-components=1 \
 && rm /tmp/cpython.tar.gz \
 && test "$(/opt/python/bin/python3.12 -c 'import platform; print(platform.python_version())')" = "3.12.16"
WORKDIR /src
COPY pyproject.toml uv.lock ./
# Ubuntu 24.04's own python3 is 3.12.3, which fails requires-python ==3.12.16; every uv call
# must use the pinned interpreter.
ENV UV_NO_CACHE=1 UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy \
    UV_PYTHON=/opt/python/bin/python3.12
# --locked fails if pyproject.toml and uv.lock disagree. The export carries every wheel hash;
# --require-hashes refuses any artifact whose hash is not in the lock, --no-deps refuses any
# package the lock did not name, and --no-build refuses sdists.
RUN uv export --locked --no-dev --no-emit-project --format requirements-txt \
      --python /opt/python/bin/python3.12 -o /tmp/requirements.txt \
 && uv venv /opt/venv --python /opt/python/bin/python3.12 \
 && uv pip install --python /opt/venv/bin/python --require-hashes --no-deps --no-build \
      --compile-bytecode -r /tmp/requirements.txt \
 && uv pip check --python /opt/venv/bin/python \
 && rm /tmp/requirements.txt

FROM docker.io/nvidia/cuda:13.0.3-base-ubuntu24.04@sha256:97d085a7423ee18ec483a2878b9be2c976dc4ba908aef96518beb00e1899dcc4
COPY --from=build /opt/python /opt/python
COPY --from=build /opt/venv /opt/venv
# Driver floor, made mechanical: remove the base image's CUDA forward-compatibility libcuda so it
# can never be selected, and (below) require CUDA 13.0 from the host driver with no brand
# exceptions, so the container toolkit refuses hosts on the 535-575 driver branches.
RUN rm -rf /usr/local/cuda/compat /usr/local/cuda-13.0/compat \
 && ldconfig \
 && test ! -e /usr/local/cuda/compat
RUN groupadd --gid 10001 trainer \
 && useradd --uid 10001 --gid 10001 --create-home --home-dir /home/trainer \
      --shell /usr/sbin/nologin trainer
# These variables only stop the Hub, package-index and telemetry clients from trying to connect.
# They do not remove network access: the operator must enforce `--network none` at run time.
# LD_LIBRARY_PATH keeps only the base image's /usr/local/nvidia/lib{,64} driver entries and drops
# /usr/local/cuda/lib64, so the base cudart is not preferred over the wheels' through the
# environment. (The ld.so.conf entry remains; item 10 records the path actually loaded.)
ENV PATH="/opt/venv/bin:${PATH}" \
    LD_LIBRARY_PATH=/usr/local/nvidia/lib:/usr/local/nvidia/lib64 \
    NVIDIA_REQUIRE_CUDA="cuda>=13.0" \
    PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONNOUSERSITE=1 \
    HF_HOME=/home/trainer/.cache/huggingface \
    HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_DATASETS_OFFLINE=1 \
    HF_HUB_DISABLE_TELEMETRY=1 DO_NOT_TRACK=1 \
    PIP_NO_INDEX=1 UV_OFFLINE=1 \
    OPENTOUR_GPU_LOCK=training/gpu/uv.lock OPENTOUR_GPU_QUALIFICATION=NOT_RUN
WORKDIR /home/trainer
USER 10001:10001
ENTRYPOINT []
# Default command only reports the installed stack. It is component evidence, not qualification.
CMD ["python", "-c", "import importlib.metadata as m,json,platform; print(json.dumps({'kind':'component','check':'gpu-trainer-image-versions','python':platform.python_version(),'versions':{p:m.version(p) for p in ('torch','transformers','peft','bitsandbytes','accelerate','safetensors','tokenizers')},'gpu_qualification':'NOT_RUN','product_acceptance':'NOT_RUN'}))"]
