# Bambu Lab Printer Plugin
from __future__ import annotations

from typing import Any

__all__ = ["Driver"]


def __getattr__(name: str) -> Any:
    if name == "Driver":
        from .driver import Driver

        return Driver
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")