ARG PYTHON_IMAGE
ARG UV_IMAGE
FROM ${UV_IMAGE} AS uv
FROM ${PYTHON_IMAGE}
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
COPY training ./
ARG MODULE
ARG ROLE
RUN uv sync --frozen --no-dev --no-python-downloads
ENV PATH="/app/.venv/bin:${PATH}" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ENV OPENTOUR_SMOKE_MODULE=${MODULE} OPENTOUR_BOOTSTRAP_ROLE=${ROLE}
USER 10001:10001
CMD ["python", "-c", "import importlib,json,os,platform; importlib.import_module(os.environ['OPENTOUR_SMOKE_MODULE']); print(json.dumps({'kind':'local_integration','check':'learning-container-package-import','role':os.environ['OPENTOUR_BOOTSTRAP_ROLE'],'python':platform.python_version(),'status':'PASS','gpu_qualification':'NOT_RUN','product_acceptance':'NOT_RUN'}))"]
