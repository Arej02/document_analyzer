"""The in-memory wp_compat backend — design_agent3.md §6, §8. Records every call
(`.calls`) so a behaviour test can assert the expected `external_calls`
happened, without a DB session or a real network call. Active by default in
every test (`tests/conftest.py`, autouse) — specs FR5: tests never touch the
network.
"""

from dataclasses import dataclass, field

from .backend import WPCompatBackend


@dataclass
class FakeWPCompatBackend(WPCompatBackend):
    calls: list[tuple[str, tuple, dict]] = field(default_factory=list)
    _options: dict = field(default_factory=dict)
    _meta: dict = field(default_factory=dict)
    _transients: dict = field(default_factory=dict)

    def _record(self, name: str, *args, **kwargs) -> None:
        self.calls.append((name, args, kwargs))

    def get_option(self, key, default=None):
        self._record("get_option", key, default=default)
        return self._options.get(key, default)

    def update_option(self, key, value):
        self._record("update_option", key, value)
        self._options[key] = value
        return True

    def delete_option(self, key):
        self._record("delete_option", key)
        self._options.pop(key, None)
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
        self._record(f"get_{object_type}_meta", object_id, key, single=single)
        value = self._meta.get((object_type, object_id, key))
        return value if single else ([value] if value is not None else [])

    def _set_meta(self, object_type: str, object_id, key: str, value):
        self._record(f"update_{object_type}_meta", object_id, key, value)
        self._meta[(object_type, object_id, key)] = value
        return True

    def get_transient(self, key):
        self._record("get_transient", key)
        return self._transients.get(key)

    def set_transient(self, key, value, expire_seconds=0):
        self._record("set_transient", key, value, expire_seconds=expire_seconds)
        self._transients[key] = value
        return True

    def delete_transient(self, key):
        self._record("delete_transient", key)
        self._transients.pop(key, None)
        return True

    def wp_remote_get(self, url, **kwargs):
        self._record("wp_remote_get", url, **kwargs)
        return {"response": {"code": 200}, "body": ""}

    def wp_remote_post(self, url, **kwargs):
        self._record("wp_remote_post", url, **kwargs)
        return {"response": {"code": 200}, "body": ""}
