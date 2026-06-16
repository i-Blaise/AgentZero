"""The 'working on it' filler now fires ONLY when the bot hits the internet
(web_search / web_fetch) — not on fast local replies like a transcribed voice note."""
import pytest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from agentzero.llm import LoopResult
from agentzero.main import _handle_nl
from agentzero.prompts import THINKING_FILLERS

CHAT_ID = 999


def _loop_provider(tool_calls, final_text="done"):
    """run_tool_loop that invokes the real execute callback for each (name, args)."""
    prov = MagicMock()

    async def fake_loop(messages, system, tools, execute,
                        image=None, image_mime="image/jpeg", max_iters=6):
        for name, args in tool_calls:
            await execute(name, args)
        return LoopResult(text=final_text, tool_calls_made=len(tool_calls), last_results=[])

    prov.run_tool_loop = fake_loop
    return prov


def _patches(prov):
    return (
        patch("agentzero.main.ALLOWED_CHAT_ID", CHAT_ID),
        patch("agentzero.main.get_provider", return_value=prov),
        patch("agentzero.main.build_system_prompt", new_callable=AsyncMock, return_value="sys"),
        patch("agentzero.web.web_search", new_callable=AsyncMock, return_value="results"),
        patch("agentzero.main.send", new_callable=AsyncMock),
    )


@pytest.mark.asyncio
async def test_filler_fires_on_web_search(mock_db):
    prov = _loop_provider([("web_search", {"query": "usd to ghs"})], final_text="≈ 12 GHS")
    p_chat, p_prov, p_sys, p_web, p_send = _patches(prov)
    with p_chat, p_prov, p_sys, p_web, p_send as mock_send:
        await _handle_nl(CHAT_ID, "what's the dollar to cedi rate?")

    sent = [c[0][1] for c in mock_send.call_args_list]
    assert any(s in THINKING_FILLERS for s in sent)   # a filler went out
    assert sent[-1] == "≈ 12 GHS"                       # final answer still delivered


@pytest.mark.asyncio
async def test_no_filler_without_web_tool(mock_db):
    """A fast reply with no web tool (e.g. a transcribed voice note) gets no filler."""
    prov = _loop_provider([], final_text="Sure, noted.")
    p_chat, p_prov, p_sys, p_web, p_send = _patches(prov)
    with p_chat, p_prov, p_sys, p_web, p_send as mock_send:
        await _handle_nl(CHAT_ID, "remember I like oat milk")

    sent = [c[0][1] for c in mock_send.call_args_list]
    assert not any(s in THINKING_FILLERS for s in sent)
    assert sent == ["Sure, noted."]


@pytest.mark.asyncio
async def test_filler_only_once_per_turn(mock_db):
    """Multiple web calls in one turn still produce at most one filler."""
    prov = _loop_provider(
        [("web_search", {"query": "a"}), ("web_search", {"query": "b"})],
        final_text="here you go",
    )
    p_chat, p_prov, p_sys, p_web, p_send = _patches(prov)
    with p_chat, p_prov, p_sys, p_web, p_send as mock_send:
        await _handle_nl(CHAT_ID, "compare two things online")

    sent = [c[0][1] for c in mock_send.call_args_list]
    assert sum(1 for s in sent if s in THINKING_FILLERS) == 1
