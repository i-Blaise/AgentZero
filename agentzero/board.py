"""
Productivity board — structured read access to tasks + reminders for the dashboard API.

What's on the table (open tasks, active reminders), what's done, and everything in between
(snoozed tasks, fired-but-unconfirmed reminders). Read-only; the API just serializes these.
Tasks/projects are global (single-user app); reminders are scoped by chat_id.
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from agentzero.config import TIMEZONE
from agentzero.db import get_db


def _aware(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _iso(dt) -> str | None:
    dt = _aware(dt)
    return dt.isoformat() if dt else None


_DOW_HUMAN = {"*": "every day", "mon-fri": "every weekday", "sat,sun": "on weekends", "sat-sun": "on weekends"}


def _humanise_dow(dow: str) -> str:
    return _DOW_HUMAN.get((dow or "*").lower().strip(), f"on {dow}")


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------

async def query_tasks(status: str | None = None, scope: str | None = None) -> list[tuple]:
    """Returns [(task, project|None)] filtered by status and/or scope."""
    db = get_db()
    projects = {p["_id"]: p for p in await db.projects.find({}).to_list(None)}
    out = []
    for t in await db.tasks.find({}).to_list(None):
        proj = projects.get(t.get("project_id"))
        if status and t.get("status") != status:
            continue
        if scope and (not proj or proj.get("scope") != scope):
            continue
        out.append((t, proj))
    return out


def serialize_task(task: dict, project: dict | None) -> dict:
    due = _aware(task.get("due_date"))
    today = datetime.now(ZoneInfo(TIMEZONE)).date()
    is_overdue = bool(
        task.get("status") == "open" and due and due.astimezone(ZoneInfo(TIMEZONE)).date() < today
    )
    return {
        "id": str(task.get("_id")),
        "title": task.get("title"),
        "project": (project or {}).get("name"),
        "scope": (project or {}).get("scope"),
        "status": task.get("status"),
        # Goal/step hierarchy: None → standalone/goal, set → a step under that goal id.
        "parent_task_id": str(task["parent_task_id"]) if task.get("parent_task_id") else None,
        "due_date": _iso(task.get("due_date")),
        "snoozed_until": _iso(task.get("snoozed_until")),
        "is_overdue": is_overdue,
        "created_at": _iso(task.get("created_at")),
        "updated_at": _iso(task.get("updated_at")),
    }


def task_status_counts(tasks: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for t in tasks:
        s = t.get("status", "open")
        counts[s] = counts.get(s, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Reminders
# ---------------------------------------------------------------------------

async def query_reminders(chat_id: int, status: str | None = None) -> list[dict]:
    db = get_db()
    q: dict = {"chat_id": chat_id}
    if status:
        q["status"] = status
    rows = await db.reminders.find(q).to_list(None)
    rows.sort(key=lambda r: _aware(r.get("fire_at")) or datetime.min.replace(tzinfo=timezone.utc))
    return rows


def serialize_reminder(r: dict) -> dict:
    return {
        "id": str(r.get("_id")),
        "text": r.get("text"),
        "status": r.get("status"),
        "awaiting_ack": r.get("status") == "awaiting_ack",
        "fire_at": _iso(r.get("fire_at")),
        "fired_at": _iso(r.get("fired_at")),
        "completed_at": _iso(r.get("completed_at")),
        "created_at": _iso(r.get("created_at")),
        "nudge_count": r.get("nudge_count") or 0,
    }


def reminder_status_counts(rows: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for r in rows:
        s = r.get("status", "pending")
        counts[s] = counts.get(s, 0) + 1
    return counts


async def query_recurring(chat_id: int) -> list[dict]:
    db = get_db()
    return await db.recurring_reminders.find({"chat_id": chat_id, "active": True}).to_list(None)


def serialize_recurring(r: dict) -> dict:
    return {
        "id": str(r.get("_id")),
        "text": r.get("text"),
        "hour": r.get("hour"),
        "minute": r.get("minute", 0),
        "day_of_week": r.get("day_of_week", "*"),
        "schedule": f"{_humanise_dow(r.get('day_of_week', '*'))} at {r.get('hour', 0):02d}:{r.get('minute', 0):02d}",
        "active": bool(r.get("active", True)),
    }


# ---------------------------------------------------------------------------
# Overview (KPI rollup)
# ---------------------------------------------------------------------------

async def overview(chat_id: int) -> dict:
    db = get_db()
    task_items = await query_tasks()
    reminders = await query_reminders(chat_id)
    projects = await db.projects.count_documents({})
    return {
        "tasks": task_status_counts([t for t, _ in task_items]),
        "reminders": reminder_status_counts(reminders),
        "projects": projects,
    }
