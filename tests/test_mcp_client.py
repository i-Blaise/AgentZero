"""
MCP client layer tests — tool loading, namespacing, routing.

The actual MCP network calls are patched out (no live server needed). We verify
that server tools get namespaced, routed, and exposed in neutral format.
"""
import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from agentzero import mcp_client


def _fake_tool(name, description, schema):
    return SimpleNamespace(name=name, description=description, inputSchema=schema)


@pytest.fixture(autouse=True)
def _reset_state():
    # Ensure clean module state between tests
    mcp_client._mcp_tools = []
    mcp_client._routes = {}
    yield
    mcp_client._mcp_tools = []
    mcp_client._routes = {}


@pytest.mark.asyncio
async def test_load_namespaces_tools():
    fake = [
        _fake_tool("search_gmail_messages", "Search Gmail", {"type": "object", "properties": {}}),
        _fake_tool("list_calendar_events", "List events", {"type": "object", "properties": {}}),
    ]
    with patch("agentzero.mcp_client.MCP_SERVERS", [{"name": "google", "url": "http://x/mcp"}]), \
         patch("agentzero.mcp_client._list_server_tools", new_callable=AsyncMock, return_value=fake):
        tools = await mcp_client.load_mcp_tools()

    names = {t["name"] for t in tools}
    assert names == {"google__search_gmail_messages", "google__list_calendar_events"}
    assert mcp_client.is_mcp_tool("google__search_gmail_messages")
    assert not mcp_client.is_mcp_tool("add_task")
    # neutral format preserved
    t = next(t for t in tools if t["name"] == "google__search_gmail_messages")
    assert t["description"] == "Search Gmail"
    assert t["parameters"]["type"] == "object"


@pytest.mark.asyncio
async def test_load_survives_unreachable_server():
    with patch("agentzero.mcp_client.MCP_SERVERS", [{"name": "google", "url": "http://x/mcp"}]), \
         patch("agentzero.mcp_client._list_server_tools", new_callable=AsyncMock, side_effect=ConnectionError("down")):
        tools = await mcp_client.load_mcp_tools()
    assert tools == []  # no crash, just no tools


@pytest.mark.asyncio
async def test_call_unknown_tool_returns_message():
    # No mcp package import happens — route lookup fails first
    result = await mcp_client.call_mcp_tool("google__nope", {})
    assert "Unknown MCP tool" in result
