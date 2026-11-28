"""Shared pilot test configuration. Generated fixtures only.

Every pilot test gets its own private anti-rollback directory (OPE-543), injected through
``OPENTOUR_ANTI_ROLLBACK_DIRECTORY`` so child processes inherit it too. No test may read or
write the real worker-host location ``/var/lib/opentour-antirollback``.

Production refuses a location on the ledger's filesystem device (``anti_rollback_same_volume``),
and every ledger directory in these tests lives under pytest's temporary root. The location
is therefore created on a genuinely different filesystem, the ``/dev/shm`` tmpfs, so parent
and child processes run unmodified production code with no seam. Without such a filesystem
the fixture fails: never a skip, never a weaker check.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

ANTI_ROLLBACK_ENV = "OPENTOUR_ANTI_ROLLBACK_DIRECTORY"
REAL_ANTI_ROLLBACK = Path("/var/lib/opentour-antirollback")
SEPARATE_FILESYSTEM = Path("/dev/shm")


@pytest.fixture(scope="session")
def anti_rollback_volume(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """A private directory on a filesystem device other than pytest's temporary root."""
    temporary = tmp_path_factory.getbasetemp()
    try:
        separate = os.stat(SEPARATE_FILESYSTEM).st_dev != os.stat(temporary).st_dev
    except OSError:
        separate = False
    if not separate:
        pytest.fail(
            "OPE-543 tests need /dev/shm on a filesystem other than the pytest temporary root"
        )
    base = Path(tempfile.mkdtemp(prefix="opentour-antirollback-test-", dir=SEPARATE_FILESYSTEM))
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


@pytest.fixture(autouse=True)
def anti_rollback_directory(anti_rollback_volume: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    location = Path(tempfile.mkdtemp(prefix="test-", dir=anti_rollback_volume))
    location.chmod(0o700)
    assert location.resolve() != REAL_ANTI_ROLLBACK
    monkeypatch.setenv(ANTI_ROLLBACK_ENV, str(location))
    return location
