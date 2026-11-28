"""Shared pilot test configuration. Generated fixtures only.

Every pilot test gets its own private anti-rollback directory (OPE-543), injected through
``OPENTOUR_ANTI_ROLLBACK_DIRECTORY`` so child processes inherit it too. No test may read or
write the real worker-host location ``/var/lib/opentour-antirollback``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ANTI_ROLLBACK_ENV = "OPENTOUR_ANTI_ROLLBACK_DIRECTORY"
REAL_ANTI_ROLLBACK = Path("/var/lib/opentour-antirollback")


@pytest.fixture(autouse=True)
def anti_rollback_directory(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Path:
    location = tmp_path_factory.mktemp("antirollback")
    location.chmod(0o700)
    assert location.resolve() != REAL_ANTI_ROLLBACK
    monkeypatch.setenv(ANTI_ROLLBACK_ENV, str(location))
    return location
