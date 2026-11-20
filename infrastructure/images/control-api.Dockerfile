# Base image pins match tools/toolchain.json.
FROM ghcr.io/astral-sh/uv:0.13.8@sha256:5cc1805e2426484abca4ad46ea783bab281fdb225fd82d8ce6d66a81254bac85 AS uv
FROM docker.io/library/python:3.12.16-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df AS build

COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY services ./services
COPY packages/authority-journal ./packages/authority-journal
RUN uv sync --frozen --no-dev --no-editable --no-python-downloads \
    --no-cache --package opentour-control
RUN .venv/bin/python -c "from importlib.util import find_spec; assert find_spec('opentour_control.api.server')"

FROM docker.io/library/python:3.12.16-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df

WORKDIR /app
COPY --from=build /app/.venv ./.venv
# Fargate seeds the ephemeral mount from this empty runtime directory.
RUN install -d -m 0755 -o 0 -g 0 /run/opentour \
    && install -d -m 0700 -o 10001 -g 10001 /run/opentour/runtime
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
USER 10001:10001
VOLUME ["/run/opentour"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s \
    CMD ["python", "-c", "from http.client import HTTPConnection; c = HTTPConnection('127.0.0.1', 8000, timeout=2); c.request('GET', '/health/ready'); raise SystemExit(c.getresponse().status != 200)"]
CMD ["python", "-m", "opentour_control.api.server"]
