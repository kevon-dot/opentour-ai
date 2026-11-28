"""Guard the declared runtime dependency needed by the pilot verifier."""

import re
import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"


def test_cell_runtime_declares_opentour_journal_dependency() -> None:
    manifest = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    project = manifest["project"]
    assert project["name"] == "opentour-cell"
    names = {
        re.split(r"[\s\[<>=!~;@]", requirement, maxsplit=1)[0].lower().replace("_", "-")
        for requirement in project["dependencies"]
    }
    assert "opentour-journal" in names
    assert manifest["tool"]["uv"]["sources"]["opentour-journal"] == {"workspace": True}
