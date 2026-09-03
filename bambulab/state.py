from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
import asyncio


@dataclass(slots=True)
class PendingSpool:
    """Tracks one pending spool assignment request and its timeout task."""

    spool_id: int
    filament_data: dict[str, Any]
    slot_index: str | None = None
    started_at: datetime = field(default_factory=datetime.utcnow)
    timer: asyncio.Task | None = None
