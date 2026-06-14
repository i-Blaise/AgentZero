"""
NL orchestration tests for _handle_nl — tool results get narrated in voice,
with a fallback to raw results if narration fails.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from agentzero.llm import LLMResponse, ToolCall

CHAT_ID = 999


def _provider(tool_calls, narration="All set — added them.", narration_error=False):
    prov = MagicMock()
    prov.chat_with_tools = AsyncMock(
        return_value=LLMResponse(content=None, tool_calls=tool_calls)
    )
    if narration_error:
        prov.chat = AsyncMock(side_effect=RuntimeError("api down"))
    else:
        prov.chat = AsyncMock(return_value=narration)
    return prov


async def _seed_project(mock_db):
    await mock_db.projects.insert_one(
        {"name": "Work", "scope": "work", "created_at": __import__("datetime").datetime.utcnow(),
         "updated_at": __import__("datetime").datetime.utcnow()}
    )


@pytest.mark.asyncio
async def test_tool_results_are_narrated(mock_db):
    await _seed_project(mock_db)
    calls = [
        ToolCall(name="add_task", args={"project_name": "Work", "title": "task A"}),
        ToolCall(name="add_task", args={"project_name": "Work", "title": "task B"}),
    ]
    prov = _provider(calls, narration="Done — two things on the Work pile now.")

    from agentzero.main import _handle_nl

    with patch("agentzero.main.ALLOWED_CHAT_ID", CHAT_ID), \
         patch("agentzero.main.get_provider", return_value=prov), \
         patch("agentzero.main.build_system_prompt", new_callable=AsyncMock, return_value="sys"), \
         patch("agentzero.main.send", new_callable=AsyncMock) as mock_send:
        await _handle_nl(CHAT_ID, "add task A and task B to work")

    # The narrated reply is what got sent, not the raw "Added ..." lines
    sent = mock_send.call_args[0][1]
    assert sent == "Done — two things on the Work pile now."
    prov.chat.assert_awaited_once()
    # Tasks really were created
    assert await mock_db.tasks.count_documents({"status": "open"}) == 2


@pytest.mark.asyncio
async def test_narration_falls_back_to_raw_on_error(mock_db):
    await _seed_project(mock_db)
    calls = [ToolCall(name="add_task", args={"project_name": "Work", "title": "task A"})]
    prov = _provider(calls, narration_error=True)

    from agentzero.main import _handle_nl

    with patch("agentzero.main.ALLOWED_CHAT_ID", CHAT_ID), \
         patch("agentzero.main.get_provider", return_value=prov), \
         patch("agentzero.main.build_system_prompt", new_callable=AsyncMock, return_value="sys"), \
         patch("agentzero.main.send", new_callable=AsyncMock) as mock_send:
        await _handle_nl(CHAT_ID, "add task A to work")

    sent = mock_send.call_args[0][1]
    assert "task A" in sent  # raw executor confirmation still delivered
