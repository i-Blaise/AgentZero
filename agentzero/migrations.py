"""
One-shot, idempotent data migrations, run at startup before the scheduler loads jobs.

2026-07-14 — tasks/reminders merge: every doc in the legacy `reminders` collection is
copied into `tasks` (a reminder is now a task with a remind_at). Each migrated task KEEPS
its original ObjectId, so old inline-button callbacks and scheduled job ids still resolve;
that same id-reuse is what makes the migration idempotent (a reminder whose _id already
exists in tasks is skipped). The old collection is left untouched as a backup — nothing
reads it any more.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from agentzero.config import TIMEZONE
from agentzero.db import get_db

logger = logging.getLogger(__name__)


def _naive(dt: datetime | None) -> datetime | None:
    """Legacy reminders stored timezone-aware UTC; tasks store naive UTC."""
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


def _status_for(reminder_status: str | None) -> str:
    if reminder_status == "done":
        return "done"
    if reminder_status == "cancelled":
        return "cancelled"
    # pending / awaiting_ack / fired (legacy) / missing → still open work.
    return "open"


async def migrate_reminders_to_tasks() -> int:
    """Returns how many reminders were migrated this run (0 on every run after the first)."""
    from agentzero.executor import ensure_inbox_project

    db = get_db()
    reminders = await db.reminders.find({}).to_list(None)
    if not reminders:
        return 0

    existing_ids = {
        t["_id"] for t in await db.tasks.find({}, {"_id": 1}).to_list(None)
    }
    todo = [r for r in reminders if r["_id"] not in existing_ids]
    if not todo:
        return 0

    inbox = await ensure_inbox_project()
    migrated = 0
    for r in todo:
        remind_at = _naive(r.get("fire_at"))
        due_date = None
        if remind_at is not None:
            local = remind_at.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(TIMEZONE))
            due_date = datetime(local.year, local.month, local.day)
        # "fired" (legacy) had no fired_at; treat any non-pending active status as fired
        # at its fire time so the follow-up loop picks the nag back up.
        status = _status_for(r.get("status"))
        reminded_at = _naive(r.get("fired_at"))
        if reminded_at is None and status == "open" and r.get("status") in ("awaiting_ack", "fired"):
            reminded_at = remind_at
        await db.tasks.insert_one(
            {
                "_id": r["_id"],
                "project_id": inbox["_id"],
                "parent_task_id": None,
                "title": r.get("text") or "(untitled reminder)",
                "status": status,
                "due_date": due_date,
                "snoozed_until": None,
                "last_nudged_at": None,
                "remind_at": remind_at,
                "reminded_at": reminded_at,
                "next_nudge_at": _naive(r.get("next_nudge_at")),
                "nudge_count": r.get("nudge_count") or 0,
                "completed_at": _naive(r.get("completed_at")),
                "created_at": _naive(r.get("created_at")) or datetime.utcnow(),
                "updated_at": _naive(r.get("completed_at"))
                or _naive(r.get("created_at"))
                or datetime.utcnow(),
                "migrated_from": "reminders",
            }
        )
        migrated += 1
    logger.info("Migrated %d reminder(s) into the tasks collection", migrated)
    return migrated
