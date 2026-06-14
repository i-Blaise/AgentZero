"""
APScheduler — runs one-off reminders now; digest cron jobs land here in Phase 3.

Reminders are persisted in MongoDB (`reminders` collection) and re-loaded on
startup, so a restart never drops a pending reminder.  Times are stored in UTC.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from bson import ObjectId

from agentzero.config import (
    EVENING_DIGEST_HOUR,
    EVENING_DIGEST_MINUTE,
    HEARTBEAT_MINUTES,
    MORNING_DIGEST_HOUR,
    MORNING_DIGEST_MINUTE,
    REMINDER_FOLLOWUP_MINUTES,
    TIMEZONE,
)
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
        now = datetime.now(timezone.utc)
        # Don't mark done — await the user's confirmation. It moves to awaiting_ack
        # and the follow-up loop keeps nudging until the user says it's handled.
        await db.reminders.update_one(
            {"_id": ObjectId(reminder_id)},
            {
                "$set": {
                    "status": "awaiting_ack",
                    "fired_at": now,
                    "next_nudge_at": now + timedelta(minutes=REMINDER_FOLLOWUP_MINUTES),
                    "nudge_count": 0,
                }
            },
        )
    except Exception:
        logger.exception("Failed to fire reminder %s", reminder_id)


async def _reminder_followup_job(chat_id: int) -> None:
    """Re-nudge reminders the user fired but hasn't confirmed done. Quiet-hours aware."""
    from agentzero.autonomy import _in_quiet_hours

    db = get_db()
    now = datetime.now(timezone.utc)
    if _in_quiet_hours(now.astimezone(ZoneInfo(TIMEZONE))):
        return

    due = await db.reminders.find(
        {"chat_id": chat_id, "status": "awaiting_ack"}
    ).to_list(None)
    for r in due:
        nxt = r.get("next_nudge_at")
        if nxt and nxt.tzinfo is None:
            nxt = nxt.replace(tzinfo=timezone.utc)
        if nxt and nxt > now:
            continue
        count = r.get("nudge_count", 0) + 1
        try:
            await send(
                chat_id,
                await _phrase_reminder(
                    f"{r['text']} (still not marked done — tell me when it's handled)"
                ),
            )
        except Exception:
            logger.exception("Follow-up nudge failed for reminder %s", r["_id"])
            continue
        await db.reminders.update_one(
            {"_id": r["_id"]},
            {
                "$set": {
                    "nudge_count": count,
                    "next_nudge_at": now + timedelta(minutes=REMINDER_FOLLOWUP_MINUTES),
                }
            },
        )


def schedule_reminder_followups(chat_id: int) -> None:
    sched = get_scheduler()
    sched.add_job(
        _reminder_followup_job,
        trigger=IntervalTrigger(minutes=max(15, REMINDER_FOLLOWUP_MINUTES // 3)),
        args=[chat_id],
        id="reminder_followups",
        replace_existing=True,
        misfire_grace_time=300,
    )
    logger.info("Reminder follow-up loop scheduled")


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


async def _morning_digest_job(chat_id: int) -> None:
    from agentzero.digest import send_morning_digest

    try:
        await send_morning_digest(chat_id)
    except Exception:
        logger.exception("Morning digest job failed")


def schedule_morning_digest(chat_id: int) -> None:
    sched = get_scheduler()
    sched.add_job(
        _morning_digest_job,
        trigger=CronTrigger(hour=MORNING_DIGEST_HOUR, minute=MORNING_DIGEST_MINUTE),
        args=[chat_id],
        id="morning_digest",
        replace_existing=True,
        misfire_grace_time=3600,  # still fire if we were down up to an hour past 08:00
    )
    logger.info(
        "Morning digest scheduled daily at %02d:%02d",
        MORNING_DIGEST_HOUR,
        MORNING_DIGEST_MINUTE,
    )


async def _evening_digest_job(chat_id: int) -> None:
    from agentzero.digest import send_evening_digest

    try:
        await send_evening_digest(chat_id)
    except Exception:
        logger.exception("Evening digest job failed")


def schedule_evening_digest(chat_id: int) -> None:
    sched = get_scheduler()
    sched.add_job(
        _evening_digest_job,
        trigger=CronTrigger(hour=EVENING_DIGEST_HOUR, minute=EVENING_DIGEST_MINUTE),
        args=[chat_id],
        id="evening_digest",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    logger.info(
        "Evening digest scheduled daily at %02d:%02d",
        EVENING_DIGEST_HOUR,
        EVENING_DIGEST_MINUTE,
    )


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
