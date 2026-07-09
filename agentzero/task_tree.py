"""Grouping helpers for the goal → step task hierarchy.

A "goal" is any task that has child tasks (tasks whose ``parent_task_id`` points at it).
A task with no parent and no children is a plain standalone task. The tree is intentionally
just TWO levels deep (goal → steps); the executor flattens anything deeper on write.

These pure helpers turn a flat list of task dicts into a forest so the chat snapshot,
/status, the digest, and the board all render the same tree and the same (done/total)
progress counts — one source of truth, no drift.
"""
from __future__ import annotations

from datetime import datetime

_ACTIVE = ("open", "snoozed")


def _created(t: dict) -> datetime:
    c = t.get("created_at")
    return c if isinstance(c, datetime) else datetime.min


def progress(steps: list[dict]) -> tuple[int, int]:
    """(#done, #total) over a goal's steps. 'total' counts every step; 'done' counts those
    marked done."""
    total = len(steps)
    done = sum(1 for s in steps if s.get("status") == "done")
    return done, total


def build_forest(tasks: list[dict]) -> list[dict]:
    """Group a flat task list (ALL statuses) into top-level nodes, each:

        {"task": <dict>, "steps": [<child dict>, ...], "done": int, "total": int}

    Steps are that task's children in creation order. Standalone tasks get steps=[]. An
    orphan (parent_task_id set but the parent isn't in ``tasks``) is treated as top-level."""
    by_id = {t["_id"]: t for t in tasks}
    children: dict = {}
    tops: list[dict] = []
    for t in tasks:
        p = t.get("parent_task_id")
        if p is not None and p in by_id:
            children.setdefault(p, []).append(t)
        else:
            tops.append(t)

    tops.sort(key=_created)
    forest = []
    for t in tops:
        steps = sorted(children.get(t["_id"], []), key=_created)
        done, total = progress(steps)
        forest.append({"task": t, "steps": steps, "done": done, "total": total})
    return forest


def _due_suffix(t: dict) -> str:
    d = t.get("due_date")
    if not d:
        return ""
    try:
        return f" — due {d.strftime('%Y-%m-%d')}"
    except Exception:
        return ""


def active_forest_lines(tasks: list[dict]) -> list[str]:
    """Plain-text lines for the ACTIVE (open/snoozed) portion of one project's task tree.
    Goals show a (done/total) counter with their open steps indented beneath; standalone
    tasks are a single bullet. Returns [] when nothing is active. Lines are un-prefixed
    (callers add their own left margin)."""
    lines: list[str] = []
    for node in build_forest(tasks):
        t = node["task"]
        open_steps = [s for s in node["steps"] if s.get("status") in _ACTIVE]
        t_active = t.get("status") in _ACTIVE
        if not t_active and not open_steps:
            continue
        if node["total"]:
            lines.append(f"• {t['title']} ({node['done']}/{node['total']}){_due_suffix(t)}")
            for s in open_steps:
                lines.append(f"    - {s['title']}{_due_suffix(s)}")
        else:
            lines.append(f"• {t['title']}{_due_suffix(t)}")
    return lines
