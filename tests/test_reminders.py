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


def _utc(dt: datetime) -> datetime:
    """mongomock can hand datetimes back naive; coerce to UTC for comparison."""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


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
    assert "no active reminders" in result.lower()


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
    # Fired reminders await the user's confirmation, not auto-done
    assert doc["status"] == "awaiting_ack"
    assert doc.get("next_nudge_at") is not None


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
    assert doc["status"] == "awaiting_ack"


@pytest.mark.asyncio
async def test_complete_reminder_stops_followups(mock_db):
    """Confirming a fired reminder marks it done so follow-ups stop."""
    rid = (await mock_db.reminders.insert_one(
        {"chat_id": CHAT_ID, "text": "submit the proposal", "fire_at": datetime.now(timezone.utc),
         "status": "awaiting_ack", "created_at": datetime.now(timezone.utc),
         "next_nudge_at": datetime.now(timezone.utc)}
    )).inserted_id
    with patch("agentzero.scheduler.get_scheduler"):
        result = await execute_tool(CHAT_ID, _tc("complete_reminder", query="proposal"))
    assert "done" in result.lower()
    doc = await mock_db.reminders.find_one({"_id": rid})
    assert doc["status"] == "done"


@pytest.mark.asyncio
async def test_followup_renudges_unacknowledged(mock_db):
    """The follow-up loop re-pings a fired-but-unconfirmed reminder when due."""
    from agentzero import scheduler

    past = datetime.now(timezone.utc) - timedelta(minutes=5)
    rid = (await mock_db.reminders.insert_one(
        {"chat_id": CHAT_ID, "text": "email the client", "fire_at": past,
         "status": "awaiting_ack", "created_at": past, "next_nudge_at": past, "nudge_count": 0}
    )).inserted_id

    prov = MagicMock()
    prov.chat = AsyncMock(return_value="⏰ Still need: email the client.")
    with patch("agentzero.autonomy._in_quiet_hours", return_value=False), \
         patch("agentzero.llm.get_provider", return_value=prov), \
         patch("agentzero.scheduler.send", new_callable=AsyncMock) as mock_send:
        await scheduler._reminder_followup_job(CHAT_ID)

    mock_send.assert_called_once()
    doc = await mock_db.reminders.find_one({"_id": rid})
    assert doc["nudge_count"] == 1
    assert doc["status"] == "awaiting_ack"  # still awaiting until user confirms


@pytest.mark.asyncio
async def test_followup_silent_in_quiet_hours(mock_db):
    from agentzero import scheduler

    past = datetime.now(timezone.utc) - timedelta(minutes=5)
    await mock_db.reminders.insert_one(
        {"chat_id": CHAT_ID, "text": "x", "fire_at": past, "status": "awaiting_ack",
         "created_at": past, "next_nudge_at": past, "nudge_count": 0}
    )
    with patch("agentzero.autonomy._in_quiet_hours", return_value=True), \
         patch("agentzero.scheduler.send", new_callable=AsyncMock) as mock_send:
        await scheduler._reminder_followup_job(CHAT_ID)
    mock_send.assert_not_called()


@pytest.mark.asyncio
async def test_followup_consolidates_backlog_into_one_message(mock_db):
    """Several due reminders produce ONE message, not a 5-8 message dump."""
    from agentzero import scheduler

    past = datetime.now(timezone.utc) - timedelta(minutes=5)
    for text in ("email client", "call bank", "submit invoice", "book flight"):
        await mock_db.reminders.insert_one(
            {"chat_id": CHAT_ID, "text": text, "fire_at": past, "status": "awaiting_ack",
             "created_at": past, "next_nudge_at": past, "nudge_count": 0}
        )

    prov = MagicMock()
    prov.chat = AsyncMock(return_value="⏰ Still open: email client; call bank; submit invoice; book flight.")
    with patch("agentzero.autonomy._in_quiet_hours", return_value=False), \
         patch("agentzero.llm.get_provider", return_value=prov), \
         patch("agentzero.scheduler.send", new_callable=AsyncMock) as mock_send:
        await scheduler._reminder_followup_job(CHAT_ID)

    mock_send.assert_called_once()  # the whole point: one message, not four
    # every due reminder advanced its nudge counter and next time
    async for doc in mock_db.reminders.find({"chat_id": CHAT_ID}):
        assert doc["nudge_count"] == 1
        assert _utc(doc["next_nudge_at"]) > past


@pytest.mark.asyncio
async def test_snooze_reminder_pushes_next_nudge(mock_db):
    """'remind me later' on a fired reminder pushes its next nudge out."""
    now = datetime.now(timezone.utc)
    rid = (await mock_db.reminders.insert_one(
        {"chat_id": CHAT_ID, "text": "call the plumber", "fire_at": now,
         "status": "awaiting_ack", "created_at": now, "next_nudge_at": now, "nudge_count": 1}
    )).inserted_id

    result = await execute_tool(CHAT_ID, _tc("snooze_reminder", query="plumber", minutes=90))
    assert "plumber" in result.lower()
    doc = await mock_db.reminders.find_one({"_id": rid})
    assert _utc(doc["next_nudge_at"]) > now + timedelta(minutes=80)


@pytest.mark.asyncio
async def test_snooze_reminder_all_when_no_query(mock_db):
    now = datetime.now(timezone.utc)
    for text in ("a", "b"):
        await mock_db.reminders.insert_one(
            {"chat_id": CHAT_ID, "text": text, "fire_at": now, "status": "awaiting_ack",
             "created_at": now, "next_nudge_at": now, "nudge_count": 0}
        )
    result = await execute_tool(CHAT_ID, _tc("snooze_reminder", minutes=30))
    assert "all" in result.lower()
    async for doc in mock_db.reminders.find({"chat_id": CHAT_ID}):
        assert _utc(doc["next_nudge_at"]) > now + timedelta(minutes=20)


@pytest.mark.asyncio
async def test_set_reminder_cadence_persists_and_clamps(mock_db):
    """'space them apart' stores a clamped per-chat cadence the loop reads."""
    result = await execute_tool(CHAT_ID, _tc("set_reminder_cadence", minutes=180))
    assert "every" in result.lower()
    state = await mock_db.system_state.find_one({"chat_id": CHAT_ID})
    assert state["nudge_interval_minutes"] == 180

    # below the floor → clamped up, not stored as-is
    await execute_tool(CHAT_ID, _tc("set_reminder_cadence", minutes=1))
    state = await mock_db.system_state.find_one({"chat_id": CHAT_ID})
    assert state["nudge_interval_minutes"] >= 30


@pytest.mark.asyncio
async def test_cancel_reminder(mock_db):
    with patch("agentzero.scheduler.schedule_reminder"):
        await execute_tool(CHAT_ID, _tc("set_reminder", text="dentist appointment", fire_at=_future_local(60)))
    with patch("agentzero.scheduler.get_scheduler"):
        result = await execute_tool(CHAT_ID, _tc("cancel_reminder", query="dentist"))
    assert "cancelled" in result.lower()
    doc = await mock_db.reminders.find_one({"text": "dentist appointment"})
    assert doc["status"] == "cancelled"
