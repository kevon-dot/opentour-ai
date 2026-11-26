"""Make the optional training-to-cell interface importable in the isolated numeric test project."""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(REPO / "services/cell-runtime/src"), str(REPO / "training/profiles")]
