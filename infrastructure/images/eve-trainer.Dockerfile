# Build through eve-bake.hcl so this name resolves to the locked local build target.
FROM eve-runtime.invalid/locked
LABEL org.opencontainers.image.source="https://github.com/kevon-dot/opentour-ai"
COPY training/src/opentour_training/__init__.py /opt/venv/lib/python3.12/site-packages/opentour_training/
COPY training/src/opentour_training/training/__init__.py training/src/opentour_training/training/eve_run.py training/src/opentour_training/training/eve_sft.py training/src/opentour_training/training/eve_host.py /opt/venv/lib/python3.12/site-packages/opentour_training/training/
COPY services/cell-runtime/src/opentour_cell/pilot/eve_exchange.py /opt/venv/lib/python3.12/site-packages/opentour_cell/pilot/
ENTRYPOINT ["/opt/venv/bin/python", "-I", "-B", "-m", "opentour_training.training.eve_sft"]
CMD ["--help"]
