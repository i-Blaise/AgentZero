"""Job-application tracking: baseline, confirmation→track, reply→status update,
stale follow-up, and the list/track/update tools. Yahoo fetch + LLM classifier mocked."""
import json
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from agentzero import applications
from agentzero.executor import execute_tool
from agentzero.llm import ToolCall

CHAT_ID = 999


def _tc(tool: str, **kwargs) -> ToolCall:
    return ToolCall(name=tool, args=kwargs)


def _email(uid, frm, subject, snippet="", date="2026-06-16 10:00"):
    return {"uid": uid, "from": frm, "subject": subject, "date": date, "snippet": snippet}


def _provider(results):
    prov = MagicMock()
    prov.chat = AsyncMock(return_value=json.dumps({"results": results}))
    return prov


@pytest.mark.asyncio
async def test_first_scan_sets_baseline_only(mock_db):
    """First run must NOT classify history — just set a forward baseline."""
    emails = [_email("50", "x@y.com", "Newsletter"), _email("51", "a@b.com", "Hi")]
    prov = _provider([])
    with patch("agentzero.yahoo_mail.fetch_recent", new=AsyncMock(return_value=emails)), \
         patch("agentzero.applications.get_provider", return_value=prov):
        changes = await applications.scan_inbox(CHAT_ID)

    assert changes == {"new": [], "updates": []}
    prov.chat.assert_not_called()  # no classification on baseline
    state = await mock_db.system_state.find_one({"chat_id": CHAT_ID})
    assert state["last_app_scan_uid"] == "51"


@pytest.mark.asyncio
async def test_confirmation_starts_tracking(mock_db):
    await mock_db.system_state.insert_one({"chat_id": CHAT_ID, "last_app_scan_uid": "100"})
    emails = [_email("101", "jobs@acme.com", "We received your application",
                     "Thanks for applying to Acme for Backend Engineer")]
    prov = _provider([{"uid": "101", "category": "confirmation",
                       "company": "Acme", "role": "Backend Engineer", "new_status": ""}])
    with patch("agentzero.yahoo_mail.fetch_recent", new=AsyncMock(return_value=emails)), \
         patch("agentzero.applications.get_provider", return_value=prov):
        changes = await applications.scan_inbox(CHAT_ID)

    assert len(changes["new"]) == 1
    app = await mock_db.applications.find_one({"chat_id": CHAT_ID, "company": "Acme"})
    assert app["status"] == "applied"
    assert app["role"] == "Backend Engineer"


@pytest.mark.asyncio
async def test_reply_updates_status(mock_db):
    await mock_db.system_state.insert_one({"chat_id": CHAT_ID, "last_app_scan_uid": "101"})
    await mock_db.applications.insert_one(
        {"chat_id": CHAT_ID, "company": "Acme", "role": "Backend Engineer", "status": "applied",
         "applied_at": datetime.now(timezone.utc), "last_update_at": datetime.now(timezone.utc),
         "stale_notified": False, "created_at": datetime.now(timezone.utc)}
    )
    emails = [_email("102", "recruiter@acme.com", "Interview invitation",
                     "We'd love to schedule an interview")]
    prov = _provider([{"uid": "102", "category": "update", "company": "Acme",
                       "role": "", "new_status": "interview"}])
    with patch("agentzero.yahoo_mail.fetch_recent", new=AsyncMock(return_value=emails)), \
         patch("agentzero.applications.get_provider", return_value=prov):
        changes = await applications.scan_inbox(CHAT_ID)

    assert changes["updates"] and changes["updates"][0][1] == "interview"
    app = await mock_db.applications.find_one({"chat_id": CHAT_ID, "company": "Acme"})
    assert app["status"] == "interview"
    # didn't create a duplicate
    assert await mock_db.applications.count_documents({"chat_id": CHAT_ID, "company": "Acme"}) == 1


@pytest.mark.asyncio
async def test_reply_creates_tracking_when_no_confirmation(mock_db):
    """An interview/rejection from a company we never saw a confirmation for still starts
    tracking — from the reply."""
    await mock_db.system_state.insert_one({"chat_id": CHAT_ID, "last_app_scan_uid": "200"})
    emails = [_email("201", "talent@initech.com", "Interview invitation — Initech",
                     "We reviewed your application and would like to interview you")]
    prov = _provider([{"uid": "201", "category": "update", "company": "Initech",
                       "role": "Data Analyst", "new_status": "interview"}])
    with patch("agentzero.yahoo_mail.fetch_recent", new=AsyncMock(return_value=emails)), \
         patch("agentzero.applications.get_provider", return_value=prov):
        changes = await applications.scan_inbox(CHAT_ID)

    # created flag is True (3rd tuple element) and a record now exists at interview stage
    assert changes["updates"][0][1] == "interview"
    assert changes["updates"][0][2] is True
    app = await mock_db.applications.find_one({"chat_id": CHAT_ID, "company": "Initech"})
    assert app is not None and app["status"] == "interview"


SENT_ACCT = {"source": "yahoo", "host": "h", "user": "u", "password": "p", "sent_folder": "Sent"}


@pytest.mark.asyncio
async def test_sent_first_scan_sets_baseline(mock_db):
    emails = [_email("50", "me@y.com", "hi"), _email("51", "me@y.com", "yo")]
    prov = _provider([])
    with patch("agentzero.imap_mail.mail_accounts", return_value=[SENT_ACCT]), \
         patch("agentzero.imap_mail.fetch_recent", new=AsyncMock(return_value=emails)), \
         patch("agentzero.applications.get_provider", return_value=prov):
        new = await applications.scan_sent(CHAT_ID)

    assert new == []
    prov.chat.assert_not_called()  # no classification on baseline
    st = await mock_db.system_state.find_one({"chat_id": CHAT_ID})
    assert st["sent_app_cursor_yahoo"] == "51"


@pytest.mark.asyncio
async def test_sent_application_starts_tracking(mock_db):
    await mock_db.system_state.insert_one({"chat_id": CHAT_ID, "sent_app_cursor_yahoo": "100"})
    emails = [_email("101", "me@y.com", "Application: Backend Engineer", "Please find my CV attached")]
    prov = _provider([{"uid": "101", "category": "application",
                       "company": "Acme", "role": "Backend Engineer"}])
    with patch("agentzero.imap_mail.mail_accounts", return_value=[SENT_ACCT]), \
         patch("agentzero.imap_mail.fetch_recent", new=AsyncMock(return_value=emails)), \
         patch("agentzero.applications.get_provider", return_value=prov):
        new = await applications.scan_sent(CHAT_ID)

    assert len(new) == 1
    app = await mock_db.applications.find_one({"chat_id": CHAT_ID, "company": "Acme"})
    assert app["status"] == "applied"
    assert app["role"] == "Backend Engineer"
    assert app["source"] == "yahoo:sent"


@pytest.mark.asyncio
async def test_sent_non_application_ignored(mock_db):
    await mock_db.system_state.insert_one({"chat_id": CHAT_ID, "sent_app_cursor_yahoo": "100"})
    emails = [_email("101", "me@y.com", "lunch plans")]
    prov = _provider([{"uid": "101", "category": "other", "company": "", "role": ""}])
    with patch("agentzero.imap_mail.mail_accounts", return_value=[SENT_ACCT]), \
         patch("agentzero.imap_mail.fetch_recent", new=AsyncMock(return_value=emails)), \
         patch("agentzero.applications.get_provider", return_value=prov):
        new = await applications.scan_sent(CHAT_ID)

    assert new == []
    assert await mock_db.applications.count_documents({"chat_id": CHAT_ID}) == 0


@pytest.mark.asyncio
async def test_stale_followup_flagged_once(mock_db):
    await mock_db.applications.insert_one(
        {"chat_id": CHAT_ID, "company": "GhostCorp", "role": "", "status": "applied",
         "applied_at": datetime.now(timezone.utc) - timedelta(days=20),
         "last_update_at": datetime.now(timezone.utc) - timedelta(days=20),
         "stale_notified": False, "created_at": datetime.now(timezone.utc)}
    )
    with patch("agentzero.yahoo_mail.fetch_recent", new=AsyncMock(return_value=[])):
        first = await applications.gather_application_update(CHAT_ID)
        second = await applications.gather_application_update(CHAT_ID)

    assert first and "GhostCorp" in first and "gone quiet" in first
    assert second is None  # warned once, not every scan


@pytest.mark.asyncio
async def test_track_and_list_and_update_tools(mock_db):
    out = await execute_tool(CHAT_ID, _tc("track_application", company="Globex", role="SRE"))
    assert "Globex" in out

    listed = await execute_tool(CHAT_ID, _tc("list_applications"))
    assert "Globex" in listed and "SRE" in listed

    upd = await execute_tool(CHAT_ID, _tc("update_application", query="Globex", status="offer"))
    assert "offer" in upd.lower()
    app = await mock_db.applications.find_one({"chat_id": CHAT_ID, "company": "Globex"})
    assert app["status"] == "offer"


@pytest.mark.asyncio
async def test_check_job_replies_tool_no_double_send(mock_db):
    """The tool returns text for the loop to deliver — it must not send a message itself."""
    with patch("agentzero.yahoo_mail.fetch_recent", new=AsyncMock(return_value=[])), \
         patch("agentzero.telegram_io.send", new_callable=AsyncMock) as mock_send:
        out = await execute_tool(CHAT_ID, _tc("check_job_replies"))
    assert "no new" in out.lower()
    mock_send.assert_not_called()
