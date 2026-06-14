"""
APScheduler — runs one-off reminders now; digest cron jobs land here in Phase 3.

Reminders are persisted in MongoDB (`reminders` collection) and re-loaded on
startup, so a restart never drops a pending reminder.  Times are stored in UTC.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from bson import ObjectId

from agentzero.config import HEARTBEAT_MINUTES, TIMEZONE
from agentzero.db import get_db
from agentzero.telegram_io import send

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


def get_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler(timezone=ZoneInfo(TIMEZONE))
    return _scheduler


def start_scheduler() -> None:
    sched = get_scheduler()
    if not sched.running:
        sched.start()
        logger.info("Scheduler started")


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        _scheduler = None


async def _phrase_reminder(text: str) -> str:
    """Render the reminder in AgentZero's voice, falling back to plain text."""
    from agentzero.llm import get_provider
    from agentzero.prompts import PERSONALITY

    system = (
        f"{PERSONALITY}\n\n"
        "Deliver the reminder below to the user as a single-line Telegram message in your "
        "voice — dry and a little sharp is good. The thing they need to remember must stay "
        "crystal clear. Start with the ⏰ emoji. No preamble, no sign-off, one line."
    )
    try:
        msg = (
            await get_provider().chat(
                [{"role": "user", "content": f"Reminder to deliver: {text}"}], system
            )
        ).strip()
        return msg or f"⏰ Reminder: {text}"
    except Exception:
        logger.exception("Reminder phrasing failed — falling back to plain text")
        return f"⏰ Reminder: {text}"


async def _fire_reminder(reminder_id: str, chat_id: int, text: str) -> None:
    try:
        await send(chat_id, await _phrase_reminder(text))
        db = get_db()
        await db.reminders.update_one(
            {"_id": ObjectId(reminder_id)},
            {"$set": {"status": "fired", "fired_at": datetime.now(timezone.utc)}},
        )
    except Exception:
        logger.exception("Failed to fire reminder %s", reminder_id)


def schedule_reminder(
    reminder_id: str, chat_id: int, text: str, fire_at_utc: datetime
) -> None:
    """Register a one-off job. fire_at_utc must be timezone-aware UTC."""
    sched = get_scheduler()
    sched.add_job(
        _fire_reminder,
        trigger=DateTrigger(run_date=fire_at_utc),
        args=[reminder_id, chat_id, text],
        id=f"reminder:{reminder_id}",
        replace_existing=True,
        misfire_grace_time=600,  # still fire if we were down up to 10 min past
    )


async def _heartbeat_job(chat_id: int) -> None:
    from agentzero.autonomy import run_heartbeat

    try:
        await run_heartbeat(chat_id)
    except Exception:
        logger.exception("Heartbeat job failed")


def schedule_heartbeat(chat_id: int) -> None:
    sched = get_scheduler()
    sched.add_job(
        _heartbeat_job,
        trigger=IntervalTrigger(minutes=HEARTBEAT_MINUTES),
        args=[chat_id],
        id="heartbeat",
        replace_existing=True,
        misfire_grace_time=120,
    )
    logger.info("Heartbeat scheduled every %d min", HEARTBEAT_MINUTES)


async def load_pending_reminders() -> None:
    """Re-schedule every still-pending reminder after a restart."""
    db = get_db()
    now = datetime.now(timezone.utc)
    pending = await db.reminders.find({"status": "pending"}).to_list(None)
    for r in pending:
        fire_at = r["fire_at"]
        if fire_at.tzinfo is None:
            fire_at = fire_at.replace(tzinfo=timezone.utc)
        if fire_at <= now:
            await _fire_reminder(str(r["_id"]), r["chat_id"], r["text"])
        else:
            schedule_reminder(str(r["_id"]), r["chat_id"], r["text"], fire_at)
    if pending:
        logger.info("Re-loaded %d pending reminder(s)", len(pending))
