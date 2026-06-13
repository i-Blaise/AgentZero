"""
Deterministic tool executor.  The LLM never writes to the database directly;
it proposes tool calls and this module validates and applies them.

Every write is logged to the `events` collection so /undo can reverse it.
Fuzzy matching uses difflib SequenceMatcher (threshold 0.4).
"""
from __future__ import annotations

import re
from datetime import datetime
from difflib import SequenceMatcher
from typing import Any

from bson import ObjectId

from agentzero.db import get_db
from agentzero.llm import ToolCall


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
        await coll.delete_one({"_id": doc_id})
    else:
        prev = {k: v for k, v in event["prev_state"].items() if k != "_id"}
        await coll.update_one({"_id": doc_id}, {"$set": prev})

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
