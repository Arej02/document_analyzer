"""Hook dispatcher — design_agent3.md §8, the target of `add_action`/`add_filter`,
`do_action` and `apply_filters` in generated code. Fixed template code: never
touched by generated units.

Ordering matches WordPress: handlers run in ascending priority order, and two
handlers registered at the same priority run in the order they were registered
(a stable sort on registration index, never on name or anything else).
"""

import itertools
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field

_next_sequence = itertools.count()


@dataclass(order=True)
class _Registration:
    priority: int
    sequence: int
    handler: Callable = field(compare=False)


class EventDispatcher:
    def __init__(self) -> None:
        self._handlers: dict[str, list[_Registration]] = defaultdict(list)

    def on(self, name: str, priority: int = 10):
        """`@events.on("hook_name", priority=n)` — registers `fn` for `name` and
        returns it unchanged, so it still works as a plain function too."""

        def decorator(fn: Callable) -> Callable:
            self._handlers[name].append(_Registration(priority, next(_next_sequence), fn))
            self._handlers[name].sort()
            return fn

        return decorator

    def handlers_for(self, name: str) -> list[Callable]:
        """Design §6 `hooks_bound` contract test: non-empty iff something registered
        for `name`."""
        return [reg.handler for reg in self._handlers.get(name, [])]

    def fire(self, name: str, *args, **kwargs) -> None:
        """`do_action` — calls every handler for `name` in order, ignoring return values."""
        for handler in self.handlers_for(name):
            handler(*args, **kwargs)

    def apply_filters(self, name: str, value, *args, **kwargs):
        """`apply_filters` — threads `value` through every handler for `name` in order,
        each handler's return value becoming the next handler's input."""
        for handler in self.handlers_for(name):
            value = handler(value, *args, **kwargs)
        return value

    def reset(self) -> None:
        """Test-only: clears every registration (`tests/conftest.py`, autouse)."""
        self._handlers.clear()


dispatcher = EventDispatcher()
on = dispatcher.on
fire = dispatcher.fire
apply_filters = dispatcher.apply_filters
handlers_for = dispatcher.handlers_for
reset = dispatcher.reset
