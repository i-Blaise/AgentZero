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
import random
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from zoneinfo import ZoneInfo

from agentzero.config import (
    AUTONOMY_ENABLED,
    NUDGE_MAX_GAP_MINUTES,
    NUDGE_MIN_GAP_MINUTES,
    QUIET_HOURS_END,
    QUIET_HOURS_START,
    STALL_DAYS_PERSONAL,
    STALL_DAYS_WORK,
    TIMEZONE,
)
from agentzero.db import get_db
from agentzero.llm import get_provider
from agentzero.prompts import PERSONALITY
from agentzero.telegram_io import send

logger = logging.getLogger(__name__)

SILENT = "SILENT"
RENUDGE_HOURS = 24  # don't surface the same task again within this window


def _next_gap_minutes() -> int:
    """A randomised gap until the next proactive nudge — keeps the cadence spontaneous."""
    lo = max(1, min(NUDGE_MIN_GAP_MINUTES, NUDGE_MAX_GAP_MINUTES))
    hi = max(NUDGE_MIN_GAP_MINUTES, NUDGE_MAX_GAP_MINUTES)
    return random.randint(lo, hi)


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


async def _get_next_proactive_at(chat_id: int) -> datetime | None:
    db = get_db()
    doc = await db.system_state.find_one({"chat_id": chat_id})
    return _aware(doc.get("next_proactive_at")) if doc else None


async def _set_last_nudge(chat_id: int, when: datetime) -> None:
    """Record this nudge and schedule the next allowed one a random gap out."""
    db = get_db()
    next_at = when + timedelta(minutes=_next_gap_minutes())
    await db.system_state.update_one(
        {"chat_id": chat_id},
        {"$set": {"last_proactive_nudge_at": when, "next_proactive_at": next_at}},
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


def _ranked(c: dict) -> list[tuple]:
    """Flatten candidates into ONE urgency-ordered list: overdue (most overdue first),
    then due-soon (soonest first), then stalled. The bot nudges about the top item."""
    overdue = sorted(c["overdue"], key=lambda e: e[0]["due_date"])
    due_soon = sorted(c["due_soon"], key=lambda e: e[0]["due_date"])
    return overdue + due_soon + c["stalled"]


def _format_candidates(c: dict) -> str:
    ranked = _ranked(c)
    lines: list[str] = []
    if ranked:
        lines.append("Open tasks, most pressing first:")
        for t, p, s in ranked:
            due = t.get("due_date")
            if due and s in ("work", "personal") and t in [e[0] for e in c["overdue"]]:
                tag = f" — was due {due.strftime('%Y-%m-%d')} (OVERDUE)"
            elif due:
                tag = f" — due {due.strftime('%Y-%m-%d')}"
            else:
                tag = " — no due date, stalled"
            lines.append(f"  - [{s}] {t['title']} ({p}){tag}")
    if c["memories"]:
        lines.append("What you know about the user:")
        for m in c["memories"]:
            lines.append(f"  - {m}")
    return "\n".join(lines)


def _suppress_after_nudge(reply: str, ranked: list[tuple]) -> list:
    """Figure out which SINGLE task the bot just nudged about, so only that one is
    suppressed for 24h and the next heartbeat is free to pick the next-urgent one."""
    if not ranked:
        return []
    low = reply.lower()

    def overlap(title: str) -> float:
        words = [w for w in title.lower().split() if len(w) > 3]
        if not words:
            return SequenceMatcher(None, title.lower(), low).ratio()
        hits = sum(1 for w in words if w in low)
        return hits / len(words)

    best, best_score = ranked[0], 0.0
    for entry in ranked:
        score = overlap(entry[0]["title"])
        if score > best_score:
            best, best_score = entry, score
    # If the message clearly references one task, suppress that; otherwise assume it
    # nudged the single most-urgent one (top of the ranked list).
    return [best[0]["_id"]]


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
        # Spontaneous spacing: only ping once the randomised gap from the last nudge has
        # elapsed. Fall back to a minimum-gap floor if no next time was recorded yet.
        next_at = await _get_next_proactive_at(chat_id)
        if next_at and now_utc < next_at:
            return None
        if not next_at:
            last = await _get_last_nudge(chat_id)
            floor = max(1, min(NUDGE_MIN_GAP_MINUTES, NUDGE_MAX_GAP_MINUTES))
            if last and now_utc < last + timedelta(minutes=floor):
                return None

    c = await gather_candidates(chat_id)
    ranked = _ranked(c)
    if not ranked and not c["memories"]:
        return None  # nothing to even consider — skip the LLM call

    summary = _format_candidates(c)
    system = (
        f"You are AgentZero, a proactive personal assistant. Current local time: "
        f"{now_local.strftime('%Y-%m-%d %H:%M')} ({TIMEZONE}).\n\n"
        f"{PERSONALITY}\n\n"
        "Below is the user's current state, with open tasks ordered most-pressing first. "
        "Your job: pick the SINGLE most important thing to nudge them about right now and "
        "send ONE message about THAT ONE THING only — not a list, not a roundup. You'll get "
        "more chances later to raise the others, so don't dump them all at once. Use your own "
        "judgment about what's genuinely most urgent (a deadline that's slipping usually beats "
        "something merely stalled).\n\n"
        "Write ONE short, specific Telegram message in your voice (1-2 sentences, no preamble, "
        "no sign-off) naming that one task and prompting action — dry and witty, but the point "
        "must be unmistakable. If genuinely nothing is worth interrupting them for right now, "
        f"reply with exactly: {SILENT}"
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

    # Suppress ONLY the one task it just nudged, so the next heartbeat is free to raise the
    # next-most-urgent one — this is what makes the nudges trickle out one at a time.
    nudged_ids = _suppress_after_nudge(reply, ranked)
    if nudged_ids:
        await db.tasks.update_many(
            {"_id": {"$in": nudged_ids}}, {"$set": {"last_nudged_at": now_utc}}
        )
    return reply
