"""SQLAlchemy engine/session — design_agent3.md §8, the `$wpdb` replacement.
Fixed template code: never touched by generated units, which import `app.db`
and use a session, never build raw SQL strings from input.

Defaults to an in-memory SQLite database on a `StaticPool` (one shared
connection), so the assembled app works unmodified inside the sandbox, whose
root filesystem is read-only and only `/tmp` is writable (design_agent3.md
§7) — nothing here needs a config file or a writable path. `DATABASE_URL`
overrides it for a real deployment.
"""

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import StaticPool

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite+pysqlite:///:memory:")

_engine_kwargs: dict = {}
if DATABASE_URL.startswith("sqlite"):
    _engine_kwargs["connect_args"] = {"check_same_thread": False}
    if DATABASE_URL.endswith(":memory:"):
        # A plain in-memory SQLite DB is per-connection; StaticPool keeps every
        # session on the same connection so they all see the same data.
        _engine_kwargs["poolclass"] = StaticPool

engine = create_engine(DATABASE_URL, **_engine_kwargs)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    """FastAPI dependency: `db: Session = Depends(get_db)`."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
