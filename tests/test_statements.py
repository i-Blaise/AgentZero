"""MoMo statement import — logs spending only, deduped by MoMo reference.
The PDF fetch + text extraction + LLM parse are mocked (no IMAP, no pdfplumber, no network)."""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from agentzero import statements
from agentzero.executor import execute_tool
from agentzero.llm import ToolCall

CHAT_ID = 999

ATT = {"filename": "MomoStatementReport.pdf", "bytes": b"%PDF-1.4 fake",
       "uid": "77", "date": "2026-06-22 10:00", "source": "yahoo"}

TXNS = [
    {"merchant": "MTN Airtime", "amount": "20", "currency": "GHS", "date": "2026-06-18",
     "expense_category": "bills", "ref": "REF123", "description": "airtime top-up"},
    {"merchant": "ShopRite", "amount": "150.50", "currency": "GHS", "date": "2026-06-19",
     "expense_category": "shopping", "ref": "REF124", "description": "groceries"},
]


def _provider_txns(txns):
    prov = MagicMock()
    prov.chat = AsyncMock(return_value=json.dumps({"transactions": txns}))
    return prov


@pytest.mark.asyncio
async def test_import_logs_spending_and_dedupes(mock_db):
    with patch("agentzero.imap_mail.find_pdf_attachment", new=AsyncMock(return_value=ATT)), \
         patch("agentzero.statements._extract_pdf_text", return_value="line1\nMTN Airtime 20\nShopRite 150.50"), \
         patch("agentzero.statements.get_provider", return_value=_provider_txns(TXNS)):
        out = await statements.import_momo_statement(CHAT_ID)

    assert "Imported 2" in out
    assert "ShopRite" in out
    assert await mock_db.expenses.count_documents({"chat_id": CHAT_ID, "source": "momo"}) == 2
    doc = await mock_db.expenses.find_one({"chat_id": CHAT_ID, "merchant": "ShopRite"})
    assert doc["momo_ref"] == "REF124" and doc["amount"] == 150.5

    # Re-import the same statement → every txn deduped by ref → nothing new added.
    with patch("agentzero.imap_mail.find_pdf_attachment", new=AsyncMock(return_value=ATT)), \
         patch("agentzero.statements._extract_pdf_text", return_value="line1\nMTN Airtime 20\nShopRite 150.50"), \
         patch("agentzero.statements.get_provider", return_value=_provider_txns(TXNS)):
        out2 = await statements.import_momo_statement(CHAT_ID)
    assert "no new spending" in out2.lower()
    assert await mock_db.expenses.count_documents({"chat_id": CHAT_ID, "source": "momo"}) == 2


@pytest.mark.asyncio
async def test_import_no_attachment(mock_db):
    with patch("agentzero.imap_mail.find_pdf_attachment", new=AsyncMock(return_value=None)):
        out = await statements.import_momo_statement(CHAT_ID)
    assert "couldn't find" in out.lower()


@pytest.mark.asyncio
async def test_import_scanned_pdf_no_text(mock_db):
    with patch("agentzero.imap_mail.find_pdf_attachment", new=AsyncMock(return_value=ATT)), \
         patch("agentzero.statements._extract_pdf_text", return_value="   "):
        out = await statements.import_momo_statement(CHAT_ID)
    assert "no extractable text" in out.lower()


@pytest.mark.asyncio
async def test_tool_routes_to_importer(mock_db):
    with patch("agentzero.statements.import_momo_statement", new=AsyncMock(return_value="IMPORT OK")) as imp:
        out = await execute_tool(CHAT_ID, ToolCall(name="import_momo_statement", args={}))
    imp.assert_awaited_once()
    assert out == "IMPORT OK"
