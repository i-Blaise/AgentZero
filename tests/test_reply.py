"""Reply-to-message awareness: when the user replies to a specific message, the bot
sees the quoted text."""
import pytest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from agentzero.llm import LoopResult
from agentzero.main import _quoted_context, _handle_nl

CHAT_ID = 999


def _msg(reply_to=None):
    return SimpleNamespace(reply_to_message=reply_to)


def _quoted(text=None, caption=None, photo=None, voice=None, is_bot=False):
    return SimpleNamespace(
        text=text, caption=caption, photo=photo, voice=voice,
        from_user=SimpleNamespace(is_bot=is_bot),
    )


def test_no_reply_returns_none():
    assert _quoted_context(_msg()) is None


def test_reply_to_user_message():
    out = _quoted_context(_msg(_quoted(text="Buy milk on the way home")))
    assert "an earlier message" in out
    assert "Buy milk on the way home" in out


def test_reply_to_bot_message():
    out = _quoted_context(_msg(_quoted(text="You've got 3 overdue tasks", is_bot=True)))
    assert "your own earlier message" in out
    assert "3 overdue tasks" in out


def test_reply_to_photo_without_caption():
    out = _quoted_context(_msg(_quoted(photo=["fileid"])))
    assert "[an image]" in out


def _loop_provider():
    prov = MagicMock()

    async def fake_loop(messages, system, tools, execute,
                        image=None, image_mime="image/jpeg", max_iters=6):
        return LoopResult(text="ok", tool_calls_made=0, last_results=[])

    prov.run_tool_loop = fake_loop
    return prov


@pytest.mark.asyncio
async def test_reply_context_reaches_history(mock_db):
    """The quoted message is prepended to the stored user turn so the model sees it."""
    prov = _loop_provider()
    with patch("agentzero.main.ALLOWED_CHAT_ID", CHAT_ID), \
         patch("agentzero.main.get_provider", return_value=prov), \
         patch("agentzero.main.build_system_prompt", new_callable=AsyncMock, return_value="sys"), \
         patch("agentzero.main.send", new_callable=AsyncMock):
        await _handle_nl(CHAT_ID, "what's this about?",
                         reply_to='an earlier message: "Invoice #42 is overdue"')

    doc = await mock_db.chat_history.find_one({"chat_id": CHAT_ID, "role": "user"})
    assert "Replying to" in doc["content"]
    assert "Invoice #42 is overdue" in doc["content"]
    assert "what's this about?" in doc["content"]
