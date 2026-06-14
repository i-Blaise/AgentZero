"""
Deterministic tool executor.  The LLM never writes to the database directly;
it proposes tool calls and this module validates and applies them.

Every write is logged to the `events` collection so /undo can reverse it.
Fuzzy matching uses difflib SequenceMatcher (threshold 0.4).
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any
from zoneinfo import ZoneInfo

from bson import ObjectId

from agentzero.config import TIMEZONE
from agentzero.db import get_db
from agentzero.llm import ToolCall


def _parse_datetime(s: str) -> datetime | None:
    """Parse an ISO 8601 (or common) datetime string; returns naive local time."""
    try:
        dt = datetime.fromisoformat(s)
        return dt.replace(tzinfo=None) if dt.tzinfo else dt
    except ValueError:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sim(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


_DATE_FORMATS = ["%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%d/%m/%Y", "%m/%d/%Y"]


def _parse_date(s: str) -> datetime | None:
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


async def _fuzzy_tasks(query: str, status: str | None = "open") -> list[dict]:
    db = get_db()
    filt: dict = {}
    if status:
        filt["status"] = status
    tasks = await db.tasks.find(filt).to_list(None)
    scored = sorted(
        ((t, _sim(query, t["title"])) for t in tasks),
        key=lambda x: x[1],
        reverse=True,
    )
    return [t for t, score in scored if score >= 0.4]


async def _fuzzy_project(name: str) -> dict | None:
    db = get_db()
    projects = await db.projects.find({}).to_list(None)
    if not projects:
        return None
    best = max(projects, key=lambda p: _sim(name, p["name"]))
    return best if _sim(name, best["name"]) >= 0.4 else None


async def _log_event(
    chat_id: int,
    operation: str,
    collection: str,
    doc_id: ObjectId,
    prev_state: dict | None,
) -> None:
    db = get_db()
    await db.events.insert_one(
        {
            "chat_id": chat_id,
            "operation": operation,
            "collection": collection,
            "document_id": doc_id,
            "prev_state": prev_state,
            "created_at": datetime.utcnow(),
        }
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def execute_tool(chat_id: int, tc: ToolCall) -> str:
    """Apply one tool call and return a human-readable confirmation string."""
    handlers = {
        "create_project": _create_project,
        "add_task": _add_task,
        "mark_done": _mark_done,
        "get_status": _get_status,
        "update_task": _update_task,
        "snooze": _snooze,
        "set_reminder": _set_reminder,
        "list_reminders": _list_reminders,
        "cancel_reminder": _cancel_reminder,
        "complete_reminder": _complete_reminder,
        "remember": _remember,
        "forget": _forget,
    }
    handler = handlers.get(tc.name)
    if handler is None:
        return f"Unknown tool: {tc.name}"
    return await handler(chat_id, tc.args)


async def undo_last(chat_id: int) -> str:
    """Reverse the most recent event for this chat."""
    db = get_db()
    rows = await db.events.find({"chat_id": chat_id}).sort("_id", -1).limit(1).to_list(1)
    event = rows[0] if rows else None
    if not event:
        return "Nothing to undo."

    coll = db[event["collection"]]
    doc_id = event["document_id"]

    if event["prev_state"] is None:
        # was a create → undo by deleting
        await coll.delete_one({"_id": doc_id})
    else:
        # was an update or delete → restore the full prior document
        # (replace_one with upsert re-inserts it if it had been deleted)
        await coll.replace_one({"_id": doc_id}, event["prev_state"], upsert=True)

    await db.events.delete_one({"_id": event["_id"]})
    return f'Undid {event["operation"]}.'


async def get_status(scope: str = "all", project_name: str | None = None) -> str:
    """Public wrapper used by /status command and get_status tool."""
    return await _get_status(0, {"scope": scope, "project_name": project_name})


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

async def _create_project(chat_id: int, args: dict) -> str:
    db = get_db()
    name = args["name"].strip()
    scope = args["scope"]

    existing = await db.projects.find_one(
        {"name": {"$regex": f"^{re.escape(name)}$", "$options": "i"}}
    )
    if existing:
        return f'Project "{existing["name"]}" already exists ({existing["scope"]}).'

    now = datetime.utcnow()
    result = await db.projects.insert_one(
        {"name": name, "scope": scope, "created_at": now, "updated_at": now}
    )
    await _log_event(chat_id, "create_project", "projects", result.inserted_id, None)
    return f'Created project "{name}" ({scope}).'


async def _add_task(chat_id: int, args: dict) -> str:
    db = get_db()
    project = await _fuzzy_project(args["project_name"])
    if not project:
        return f'Project "{args["project_name"]}" not found. Create it first.'

    title = args["title"].strip()
    due_date: datetime | None = None
    if raw := args.get("due_date"):
        due_date = _parse_date(raw)
        if not due_date:
            return f'Could not parse date "{raw}". Use YYYY-MM-DD.'

    now = datetime.utcnow()
    result = await db.tasks.insert_one(
        {
            "project_id": project["_id"],
            "title": title,
            "status": "open",
            "due_date": due_date,
            "snoozed_until": None,
            "last_nudged_at": None,
            "created_at": now,
            "updated_at": now,
        }
    )
    await _log_event(chat_id, "add_task", "tasks", result.inserted_id, None)
    due_str = f" (due {due_date.strftime('%Y-%m-%d')})" if due_date else ""
    return f'Added "{title}" to {project["name"]}{due_str}.'


async def _mark_done(chat_id: int, args: dict) -> str:
    db = get_db()
    query = args["task_query"]
    matches = await _fuzzy_tasks(query)

    if not matches:
        return f'No open task matching "{query}".'
    if len(matches) > 1:
        listed = "\n".join(f"  {i+1}. {m['title']}" for i, m in enumerate(matches[:5]))
        return f'Found {len(matches)} tasks matching "{query}" — be more specific:\n{listed}'

    task = matches[0]
    prev_state = dict(task)
    await db.tasks.update_one(
        {"_id": task["_id"]},
        {"$set": {"status": "done", "updated_at": datetime.utcnow()}},
    )
    await _log_event(chat_id, "mark_done", "tasks", task["_id"], prev_state)
    return f'Done: "{task["title"]}".'


async def _update_task(chat_id: int, args: dict) -> str:
    db = get_db()
    query = args["task_query"]
    matches = await _fuzzy_tasks(query, status=None)
    matches = [m for m in matches if m["status"] != "done"]

    if not matches:
        return f'No active task matching "{query}".'
    if len(matches) > 1:
        listed = "\n".join(f"  {i+1}. {m['title']}" for i, m in enumerate(matches[:5]))
        return f'Found {len(matches)} tasks matching "{query}" — be more specific:\n{listed}'

    task = matches[0]
    prev_state = dict(task)
    updates: dict[str, Any] = {"updated_at": datetime.utcnow()}

    if new_title := args.get("new_title"):
        updates["title"] = new_title.strip()
    if raw := args.get("new_due_date"):
        due = _parse_date(raw)
        if not due:
            return f'Could not parse date "{raw}". Use YYYY-MM-DD.'
        updates["due_date"] = due

    await db.tasks.update_one({"_id": task["_id"]}, {"$set": updates})
    await _log_event(chat_id, "update_task", "tasks", task["_id"], prev_state)

    parts = []
    if "title" in updates:
        parts.append(f'renamed to "{updates["title"]}"')
    if "due_date" in updates:
        parts.append(f"due {updates['due_date'].strftime('%Y-%m-%d')}")
    return f'Updated "{task["title"]}": {", ".join(parts) or "no changes"}.'


async def _snooze(chat_id: int, args: dict) -> str:
    db = get_db()
    query = args["task_query"]
    until = _parse_date(args["until"])
    if not until:
        return f'Could not parse date "{args["until"]}". Use YYYY-MM-DD.'

    matches = await _fuzzy_tasks(query)
    if not matches:
        return f'No open task matching "{query}".'
    if len(matches) > 1:
        listed = "\n".join(f"  {i+1}. {m['title']}" for i, m in enumerate(matches[:5]))
        return f'Found {len(matches)} tasks matching "{query}" — be more specific:\n{listed}'

    task = matches[0]
    prev_state = dict(task)
    await db.tasks.update_one(
        {"_id": task["_id"]},
        {
            "$set": {
                "status": "snoozed",
                "snoozed_until": until,
                "updated_at": datetime.utcnow(),
            }
        },
    )
    await _log_event(chat_id, "snooze", "tasks", task["_id"], prev_state)
    return f'Snoozed "{task["title"]}" until {until.strftime("%Y-%m-%d")}.'


async def _get_status(chat_id: int, args: dict) -> str:
    db = get_db()
    scope = (args.get("scope") or "all")
    proj_filter = args.get("project_name")

    query: dict = {}
    if scope != "all":
        query["scope"] = scope

    projects = await db.projects.find(query).to_list(None)
    if proj_filter:
        projects = [p for p in projects if _sim(proj_filter, p["name"]) >= 0.4]

    if not projects:
        return "No projects found."

    lines: list[str] = []
    for proj in projects:
        open_tasks = await db.tasks.find(
            {"project_id": proj["_id"], "status": "open"}
        ).to_list(None)
        tag = f"[{proj['scope']}]"
        if open_tasks:
            lines.append(f"{tag} {proj['name']} ({len(open_tasks)} open)")
            for t in open_tasks:
                due = (
                    f" — due {t['due_date'].strftime('%Y-%m-%d')}"
                    if t.get("due_date")
                    else ""
                )
                lines.append(f"  • {t['title']}{due}")
        else:
            lines.append(f"{tag} {proj['name']} (no open tasks)")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Reminders
# ---------------------------------------------------------------------------

async def _set_reminder(chat_id: int, args: dict) -> str:
    from agentzero.scheduler import schedule_reminder

    text = args["text"].strip()
    fire_local = _parse_datetime(args["fire_at"])
    if not fire_local:
        return f'Could not understand the time "{args["fire_at"]}".'

    tz = ZoneInfo(TIMEZONE)
    fire_utc = fire_local.replace(tzinfo=tz).astimezone(timezone.utc)
    now_utc = datetime.now(timezone.utc)
    if fire_utc <= now_utc:
        return "That time is already in the past — give me a future time."

    db = get_db()
    result = await db.reminders.insert_one(
        {
            "chat_id": chat_id,
            "text": text,
            "fire_at": fire_utc,
            "status": "pending",
            "created_at": now_utc,
        }
    )
    schedule_reminder(str(result.inserted_id), chat_id, text, fire_utc)
    await _log_event(chat_id, "set_reminder", "reminders", result.inserted_id, None)

    when = fire_local.strftime("%a %d %b, %H:%M")
    return f"Got it — I'll remind you to {text} at {when}."


async def _list_reminders(chat_id: int, args: dict) -> str:
    db = get_db()
    tz = ZoneInfo(TIMEZONE)
    rows = (
        await db.reminders.find(
            {"chat_id": chat_id, "status": {"$in": ["pending", "awaiting_ack"]}}
        )
        .sort("fire_at", 1)
        .to_list(None)
    )
    if not rows:
        return "No active reminders."
    lines = ["Reminders:"]
    for r in rows:
        fire_at = r["fire_at"]
        if fire_at.tzinfo is None:
            fire_at = fire_at.replace(tzinfo=timezone.utc)
        local = fire_at.astimezone(tz)
        tag = " — awaiting your confirmation" if r.get("status") == "awaiting_ack" else ""
        lines.append(f"  • {r['text']} — {local.strftime('%a %d %b, %H:%M')}{tag}")
    return "\n".join(lines)


async def _complete_reminder(chat_id: int, args: dict) -> str:
    """Mark a reminder done once the user confirms it's handled (stops follow-ups)."""
    db = get_db()
    query = args["query"]
    rows = await db.reminders.find(
        {"chat_id": chat_id, "status": {"$in": ["pending", "awaiting_ack"]}}
    ).to_list(None)
    if not rows:
        return "No active reminders to close out."

    best = max(rows, key=lambda r: _sim(query, r["text"]))
    if _sim(query, best["text"]) < 0.3:
        return f'No active reminder matching "{query}".'

    prev_state = dict(best)
    await db.reminders.update_one(
        {"_id": best["_id"]},
        {"$set": {"status": "done", "completed_at": datetime.now(timezone.utc)}},
    )
    await _log_event(chat_id, "complete_reminder", "reminders", best["_id"], prev_state)
    try:
        from agentzero.scheduler import get_scheduler

        get_scheduler().remove_job(f"reminder:{best['_id']}")
    except Exception:
        pass
    return f'Nice — marked "{best["text"]}" done. I\'ll stop nagging.'


async def _cancel_reminder(chat_id: int, args: dict) -> str:
    db = get_db()
    query = args["query"]
    rows = await db.reminders.find(
        {"chat_id": chat_id, "status": "pending"}
    ).to_list(None)
    if not rows:
        return "No upcoming reminders to cancel."

    scored = sorted(rows, key=lambda r: _sim(query, r["text"]), reverse=True)
    best = scored[0]
    if _sim(query, best["text"]) < 0.3:
        return f'No reminder matching "{query}".'

    await db.reminders.update_one(
        {"_id": best["_id"]}, {"$set": {"status": "cancelled"}}
    )
    try:
        from agentzero.scheduler import get_scheduler

        get_scheduler().remove_job(f"reminder:{best['_id']}")
    except Exception:
        pass
    return f'Cancelled reminder: "{best["text"]}".'


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------

async def _remember(chat_id: int, args: dict) -> str:
    db = get_db()
    content = args["content"].strip()
    category = (args.get("category") or "general").strip()

    # Dedupe near-identical memories
    existing = await db.memory.find({"chat_id": chat_id}).to_list(None)
    for m in existing:
        if _sim(content, m["content"]) > 0.85:
            return f"Already noted: {m['content']}"

    now = datetime.now(timezone.utc)
    result = await db.memory.insert_one(
        {
            "chat_id": chat_id,
            "content": content,
            "category": category,
            "created_at": now,
            "updated_at": now,
        }
    )
    await _log_event(chat_id, "remember", "memory", result.inserted_id, None)
    return f"Noted: {content}"


async def _forget(chat_id: int, args: dict) -> str:
    db = get_db()
    query = args["query"]
    rows = await db.memory.find({"chat_id": chat_id}).to_list(None)
    if not rows:
        return "I don't have anything remembered yet."

    best = max(rows, key=lambda m: _sim(query, m["content"]))
    if _sim(query, best["content"]) < 0.3:
        return f'Nothing remembered matching "{query}".'

    prev_state = dict(best)
    await db.memory.delete_one({"_id": best["_id"]})
    await _log_event(chat_id, "forget", "memory", best["_id"], prev_state)
    return f'Forgot: {best["content"]}'
