"""Engine, session factory, schema init. All SQL for the index lives behind
this module + index_query.py + indexer/full_index.py — nothing else touches
the DB directly."""

from __future__ import annotations

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from src.config import get_settings
from src.models import Base

_engine = None
_SessionLocal = None


def get_engine(database_url: str | None = None):
    global _engine, _SessionLocal
    if _engine is None or database_url is not None:
        url = database_url or get_settings().database_url
        if not url:
            raise RuntimeError("DATABASE_URL is not set")
        _engine = create_engine(url, future=True)
        _SessionLocal = sessionmaker(bind=_engine, future=True)
    return _engine


def get_session(database_url: str | None = None) -> Session:
    get_engine(database_url)
    return _SessionLocal()


def init_schema(database_url: str | None = None) -> None:
    """create_all()-style init. Also ensures pg_trgm, which the trigram
    index depends on. No alembic yet — re-run is idempotent."""
    engine = get_engine(database_url)
    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
    Base.metadata.create_all(engine)
