"""
Memory tests — remember / forget / dedupe / undo-restore.
"""
import pytest

from agentzero.executor import execute_tool, undo_last
from agentzero.llm import ToolCall

CHAT_ID = 999


def _tc(tool: str, **kwargs) -> ToolCall:
    return ToolCall(name=tool, args=kwargs)


@pytest.mark.asyncio
async def test_remember(mock_db):
    result = await execute_tool(
        CHAT_ID, _tc("remember", content="Blaise prefers morning meetings", category="preference")
    )
    assert "noted" in result.lower()
    doc = await mock_db.memory.find_one({"content": "Blaise prefers morning meetings"})
    assert doc is not None
    assert doc["category"] == "preference"


@pytest.mark.asyncio
async def test_remember_dedupe(mock_db):
    await execute_tool(CHAT_ID, _tc("remember", content="My passport expires in August 2027"))
    await execute_tool(CHAT_ID, _tc("remember", content="My passport expires in August 2027"))
    count = await mock_db.memory.count_documents({})
    assert count == 1


@pytest.mark.asyncio
async def test_forget(mock_db):
    await execute_tool(CHAT_ID, _tc("remember", content="I drink oat milk"))
    result = await execute_tool(CHAT_ID, _tc("forget", query="oat milk"))
    assert "forgot" in result.lower()
    assert await mock_db.memory.find_one({"content": "I drink oat milk"}) is None


@pytest.mark.asyncio
async def test_forget_nothing(mock_db):
    result = await execute_tool(CHAT_ID, _tc("forget", query="anything"))
    assert "don't have anything" in result.lower()


@pytest.mark.asyncio
async def test_undo_forget_restores(mock_db):
    """Undoing a forget should bring the memory back."""
    await execute_tool(CHAT_ID, _tc("remember", content="I work best after coffee"))
    await execute_tool(CHAT_ID, _tc("forget", query="coffee"))
    assert await mock_db.memory.find_one({"content": "I work best after coffee"}) is None

    await undo_last(CHAT_ID)
    restored = await mock_db.memory.find_one({"content": "I work best after coffee"})
    assert restored is not None


@pytest.mark.asyncio
async def test_undo_remember_removes(mock_db):
    await execute_tool(CHAT_ID, _tc("remember", content="temporary fact"))
    await undo_last(CHAT_ID)
    assert await mock_db.memory.find_one({"content": "temporary fact"}) is None
