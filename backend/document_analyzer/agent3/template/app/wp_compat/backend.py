"""The wp_compat backend interface — design_agent3.md §8. `real.py` and `fake.py`
each implement every method the same way; nothing else in the app should care
which one is active.
"""

from abc import ABC, abstractmethod


class WPCompatBackend(ABC):
    @abstractmethod
    def get_option(self, key, default=None): ...

    @abstractmethod
    def update_option(self, key, value): ...

    @abstractmethod
    def delete_option(self, key): ...

    @abstractmethod
    def get_post_meta(self, post_id, key, single=True): ...

    @abstractmethod
    def update_post_meta(self, post_id, key, value): ...

    @abstractmethod
    def get_user_meta(self, user_id, key, single=True): ...

    @abstractmethod
    def update_user_meta(self, user_id, key, value): ...

    @abstractmethod
    def get_transient(self, key): ...

    @abstractmethod
    def set_transient(self, key, value, expire_seconds=0): ...

    @abstractmethod
    def delete_transient(self, key): ...

    @abstractmethod
    def wp_remote_get(self, url, **kwargs): ...

    @abstractmethod
    def wp_remote_post(self, url, **kwargs): ...
