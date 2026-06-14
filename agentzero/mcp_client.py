"""
Generic MCP client layer.

Connects to the MCP servers listed in config.MCP_SERVERS (streamable-HTTP),
pulls each server's tool list, and exposes them to the rest of the app in the
same neutral JSON-Schema format as the local tools.  Tool names are namespaced
with the server's name (e.g. `google__search_gmail_messages`) so they can't
collide with local tools, and so calls can be routed back to the right server.

Connections are opened per operation (list / call). For a localhost MCP server
that's cheap and far simpler than holding a long-lived session across the
FastAPI app's lifecycle.
"""
from __future__ import annotations

import logging

from agentzero.config import MCP_SERVERS

logger = logging.getLogger(__name__)

SEP = "__"

_mcp_tools: list[dict] = []                      # neutral tool defs (cached at startup)
_routes: dict[str, tuple[str, str]] = {}         # namespaced name -> (server_url, original_name)


def _import_client():
    from mcp import ClientSession

    try:
        from mcp.client.streamable_http import streamablehttp_client
    except ImportError:  # older/newer SDK module spelling
        from mcp.client.streamablehttp import streamablehttp_client
    return ClientSession, streamablehttp_client


async def _list_server_tools(url: str):
    ClientSession, streamablehttp_client = _import_client()
    async with streamablehttp_client(url) as streams:
        read, write = streams[0], streams[1]
        async with ClientSession(read, write) as session:
            await session.initialize()
            resp = await session.list_tools()
            return resp.tools


async def load_mcp_tools() -> list[dict]:
    """Connect to every configured server and cache its tools. Call at startup."""
    global _mcp_tools, _routes
    _mcp_tools = []
    _routes = {}
    for server in MCP_SERVERS:
        name, url = server["name"], server["url"]
        try:
            tools = await _list_server_tools(url)
        except Exception:
            logger.exception("Could not load MCP tools from %s (%s)", name, url)
            continue
        for t in tools:
            namespaced = f"{name}{SEP}{t.name}"
            _mcp_tools.append(
                {
                    "name": namespaced,
                    "description": t.description or "",
                    "parameters": t.inputSchema or {"type": "object", "properties": {}},
                }
            )
            _routes[namespaced] = (url, t.name)
        logger.info("Loaded %d MCP tool(s) from %s", len(tools), name)
    return _mcp_tools


def get_mcp_tools() -> list[dict]:
    return _mcp_tools


def is_mcp_tool(name: str) -> bool:
    return name in _routes


async def call_mcp_tool(name: str, args: dict) -> str:
    route = _routes.get(name)
    if not route:
        return f"Unknown MCP tool: {name}"

    url, original = route
    ClientSession, streamablehttp_client = _import_client()
    try:
        async with streamablehttp_client(url) as streams:
            read, write = streams[0], streams[1]
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(name=original, arguments=args)
                parts = [
                    block.text
                    for block in result.content
                    if getattr(block, "type", None) == "text"
                ]
                return "\n".join(parts) if parts else "(no content returned)"
    except Exception:
        logger.exception("MCP tool call failed: %s", name)
        return f"Couldn't reach {name} right now."
