"""Expense tracking: multi-mailbox receipt scan, dedup, baseline, summaries, manual add.
IMAP fetch + LLM classifier are mocked, so no network is touched."""
import json
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from agentzero import expenses
from agentzero.executor import execute_tool
from agentzero.llm import ToolCall

CHAT_ID = 999
ACCT = {"source": "yahoo", "host": "h", "user": "u", "password": "p"}


def _tc(tool: str, **kwargs) -> ToolCall:
    return ToolCall(name=tool, args=kwargs)


def _email(uid, frm, subject, snippet="", date="2026-06-16 10:00"):
    return {"uid": uid, "from": frm, "subject": subject, "date": date, "snippet": snippet}


def _provider(results):
    prov = MagicMock()
    prov.chat = AsyncMock(return_value=json.dumps({"results": results}))
    return prov


def test_parse_amount_handles_symbols_and_commas():
    assert expenses._parse_amount("₵1,250.50") == 1250.50
    assert expenses._parse_amount("$20") == 20.0
    assert expenses._parse_amount(15) == 15.0
    assert expenses._parse_amount("n/a") is None


@pytest.mark.asyncio
async def test_first_scan_sets_baseline_only(mock_db):
    emails = [_email("40", "x@y.com", "Hi"), _email("41", "z@w.com", "Yo")]
    prov = _provider([])
    with patch("agentzero.imap_mail.mail_accounts", return_value=[ACCT]), \
         patch("agentzero.imap_mail.fetch_recent", new=AsyncMock(return_value=emails)), \
         patch("agentzero.expenses.get_provider", return_value=prov):
        logged = await expenses.scan_receipts(CHAT_ID)

    assert logged == []
    prov.chat.assert_not_called()
    state = await mock_db.system_state.find_one({"chat_id": CHAT_ID})
    assert state["receipt_cursor_yahoo"] == "41"


@pytest.mark.asyncio
async def test_receipt_logged_and_deduped(mock_db):
    await mock_db.system_state.insert_one({"chat_id": CHAT_ID, "receipt_cursor_yahoo": "100"})
    emails = [_email("101", "no-reply@uber.com", "Your Tuesday trip with Uber",
                     "Total GHS 38.50 — thanks for riding")]
    classified = [{"uid": "101", "category": "receipt", "merchant": "Uber", "amount": "38.50",
                   "currency": "GHS", "date": "2026-06-16", "expense_category": "transport",
                   "description": "Tuesday trip"}]
    with patch("agentzero.imap_mail.mail_accounts", return_value=[ACCT]), \
         patch("agentzero.imap_mail.fetch_recent", new=AsyncMock(return_value=emails)), \
         patch("agentzero.expenses.get_provider", return_value=_provider(classified)):
        logged = await expenses.scan_receipts(CHAT_ID)

    assert len(logged) == 1
    exp = await mock_db.expenses.find_one({"chat_id": CHAT_ID, "merchant": "Uber"})
    assert exp["amount"] == 38.5 and exp["currency"] == "GHS" and exp["category"] == "transport"
    assert exp["email_id"] == "yahoo:101"

    # Re-scanning the same uid (cursor rewound) must not double-log it.
    await mock_db.system_state.update_one(
        {"chat_id": CHAT_ID}, {"$set": {"receipt_cursor_yahoo": "100"}}
    )
    with patch("agentzero.imap_mail.mail_accounts", return_value=[ACCT]), \
         patch("agentzero.imap_mail.fetch_recent", new=AsyncMock(return_value=emails)), \
         patch("agentzero.expenses.get_provider", return_value=_provider(classified)):
        again = await expenses.scan_receipts(CHAT_ID)
    assert again == []
    assert await mock_db.expenses.count_documents({"chat_id": CHAT_ID, "merchant": "Uber"}) == 1


@pytest.mark.asyncio
async def test_non_receipt_ignored(mock_db):
    await mock_db.system_state.insert_one({"chat_id": CHAT_ID, "receipt_cursor_yahoo": "100"})
    emails = [_email("101", "news@medium.com", "Your weekly digest")]
    classified = [{"uid": "101", "category": "other", "merchant": "", "amount": ""}]
    with patch("agentzero.imap_mail.mail_accounts", return_value=[ACCT]), \
         patch("agentzero.imap_mail.fetch_recent", new=AsyncMock(return_value=emails)), \
         patch("agentzero.expenses.get_provider", return_value=_provider(classified)):
        logged = await expenses.scan_receipts(CHAT_ID)
    assert logged == []
    assert await mock_db.expenses.count_documents({"chat_id": CHAT_ID}) == 0


@pytest.mark.asyncio
async def test_add_list_and_summary_tools(mock_db):
    await execute_tool(CHAT_ID, _tc("add_expense", merchant="Cafe", amount=50, currency="GHS", category="food"))
    await execute_tool(CHAT_ID, _tc("add_expense", merchant="Bolt", amount=20, currency="GHS", category="transport"))
    await execute_tool(CHAT_ID, _tc("add_expense", merchant="Steam", amount=10, currency="USD", category="entertainment"))

    listed = await execute_tool(CHAT_ID, _tc("list_expenses", period="month"))
    assert "Cafe" in listed and "Bolt" in listed

    summary = await execute_tool(CHAT_ID, _tc("expense_summary", period="month"))
    # totals grouped per currency, not summed across them
    assert "GHS 70.00" in summary
    assert "USD 10.00" in summary


@pytest.mark.asyncio
async def test_backfill_logs_history_and_dedupes(mock_db):
    """Historical backfill logs receipts from the window and never double-logs an email."""
    emails = [
        _email("301", "no-reply@netflix.com", "Your Netflix receipt", "GHS 65 charged"),
        _email("302", "friend@gmail.com", "lunch?", "wanna grab food"),  # not a receipt
    ]
    classified = [
        {"uid": "301", "category": "receipt", "merchant": "Netflix", "amount": "65",
         "currency": "GHS", "date": "2026-06-01", "expense_category": "subscription", "description": "monthly"},
        {"uid": "302", "category": "other", "merchant": "", "amount": ""},
    ]
    with patch("agentzero.imap_mail.mail_accounts", return_value=[ACCT]), \
         patch("agentzero.imap_mail.fetch_since", new=AsyncMock(return_value=emails)), \
         patch("agentzero.expenses.get_provider", return_value=_provider(classified)):
        logged = await expenses.backfill_receipts(CHAT_ID, 30)
        assert len(logged) == 1
        # run again — same emails — must not duplicate
        again = await expenses.backfill_receipts(CHAT_ID, 30)

    assert again == []
    assert await mock_db.expenses.count_documents({"chat_id": CHAT_ID, "merchant": "Netflix"}) == 1


@pytest.mark.asyncio
async def test_check_receipts_days_triggers_backfill(mock_db):
    emails = [_email("400", "no-reply@spotify.com", "Spotify Premium receipt", "USD 10 charged")]
    classified = [{"uid": "400", "category": "receipt", "merchant": "Spotify", "amount": "10",
                   "currency": "USD", "date": "2026-06-05", "expense_category": "subscription", "description": ""}]
    with patch("agentzero.imap_mail.mail_accounts", return_value=[ACCT]), \
         patch("agentzero.imap_mail.fetch_since", new=AsyncMock(return_value=emails)) as fs, \
         patch("agentzero.expenses.get_provider", return_value=_provider(classified)):
        out = await execute_tool(CHAT_ID, _tc("check_receipts", days=30))
    fs.assert_awaited()  # went through the backfill (fetch_since), not the forward scan
    assert "Spotify" in out
    assert "last 30 days" in out


@pytest.mark.asyncio
async def test_delete_expense_by_merchant(mock_db):
    await execute_tool(CHAT_ID, _tc("add_expense", merchant="CalBank", amount=4000, currency="GHS"))
    await execute_tool(CHAT_ID, _tc("add_expense", merchant="Uber", amount=28, currency="GHS"))

    out = await execute_tool(CHAT_ID, _tc("delete_expense", query="CalBank"))
    assert "removed" in out.lower()
    assert await mock_db.expenses.count_documents({"chat_id": CHAT_ID, "merchant": "CalBank"}) == 0
    assert await mock_db.expenses.count_documents({"chat_id": CHAT_ID, "merchant": "Uber"}) == 1


@pytest.mark.asyncio
async def test_delete_expense_disambiguates_by_amount(mock_db):
    await execute_tool(CHAT_ID, _tc("add_expense", merchant="CalBank", amount=4000, currency="GHS"))
    await execute_tool(CHAT_ID, _tc("add_expense", merchant="CalBank", amount=5, currency="GHS"))

    # ambiguous → asks for the amount, deletes nothing
    out = await execute_tool(CHAT_ID, _tc("delete_expense", query="CalBank"))
    assert "amount" in out.lower()
    assert await mock_db.expenses.count_documents({"chat_id": CHAT_ID}) == 2

    # with amount → removes the right one
    out2 = await execute_tool(CHAT_ID, _tc("delete_expense", query="CalBank", amount=4000))
    assert "removed" in out2.lower()
    remaining = await mock_db.expenses.find({"chat_id": CHAT_ID}).to_list(None)
    assert len(remaining) == 1 and remaining[0]["amount"] == 5


@pytest.mark.asyncio
async def test_delete_expense_not_found(mock_db):
    await execute_tool(CHAT_ID, _tc("add_expense", merchant="Uber", amount=28))
    out = await execute_tool(CHAT_ID, _tc("delete_expense", query="Nonexistent Merchant XYZ"))
    assert "no expense matching" in out.lower()
    assert await mock_db.expenses.count_documents({"chat_id": CHAT_ID}) == 1


@pytest.mark.asyncio
async def test_purge_scanned_keeps_manual(mock_db):
    from agentzero.expenses import purge_scanned_expenses

    now = datetime(2026, 6, 16, tzinfo=timezone.utc)
    await mock_db.expenses.insert_many([
        {"chat_id": CHAT_ID, "merchant": "Uber", "amount": 28, "currency": "GHS",
         "category": "transport", "spent_at": now, "source": "yahoo", "email_id": "yahoo:1"},
        {"chat_id": CHAT_ID, "merchant": "Spotify", "amount": 10, "currency": "USD",
         "category": "subscription", "spent_at": now, "source": "gmail", "email_id": "gmail:2"},
        {"chat_id": CHAT_ID, "merchant": "Lunch", "amount": 50, "currency": "GHS",
         "category": "food", "spent_at": now, "source": "manual", "email_id": ""},
    ])
    deleted = await purge_scanned_expenses(CHAT_ID)
    assert deleted == 2
    rows = await mock_db.expenses.find({"chat_id": CHAT_ID}).to_list(None)
    assert len(rows) == 1 and rows[0]["source"] == "manual"


@pytest.mark.asyncio
async def test_check_receipts_tool_no_double_send(mock_db):
    with patch("agentzero.imap_mail.mail_accounts", return_value=[]), \
         patch("agentzero.telegram_io.send", new_callable=AsyncMock) as mock_send:
        out = await execute_tool(CHAT_ID, _tc("check_receipts"))
    assert "no new receipts" in out.lower()
    mock_send.assert_not_called()
