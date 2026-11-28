"""OPE-573 import boundary: the replay worker reaches no scorer, aggregator, evaluator or gate.

The separate evaluator (``eve_evaluator``) is the independent scorer: it may read the replay
module's types, but never the worker's scorer, the aggregator, the staging gate or the worker
entry. The staging gate (``eve_staging``) combines the rebuild and the aggregator; nothing the
replay worker or the aggregator imports reaches it.

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
EVALUATOR = "opentour_cell.pilot.eve_evaluator"
STAGING = "opentour_cell.pilot.eve_staging"
WORKER = "opentour_cell.pilot.eve_worker"
BACKEND = "opentour_cell.pilot.eve_reference_backend"


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


def test_b3b_replay_worker_and_aggregator_reach_no_evaluator_gate_or_worker_entry() -> None:
    for module in (EVALUATOR, STAGING, WORKER, BACKEND):
        assert module_file(module) is not None, module
    for root in (REPLAY, AGGREGATOR):
        reachable = closure(root)
        assert not {EVALUATOR, STAGING, WORKER, BACKEND} & reachable, root


def test_b3c_the_independent_scorer_shares_no_code_with_the_worker_scorer_or_aggregator() -> None:
    reachable = closure(EVALUATOR)
    assert REPLAY in reachable  # not vacuous: it reads the replay module's types and verifier
    assert not {SCORER, AGGREGATOR, STAGING, WORKER} & reachable
    evaluator_file = module_file(EVALUATOR)
    assert evaluator_file is not None
    source = evaluator_file.read_text()
    assert "class IndependentGenerativeScorer" in source and "def judge(" in source
    assert "verify_output(" in source  # the production caller of the commitment check


def test_b3e_the_worker_entry_never_reaches_the_evaluator() -> None:
    """The worker runs in its own process and consumes only the exported verdict document."""
    reachable = closure(WORKER)
    assert {REPLAY, STAGING, SCORER} <= reachable  # not vacuous: it is the worker's wiring
    assert EVALUATOR not in reachable
    probe = (
        "import sys, importlib\n"
        "importlib.import_module(sys.argv[1])\n"
        "print('\\n'.join(sorted(m for m in sys.modules if m.startswith('opentour_cell'))))\n"
    )
    loaded = subprocess.run(
        [sys.executable, "-c", probe, WORKER],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    ).stdout.split()
    assert WORKER in loaded and EVALUATOR not in loaded


def test_b3d_the_staging_gate_is_the_one_place_rebuild_and_reconcile_meet() -> None:
    reachable = closure(STAGING)
    assert {REPLAY, AGGREGATOR} <= reachable
    assert not {SCORER, EVALUATOR, WORKER} & reachable


def test_b4_importing_the_replay_module_loads_neither_at_runtime() -> None:
    probe = (
        "import sys, importlib\n"
        "importlib.import_module(sys.argv[1])\n"
        "print('\\n'.join(sorted(m for m in sys.modules if m.startswith('opentour_cell'))))\n"
    )
    for root, forbidden in (
        (REPLAY, {SCORER, AGGREGATOR, EVALUATOR, STAGING, WORKER, BACKEND}),
        (AGGREGATOR, {SCORER, EVALUATOR, STAGING, WORKER}),
        (EVALUATOR, {SCORER, AGGREGATOR, STAGING, WORKER}),
    ):
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


# Dynamic imports cannot be followed statically, so none is allowed anywhere in the closure.
# Besides the import machinery this denies the reflection routes the PR #210 review listed
# (``globals()[...]``, ``__loader__``, ``__spec__``, ``operator.methodcaller``,
# ``inspect.currentframe().f_builtins``, ``zipimport``) and their close relatives: frame and
# function globals, ``object.__subclasses__``, loader methods, and the modules that execute or
# load code from bytes (``marshal``, ``pickle``, ``code``, ``codeop``, ``ctypes``).
_DYNAMIC_MODULES = frozenset(
    {
        "importlib",
        "runpy",
        "builtins",
        "pkgutil",
        "inspect",
        "zipimport",
        "operator",
        "marshal",
        "pickle",
        "code",
        "codeop",
        "ctypes",
    }
)
_DYNAMIC_ATTRS = frozenset(
    {
        "__import__",
        "import_module",
        "find_spec",
        "run_module",
        "run_path",
        "resolve_name",
        "__loader__",
        "__spec__",
        "methodcaller",
        "currentframe",
        "_getframe",
        "f_builtins",
        "f_globals",
        "f_back",
        "__globals__",
        "__subclasses__",
        "load_module",
        "exec_module",
    }
)
# Bare names: the builtins that import or run code from a string, the builtins namespace and the
# builtins that hand out a namespace.
_DYNAMIC_NAMES = _DYNAMIC_ATTRS | {
    "exec",
    "eval",
    "compile",
    "__builtins__",
    "globals",
    "locals",
    "vars",
}


def dynamic_import_uses(source: str) -> list[str]:
    """Every use of a dynamic import facility: importlib, runpy, pkgutil, builtins, __import__,
    exec, eval, compile or sys.modules. ``re.compile`` and the like are attributes, not flagged."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found += [a.name for a in node.names if a.name.split(".")[0] in _DYNAMIC_MODULES]
        elif isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".")[0]
            if root in _DYNAMIC_MODULES or (
                root == "sys" and any(a.name == "modules" for a in node.names)
            ):
                found.append(node.module)
        elif isinstance(node, ast.Name) and node.id in _DYNAMIC_NAMES:
            found.append(node.id)
        elif isinstance(node, ast.Attribute):
            if node.attr in _DYNAMIC_ATTRS or (
                node.attr == "modules" and getattr(node.value, "id", None) == "sys"
            ):
                found.append(node.attr)
        elif isinstance(node, ast.Constant) and node.value in _DYNAMIC_NAMES:
            found.append(str(node.value))  # getattr(module, "import_module") and the like
    return found


def test_b6_nothing_reachable_from_the_replay_worker_or_aggregator_imports_dynamically() -> None:
    # The evaluator and the gate too: their "never reaches the worker scorer" guarantees above
    # are only as complete as the static scan of their closures.
    for root in (REPLAY, AGGREGATOR, EVALUATOR, STAGING):
        reachable = closure(root)
        assert REPLAY in reachable  # the aggregator reads the replay module's constants
        for name in sorted(reachable):
            path = module_file(name)
            if path is not None:
                assert dynamic_import_uses(path.read_text()) == [], (root, name)


@pytest.mark.parametrize(
    "source",
    [
        pytest.param("from importlib import import_module as im\nim('x')\n", id="alias"),
        pytest.param(
            "import importlib\nimportlib.import_module('.eve_scorer', 'opentour_cell.pilot')\n",
            id="relative-import-module",
        ),
        pytest.param("import importlib\nimportlib.import_module(name='x')\n", id="name-keyword"),
        pytest.param(
            "import importlib\nimportlib.import_module('opentour_cell.pilot.' + 'eve_scorer')\n",
            id="string-built",
        ),
        pytest.param("m = __builtins__\ngetattr(m, '__import__')('x')\n", id="getattr-dunder"),
        pytest.param("import runpy\nrunpy.run_module('x')\n", id="runpy"),
        pytest.param("from importlib.util import find_spec\nfind_spec('x')\n", id="find-spec"),
        pytest.param("import sys\nsys.modules['opentour_cell.pilot.x']\n", id="sys-modules"),
        pytest.param("from sys import modules\nmodules['x']\n", id="from-sys-modules"),
        pytest.param("def f():\n    __import__('x')\n", id="lazy-dunder"),
        pytest.param("exec('import opentour_cell.pilot.eve_scorer')\n", id="exec"),
        pytest.param("eval('1')\n", id="eval"),
        pytest.param("code = compile('import x', 'f', 'exec')\n", id="compile"),
        pytest.param("import builtins\nbuiltins.__dict__['imp' + 'ort']\n", id="builtins-dict"),
        pytest.param("from builtins import __dict__ as d\n", id="from-builtins"),
        pytest.param("__builtins__['x']\n", id="dunder-builtins"),
        pytest.param("import pkgutil\npkgutil.resolve_name('x:y')\n", id="pkgutil"),
        pytest.param("from pkgutil import resolve_name\n", id="from-pkgutil"),
        pytest.param("f = getattr(__builtins__, 'ev' + 'al')\n", id="getattr-built-name"),
        pytest.param("globals()['__builtins__']\n", id="globals"),
        pytest.param("vars()['x']\n", id="vars"),
        pytest.param("locals()['x']\n", id="locals"),
        pytest.param("__loader__.load_module('x')\n", id="loader"),
        pytest.param("__spec__.loader\n", id="spec"),
        pytest.param("import operator\noperator.methodcaller('x', 'y')(m)\n", id="methodcaller"),
        pytest.param("from operator import methodcaller\n", id="from-operator"),
        pytest.param("import inspect\ninspect.currentframe().f_builtins\n", id="currentframe"),
        pytest.param("import sys\nsys._getframe(0).f_globals\n", id="getframe"),
        pytest.param("def f():\n    pass\nf.__globals__['__builtins__']\n", id="func-globals"),
        pytest.param("().__class__.__base__.__subclasses__()\n", id="subclasses"),
        pytest.param("import zipimport\nzipimport.zipimporter('x')\n", id="zipimport"),
        pytest.param("import marshal\nmarshal.loads(b'')\n", id="marshal"),
        pytest.param("import pickle\npickle.loads(b'')\n", id="pickle"),
        pytest.param("import code\ncode.interact()\n", id="code"),
        pytest.param("import ctypes\nctypes.pythonapi\n", id="ctypes"),
        pytest.param("spec.loader.exec_module(m)\n", id="exec-module"),
        pytest.param("f = getattr(sys, '_getframe')\n", id="getattr-getframe-name"),
    ],
)
def test_b7_the_dynamic_import_scanner_flags_every_form(source: str) -> None:
    assert dynamic_import_uses(source), source
