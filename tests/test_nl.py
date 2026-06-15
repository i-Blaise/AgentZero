"""
NL orchestration tests for _handle_nl — the agentic tool loop.

We fake the provider's run_tool_loop: it invokes the real `execute` callback
(so the executor actually runs against mock_db), then returns a LoopResult.
"""
import pytest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from agentzero.llm import LoopResult

CHAT_ID = 999


def _loop_provider(tool_calls, final_text=""):
    """Provider whose run_tool_loop runs the given (name, args) calls via `execute`."""
    prov = MagicMock()

    async def fake_loop(messages, system, tools, execute,
                        image=None, image_mime="image/jpeg", max_iters=6):
        results = []
        for name, args in tool_calls:
            results.append(await execute(name, args))
        return LoopResult(text=final_text, tool_calls_made=len(tool_calls), last_results=results)

    prov.run_tool_loop = fake_loop
    return prov


async def _seed_project(mock_db):
    await mock_db.projects.insert_one(
        {"name": "Work", "scope": "work",
         "created_at": datetime.utcnow(), "updated_at": datetime.utcnow()}
    )


def _patches(prov):
    return (
        patch("agentzero.main.ALLOWED_CHAT_ID", CHAT_ID),
        patch("agentzero.main.get_provider", return_value=prov),
        patch("agentzero.main.build_system_prompt", new_callable=AsyncMock, return_value="sys"),
        patch("agentzero.main.send", new_callable=AsyncMock),
    )


@pytest.mark.asyncio
async def test_loop_executes_tools_and_replies(mock_db):
    await _seed_project(mock_db)
    calls = [
        ("add_task", {"project_name": "Work", "title": "task A"}),
        ("add_task", {"project_name": "Work", "title": "task B"}),
    ]
    prov = _loop_provider(calls, final_text="Done — two on the Work pile.")
    from agentzero.main import _handle_nl

    p_chat, p_prov, p_sys, p_send = _patches(prov)
    with p_chat, p_prov, p_sys, p_send as mock_send:
        await _handle_nl(CHAT_ID, "add task A and task B to work")

    assert mock_send.call_args[0][1] == "Done — two on the Work pile."
    assert await mock_db.tasks.count_documents({"status": "open"}) == 2


@pytest.mark.asyncio
async def test_loop_empty_text_falls_back_to_results(mock_db):
    await _seed_project(mock_db)
    calls = [("add_task", {"project_name": "Work", "title": "task A"})]
    prov = _loop_provider(calls, final_text="")  # model gave no final prose
    from agentzero.main import _handle_nl

    p_chat, p_prov, p_sys, p_send = _patches(prov)
    with p_chat, p_prov, p_sys, p_send as mock_send:
        await _handle_nl(CHAT_ID, "add task A to work")

    sent = mock_send.call_args[0][1]
    assert "task A" in sent  # raw executor confirmation delivered as fallback


@pytest.mark.asyncio
async def test_loop_chitchat_no_tools_no_writes(mock_db):
    prov = _loop_provider([], final_text="Hey — what do you need?")
    from agentzero.main import _handle_nl

    p_chat, p_prov, p_sys, p_send = _patches(prov)
    with p_chat, p_prov, p_sys, p_send as mock_send:
        await _handle_nl(CHAT_ID, "hello")

    assert mock_send.call_args[0][1] == "Hey — what do you need?"
    assert await mock_db.tasks.count_documents({}) == 0
    assert await mock_db.projects.count_documents({}) == 0
