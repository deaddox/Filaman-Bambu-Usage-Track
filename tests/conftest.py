"""Bootstrap: stub all app.* deps then load driver module directly.

This conftest runs before any test module is imported.  It:
  1. Injects minimal stub modules into sys.modules for every app.* name the
     driver uses at import time.
  2. Loads bambulab/driver.py via importlib (bypassing bambulab/__init__.py)
     and registers it under the two names the package __init__ references, so
     subsequent package imports are satisfied from the cache.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path


def _make_module(name: str) -> types.ModuleType:
    mod = types.ModuleType(name)
    sys.modules[name] = mod
    return mod


# ---------------------------------------------------------------------------
# sqlalchemy stubs
# ---------------------------------------------------------------------------
_sa = _make_module("sqlalchemy")
_sa.func = None
_sa.select = lambda *a, **kw: None

_sa_orm = _make_module("sqlalchemy.orm")
_sa_orm.selectinload = lambda *a, **kw: None

# ---------------------------------------------------------------------------
# app.* stubs
# ---------------------------------------------------------------------------
_make_module("app")
_make_module("app.core")

_cfg = _make_module("app.core.config")
_cfg.settings = types.SimpleNamespace(debug=False)

_db = _make_module("app.core.database")
_db.async_session_maker = None

_make_module("app.models")
_loc = _make_module("app.models.location")
_loc.Location = object
_prn = _make_module("app.models.printer")
_prn.Printer = object
_prn.PrinterSlot = object
_prn.PrinterSlotAssignment = object
_sp = _make_module("app.models.spool")
_sp.Spool = object

_make_module("app.plugins")
_base_mod = _make_module("app.plugins.base")


class _BaseDriver:
    def __init__(self, printer_id, config, emitter):
        self.printer_id = printer_id
        self.config = config
        self.emit = emitter
        self._running = False


_base_mod.BaseDriver = _BaseDriver

_make_module("app.services")
_svc = _make_module("app.services.spool_service")
_svc.SpoolService = object

# ---------------------------------------------------------------------------
# Load the driver module directly and register it under both canonical names
# so that bambulab/__init__.py's `from app.plugins.bambulab.driver import
# Driver` is satisfied from sys.modules cache.
# ---------------------------------------------------------------------------
_driver_path = Path(__file__).parent.parent / "bambulab" / "driver.py"
_spec = importlib.util.spec_from_file_location("bambulab.driver", _driver_path)
_driver_mod = importlib.util.module_from_spec(_spec)
sys.modules["bambulab.driver"] = _driver_mod
_make_module("app.plugins.bambulab")
sys.modules["app.plugins.bambulab.driver"] = _driver_mod
_spec.loader.exec_module(_driver_mod)  # type: ignore[union-attr]
