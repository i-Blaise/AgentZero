"""
Reminder tests — set / list / cancel, plus the past-time guard.

The scheduler's add_job is patched out so no real APScheduler runs during tests;
we only verify the DB state and confirmation strings.
"""
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo
from bson import ObjectId

from agentzero.config import TIMEZONE
from agentzero.executor import execute_tool
from agentzero.llm import ToolCall

CHAT_ID = 999


def _tc(tool: str, **kwargs) -> ToolCall:
    return ToolCall(name=tool, args=kwargs)


def _future_local(minutes: int = 5) -> str:
    now = datetime.now(ZoneInfo(TIMEZONE))
    return (now + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S")


def _past_local(minutes: int = 5) -> str:
    now = datetime.now(ZoneInfo(TIMEZONE))
    return (now - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S")


@pytest.mark.asyncio
async def test_set_reminder(mock_db):
    with patch("agentzero.scheduler.schedule_reminder") as mock_sched:
        result = await execute_tool(
            CHAT_ID, _tc("set_reminder", text="take a break", fire_at=_future_local(2))
        )
    assert "remind you to take a break" in result.lower()
    doc = await mock_db.reminders.find_one({"text": "take a break"})
    assert doc is not None
    assert doc["status"] == "pending"
    mock_sched.assert_called_once()


@pytest.mark.asyncio
async def test_set_reminder_past_time(mock_db):
    with patch("agentzero.scheduler.schedule_reminder"):
        result = await execute_tool(
            CHAT_ID, _tc("set_reminder", text="too late", fire_at=_past_local(10))
        )
    assert "past" in result.lower()
    assert await mock_db.reminders.find_one({"text": "too late"}) is None


@pytest.mark.asyncio
async def test_list_reminders(mock_db):
    with patch("agentzero.scheduler.schedule_reminder"):
        await execute_tool(CHAT_ID, _tc("set_reminder", text="call mum", fire_at=_future_local(30)))
        await execute_tool(CHAT_ID, _tc("set_reminder", text="stretch", fire_at=_future_local(10)))
    result = await execute_tool(CHAT_ID, _tc("list_reminders"))
    assert "call mum" in result
    assert "stretch" in result


@pytest.mark.asyncio
async def test_list_reminders_empty(mock_db):
    result = await execute_tool(CHAT_ID, _tc("list_reminders"))
    assert "no upcoming reminders" in result.lower()


@pytest.mark.asyncio
async def test_fire_reminder_uses_personality(mock_db):
    """Firing renders the text through the LLM and marks the reminder fired."""
    from agentzero import scheduler

    rid = (await mock_db.reminders.insert_one(
        {"chat_id": CHAT_ID, "text": "take a break", "fire_at": datetime.now(timezone.utc),
         "status": "pending", "created_at": datetime.now(timezone.utc)}
    )).inserted_id

    prov = MagicMock()
    prov.chat = AsyncMock(return_value="⏰ Your spine called — take a break.")
    with patch("agentzero.llm.get_provider", return_value=prov), \
         patch("agentzero.scheduler.send", new_callable=AsyncMock) as mock_send:
        await scheduler._fire_reminder(str(rid), CHAT_ID, "take a break")

    sent = mock_send.call_args[0][1]
    assert "take a break" in sent.lower()
    assert sent != "⏰ Reminder: take a break"  # it went through the voice
    doc = await mock_db.reminders.find_one({"_id": rid})
    assert doc["status"] == "fired"


@pytest.mark.asyncio
async def test_fire_reminder_falls_back_on_llm_error(mock_db):
    """If the LLM call fails, the plain reminder still goes out."""
    from agentzero import scheduler

    rid = (await mock_db.reminders.insert_one(
        {"chat_id": CHAT_ID, "text": "call the bank", "fire_at": datetime.now(timezone.utc),
         "status": "pending", "created_at": datetime.now(timezone.utc)}
    )).inserted_id

    prov = MagicMock()
    prov.chat = AsyncMock(side_effect=RuntimeError("api down"))
    with patch("agentzero.llm.get_provider", return_value=prov), \
         patch("agentzero.scheduler.send", new_callable=AsyncMock) as mock_send:
        await scheduler._fire_reminder(str(rid), CHAT_ID, "call the bank")

    assert mock_send.call_args[0][1] == "⏰ Reminder: call the bank"
    doc = await mock_db.reminders.find_one({"_id": rid})
    assert doc["status"] == "fired"


@pytest.mark.asyncio
async def test_cancel_reminder(mock_db):
    with patch("agentzero.scheduler.schedule_reminder"):
        await execute_tool(CHAT_ID, _tc("set_reminder", text="dentist appointment", fire_at=_future_local(60)))
    with patch("agentzero.scheduler.get_scheduler"):
        result = await execute_tool(CHAT_ID, _tc("cancel_reminder", query="dentist"))
    assert "cancelled" in result.lower()
    doc = await mock_db.reminders.find_one({"text": "dentist appointment"})
    assert doc["status"] == "cancelled"
