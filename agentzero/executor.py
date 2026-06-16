"""
Deterministic tool executor.  The LLM never writes to the database directly;
it proposes tool calls and this module validates and applies them.

Every write is logged to the `events` collection so /undo can reverse it.
Fuzzy matching uses difflib SequenceMatcher (threshold 0.4).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
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
        "set_recurring_reminder": _set_recurring_reminder,
        "list_reminders": _list_reminders,
        "cancel_reminder": _cancel_reminder,
        "complete_reminder": _complete_reminder,
        "snooze_reminder": _snooze_reminder,
        "set_reminder_cadence": _set_reminder_cadence,
        "remember": _remember,
        "forget": _forget,
        "set_job_profile": _set_job_profile,
        "find_jobs": _find_jobs,
        "web_search": _web_search,
        "web_fetch": _web_fetch,
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
    recurring = await db.recurring_reminders.find(
        {"chat_id": chat_id, "active": True}
    ).to_list(None)

    if not rows and not recurring:
        return "No active reminders."

    lines: list[str] = []
    if rows:
        lines.append("Reminders:")
        for r in rows:
            fire_at = r["fire_at"]
            if fire_at.tzinfo is None:
                fire_at = fire_at.replace(tzinfo=timezone.utc)
            local = fire_at.astimezone(tz)
            tag = " — awaiting your confirmation" if r.get("status") == "awaiting_ack" else ""
            lines.append(f"  • {r['text']} — {local.strftime('%a %d %b, %H:%M')}{tag}")
    if recurring:
        lines.append("Recurring:")
        for r in recurring:
            lines.append(
                f"  • {r['text']} — {_humanise_dow(r.get('day_of_week', '*'))} "
                f"at {r['hour']:02d}:{r.get('minute', 0):02d}"
            )
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
    one_offs = await db.reminders.find(
        {"chat_id": chat_id, "status": "pending"}
    ).to_list(None)
    recurring = await db.recurring_reminders.find(
        {"chat_id": chat_id, "active": True}
    ).to_list(None)
    if not one_offs and not recurring:
        return "No upcoming reminders to cancel."

    # Pick the best match across both one-off and recurring reminders.
    candidates = [("one_off", r) for r in one_offs] + [("recurring", r) for r in recurring]
    kind, best = max(candidates, key=lambda c: _sim(query, c[1]["text"]))
    if _sim(query, best["text"]) < 0.3:
        return f'No reminder matching "{query}".'

    if kind == "recurring":
        await db.recurring_reminders.update_one(
            {"_id": best["_id"]}, {"$set": {"active": False}}
        )
        job_id = f"recurring:{best['_id']}"
    else:
        await db.reminders.update_one(
            {"_id": best["_id"]}, {"$set": {"status": "cancelled"}}
        )
        job_id = f"reminder:{best['_id']}"
    try:
        from agentzero.scheduler import get_scheduler

        get_scheduler().remove_job(job_id)
    except Exception:
        pass
    label = "recurring reminder" if kind == "recurring" else "reminder"
    return f'Cancelled {label}: "{best["text"]}".'


async def _snooze_reminder(chat_id: int, args: dict) -> str:
    """Push a reminder's next ping further out ('remind me later')."""
    from agentzero.scheduler import schedule_reminder

    db = get_db()
    minutes = int(args.get("minutes") or 60)
    if minutes <= 0:
        minutes = 60
    query = (args.get("query") or "").strip()

    rows = await db.reminders.find(
        {"chat_id": chat_id, "status": {"$in": ["pending", "awaiting_ack"]}}
    ).to_list(None)
    if not rows:
        return "No active reminders to push back."

    if query:
        best = max(rows, key=lambda r: _sim(query, r["text"]))
        if _sim(query, best["text"]) < 0.3:
            return f'No active reminder matching "{query}".'
        targets = [best]
    else:
        targets = rows  # push everything outstanding

    now = datetime.now(timezone.utc)
    new_at = now + timedelta(minutes=minutes)
    for r in targets:
        if r.get("status") == "awaiting_ack":
            # Delay the next follow-up nudge.
            await db.reminders.update_one(
                {"_id": r["_id"]}, {"$set": {"next_nudge_at": new_at}}
            )
        else:
            # Still pending — move when it first fires and reschedule the job.
            await db.reminders.update_one(
                {"_id": r["_id"]}, {"$set": {"fire_at": new_at}}
            )
            schedule_reminder(str(r["_id"]), chat_id, r["text"], new_at)

    pretty = f"{minutes} min" if minutes < 120 else f"{round(minutes / 60, 1)} h"
    if len(targets) == 1:
        return f'Pushed "{targets[0]["text"]}" back by {pretty}.'
    return f"Pushed all {len(targets)} reminders back by {pretty}."


async def _set_reminder_cadence(chat_id: int, args: dict) -> str:
    """Change how often follow-up nudges fire ('space them apart')."""
    from agentzero.scheduler import clamp_followup_minutes

    raw = args.get("minutes")
    if raw is None:
        return "Tell me how often — e.g. every 60 minutes, or every 3 hours."
    minutes = clamp_followup_minutes(int(raw))
    db = get_db()
    await db.system_state.update_one(
        {"chat_id": chat_id},
        {"$set": {"nudge_interval_minutes": minutes}},
        upsert=True,
    )
    pretty = f"{minutes} min" if minutes < 120 else f"{round(minutes / 60, 1)} h"
    return f"Done — I'll re-nudge about unfinished reminders every {pretty} now."


# ---------------------------------------------------------------------------
# Recurring reminders (cron-style: "every weekday at 8")
# ---------------------------------------------------------------------------

_DOW_HUMAN = {
    "*": "every day",
    "mon-fri": "every weekday",
    "sat,sun": "on weekends",
    "sat-sun": "on weekends",
}


def _humanise_dow(dow: str) -> str:
    return _DOW_HUMAN.get(dow.lower().strip(), f"on {dow}")


async def _set_recurring_reminder(chat_id: int, args: dict) -> str:
    from agentzero.scheduler import schedule_recurring_reminder

    text = (args.get("text") or "").strip()
    if not text:
        return "What should I remind you about?"
    try:
        hour = int(args["hour"])
        minute = int(args.get("minute") or 0)
    except (KeyError, ValueError, TypeError):
        return "I need a time of day (hour, and optionally minute)."
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return "That time of day doesn't look right — give me an hour 0-23."
    day_of_week = (args.get("day_of_week") or "*").lower().strip()

    db = get_db()
    result = await db.recurring_reminders.insert_one(
        {
            "chat_id": chat_id,
            "text": text,
            "hour": hour,
            "minute": minute,
            "day_of_week": day_of_week,
            "active": True,
            "created_at": datetime.now(timezone.utc),
        }
    )
    schedule_recurring_reminder(
        str(result.inserted_id), chat_id, text, hour, minute, day_of_week
    )
    await _log_event(chat_id, "set_recurring_reminder", "recurring_reminders", result.inserted_id, None)
    return f"Got it — I'll remind you to {text} {_humanise_dow(day_of_week)} at {hour:02d}:{minute:02d}."


# ---------------------------------------------------------------------------
# By-id reminder/task actions — used by inline buttons (no fuzzy matching needed)
# ---------------------------------------------------------------------------

def _oid(s: str) -> ObjectId | None:
    try:
        return ObjectId(s)
    except Exception:
        return None


async def complete_reminder_by_id(chat_id: int, rid: str) -> str:
    db = get_db()
    oid = _oid(rid)
    r = await db.reminders.find_one({"_id": oid, "chat_id": chat_id}) if oid else None
    if not r or r.get("status") in ("done", "cancelled"):
        return "Already handled."
    prev = dict(r)
    await db.reminders.update_one(
        {"_id": oid}, {"$set": {"status": "done", "completed_at": datetime.now(timezone.utc)}}
    )
    await _log_event(chat_id, "complete_reminder", "reminders", oid, prev)
    try:
        from agentzero.scheduler import get_scheduler

        get_scheduler().remove_job(f"reminder:{rid}")
    except Exception:
        pass
    return f'✅ Done: "{r["text"]}".'


async def snooze_reminder_by_id(chat_id: int, rid: str, minutes: int) -> str:
    from agentzero.scheduler import schedule_reminder

    db = get_db()
    oid = _oid(rid)
    r = await db.reminders.find_one({"_id": oid, "chat_id": chat_id}) if oid else None
    if not r or r.get("status") in ("done", "cancelled"):
        return "That reminder's no longer active."
    now = datetime.now(timezone.utc)
    new_at = now + timedelta(minutes=minutes)
    if r.get("status") == "awaiting_ack":
        await db.reminders.update_one({"_id": oid}, {"$set": {"next_nudge_at": new_at}})
    else:
        await db.reminders.update_one({"_id": oid}, {"$set": {"fire_at": new_at}})
        schedule_reminder(rid, chat_id, r["text"], new_at)
    pretty = f"{minutes} min" if minutes < 120 else f"{round(minutes / 60, 1)} h"
    return f'⏰ Snoozed "{r["text"]}" for {pretty}.'


async def mark_done_by_id(chat_id: int, tid: str) -> str:
    db = get_db()
    oid = _oid(tid)
    t = await db.tasks.find_one({"_id": oid}) if oid else None
    if not t:
        return "Can't find that task."
    if t["status"] == "done":
        return f'Already done: "{t["title"]}".'
    prev = dict(t)
    await db.tasks.update_one(
        {"_id": oid}, {"$set": {"status": "done", "updated_at": datetime.utcnow()}}
    )
    await _log_event(chat_id, "mark_done", "tasks", oid, prev)
    return f'✅ Done: "{t["title"]}".'


async def mute_task_nudge_by_id(chat_id: int, tid: str, days: int = 2) -> str:
    """Pause proactive nudges for a task without hiding it — push last_nudged_at forward."""
    db = get_db()
    oid = _oid(tid)
    t = await db.tasks.find_one({"_id": oid}) if oid else None
    if not t:
        return "Can't find that task."
    future = datetime.now(timezone.utc) + timedelta(days=days)
    await db.tasks.update_one({"_id": oid}, {"$set": {"last_nudged_at": future}})
    return f'🔕 I\'ll hold off on "{t["title"]}" for a couple of days.'


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


# ---------------------------------------------------------------------------
# Job hunting
# ---------------------------------------------------------------------------

async def _set_job_profile(chat_id: int, args: dict) -> str:
    db = get_db()
    cv = (args.get("cv") or "").strip()
    criteria = (args.get("criteria") or "").strip()
    if not cv and not criteria:
        return "Give me your CV/background and/or what kind of role you're after."

    update: dict = {"updated_at": datetime.now(timezone.utc)}
    if cv:
        update["cv"] = cv
    if criteria:
        update["criteria"] = criteria
    await db.profile.update_one(
        {"chat_id": chat_id}, {"$set": update, "$setOnInsert": {"chat_id": chat_id}}, upsert=True
    )
    parts = []
    if cv:
        parts.append("CV")
    if criteria:
        parts.append("job criteria")
    return f"Saved your {' and '.join(parts)}. I'll match new postings against it."


async def _find_jobs(chat_id: int, args: dict) -> str:
    from agentzero.jobs import fetch_jobs, format_jobs

    db = get_db()
    query = args.get("query")
    if not query:
        prof = await db.profile.find_one({"chat_id": chat_id}) or {}
        query = prof.get("criteria") or None
    limit = int(args.get("limit") or 15)

    jobs = await fetch_jobs(chat_id, query=query, limit=limit)
    if not jobs:
        return "No new postings right now (nothing fresh since last check)."
    return format_jobs(jobs)


# ---------------------------------------------------------------------------
# Web (search + fetch) — no DB writes, so nothing is logged to /undo
# ---------------------------------------------------------------------------

async def _web_search(chat_id: int, args: dict) -> str:
    from agentzero.web import web_search

    return await web_search(args["query"], int(args.get("max_results") or 5))


async def _web_fetch(chat_id: int, args: dict) -> str:
    from agentzero.web import web_fetch

    return await web_fetch(args["url"])
