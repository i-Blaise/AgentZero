"""
Collectors interface — Phase 4.

A collector pulls task updates from an external source and returns a list of
TaskUpdate objects that the executor can apply.  Scope is deterministic config
(never LLM-inferred).  Multiple instances of the same collector type can run
with different configurations (e.g. two Jira instances with different scopes).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass
class TaskUpdate:
    """A task mutation proposed by a collector."""
    source: str           # e.g. "jira", "linear", "manual"
    project_name: str
    task_title: str
    scope: str            # "work" | "personal" — from collector config, never inferred
    action: str           # "add" | "mark_done" | "update"
    due_date: str | None = None


@runtime_checkable
class Collector(Protocol):
    scope: str            # deterministic per instance, set at construction

    async def collect(self) -> list[TaskUpdate]: ...
