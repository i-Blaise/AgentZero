"""
Phase 1 acceptance tests (AC 1-4, 7).

AC 1  — NL "add deploy task to work" → project created (or reused), task added, confirmed
AC 2  — NL "mark deploy done" → fuzzy match, task marked done, confirmed
AC 3  — /undo → reverses last operation
AC 4  — get_status → formatted project/task list
AC 7  — non-actionable message → LLM replies conversationally, no DB writes

All tests use mongomock-motor (injected via conftest) and mock the LLM provider
so no real API calls are made.
"""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from bson import ObjectId

from agentzero.executor import execute_tool, undo_last, get_status
from agentzero.llm import ToolCall, LLMResponse

CHAT_ID = 999


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tc(tool: str, **kwargs) -> ToolCall:
    return ToolCall(name=tool, args=kwargs)


# ---------------------------------------------------------------------------
# AC 1 — create project + add task
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_project(mock_db):
    result = await execute_tool(CHAT_ID, _tc("create_project", name="Work Stuff", scope="work"))
    assert 'Created project "Work Stuff"' in result
    doc = await mock_db.projects.find_one({"name": "Work Stuff"})
    assert doc is not None
    assert doc["scope"] == "work"


@pytest.mark.asyncio
async def test_create_project_duplicate(mock_db):
    await execute_tool(CHAT_ID, _tc("create_project", name="Work Stuff", scope="work"))
    result = await execute_tool(CHAT_ID, _tc("create_project", name="Work Stuff", scope="work"))
    assert "already exists" in result
    count = await mock_db.projects.count_documents({"name": "Work Stuff"})
    assert count == 1


@pytest.mark.asyncio
async def test_add_task(mock_db):
    await execute_tool(CHAT_ID, _tc("create_project", name="Work Stuff", scope="work"))
    result = await execute_tool(CHAT_ID, _tc("add_task", project_name="Work Stuff", title="deploy"))
    assert 'Added "deploy"' in result
    task = await mock_db.tasks.find_one({"title": "deploy"})
    assert task is not None
    assert task["status"] == "open"


@pytest.mark.asyncio
async def test_add_task_project_not_found(mock_db):
    result = await execute_tool(CHAT_ID, _tc("add_task", project_name="ghost", title="thing"))
    assert "not found" in result.lower()


@pytest.mark.asyncio
async def test_add_task_fuzzy_project_match(mock_db):
    await execute_tool(CHAT_ID, _tc("create_project", name="Work Stuff", scope="work"))
    result = await execute_tool(CHAT_ID, _tc("add_task", project_name="work stuff", title="deploy"))
    assert 'Added "deploy"' in result


# ---------------------------------------------------------------------------
# AC 2 — mark done with fuzzy match
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mark_done(mock_db):
    await execute_tool(CHAT_ID, _tc("create_project", name="Work", scope="work"))
    await execute_tool(CHAT_ID, _tc("add_task", project_name="Work", title="deploy backend"))
    result = await execute_tool(CHAT_ID, _tc("mark_done", task_query="deploy backend"))
    assert "Done" in result
    task = await mock_db.tasks.find_one({"title": "deploy backend"})
    assert task["status"] == "done"


@pytest.mark.asyncio
async def test_mark_done_fuzzy(mock_db):
    await execute_tool(CHAT_ID, _tc("create_project", name="Work", scope="work"))
    await execute_tool(CHAT_ID, _tc("add_task", project_name="Work", title="deploy backend"))
    result = await execute_tool(CHAT_ID, _tc("mark_done", task_query="deploy"))
    assert "Done" in result


@pytest.mark.asyncio
async def test_mark_done_not_found(mock_db):
    result = await execute_tool(CHAT_ID, _tc("mark_done", task_query="nonexistent task xyz"))
    assert "No open task" in result


@pytest.mark.asyncio
async def test_mark_done_ambiguous(mock_db):
    await execute_tool(CHAT_ID, _tc("create_project", name="Work", scope="work"))
    await execute_tool(CHAT_ID, _tc("add_task", project_name="Work", title="meeting with Alice"))
    await execute_tool(CHAT_ID, _tc("add_task", project_name="Work", title="meeting with Bob"))
    result = await execute_tool(CHAT_ID, _tc("mark_done", task_query="meeting"))
    assert "more specific" in result.lower()
    # Both tasks still open
    count = await mock_db.tasks.count_documents({"status": "open"})
    assert count == 2


# ---------------------------------------------------------------------------
# AC 3 — /undo
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_undo_create_project(mock_db):
    await execute_tool(CHAT_ID, _tc("create_project", name="Temp", scope="personal"))
    assert await mock_db.projects.find_one({"name": "Temp"}) is not None
    result = await undo_last(CHAT_ID)
    assert "Undid" in result
    assert await mock_db.projects.find_one({"name": "Temp"}) is None


@pytest.mark.asyncio
async def test_undo_add_task(mock_db):
    await execute_tool(CHAT_ID, _tc("create_project", name="Work", scope="work"))
    await execute_tool(CHAT_ID, _tc("add_task", project_name="Work", title="to-undo task"))
    assert await mock_db.tasks.find_one({"title": "to-undo task"}) is not None
    await undo_last(CHAT_ID)
    assert await mock_db.tasks.find_one({"title": "to-undo task"}) is None


@pytest.mark.asyncio
async def test_undo_mark_done(mock_db):
    await execute_tool(CHAT_ID, _tc("create_project", name="Work", scope="work"))
    await execute_tool(CHAT_ID, _tc("add_task", project_name="Work", title="finish report"))
    await execute_tool(CHAT_ID, _tc("mark_done", task_query="finish report"))
    task = await mock_db.tasks.find_one({"title": "finish report"})
    assert task["status"] == "done"
    await undo_last(CHAT_ID)
    task = await mock_db.tasks.find_one({"title": "finish report"})
    assert task["status"] == "open"


@pytest.mark.asyncio
async def test_undo_nothing(mock_db):
    result = await undo_last(CHAT_ID)
    assert "Nothing to undo" in result


# ---------------------------------------------------------------------------
# AC 4 — get_status
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_status_empty(mock_db):
    result = await get_status()
    assert "No projects" in result


@pytest.mark.asyncio
async def test_get_status_with_data(mock_db):
    await execute_tool(CHAT_ID, _tc("create_project", name="Work", scope="work"))
    await execute_tool(CHAT_ID, _tc("add_task", project_name="Work", title="task one"))
    await execute_tool(CHAT_ID, _tc("add_task", project_name="Work", title="task two"))
    result = await get_status()
    assert "Work" in result
    assert "task one" in result
    assert "task two" in result
    assert "2 open" in result


@pytest.mark.asyncio
async def test_get_status_scope_filter(mock_db):
    await execute_tool(CHAT_ID, _tc("create_project", name="Work", scope="work"))
    await execute_tool(CHAT_ID, _tc("create_project", name="Home", scope="personal"))
    work_result = await get_status(scope="work")
    assert "Work" in work_result
    assert "Home" not in work_result


# ---------------------------------------------------------------------------
# AC 7 — non-actionable message (no DB writes from LLM path)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_db_writes_for_chitchat(mock_db):
    """When the LLM returns no tool calls, the executor is never called."""
    from agentzero.llm import LLMResponse
    from agentzero.main import process_update

    mock_response = LLMResponse(content="Sure, how can I help?", tool_calls=[])
    mock_prov = MagicMock()
    mock_prov.chat_with_tools = AsyncMock(return_value=mock_response)

    update = MagicMock()
    update.message.chat_id = CHAT_ID
    update.message.text = "Hey, how are you?"

    with patch("agentzero.main.ALLOWED_CHAT_ID", CHAT_ID), \
         patch("agentzero.main.get_provider", return_value=mock_prov), \
         patch("agentzero.main.build_system_prompt", new_callable=AsyncMock, return_value="sys"), \
         patch("agentzero.main.send", new_callable=AsyncMock) as mock_send:
        await process_update(update)
        mock_send.assert_called_once()
        sent_text = mock_send.call_args[0][1]
        assert sent_text  # non-empty reply

    # No projects or tasks written
    assert await mock_db.projects.count_documents({}) == 0
    assert await mock_db.tasks.count_documents({}) == 0
