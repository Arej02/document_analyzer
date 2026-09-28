"""FastAPI app entrypoint — design_agent3.md §8. Fixed template code: never
touched by generated units.

`app.units` (written by stage `assemble`, in plan order) is the one place any
unit's code gets imported; importing it here runs every unit's hook
registrations. This module then walks `app.units`'s own submodules, in that
same sorted order, and includes each one's `router` attribute if it has one —
there is no separate discovery pass and no second way to reach a unit's code.
"""

import pkgutil
from importlib import import_module

from fastapi import FastAPI

from . import units

app = FastAPI()


def _include_unit_routers(app: FastAPI) -> None:
    for module_info in sorted(pkgutil.iter_modules(units.__path__), key=lambda m: m.name):
        module = import_module(f"{units.__name__}.{module_info.name}")
        router = getattr(module, "router", None)
        if router is not None:
            app.include_router(router)


_include_unit_routers(app)
