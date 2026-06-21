"""
Self-updating user model.

A daily reflection (scheduler._user_model_job) reads what the bot knows — the authoritative
operating manual, saved memory facts, the previous model, and recent ACTIVITY (open/stalled/
done tasks, reminders the user keeps snoozing, job-application follow-through) — and asks the
LLM to distil an evolving portrait: WHO the user is, what they're WORKING ON, their GOALS, and
observed working-style PATTERNS. It's stored on the profile (`user_model`) and injected into
every system prompt, so the bot's read of the user sharpens over time and drives prioritisation.

Scope is deliberately work/goals/productivity — the prompt forbids inferring sensitive personal
matters (health, relationships, finances, beliefs) unless the user stated them as work-relevant.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from agentzero.db import get_db
from agentzero.llm import get_provider

logger = logging.getLogger(__name__)


def _aware(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def get_user_model(chat_id: int) -> str | None:
    db = get_db()
    prof = await db.profile.find_one({"chat_id": chat_id}) or await db.profile.find_one({}) or {}
    return (prof.get("user_model") or "").strip() or None


async def _gather_signals(chat_id: int) -> str:
    db = get_db()
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=30)

    prof = await db.profile.find_one({"chat_id": chat_id}) or await db.profile.find_one({}) or {}
    manual = (prof.get("manual") or "").strip()
    criteria = (prof.get("criteria") or "").strip()
    previous = (prof.get("user_model") or "").strip()

    memories = [m["content"] for m in await db.memory.find({"chat_id": chat_id}).to_list(None)]

    projects = {p["_id"]: p for p in await db.projects.find({}).to_list(None)}
    open_tasks, done_recent, stalled = [], [], []
    for t in await db.tasks.find({}).to_list(None):
        pname = projects.get(t["project_id"], {}).get("name", "?")
        label = f"{t['title']} ({pname})"
        if t.get("status") == "open":
            open_tasks.append(label)
            created = _aware(t.get("created_at"))
            if created and created < cutoff:
                stalled.append(label)
        elif t.get("status") == "done":
            updated = _aware(t.get("updated_at"))
            if updated and updated >= cutoff:
                done_recent.append(label)

    snoozy = []
    for r in await db.reminders.find({"chat_id": chat_id, "status": "awaiting_ack"}).to_list(None):
        if (r.get("nudge_count") or 0) >= 3:
            snoozy.append(f"{r['text']} (nudged {r['nudge_count']}x, still not done)")

    apps = [
        f"{a.get('company')} — {a.get('status')}"
        for a in await db.applications.find({"chat_id": chat_id}).to_list(None)
    ]

    def block(title, items, limit=20):
        if not items:
            return f"{title}: (none)"
        return f"{title}:\n" + "\n".join(f"  - {x}" for x in items[:limit])

    parts = [
        f"OPERATING MANUAL (authoritative):\n{manual or '(none)'}",
        f"PREVIOUS PROFILE (refine, don't discard):\n{previous or '(none yet)'}",
        f"JOB CRITERIA: {criteria or '(none)'}",
        block("SAVED FACTS", memories),
        block("OPEN TASKS", open_tasks),
        block("RECENTLY COMPLETED (30d)", done_recent),
        block("STALLED (open >30d)", stalled),
        block("REMINDERS REPEATEDLY UNDONE", snoozy),
        block("JOB APPLICATIONS", apps),
    ]
    return "\n\n".join(parts)


async def synthesize_user_model(chat_id: int) -> str | None:
    """Reflect over everything and write an updated profile. Returns it, or None on failure."""
    signals = await _gather_signals(chat_id)
    system = (
        "You maintain an evolving, concise profile of ONE user for their personal-assistant bot. "
        "Using the inputs below (authoritative manual, the previous profile, saved facts, and recent "
        "activity), write an UPDATED profile in four short labelled sections:\n"
        "WHO — their role/identity.\n"
        "WORKING ON — current projects/focus.\n"
        "GOALS — priorities and what success looks like for them.\n"
        "PATTERNS — observed working-style tendencies (what moves vs stalls, follow-through, where "
        "they get stuck) inferred from the activity.\n\n"
        "Ground every statement in the evidence; phrase genuine inferences tentatively ('seems to', "
        "'tends to'). Stick strictly to work, projects, goals, and productivity/working style — do "
        "NOT infer or record sensitive personal matters (health, relationships, finances, religion, "
        "or other private/protected attributes) unless the user explicitly stated them as a "
        "work-relevant fact. Be concise (under 220 words). Output ONLY the profile text."
    )
    try:
        model = (await get_provider().chat([{"role": "user", "content": signals}], system)).strip()
    except Exception:
        logger.exception("User-model synthesis failed")
        return None
    if not model:
        return None

    db = get_db()
    await db.profile.update_one(
        {"chat_id": chat_id},
        {"$set": {"user_model": model, "user_model_updated_at": datetime.now(timezone.utc)},
         "$setOnInsert": {"chat_id": chat_id}},
        upsert=True,
    )
    logger.info("User model updated for chat %s (%d chars)", chat_id, len(model))
    return model
