ARG PYTHON_IMAGE
ARG UV_IMAGE
FROM ${UV_IMAGE} AS uv
FROM ${PYTHON_IMAGE}
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY services ./services
ARG PACKAGE
ARG MODULE
RUN uv sync --frozen --no-dev --package "${PACKAGE}" --no-python-downloads
ENV PATH="/app/.venv/bin:${PATH}" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ENV OPENTOUR_SMOKE_MODULE=${MODULE}
USER 10001:10001
CMD ["python", "-c", "import importlib,json,os,platform; importlib.import_module(os.environ['OPENTOUR_SMOKE_MODULE']); print(json.dumps({'kind':'local_integration','check':'container-package-import','module':os.environ['OPENTOUR_SMOKE_MODULE'],'python':platform.python_version(),'status':'PASS','product_acceptance':'NOT_RUN'}))"]
