"""Placeholder. Stage `assemble` (design_agent3.md §5, §8) overwrites this file
per run, in plan order — the one place any unit's code is imported. `unit_slug`
starts with a digit (e.g. `01_01_cart`), so it can't be a literal import target;
use `importlib.import_module`, not `from . import <slug>`:

    import importlib
    for _slug in [...]:  # plan order
        importlib.import_module(f"{__name__}.{_slug}")

Importing a unit here runs its `@events.on(...)` hook registrations; `app.main`
then walks these submodules, in the same order, for a `router` attribute.

A per-unit sandbox workspace (stage `units`, §4) needs its own version of this
file too, importing only that unit and its (transitive) dependencies.

The template itself has no units, so this stays empty.
"""
