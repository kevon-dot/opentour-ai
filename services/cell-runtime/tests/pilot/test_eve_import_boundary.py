"""OPE-573 import boundary: the replay worker reaches neither the scorer nor the aggregator.

The static check reads source with ``ast`` and follows every import inside ``opentour_cell``
transitively, including imports inside functions, ``if TYPE_CHECKING`` blocks, relative imports
and ``importlib.import_module``/``__import__`` calls with a literal name. The runtime check imports
the replay module in a fresh interpreter and inspects ``sys.modules``. A scanner self-test proves
the static check flags each forbidden form, so it cannot pass vacuously.

This is a structural boundary only. It does not make the scorer a separate evaluator: whoever wires
a run still hands the scorer and the answer key to one process.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"
REPLAY = "opentour_cell.pilot.eve_replay"
SCORER = "opentour_cell.pilot.eve_scorer"
AGGREGATOR = "opentour_cell.pilot.eve_aggregate"


def module_file(name: str) -> Path | None:
    base = SRC.joinpath(*name.split("."))
    if base.with_suffix(".py").is_file():
        return base.with_suffix(".py")
    if (base / "__init__.py").is_file():
        return base / "__init__.py"
    return None


def imported_names(source: str, module: str, *, is_package: bool = False) -> set[str]:
    """Every module name ``source`` may import, as written or resolved from a relative import."""
    package = module if is_package else module.rpartition(".")[0]
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split(".")
                anchor = ".".join(parts[: len(parts) - node.level + 1])
                base = f"{anchor}.{node.module}" if node.module else anchor
            else:
                base = node.module or ""
            names.add(base)
            names.update(f"{base}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Call) and node.args:
            func = node.func
            called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            first = node.args[0]
            if called in {"import_module", "__import__"} and isinstance(first, ast.Constant):
                if isinstance(first.value, str):
                    names.add(first.value)
    return names


def closure(root: str) -> set[str]:
    """Modules reachable from ``root`` inside ``opentour_cell``, including parent packages."""
    seen: set[str] = set()
    todo = [root]
    while todo:
        name = todo.pop()
        parts = name.split(".")
        for i in range(1, len(parts) + 1):
            prefix = ".".join(parts[:i])
            if prefix in seen or not prefix.startswith("opentour_cell"):
                continue
            seen.add(prefix)
            path = module_file(prefix)
            if path is not None:
                todo.extend(
                    imported_names(path.read_text(), prefix, is_package=path.name == "__init__.py")
                )
    return seen


def defines_scoring_class(path: Path) -> list[str]:
    """Classes that implement ``score`` (protocol declarations excluded)."""
    found = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ClassDef):
            bases = {getattr(b, "id", getattr(b, "attr", None)) for b in node.bases}
            methods = {n.name for n in node.body if isinstance(n, ast.FunctionDef)}
            if "score" in methods and "Protocol" not in bases:
                found.append(node.name)
    return found


def test_b1_replay_worker_has_no_import_path_to_the_scorer_implementation() -> None:
    scorer_file, replay_file = module_file(SCORER), module_file(REPLAY)
    assert scorer_file is not None and replay_file is not None
    assert "DeterministicScorer" in defines_scoring_class(scorer_file)
    assert defines_scoring_class(replay_file) == []
    reachable = closure(REPLAY)
    assert REPLAY in reachable and "opentour_cell.pilot.eve_task" in reachable  # not vacuous
    assert SCORER not in reachable


def test_b2_replay_worker_has_no_import_path_to_the_aggregator() -> None:
    assert module_file(AGGREGATOR) is not None
    assert AGGREGATOR not in closure(REPLAY)


def test_b3_aggregator_has_no_import_path_to_the_scorer() -> None:
    assert SCORER not in closure(AGGREGATOR)


def test_b4_importing_the_replay_module_loads_neither_at_runtime() -> None:
    probe = (
        "import sys, importlib\n"
        "importlib.import_module(sys.argv[1])\n"
        "print('\\n'.join(sorted(m for m in sys.modules if m.startswith('opentour_cell'))))\n"
    )
    for root, forbidden in ((REPLAY, {SCORER, AGGREGATOR}), (AGGREGATOR, {SCORER})):
        loaded = subprocess.run(
            [sys.executable, "-c", probe, root],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        ).stdout.split()
        assert root in loaded
        assert not forbidden & set(loaded), (root, forbidden & set(loaded))


@pytest.mark.parametrize(
    "source",
    [
        pytest.param("from opentour_cell.pilot import eve_scorer\n", id="from-package"),
        pytest.param("from opentour_cell.pilot.eve_scorer import X\n", id="from-module"),
        pytest.param("import opentour_cell.pilot.eve_aggregate as a\n", id="import-as"),
        pytest.param("def f():\n    from . import eve_scorer\n", id="relative-in-function"),
        pytest.param("from .eve_aggregate import reconcile\n", id="relative-module"),
        pytest.param(
            "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n"
            "    from opentour_cell.pilot.eve_scorer import X\n",
            id="type-checking",
        ),
        pytest.param(
            "import importlib\nimportlib.import_module('opentour_cell.pilot.eve_aggregate')\n",
            id="importlib",
        ),
        pytest.param("__import__('opentour_cell.pilot.eve_scorer')\n", id="dunder-import"),
    ],
)
def test_b5_the_scanner_flags_every_forbidden_form(source: str) -> None:
    names = imported_names(source, "opentour_cell.pilot.generated_probe")
    assert {SCORER, AGGREGATOR} & names, names
