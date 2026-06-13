"""No-op manual collector — placeholder for Phase 4."""
from __future__ import annotations

from agentzero.collectors.base import TaskUpdate


class ManualCollector:
    """Stub collector; returns an empty list until Phase 4 wires it up."""

    def __init__(self, scope: str) -> None:
        self.scope = scope

    async def collect(self) -> list[TaskUpdate]:
        return []
