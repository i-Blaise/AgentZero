"""
Autonomy heartbeat tests.

The LLM provider is mocked so we control the SILENT-vs-message decision.
gather_candidates is exercised against real (mock) DB data.
"""
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from agentzero import autonomy
from agentzero.executor import execute_tool
from agentzero.llm import ToolCall

CHAT_ID = 999


def _tc(tool: str, **kwargs) -> ToolCall:
    return ToolCall(name=tool, args=kwargs)


def _mock_provider(reply: str):
    prov = AsyncMock()
    prov.chat = AsyncMock(return_value=reply)
    return prov


async def _seed_overdue_task(mock_db):
    """Create a project + an overdue open task directly in the DB."""
    proj = await mock_db.projects.insert_one(
        {"name": "Work", "scope": "work", "created_at": datetime.utcnow(), "updated_at": datetime.utcnow()}
    )
    past = datetime.utcnow() - timedelta(days=3)
    await mock_db.tasks.insert_one(
        {
            "project_id": proj.inserted_id,
            "title": "file the report",
            "status": "open",
            "due_date": past,
            "snoozed_until": None,
            "last_nudged_at": None,
            "created_at": datetime.utcnow() - timedelta(days=10),
            "updated_at": datetime.utcnow(),
        }
    )


@pytest.mark.asyncio
async def test_heartbeat_sends_when_overdue(mock_db):
    await _seed_overdue_task(mock_db)
    with patch("agentzero.autonomy.get_provider", return_value=_mock_provider("You've got an overdue report — want to knock it out?")), \
         patch("agentzero.autonomy.send", new_callable=AsyncMock) as mock_send:
        result = await autonomy.run_heartbeat(CHAT_ID, force=True)
    assert result is not None
    mock_send.assert_called_once()


@pytest.mark.asyncio
async def test_heartbeat_silent(mock_db):
    await _seed_overdue_task(mock_db)
    with patch("agentzero.autonomy.get_provider", return_value=_mock_provider("SILENT")), \
         patch("agentzero.autonomy.send", new_callable=AsyncMock) as mock_send:
        result = await autonomy.run_heartbeat(CHAT_ID, force=True)
    assert result is None
    mock_send.assert_not_called()


@pytest.mark.asyncio
async def test_heartbeat_nothing_to_consider(mock_db):
    """No tasks and no memory → returns None without calling the LLM."""
    with patch("agentzero.autonomy.get_provider") as mock_get:
        result = await autonomy.run_heartbeat(CHAT_ID, force=True)
    assert result is None
    mock_get.assert_not_called()


@pytest.mark.asyncio
async def test_cooldown_blocks_nudge(mock_db):
    await _seed_overdue_task(mock_db)
    # Record a very recent nudge
    await mock_db.system_state.insert_one(
        {"chat_id": CHAT_ID, "last_proactive_nudge_at": datetime.now(timezone.utc)}
    )
    with patch("agentzero.autonomy.get_provider") as mock_get, \
         patch("agentzero.autonomy.send", new_callable=AsyncMock):
        result = await autonomy.run_heartbeat(CHAT_ID, force=False)
    assert result is None
    mock_get.assert_not_called()


@pytest.mark.asyncio
async def test_quiet_hours_blocks_nudge(mock_db):
    await _seed_overdue_task(mock_db)
    # Force "now" into quiet hours by patching the quiet-hours check
    with patch("agentzero.autonomy._in_quiet_hours", return_value=True), \
         patch("agentzero.autonomy.get_provider") as mock_get:
        result = await autonomy.run_heartbeat(CHAT_ID, force=False)
    assert result is None
    mock_get.assert_not_called()


@pytest.mark.asyncio
async def test_force_bypasses_cooldown(mock_db):
    await _seed_overdue_task(mock_db)
    await mock_db.system_state.insert_one(
        {"chat_id": CHAT_ID, "last_proactive_nudge_at": datetime.now(timezone.utc)}
    )
    with patch("agentzero.autonomy.get_provider", return_value=_mock_provider("Heads up on that report.")), \
         patch("agentzero.autonomy.send", new_callable=AsyncMock) as mock_send:
        result = await autonomy.run_heartbeat(CHAT_ID, force=True)
    assert result is not None
    mock_send.assert_called_once()


@pytest.mark.asyncio
async def test_recently_nudged_task_excluded(mock_db):
    """A task nudged < 24h ago should not appear as a candidate."""
    proj = await mock_db.projects.insert_one(
        {"name": "Work", "scope": "work", "created_at": datetime.utcnow(), "updated_at": datetime.utcnow()}
    )
    await mock_db.tasks.insert_one(
        {
            "project_id": proj.inserted_id,
            "title": "already nudged",
            "status": "open",
            "due_date": datetime.utcnow() - timedelta(days=2),
            "snoozed_until": None,
            "last_nudged_at": datetime.now(timezone.utc) - timedelta(hours=1),
            "created_at": datetime.utcnow() - timedelta(days=5),
            "updated_at": datetime.utcnow(),
        }
    )
    candidates = await autonomy.gather_candidates(CHAT_ID)
    assert candidates["overdue"] == []
