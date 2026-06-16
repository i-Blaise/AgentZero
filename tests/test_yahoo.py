"""Yahoo Mail (read-only IMAP) — body extraction, search/read formatting, executor routing.
The blocking IMAP layer (_sync_search/_sync_read) is patched, so no network is touched."""
import pytest
from email.message import EmailMessage
from unittest.mock import patch

from agentzero import yahoo_mail
from agentzero.executor import execute_tool
from agentzero.llm import ToolCall

CHAT_ID = 999


def _tc(tool: str, **kwargs) -> ToolCall:
    return ToolCall(name=tool, args=kwargs)


def test_extract_body_prefers_plain_text():
    msg = EmailMessage()
    msg["Subject"] = "Hi"
    msg.set_content("the plain body")
    msg.add_alternative("<p>the <b>html</b> body</p>", subtype="html")
    assert "plain body" in yahoo_mail._extract_body(msg)


def test_extract_body_falls_back_to_html():
    msg = EmailMessage()
    msg.set_content("<p>only&nbsp;html here</p>", subtype="html")
    out = yahoo_mail._extract_body(msg)
    assert "only html here" in out
    assert "<" not in out


@pytest.mark.asyncio
async def test_search_not_configured_message():
    with patch.object(yahoo_mail, "YAHOO_MAIL_ENABLED", False):
        out = await yahoo_mail.yahoo_search("invoices")
    assert "isn't connected" in out.lower()


@pytest.mark.asyncio
async def test_search_formats_results():
    rows = [
        {"uid": "101", "from": "Bank <b@x.com>", "subject": "Statement", "date": "2026-06-15 09:00"},
        {"uid": "102", "from": "Boss", "subject": "Re: invoice", "date": "2026-06-16 08:00"},
    ]
    with patch.object(yahoo_mail, "YAHOO_MAIL_ENABLED", True), \
         patch.object(yahoo_mail, "_sync_search", return_value=rows):
        out = await yahoo_mail.yahoo_search("invoice")
    assert "uid 101" in out and "uid 102" in out
    assert "Statement" in out
    assert "yahoo_read" in out


@pytest.mark.asyncio
async def test_read_formats_message():
    msg = {"from": "Boss", "to": "Me", "date": "2026-06-16 08:00",
           "subject": "Re: invoice", "body": "Please send it by Friday."}
    with patch.object(yahoo_mail, "YAHOO_MAIL_ENABLED", True), \
         patch.object(yahoo_mail, "_sync_read", return_value=msg):
        out = await yahoo_mail.yahoo_read("102")
    assert "Re: invoice" in out
    assert "Please send it by Friday." in out


@pytest.mark.asyncio
async def test_read_handles_missing_message():
    with patch.object(yahoo_mail, "YAHOO_MAIL_ENABLED", True), \
         patch.object(yahoo_mail, "_sync_read", return_value=None):
        out = await yahoo_mail.yahoo_read("999")
    assert "no message" in out.lower()


@pytest.mark.asyncio
async def test_executor_routes_yahoo_tools(mock_db):
    from unittest.mock import AsyncMock
    with patch("agentzero.yahoo_mail.yahoo_search", new=AsyncMock(return_value="SEARCH OK")) as s:
        out = await execute_tool(CHAT_ID, _tc("yahoo_search", query="q", limit=5))
    s.assert_awaited_once()
    assert out == "SEARCH OK"

    with patch("agentzero.yahoo_mail.yahoo_read", new=AsyncMock(return_value="BODY OK")) as r:
        out = await execute_tool(CHAT_ID, _tc("yahoo_read", uid="101"))
    r.assert_awaited_once()
    assert out == "BODY OK"
