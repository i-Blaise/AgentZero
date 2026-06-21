"""Self-updating user model — synthesis, storage, prompt injection, refresh tool."""
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from agentzero import user_model
from agentzero.executor import execute_tool
from agentzero.llm import ToolCall

CHAT_ID = 999

MODEL_TEXT = (
    "WHO — Blaise, software engineer building several products.\n"
    "WORKING ON — Jobotron, Ama's RAG API.\n"
    "GOALS — ship products, earn more.\n"
    "PATTERNS — tends to let admin reminders pile up."
)


def _provider(reply):
    prov = MagicMock()
    prov.chat = AsyncMock(return_value=reply)
    return prov


async def _seed(mock_db):
    await mock_db.memory.insert_one({"chat_id": CHAT_ID, "content": "Building Jobotron"})
    proj = await mock_db.projects.insert_one(
        {"name": "Jobotron", "scope": "work", "created_at": datetime.utcnow(), "updated_at": datetime.utcnow()}
    )
    await mock_db.tasks.insert_one(
        {"project_id": proj.inserted_id, "title": "ship demo", "status": "open",
         "created_at": datetime.utcnow(), "updated_at": datetime.utcnow()}
    )


@pytest.mark.asyncio
async def test_synthesize_stores_model(mock_db):
    await _seed(mock_db)
    with patch("agentzero.user_model.get_provider", return_value=_provider(MODEL_TEXT)):
        out = await user_model.synthesize_user_model(CHAT_ID)
    assert out == MODEL_TEXT
    prof = await mock_db.profile.find_one({"chat_id": CHAT_ID})
    assert prof["user_model"] == MODEL_TEXT
    assert prof.get("user_model_updated_at") is not None


@pytest.mark.asyncio
async def test_get_user_model(mock_db):
    await mock_db.profile.insert_one({"chat_id": CHAT_ID, "user_model": MODEL_TEXT})
    assert await user_model.get_user_model(CHAT_ID) == MODEL_TEXT
    # empty / missing → None
    await mock_db.profile.delete_many({})
    assert await user_model.get_user_model(CHAT_ID) is None


@pytest.mark.asyncio
async def test_synthesis_failure_keeps_previous(mock_db):
    await mock_db.profile.insert_one({"chat_id": CHAT_ID, "user_model": "old model"})
    prov = MagicMock()
    prov.chat = AsyncMock(side_effect=RuntimeError("api down"))
    with patch("agentzero.user_model.get_provider", return_value=prov):
        out = await user_model.synthesize_user_model(CHAT_ID)
    assert out is None
    prof = await mock_db.profile.find_one({"chat_id": CHAT_ID})
    assert prof["user_model"] == "old model"  # not clobbered


@pytest.mark.asyncio
async def test_model_injected_into_system_prompt(mock_db):
    from agentzero.prompts import build_system_prompt

    await mock_db.profile.insert_one({"chat_id": CHAT_ID, "user_model": MODEL_TEXT})
    prompt = await build_system_prompt()
    assert "Your evolving read on the user" in prompt
    assert "tends to let admin reminders pile up" in prompt


@pytest.mark.asyncio
async def test_refresh_tool(mock_db):
    await _seed(mock_db)
    with patch("agentzero.user_model.get_provider", return_value=_provider(MODEL_TEXT)):
        out = await execute_tool(CHAT_ID, ToolCall(name="refresh_user_model", args={}))
    assert "Updated my read on you" in out
    assert "Jobotron" in out
