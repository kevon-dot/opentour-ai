"""C03 owns only generated local fixture lifecycle, never an existing database."""

import pytest

from tools.fixtures.postgres import DisposablePostgres
from tools.fixtures.postgres_checks import setup_schema


@pytest.fixture(scope="session")
def disposable_postgres(pytestconfig):
    database = DisposablePostgres()
    try:
        with database:
            setup_schema(database)
            yield database
    finally:
        pytestconfig._opentour_cleanup_complete = database.cleanup_complete
        # Recovery paths/PIDs stay private. Public evidence retains the failure facts.
        pytestconfig._opentour_observations.append(
            {
                key: value
                for key, value in database.receipt.items()
                if key
                not in {"retained_private_directory", "owned_postmaster_pid", "owned_helper_pid"}
            }
        )


@pytest.fixture
def record_harness_observation(pytestconfig):
    return pytestconfig._opentour_observations.append
