"""OPE-566 dependency guard. EXPECTED TO FAIL until OPE-576 lands.

``opentour_cell.pilot.signed_records`` imports ``opentour_journal``. In the workspace venv
that import succeeds only because other members depend on ``opentour-journal``: an
undeclared dependency that would pass locally and fail in the pilot kit. This test keeps
the change mechanically unmergeable until ``opentour-cell`` declares the dependency.
Do not skip or xfail it; OPE-576 (owner: OPE-122) makes it pass.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"


def test_cell_runtime_declares_opentour_journal_dependency() -> None:
    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]
    assert project["name"] == "opentour-cell"
    names = {
        re.split(r"[\s\[<>=!~;@]", requirement, maxsplit=1)[0].lower().replace("_", "-")
        for requirement in project.get("dependencies", [])
    }
    assert "opentour-journal" in names, (
        "opentour-cell imports opentour_journal but does not declare it (OPE-576)"
    )
