"""
Tests for the 2026-07-14 reminders → tasks startup migration.

Every legacy reminder doc becomes a task in the Inbox project, keeping its ObjectId
(idempotency + old inline buttons keep working). The old collection is left untouched.
"""
import pytest
from datetime import datetime, timedelta, timezone

from agentzero.migrations import migrate_reminders_to_tasks

CHAT_ID = 999


def _aware_now() -> datetime:
    return datetime.now(timezone.utc)


async def _seed_legacy(db):
    now = _aware_now()
    docs = [
        {"chat_id": CHAT_ID, "text": "call the bank", "fire_at": now + timedelta(hours=2),
         "status": "pending", "created_at": now},
        {"chat_id": CHAT_ID, "text": "email the client", "fire_at": now - timedelta(hours=1),
         "status": "awaiting_ack", "fired_at": now - timedelta(hours=1),
         "next_nudge_at": now + timedelta(minutes=30), "nudge_count": 2, "created_at": now},
        {"chat_id": CHAT_ID, "text": "old fired ghost", "fire_at": now - timedelta(days=2),
         "status": "fired", "created_at": now - timedelta(days=2)},
        {"chat_id": CHAT_ID, "text": "submitted the report", "fire_at": now - timedelta(days=3),
         "status": "done", "completed_at": now - timedelta(days=3),
         "created_at": now - timedelta(days=4)},
        {"chat_id": CHAT_ID, "text": "abandoned thing", "fire_at": now - timedelta(days=5),
         "status": "cancelled", "created_at": now - timedelta(days=6)},
    ]
    ids = []
    for d in docs:
        ids.append((await db.reminders.insert_one(d)).inserted_id)
    return ids


@pytest.mark.asyncio
async def test_migration_maps_statuses_and_keeps_ids(mock_db):
    ids = await _seed_legacy(mock_db)
    n = await migrate_reminders_to_tasks()
    assert n == 5

    inbox = await mock_db.projects.find_one({"name": "Inbox"})
    assert inbox is not None

    pending = await mock_db.tasks.find_one({"_id": ids[0]})
    assert pending["status"] == "open"
    assert pending["title"] == "call the bank"
    assert pending["remind_at"] is not None and pending["remind_at"].tzinfo is None
    assert pending["reminded_at"] is None
    assert pending["due_date"] is not None
    assert pending["project_id"] == inbox["_id"]
    assert pending["migrated_from"] == "reminders"

    fired = await mock_db.tasks.find_one({"_id": ids[1]})
    assert fired["status"] == "open"
    assert fired["reminded_at"] is not None  # nag continues seamlessly
    assert fired["next_nudge_at"] is not None
    assert fired["nudge_count"] == 2

    legacy_fired = await mock_db.tasks.find_one({"_id": ids[2]})
    assert legacy_fired["status"] == "open"
    assert legacy_fired["reminded_at"] is not None  # no fired_at → falls back to fire time

    done = await mock_db.tasks.find_one({"_id": ids[3]})
    assert done["status"] == "done"
    assert done["completed_at"] is not None and done["completed_at"].tzinfo is None

    cancelled = await mock_db.tasks.find_one({"_id": ids[4]})
    assert cancelled["status"] == "cancelled"


@pytest.mark.asyncio
async def test_migration_idempotent_and_preserves_legacy(mock_db):
    await _seed_legacy(mock_db)
    assert await migrate_reminders_to_tasks() == 5
    assert await migrate_reminders_to_tasks() == 0  # second run: nothing to do
    assert await mock_db.tasks.count_documents({}) == 5
    # The legacy collection is a backup — never dropped or modified.
    assert await mock_db.reminders.count_documents({}) == 5


@pytest.mark.asyncio
async def test_migration_noop_on_empty_db(mock_db):
    assert await migrate_reminders_to_tasks() == 0
    assert await mock_db.projects.count_documents({}) == 0  # no gratuitous Inbox


@pytest.mark.asyncio
async def test_migrated_done_reminder_shows_in_recap(mock_db):
    """History survives: a reminder confirmed done last week shows up in the recap."""
    from agentzero.executor import execute_tool
    from agentzero.llm import ToolCall

    now = _aware_now()
    await mock_db.reminders.insert_one(
        {"chat_id": CHAT_ID, "text": "renewed the domain", "fire_at": now - timedelta(days=2),
         "status": "done", "completed_at": now - timedelta(days=2),
         "created_at": now - timedelta(days=3)}
    )
    await migrate_reminders_to_tasks()
    recap = await execute_tool(CHAT_ID, ToolCall(name="get_recap", args={"days": 7}))
    assert "renewed the domain" in recap
    assert "Inbox" in recap


@pytest.mark.asyncio
async def test_migrated_pending_reminder_closeable_by_phrase(mock_db):
    from unittest.mock import patch

    from agentzero.executor import execute_tool
    from agentzero.llm import ToolCall

    now = _aware_now()
    rid = (await mock_db.reminders.insert_one(
        {"chat_id": CHAT_ID, "text": "Study for assessment with Hivemind",
         "fire_at": now + timedelta(hours=3), "status": "pending", "created_at": now}
    )).inserted_id
    await migrate_reminders_to_tasks()
    with patch("agentzero.scheduler.get_scheduler"):
        result = await execute_tool(
            CHAT_ID, ToolCall(name="mark_done", args={"task_query": "hivemind assessment"})
        )
    assert "done" in result.lower()
    task = await mock_db.tasks.find_one({"_id": rid})
    assert task["status"] == "done"
