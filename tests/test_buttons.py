"""Inline button taps (callback queries) → by-id reminder/task actions."""
import pytest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from agentzero import main
from agentzero.telegram_io import _markup

CHAT_ID = 999


def _utc(dt):
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _cq(data, chat_id=CHAT_ID):
    return SimpleNamespace(
        id="cb1",
        data=data,
        message=SimpleNamespace(chat_id=chat_id, message_id=7, text="⏰ do the thing"),
        from_user=SimpleNamespace(id=chat_id),
    )


def _fake_bot():
    bot = MagicMock()
    bot.answer_callback_query = AsyncMock()
    bot.edit_message_text = AsyncMock()
    return bot


def test_markup_builds_inline_keyboard():
    markup = _markup([("✅ Done", "rem:done:1"), ("⏰ 1h", "rem:snz:1:60")])
    row = markup.inline_keyboard[0]
    assert [b.text for b in row] == ["✅ Done", "⏰ 1h"]
    assert [b.callback_data for b in row] == ["rem:done:1", "rem:snz:1:60"]


def test_no_markup_when_empty():
    assert _markup(None) is None


@pytest.mark.asyncio
async def test_done_button_completes_reminder(mock_db):
    rid = (await mock_db.reminders.insert_one(
        {"chat_id": CHAT_ID, "text": "call the bank", "fire_at": datetime.now(timezone.utc),
         "status": "awaiting_ack", "created_at": datetime.now(timezone.utc),
         "next_nudge_at": datetime.now(timezone.utc)}
    )).inserted_id

    bot = _fake_bot()
    with patch("agentzero.main.ALLOWED_CHAT_ID", CHAT_ID), \
         patch("agentzero.main.get_bot", return_value=bot), \
         patch("agentzero.scheduler.get_scheduler"):
        await main._handle_callback(_cq(f"rem:done:{rid}"))

    doc = await mock_db.reminders.find_one({"_id": rid})
    assert doc["status"] == "done"
    bot.answer_callback_query.assert_awaited_once()
    bot.edit_message_text.assert_awaited_once()  # keyboard stripped + annotated


@pytest.mark.asyncio
async def test_snooze_button_pushes_next_nudge(mock_db):
    now = datetime.now(timezone.utc)
    rid = (await mock_db.reminders.insert_one(
        {"chat_id": CHAT_ID, "text": "stretch", "fire_at": now, "status": "awaiting_ack",
         "created_at": now, "next_nudge_at": now}
    )).inserted_id

    bot = _fake_bot()
    with patch("agentzero.main.ALLOWED_CHAT_ID", CHAT_ID), \
         patch("agentzero.main.get_bot", return_value=bot):
        await main._handle_callback(_cq(f"rem:snz:{rid}:60"))

    doc = await mock_db.reminders.find_one({"_id": rid})
    assert _utc(doc["next_nudge_at"]) > now + timedelta(minutes=50)
    assert doc["status"] == "awaiting_ack"  # snoozed, not completed


@pytest.mark.asyncio
async def test_task_done_button(mock_db):
    proj = await mock_db.projects.insert_one(
        {"name": "Work", "scope": "work", "created_at": datetime.utcnow(), "updated_at": datetime.utcnow()}
    )
    tid = (await mock_db.tasks.insert_one(
        {"project_id": proj.inserted_id, "title": "file report", "status": "open",
         "due_date": None, "created_at": datetime.utcnow(), "updated_at": datetime.utcnow()}
    )).inserted_id

    bot = _fake_bot()
    with patch("agentzero.main.ALLOWED_CHAT_ID", CHAT_ID), \
         patch("agentzero.main.get_bot", return_value=bot):
        await main._handle_callback(_cq(f"tsk:done:{tid}"))

    doc = await mock_db.tasks.find_one({"_id": tid})
    assert doc["status"] == "done"


@pytest.mark.asyncio
async def test_callback_from_wrong_chat_ignored(mock_db):
    bot = _fake_bot()
    with patch("agentzero.main.ALLOWED_CHAT_ID", CHAT_ID), \
         patch("agentzero.main.get_bot", return_value=bot):
        await main._handle_callback(_cq("rem:done:abc", chat_id=111))
    # acknowledged but no action attempted
    bot.answer_callback_query.assert_awaited_once()
    bot.edit_message_text.assert_not_awaited()
