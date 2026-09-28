"""wp_compat — design_agent3.md §8. Fixed template code: what generated units call
for `wp_*` / PHP builtins with no direct Python equivalent (options, post/user
meta, transients, `wp_remote_*`, escaping, i18n no-ops). If a unit needs
something not here, it adds a private adapter inside its own package instead
of extending this file (`# TODO(wp_compat)`).

The stateful calls (options/meta/transients/http) go through a swappable
backend (`backend.py`): `RealWPCompatBackend` (SQLAlchemy + httpx, `real.py`)
in a real deployment, `FakeWPCompatBackend` (in-memory, records every call,
`fake.py`) in tests. `tests/conftest.py` installs the fake by default and
autouse (specs FR5: tests never touch the network); a test that specifically
wants the real, DB-backed behavior calls `set_backend(RealWPCompatBackend())`
itself. The stateless helpers (escaping, i18n) carry no state, so both
backends share the same implementation.
"""

from .backend import WPCompatBackend
from .escaping import esc_attr, esc_html, esc_url, sanitize_text_field
from .i18n import _e, __, esc_html__
from .real import RealWPCompatBackend

_backend: WPCompatBackend = RealWPCompatBackend()


def set_backend(backend: WPCompatBackend) -> WPCompatBackend:
    """Swaps the active backend and returns the previous one, so a fixture (or a
    test that wants it back) can restore it."""
    global _backend
    previous = _backend
    _backend = backend
    return previous


def get_backend() -> WPCompatBackend:
    return _backend


def get_option(key, default=None):
    return _backend.get_option(key, default)


def update_option(key, value):
    return _backend.update_option(key, value)


def delete_option(key):
    return _backend.delete_option(key)


def get_post_meta(post_id, key, single=True):
    return _backend.get_post_meta(post_id, key, single)


def update_post_meta(post_id, key, value):
    return _backend.update_post_meta(post_id, key, value)


def get_user_meta(user_id, key, single=True):
    return _backend.get_user_meta(user_id, key, single)


def update_user_meta(user_id, key, value):
    return _backend.update_user_meta(user_id, key, value)


def get_transient(key):
    return _backend.get_transient(key)


def set_transient(key, value, expire_seconds=0):
    return _backend.set_transient(key, value, expire_seconds)


def delete_transient(key):
    return _backend.delete_transient(key)


def wp_remote_get(url, **kwargs):
    return _backend.wp_remote_get(url, **kwargs)


def wp_remote_post(url, **kwargs):
    return _backend.wp_remote_post(url, **kwargs)


__all__ = [
    "esc_attr",
    "esc_html",
    "esc_url",
    "sanitize_text_field",
    "__",
    "_e",
    "esc_html__",
    "WPCompatBackend",
    "RealWPCompatBackend",
    "set_backend",
    "get_backend",
    "get_option",
    "update_option",
    "delete_option",
    "get_post_meta",
    "update_post_meta",
    "get_user_meta",
    "update_user_meta",
    "get_transient",
    "set_transient",
    "delete_transient",
    "wp_remote_get",
    "wp_remote_post",
]
