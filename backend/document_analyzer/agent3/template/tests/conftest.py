"""design_agent3.md §8: wp_compat fake backend (autouse), a SQLite session, and
event dispatcher reset. Copied verbatim by stage `assemble`; every generated
unit's `test_contract.py`/`test_behaviour.py` runs under these fixtures.
"""

import pytest

from app import events
from app.db import Base, SessionLocal, engine
from app.wp_compat import get_backend, set_backend
from app.wp_compat.fake import FakeWPCompatBackend


@pytest.fixture(autouse=True)
def _reset_events():
    events.reset()
    yield
    events.reset()


@pytest.fixture(autouse=True)
def wp_compat_fake():
    """specs FR5: tests never touch the network. Every test gets a fresh
    FakeWPCompatBackend by default; a test that specifically wants the real, DB-backed
    behavior calls `wp_compat.set_backend(RealWPCompatBackend())` itself and the
    previous (fake) backend is restored afterward regardless."""
    fake = FakeWPCompatBackend()
    previous = set_backend(fake)
    try:
        yield fake
    finally:
        set_backend(previous)


@pytest.fixture()
def db_session():
    Base.metadata.create_all(engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)


@pytest.fixture()
def real_wp_compat(db_session):
    """Opt-in: swaps in the real, SQLAlchemy-backed wp_compat for a test that needs
    actual persistence (e.g. asserting a value survives across two calls)."""
    from app.wp_compat.real import RealWPCompatBackend

    previous = set_backend(RealWPCompatBackend())
    try:
        yield get_backend()
    finally:
        set_backend(previous)
