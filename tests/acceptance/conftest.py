"""The fixture boundary refuses fake evidence; real collectors remain separately owned."""

import pytest

from tools.fixtures.evidence import route_empirical


@pytest.fixture
def empirical_receipt_boundary():
    return route_empirical
