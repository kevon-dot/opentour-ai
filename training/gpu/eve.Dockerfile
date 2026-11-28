# Build through eve-bake.hcl so this name resolves to the locked local build target.
FROM eve-runtime.invalid/locked
COPY src/opentour_training/__init__.py /opt/venv/lib/python3.12/site-packages/opentour_training/
COPY src/opentour_training/training/__init__.py src/opentour_training/training/eve_run.py src/opentour_training/training/eve_sft.py /opt/venv/lib/python3.12/site-packages/opentour_training/training/
ENTRYPOINT ["/opt/venv/bin/python", "-I", "-B", "-m", "opentour_training.training.eve_sft"]
CMD ["--help"]
