"""
Autonomy — the proactive heartbeat.

Periodically (and on /checkin) this gathers the user's current state and asks the
LLM whether anything is genuinely worth messaging them about *right now*.  The
bar is deliberately high: an unhelpful proactive ping is worse than silence.

Guardrails:
  - quiet hours (no overnight pings)
  - cooldown between proactive messages
  - per-task dedupe via last_nudged_at (won't re-nudge the same task within 24h)
  - the LLM itself can answer SILENT to suppress a message

`force=True` (from /checkin) bypasses quiet hours and cooldown so the user can
test on demand.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from agentzero.config import (
    AUTONOMY_ENABLED,
    NUDGE_COOLDOWN_HOURS,
    QUIET_HOURS_END,
    QUIET_HOURS_START,
    STALL_DAYS_PERSONAL,
    STALL_DAYS_WORK,
    TIMEZONE,
)
from agentzero.db import get_db
from agentzero.llm import get_provider
from agentzero.telegram_io import send

logger = logging.getLogger(__name__)

SILENT = "SILENT"
RENUDGE_HOURS = 24  # don't surface the same task again within this window


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _in_quiet_hours(now_local: datetime) -> bool:
    h = now_local.hour
    if QUIET_HOURS_START <= QUIET_HOURS_END:
        return QUIET_HOURS_START <= h < QUIET_HOURS_END
    return h >= QUIET_HOURS_START or h < QUIET_HOURS_END


async def _get_last_nudge(chat_id: int) -> datetime | None:
    db = get_db()
    doc = await db.system_state.find_one({"chat_id": chat_id})
    return _aware(doc.get("last_proactive_nudge_at")) if doc else None


async def _set_last_nudge(chat_id: int, when: datetime) -> None:
    db = get_db()
    await db.system_state.update_one(
        {"chat_id": chat_id},
        {"$set": {"last_proactive_nudge_at": when}},
        upsert=True,
    )


async def gather_candidates(chat_id: int) -> dict:
    db = get_db()
    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(ZoneInfo(TIMEZONE))
    today = now_local.date()
    renudge_cutoff = now_utc - timedelta(hours=RENUDGE_HOURS)

    projects = {p["_id"]: p for p in await db.projects.find({}).to_list(None)}

    overdue, due_soon, stalled = [], [], []
    for t in await db.tasks.find({"status": "open"}).to_list(None):
        last_nudged = _aware(t.get("last_nudged_at"))
        if last_nudged and last_nudged > renudge_cutoff:
            continue  # already nudged recently

        proj = projects.get(t["project_id"])
        scope = proj["scope"] if proj else "personal"
        pname = proj["name"] if proj else "?"
        entry = (t, pname, scope)

        due = t.get("due_date")
        if due:
            d = due.date() if hasattr(due, "date") else due
            if d < today:
                overdue.append(entry)
            elif d <= today + timedelta(days=2):
                due_soon.append(entry)
        else:
            created = _aware(t.get("created_at"))
            stall_days = STALL_DAYS_WORK if scope == "work" else STALL_DAYS_PERSONAL
            if created and created < now_utc - timedelta(days=stall_days):
                stalled.append(entry)

    memories = [m["content"] for m in await db.memory.find({}).to_list(None)]
    return {
        "overdue": overdue,
        "due_soon": due_soon,
        "stalled": stalled,
        "memories": memories,
    }


def _format_candidates(c: dict) -> str:
    lines: list[str] = []
    if c["overdue"]:
        lines.append("Overdue tasks:")
        for t, p, s in c["overdue"]:
            lines.append(f"  - [{s}] {t['title']} ({p}) — was due {t['due_date'].strftime('%Y-%m-%d')}")
    if c["due_soon"]:
        lines.append("Due soon:")
        for t, p, s in c["due_soon"]:
            lines.append(f"  - [{s}] {t['title']} ({p}) — due {t['due_date'].strftime('%Y-%m-%d')}")
    if c["stalled"]:
        lines.append("Stalled (no movement in a while):")
        for t, p, s in c["stalled"]:
            lines.append(f"  - [{s}] {t['title']} ({p})")
    if c["memories"]:
        lines.append("What you know about the user:")
        for m in c["memories"]:
            lines.append(f"  - {m}")
    return "\n".join(lines)


async def run_heartbeat(chat_id: int, force: bool = False) -> str | None:
    """Returns the message sent, or None if it stayed silent."""
    if not AUTONOMY_ENABLED and not force:
        return None

    db = get_db()
    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(ZoneInfo(TIMEZONE))

    if not force:
        if _in_quiet_hours(now_local):
            return None
        last = await _get_last_nudge(chat_id)
        if last and last > now_utc - timedelta(hours=NUDGE_COOLDOWN_HOURS):
            return None

    c = await gather_candidates(chat_id)
    has_tasks = bool(c["overdue"] or c["due_soon"] or c["stalled"])
    if not has_tasks and not c["memories"]:
        return None  # nothing to even consider — skip the LLM call

    summary = _format_candidates(c)
    system = (
        f"You are AgentZero, a proactive personal assistant. Current local time: "
        f"{now_local.strftime('%Y-%m-%d %H:%M')} ({TIMEZONE}).\n\n"
        "Below is the user's current state. Decide whether anything is genuinely worth "
        "proactively messaging them about RIGHT NOW: an overdue or urgent task, an upcoming "
        "date you can infer from what you know about them, or a gentle nudge on something "
        "that's stalled. Be highly selective — a proactive ping that isn't clearly useful is "
        "worse than staying silent.\n\n"
        "If something is worth it, write ONE short, warm, specific Telegram message "
        "(1-3 sentences, no preamble, no sign-off). If nothing is genuinely worth "
        f"interrupting them for right now, reply with exactly: {SILENT}"
    )

    try:
        reply = (await get_provider().chat([{"role": "user", "content": summary}], system)).strip()
    except Exception:
        logger.exception("Heartbeat LLM call failed")
        return None

    if not reply or reply.upper().startswith(SILENT):
        return None

    await send(chat_id, reply)
    await _set_last_nudge(chat_id, now_utc)

    nudged_ids = [
        t["_id"]
        for group in ("overdue", "due_soon", "stalled")
        for (t, _p, _s) in c[group]
    ]
    if nudged_ids:
        await db.tasks.update_many(
            {"_id": {"$in": nudged_ids}}, {"$set": {"last_nudged_at": now_utc}}
        )
    return reply
