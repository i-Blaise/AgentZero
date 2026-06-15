"""
Web search / fetch tests — HTML stripping, backend selection, and that the
executor routes web_search / web_fetch to the web module. httpx is mocked so
no real network calls happen.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from agentzero import web
from agentzero.executor import execute_tool
from agentzero.llm import ToolCall

CHAT_ID = 777


def _tc(tool: str, **kwargs) -> ToolCall:
    return ToolCall(name=tool, args=kwargs)


def test_html_to_text_strips_tags_and_scripts():
    html = "<html><head><style>.x{}</style></head><body><h1>Hi</h1>" \
           "<script>evil()</script><p>Para&nbsp;one</p></body></html>"
    text = web._html_to_text(html)
    assert "Hi" in text
    assert "Para one" in text
    assert "evil()" not in text
    assert ".x{}" not in text
    assert "<" not in text


@pytest.mark.asyncio
async def test_web_fetch_rejects_non_http():
    out = await web.web_fetch("ftp://example.com/file")
    assert "invalid url" in out.lower()


@pytest.mark.asyncio
async def test_web_search_no_key_uses_duckduckgo(monkeypatch):
    """With provider=auto and no keys, it falls back to DuckDuckGo."""
    monkeypatch.setattr(web, "WEB_SEARCH_PROVIDER", "auto")
    monkeypatch.setattr(web, "TAVILY_API_KEY", "")
    monkeypatch.setattr(web, "BRAVE_API_KEY", "")
    with patch.object(
        web, "_duckduckgo",
        new=AsyncMock(return_value=[{"title": "Result", "url": "https://x.com", "snippet": "s"}]),
    ) as ddg:
        out = await web.web_search("python asyncio", max_results=3)
    ddg.assert_awaited_once()
    assert "Result" in out
    assert "https://x.com" in out


@pytest.mark.asyncio
async def test_web_search_prefers_tavily_when_keyed(monkeypatch):
    monkeypatch.setattr(web, "WEB_SEARCH_PROVIDER", "auto")
    monkeypatch.setattr(web, "TAVILY_API_KEY", "tvly-xxx")
    with patch.object(
        web, "_tavily",
        new=AsyncMock(return_value=[{"title": "T", "url": "https://t.com", "snippet": "ts"}]),
    ) as tav:
        out = await web.web_search("latest news")
    tav.assert_awaited_once()
    assert "https://t.com" in out


@pytest.mark.asyncio
async def test_web_search_empty_query():
    assert "something" in (await web.web_search("  ")).lower()


@pytest.mark.asyncio
async def test_executor_routes_web_tools(mock_db):
    with patch("agentzero.web.web_search", new=AsyncMock(return_value="SEARCH OK")) as s:
        out = await execute_tool(CHAT_ID, _tc("web_search", query="q", max_results=4))
    s.assert_awaited_once()
    assert out == "SEARCH OK"

    with patch("agentzero.web.web_fetch", new=AsyncMock(return_value="PAGE OK")) as f:
        out = await execute_tool(CHAT_ID, _tc("web_fetch", url="https://x.com"))
    f.assert_awaited_once()
    assert out == "PAGE OK"
