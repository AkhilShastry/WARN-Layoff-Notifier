"""Per-state scrapers for sources the generic strategies cannot handle.

Importing this package registers every handler via the `@handler("XX")`
decorator from `warn.source`.
"""

from __future__ import annotations

import importlib
import pkgutil

__all__: list[str] = []

for _module in pkgutil.iter_modules(__path__):
    if _module.name.startswith("_"):
        continue
    importlib.import_module(f"{__name__}.{_module.name}")
    __all__.append(_module.name)
