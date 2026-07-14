"""Recurring (cron-style) reminders: set / list / cancel."""
import pytest
from unittest.mock import patch

from agentzero.executor import execute_tool
from agentzero.llm import ToolCall

CHAT_ID = 999


def _tc(tool: str, **kwargs) -> ToolCall:
    return ToolCall(name=tool, args=kwargs)


@pytest.mark.asyncio
async def test_set_recurring_reminder(mock_db):
    with patch("agentzero.scheduler.schedule_recurring_reminder") as sched:
        result = await execute_tool(
            CHAT_ID,
            _tc("set_recurring_reminder", text="send invoices", hour=8, minute=0, day_of_week="mon-fri"),
        )
    assert "every weekday at 08:00" in result
    sched.assert_called_once()
    doc = await mock_db.recurring_reminders.find_one({"text": "send invoices"})
    assert doc["active"] is True
    assert doc["hour"] == 8 and doc["day_of_week"] == "mon-fri"


@pytest.mark.asyncio
async def test_set_recurring_rejects_bad_hour(mock_db):
    with patch("agentzero.scheduler.schedule_recurring_reminder"):
        result = await execute_tool(
            CHAT_ID, _tc("set_recurring_reminder", text="x", hour=99)
        )
    assert "hour" in result.lower()
    assert await mock_db.recurring_reminders.find_one({"text": "x"}) is None


@pytest.mark.asyncio
async def test_list_includes_recurring(mock_db):
    with patch("agentzero.scheduler.schedule_recurring_reminder"):
        await execute_tool(CHAT_ID, _tc("set_recurring_reminder", text="standup", hour=9, day_of_week="*"))
    out = await execute_tool(CHAT_ID, _tc("list_reminders"))
    assert "Recurring:" in out
    assert "standup" in out
    assert "every day at 09:00" in out


@pytest.mark.asyncio
async def test_cancel_recurring(mock_db):
    with patch("agentzero.scheduler.schedule_recurring_reminder"):
        await execute_tool(CHAT_ID, _tc("set_recurring_reminder", text="weekly review", hour=17, day_of_week="fri"))
    with patch("agentzero.scheduler.get_scheduler"):
        result = await execute_tool(CHAT_ID, _tc("cancel_task", query="weekly review"))
    assert "cancelled" in result.lower()
    doc = await mock_db.recurring_reminders.find_one({"text": "weekly review"})
    assert doc["active"] is False
