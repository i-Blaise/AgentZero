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
    JOB_DIGEST_HOUR,
    JOB_DIGEST_MINUTE,
    MORNING_DIGEST_HOUR,
    MORNING_DIGEST_MINUTE,
    REMINDER_FOLLOWUP_MINUTES,
    TIMEZONE,
)
from agentzero.db import get_db
from agentzero.telegram_io import send

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None

# How often the follow-up loop wakes to check; per-reminder cadence (next_nudge_at)
# gates whether each one actually nudges, so this only needs to be fine enough.
_FOLLOWUP_WAKE_MINUTES = 15
# Bounds for the user-adjustable re-nudge cadence ("space them apart").
_CADENCE_MIN = 30
_CADENCE_MAX = 24 * 60


def clamp_followup_minutes(minutes: int) -> int:
    """Clamp a requested re-nudge cadence into a sane range."""
    return max(_CADENCE_MIN, min(int(minutes), _CADENCE_MAX))


async def _followup_minutes(chat_id: int) -> int:
    """The user's chosen re-nudge cadence, or the configured default."""
    db = get_db()
    state = await db.system_state.find_one({"chat_id": chat_id}) or {}
    override = state.get("nudge_interval_minutes")
    return clamp_followup_minutes(override) if override else REMINDER_FOLLOWUP_MINUTES


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


# NOTE: fired reminders and follow-up nags used to attach inline Done/snooze buttons.
# Removed at the owner's request (2026-07-11) — he replies by text ("done", "snooze an
# hour"). The callback handlers in main.py are deliberately KEPT so buttons on old
# messages still sitting in the chat history keep working if tapped.


async def _fire_reminder(reminder_id: str, chat_id: int, text: str) -> None:
    try:
        db = get_db()
        # Guard: never resurrect a reminder the user already closed. If a stale/duplicate
        # job fires for one that's been completed or cancelled, do nothing.
        current = await db.reminders.find_one({"_id": ObjectId(reminder_id)})
        if current and current.get("status") not in ("pending", None):
            return
        now = datetime.now(timezone.utc)
        gap = await _followup_minutes(chat_id)
        await send(chat_id, await _phrase_reminder(text))
        # Don't mark done — await the user's confirmation. It moves to awaiting_ack
        # and the follow-up loop keeps nudging until the user says it's handled.
        await db.reminders.update_one(
            {"_id": ObjectId(reminder_id)},
            {
                "$set": {
                    "status": "awaiting_ack",
                    "fired_at": now,
                    "next_nudge_at": now + timedelta(minutes=gap),
                    "nudge_count": 0,
                }
            },
        )
    except Exception:
        logger.exception("Failed to fire reminder %s", reminder_id)


async def _reminder_followup_job(chat_id: int) -> None:
    """Re-nudge unconfirmed reminders ONE AT A TIME — at most one per wake, the most
    overdue first. A backlog trickles out across cycles instead of dumping at once.
    Quiet-hours aware."""
    from agentzero.autonomy import _in_quiet_hours

    db = get_db()
    now = datetime.now(timezone.utc)
    if _in_quiet_hours(now.astimezone(ZoneInfo(TIMEZONE))):
        return

    awaiting = await db.reminders.find(
        {"chat_id": chat_id, "status": "awaiting_ack"}
    ).to_list(None)

    due = []
    for r in awaiting:
        nxt = r.get("next_nudge_at")
        if nxt and nxt.tzinfo is None:
            nxt = nxt.replace(tzinfo=timezone.utc)
        if nxt and nxt > now:
            continue
        due.append((r, nxt or now))
    if not due:
        return

    # Send only the single most-overdue one this cycle; the rest wait for later wakes.
    due.sort(key=lambda x: x[1])
    r = due[0][0]
    try:
        await send(
            chat_id,
            await _phrase_reminder(
                f"{r['text']} (still not marked done — tell me when it's handled)"
            ),
        )
    except Exception:
        logger.exception("Follow-up nudge failed for reminder %s", r["_id"])
        return

    gap = await _followup_minutes(chat_id)
    await db.reminders.update_one(
        {"_id": r["_id"]},
        {
            "$set": {
                "nudge_count": r.get("nudge_count", 0) + 1,
                "next_nudge_at": now + timedelta(minutes=gap),
            }
        },
    )


def schedule_reminder_followups(chat_id: int) -> None:
    sched = get_scheduler()
    sched.add_job(
        _reminder_followup_job,
        trigger=IntervalTrigger(minutes=_FOLLOWUP_WAKE_MINUTES),
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


async def _fire_recurring(chat_id: int, text: str) -> None:
    """Fire a recurring reminder — just pings (it'll come round again), no follow-up nag."""
    try:
        await send(chat_id, await _phrase_reminder(text))
    except Exception:
        logger.exception("Failed to fire recurring reminder for chat %s", chat_id)


def schedule_recurring_reminder(
    rid: str, chat_id: int, text: str, hour: int, minute: int, day_of_week: str = "*"
) -> None:
    sched = get_scheduler()
    sched.add_job(
        _fire_recurring,
        trigger=CronTrigger(hour=hour, minute=minute, day_of_week=day_of_week),
        args=[chat_id, text],
        id=f"recurring:{rid}",
        replace_existing=True,
        misfire_grace_time=3600,
    )


async def load_recurring_reminders() -> None:
    """Re-register every active recurring reminder after a restart."""
    db = get_db()
    rows = await db.recurring_reminders.find({"active": True}).to_list(None)
    for r in rows:
        schedule_recurring_reminder(
            str(r["_id"]), r["chat_id"], r["text"],
            r["hour"], r.get("minute", 0), r.get("day_of_week", "*"),
        )
    if rows:
        logger.info("Re-loaded %d recurring reminder(s)", len(rows))


async def _application_scan_job(chat_id: int) -> None:
    """Scan the inbox for application confirmations/replies and proactively report changes.
    Quiet-hours aware — skips overnight; the next scan picks up anything missed."""
    from agentzero.applications import send_application_update
    from agentzero.autonomy import _in_quiet_hours

    if _in_quiet_hours(datetime.now(timezone.utc).astimezone(ZoneInfo(TIMEZONE))):
        return
    try:
        await send_application_update(chat_id)
    except Exception:
        logger.exception("Application scan job failed")


def schedule_application_scan(chat_id: int) -> None:
    from agentzero.config import APPLICATION_SCAN_HOURS

    sched = get_scheduler()
    sched.add_job(
        _application_scan_job,
        trigger=IntervalTrigger(hours=max(1, APPLICATION_SCAN_HOURS)),
        args=[chat_id],
        id="application_scan",
        replace_existing=True,
        misfire_grace_time=1800,
    )
    logger.info("Application scan scheduled every %d h", max(1, APPLICATION_SCAN_HOURS))


async def _receipt_scan_job(chat_id: int) -> None:
    """Silently scan mailboxes for payment receipts and log them (no per-receipt ping)."""
    from agentzero.expenses import scan_receipts

    try:
        await scan_receipts(chat_id)
    except Exception:
        logger.exception("Receipt scan job failed")


def schedule_receipt_scan(chat_id: int) -> None:
    from agentzero.config import RECEIPT_SCAN_HOURS

    sched = get_scheduler()
    sched.add_job(
        _receipt_scan_job,
        trigger=IntervalTrigger(hours=max(1, RECEIPT_SCAN_HOURS)),
        args=[chat_id],
        id="receipt_scan",
        replace_existing=True,
        misfire_grace_time=1800,
    )
    logger.info("Receipt scan scheduled every %d h", max(1, RECEIPT_SCAN_HOURS))


async def _expense_summary_job(chat_id: int) -> None:
    from agentzero.expenses import send_weekly_summary

    try:
        await send_weekly_summary(chat_id)
    except Exception:
        logger.exception("Weekly expense summary job failed")


def schedule_expense_summary(chat_id: int) -> None:
    from agentzero.config import EXPENSE_SUMMARY_DOW, EXPENSE_SUMMARY_HOUR

    sched = get_scheduler()
    sched.add_job(
        _expense_summary_job,
        trigger=CronTrigger(day_of_week=EXPENSE_SUMMARY_DOW, hour=EXPENSE_SUMMARY_HOUR, minute=0),
        args=[chat_id],
        id="expense_summary",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    logger.info(
        "Weekly expense summary scheduled %s at %02d:00", EXPENSE_SUMMARY_DOW, EXPENSE_SUMMARY_HOUR
    )


async def _user_model_job(chat_id: int) -> None:
    """Daily reflection — distil memory + activity into the evolving user model. Silent."""
    from agentzero.user_model import synthesize_user_model

    try:
        await synthesize_user_model(chat_id)
    except Exception:
        logger.exception("User-model synthesis job failed")


def schedule_user_model_synthesis(chat_id: int) -> None:
    from agentzero.config import USER_MODEL_HOUR, USER_MODEL_MINUTE

    sched = get_scheduler()
    sched.add_job(
        _user_model_job,
        trigger=CronTrigger(hour=USER_MODEL_HOUR, minute=USER_MODEL_MINUTE),
        args=[chat_id],
        id="user_model",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    logger.info("User-model synthesis scheduled daily at %02d:%02d", USER_MODEL_HOUR, USER_MODEL_MINUTE)


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


async def _job_digest_job(chat_id: int) -> None:
    from agentzero.jobs import send_job_digest

    try:
        await send_job_digest(chat_id)
    except Exception:
        logger.exception("Job digest job failed")


def schedule_job_digest(chat_id: int) -> None:
    sched = get_scheduler()
    sched.add_job(
        _job_digest_job,
        trigger=CronTrigger(hour=JOB_DIGEST_HOUR, minute=JOB_DIGEST_MINUTE),
        args=[chat_id],
        id="job_digest",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    logger.info(
        "Job drop scheduled daily at %02d:%02d", JOB_DIGEST_HOUR, JOB_DIGEST_MINUTE
    )


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
