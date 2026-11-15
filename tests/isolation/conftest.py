"""Reusable generated scope inputs; no automatic real-environment qualification."""

import pytest

from tools.fixtures.registry import MODES, generated_scope


@pytest.fixture
def four_generated_cell_scopes():
    return tuple(generated_scope(mode, tenant) for mode in MODES for tenant in (1, 2))
