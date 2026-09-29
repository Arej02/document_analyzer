"""The real wp_compat backend — design_agent3.md §8. Options/meta/transients are
SQLAlchemy tables via `app.db`; `wp_remote_*` is a real `httpx` call. Not used
by any test by default (`tests/conftest.py` installs `FakeWPCompatBackend`
instead, specs FR5) — a test that wants this backend calls
`wp_compat.set_backend(RealWPCompatBackend())` itself.
"""

import time

import httpx
from sqlalchemy import Column, String, Text

from ..db import Base, SessionLocal
from .backend import WPCompatBackend


class OptionRow(Base):
    __tablename__ = "wp_compat_options"

    key = Column(String, primary_key=True)
    value = Column(Text, nullable=True)


class MetaRow(Base):
    __tablename__ = "wp_compat_meta"

    object_type = Column(String, primary_key=True)
    object_id = Column(String, primary_key=True)
    meta_key = Column(String, primary_key=True)
    meta_value = Column(Text, nullable=True)


class TransientRow(Base):
    __tablename__ = "wp_compat_transients"

    key = Column(String, primary_key=True)
    value = Column(Text, nullable=True)
    expires_at = Column(Text, nullable=True)  # unix timestamp as text; None = never expires


class RealWPCompatBackend(WPCompatBackend):
    def get_option(self, key, default=None):
        with SessionLocal() as session:
            row = session.get(OptionRow, key)
            return row.value if row is not None else default

    def update_option(self, key, value):
        with SessionLocal() as session:
            row = session.get(OptionRow, key)
            if row is None:
                session.add(OptionRow(key=key, value=value))
            else:
                row.value = value
            session.commit()
            return True

    def delete_option(self, key):
        with SessionLocal() as session:
            row = session.get(OptionRow, key)
            if row is not None:
                session.delete(row)
                session.commit()
            return True

    def get_post_meta(self, post_id, key, single=True):
        return self._get_meta("post", post_id, key, single)

    def update_post_meta(self, post_id, key, value):
        return self._set_meta("post", post_id, key, value)

    def get_user_meta(self, user_id, key, single=True):
        return self._get_meta("user", user_id, key, single)

    def update_user_meta(self, user_id, key, value):
        return self._set_meta("user", user_id, key, value)

    def _get_meta(self, object_type: str, object_id, key: str, single: bool):
        with SessionLocal() as session:
            row = session.get(MetaRow, (object_type, str(object_id), key))
            value = row.meta_value if row is not None else None
            return value if single else ([value] if value is not None else [])

    def _set_meta(self, object_type: str, object_id, key: str, value):
        with SessionLocal() as session:
            pk = (object_type, str(object_id), key)
            row = session.get(MetaRow, pk)
            if row is None:
                session.add(MetaRow(object_type=object_type, object_id=str(object_id), meta_key=key, meta_value=value))
            else:
                row.meta_value = value
            session.commit()
            return True

    def get_transient(self, key):
        with SessionLocal() as session:
            row = session.get(TransientRow, key)
            if row is None:
                return None
            if row.expires_at is not None and float(row.expires_at) < time.time():
                session.delete(row)
                session.commit()
                return None
            return row.value

    def set_transient(self, key, value, expire_seconds=0):
        expires_at = str(time.time() + expire_seconds) if expire_seconds else None
        with SessionLocal() as session:
            row = session.get(TransientRow, key)
            if row is None:
                session.add(TransientRow(key=key, value=value, expires_at=expires_at))
            else:
                row.value = value
                row.expires_at = expires_at
            session.commit()
            return True

    def delete_transient(self, key):
        with SessionLocal() as session:
            row = session.get(TransientRow, key)
            if row is not None:
                session.delete(row)
                session.commit()
            return True

    def wp_remote_get(self, url, **kwargs):
        try:
            response = httpx.get(url, **kwargs)
            return {"response": {"code": response.status_code}, "body": response.text}
        except httpx.HTTPError as exc:
            return {"error": str(exc)}

    def wp_remote_post(self, url, **kwargs):
        try:
            response = httpx.post(url, **kwargs)
            return {"response": {"code": response.status_code}, "body": response.text}
        except httpx.HTTPError as exc:
            return {"error": str(exc)}
