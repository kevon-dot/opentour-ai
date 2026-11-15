# OPE-123 owns commands after OPE-122 handoff merge 6a72788d64a85513267839894d8234ef690c9cee.
set shell := ["sh", "-eu", "-c"]
set positional-arguments

# Keep the accepted OPE-122 setup entry usable before dependencies exist.
setup:
    @python3 tools/bootstrap.py

lint *args:
    @uv run --preview-features build-dependency-hashes --no-sync --offline --no-python-downloads python -m tools.test_runner lint "$@"

typecheck *args:
    @uv run --preview-features build-dependency-hashes --no-sync --offline --no-python-downloads python -m tools.test_runner typecheck "$@"

test-python *args:
    @uv run --preview-features build-dependency-hashes --no-sync --offline --no-python-downloads python -m tools.test_runner test-python "$@"

test-web *args:
    @uv run --preview-features build-dependency-hashes --no-sync --offline --no-python-downloads python -m tools.test_runner test-web "$@"

test-browser *args:
    @uv run --preview-features build-dependency-hashes --no-sync --offline --no-python-downloads python -m tools.test_runner test-browser "$@"

test-contract *args:
    @uv run --preview-features build-dependency-hashes --no-sync --offline --no-python-downloads python -m tools.test_runner test-contract "$@"

validate-spec *args:
    @uv run --preview-features build-dependency-hashes --no-sync --offline --no-python-downloads python -m tools.test_runner validate-spec "$@"

test-integration *args:
    @uv run --preview-features build-dependency-hashes --no-sync --offline --no-python-downloads python -m tools.test_runner test-integration "$@"

test-isolation *args:
    @uv run --preview-features build-dependency-hashes --no-sync --offline --no-python-downloads python -m tools.test_runner test-isolation "$@"

test-acceptance *args:
    @uv run --preview-features build-dependency-hashes --no-sync --offline --no-python-downloads python -m tools.test_runner test-acceptance "$@"
