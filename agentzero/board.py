"""
Productivity board — structured read access to tasks for the dashboard API.

Since the 2026-07-14 merge a "reminder" is a task with a remind_at; the /api/reminders
surface is kept as a VIEW over timed tasks (same JSON shape as before, statuses derived
back to the old pending/awaiting_ack lifecycle) so a deployed Cockpit keeps working.
Read-only; the API just serializes these. Tasks/projects are global (single-user app).
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from agentzero.config import TIMEZONE
from agentzero.db import get_db
from agentzero.task_tree import build_forest


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


async def hierarchy_maps() -> tuple[dict, dict]:
    """(by_id, children_by_parent) over ALL tasks — the lookups serialize_task needs to
    annotate goal/step relations regardless of how the caller filtered its own list."""
    db = get_db()
    all_tasks = await db.tasks.find({}).to_list(None)
    by_id = {t["_id"]: t for t in all_tasks}
    children: dict = {}
    for t in all_tasks:
        p = t.get("parent_task_id")
        if p is not None and p in by_id:
            children.setdefault(p, []).append(t)
    return by_id, children


def serialize_task(
    task: dict, project: dict | None,
    by_id: dict | None = None, children: dict | None = None,
) -> dict:
    """Serialize one task. Pass the `hierarchy_maps()` lookups to fill the goal/step fields
    (parent_title, is_goal, steps_done/steps_total); without them those degrade gracefully
    (parent id still present, counts 0)."""
    due = _aware(task.get("due_date"))
    today = datetime.now(ZoneInfo(TIMEZONE)).date()
    is_overdue = bool(
        task.get("status") == "open" and due and due.astimezone(ZoneInfo(TIMEZONE)).date() < today
    )
    parent_id = task.get("parent_task_id")
    parent = (by_id or {}).get(parent_id) if parent_id else None
    steps = (children or {}).get(task.get("_id"), [])
    return {
        "id": str(task.get("_id")),
        "title": task.get("title"),
        "project": (project or {}).get("name"),
        "scope": (project or {}).get("scope"),
        "status": task.get("status"),
        # Goal/step hierarchy: parent_task_id None → standalone (or a goal, if steps_total>0);
        # set → this task is a STEP under that goal.
        "parent_task_id": str(parent_id) if parent_id else None,
        "parent_title": parent.get("title") if parent else None,
        "is_goal": len(steps) > 0,
        "steps_done": sum(1 for s in steps if s.get("status") == "done"),
        "steps_total": len(steps),
        "due_date": _iso(task.get("due_date")),
        "snoozed_until": _iso(task.get("snoozed_until")),
        "is_overdue": is_overdue,
        # Timed ping (None for a plain task); reminded_at set → fired, awaiting confirmation.
        "remind_at": _iso(task.get("remind_at")),
        "reminded_at": _iso(task.get("reminded_at")),
        "created_at": _iso(task.get("created_at")),
        "updated_at": _iso(task.get("updated_at")),
    }


async def task_tree_view(scope: str | None = None) -> list[dict]:
    """Nested goal→steps view of the board: top-level nodes (goals + standalone tasks) each
    carrying their serialized steps. Includes ALL statuses so (steps_done/steps_total) is
    truthful — status filtering is the flat `tasks` list's job, not the tree's."""
    items = await query_tasks(None, scope)
    tasks = [t for t, _ in items]
    proj_of = {t["_id"]: p for t, p in items}
    by_id, children = await hierarchy_maps()
    out = []
    for node in build_forest(tasks):
        t = node["task"]
        ser = serialize_task(t, proj_of.get(t["_id"]), by_id, children)
        ser["steps"] = [
            serialize_task(s, proj_of.get(s["_id"]), by_id, children) for s in node["steps"]
        ]
        out.append(ser)
    return out


def task_status_counts(tasks: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for t in tasks:
        s = t.get("status", "open")
        counts[s] = counts.get(s, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Reminders — a compatibility VIEW over tasks that carry a remind_at
# ---------------------------------------------------------------------------

def _reminder_status(t: dict) -> str:
    """A timed task's status translated back to the old reminder lifecycle, so the
    /api/reminders payload keeps its pre-merge vocabulary for deployed consumers."""
    if t.get("status") == "open":
        return "awaiting_ack" if t.get("reminded_at") else "pending"
    return t.get("status") or "pending"  # done | cancelled | snoozed


async def query_reminders(chat_id: int, status: str | None = None) -> list[dict]:
    """Timed tasks, soonest ping first. `status` accepts the old reminder vocabulary
    (pending/awaiting_ack/done/cancelled) or the 'active' pseudo-status. chat_id is kept
    for signature compatibility (tasks are global — single-user app)."""
    db = get_db()
    rows = [
        t for t in await db.tasks.find({}).to_list(None)
        if t.get("remind_at") is not None
    ]
    if status == "active":
        rows = [t for t in rows if t.get("status") == "open"]
    elif status:
        rows = [t for t in rows if _reminder_status(t) == status]
    rows.sort(key=lambda t: _aware(t.get("remind_at")) or datetime.min.replace(tzinfo=timezone.utc))
    return rows


def serialize_reminder(t: dict) -> dict:
    """Same JSON shape as before the merge (text/fire_at/fired_at…), sourced from a task."""
    status = _reminder_status(t)
    return {
        "id": str(t.get("_id")),
        "text": t.get("title"),
        "status": status,
        "awaiting_ack": status == "awaiting_ack",
        "is_active": t.get("status") == "open",
        "fire_at": _iso(t.get("remind_at")),
        "fired_at": _iso(t.get("reminded_at")),
        "next_nudge_at": _iso(t.get("next_nudge_at")),
        "completed_at": _iso(t.get("completed_at")),
        "created_at": _iso(t.get("created_at")),
        "nudge_count": t.get("nudge_count") or 0,
    }


def reminder_status_counts(rows: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for t in rows:
        s = _reminder_status(t)
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
# Daily focus (today's committed 3-4 task slate — see focus.py)
# ---------------------------------------------------------------------------

async def focus_view(chat_id: int) -> dict | None:
    """Today's focus slate for the dashboard. None when no slate exists yet today
    (it's committed at the morning digest) — consumers must tolerate that, and older
    deployed APIs won't have this field at all."""
    from agentzero.focus import get_today_focus  # lazy: focus pulls in the LLM layer

    db = get_db()
    doc = await get_today_focus(chat_id)
    if not doc:
        return None
    by_id, children = await hierarchy_maps()
    projects = {p["_id"]: p for p in await db.projects.find({}).to_list(None)}
    carry = set(doc.get("carryover_ids", []))

    def _ser(tid) -> dict | None:
        t = by_id.get(tid)
        if not t:
            return None
        s = serialize_task(t, projects.get(t.get("project_id")), by_id, children)
        s["carried_over"] = tid in carry
        return s

    items = [s for s in (_ser(tid) for tid in doc.get("task_ids", [])) if s]
    # Overflow: overdue/due-today tasks that didn't make the slate — only while still open.
    overflow = [
        s for s in (_ser(tid) for tid in doc.get("overflow_ids", []))
        if s and s["status"] == "open"
    ]
    return {
        "date": doc.get("date"),
        "done": sum(1 for s in items if s["status"] == "done"),
        "total": len(items),
        "items": items,
        "overflow": overflow,
    }


# ---------------------------------------------------------------------------
# Overview (KPI rollup)
# ---------------------------------------------------------------------------

async def overview(chat_id: int) -> dict:
    db = get_db()
    task_items = await query_tasks()
    reminders = await query_reminders(chat_id)
    projects = await db.projects.count_documents({})
    # Goal rollup: how many goals exist and their aggregate step progress.
    forest = build_forest([t for t, _ in task_items])
    goal_nodes = [n for n in forest if n["total"] > 0]
    return {
        "tasks": task_status_counts([t for t, _ in task_items]),
        "reminders": reminder_status_counts(reminders),
        "reminders_active": sum(1 for r in reminders if r.get("status") == "open"),
        "goals": {
            "count": len(goal_nodes),
            "steps_done": sum(n["done"] for n in goal_nodes),
            "steps_total": sum(n["total"] for n in goal_nodes),
        },
        # Today's committed slate; null until the morning selection has run.
        "focus": await focus_view(chat_id),
        "projects": projects,
    }
