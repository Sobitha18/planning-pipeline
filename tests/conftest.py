import os

import pytest


@pytest.fixture(scope="session")
def db_url():
    """Skips all DB-backed tests with a clear message when no test DB is
    configured, so `pytest tests/` stays green without Postgres."""
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set; skipping DB-backed tests")
    os.environ["DATABASE_URL"] = url

    from src.db import get_engine, init_schema
    from src.models import Base

    engine = get_engine(url)
    Base.metadata.drop_all(engine)
    init_schema(url)
    return url
