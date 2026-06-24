"""MoMo statement import — faithful, lossless save of the full transaction table.
The PDF fetch + table extraction are mocked (no IMAP, no pdfplumber, no network)."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from agentzero import statements
from agentzero.executor import execute_tool
from agentzero.llm import ToolCall

CHAT_ID = 999

ATT = {"filename": "MomoStatementReport.pdf", "bytes": b"%PDF-1.4 fake",
       "uid": "77", "date": "2026-06-22 10:00", "source": "yahoo"}

COLUMNS = ["TRANSACTION DATE", "FROM ACCT", "FROM NAME", "FROM NO.", "TRANS. TYPE", "AMOUNT",
           "FEES", "E-LEVY", "BAL BEFORE", "BAL AFTER", "TO NO.", "TO NAME", "TO ACCT", "F_ID",
           "REF", "OVA"]
ROWS = [
    ["20-Jun-2026 03:34:37 PM", "46792472", "BLAISE SONZIE\nMENNIA", "233545296150", "TRANSFER",
     "120", "0.9", "0", "145.44", "24.54", "233552210985", "NAFISA\nAWUDU", "59175523",
     "83739953756", "G", "Internal"],
    ["19-Jun-2026 12:14:08 PM", "46792472", "BLAISE SONZIE\nMENNIA", "233545296150", "CASH_OUT",
     "200", "2", "0", "609.39", "407.39", "233553541290", "KODZO\nAGBAVOR", "104258507",
     "83660872440", "NationalId--", "Internal"],
]
PERIOD = "From: 23-May-2026 To: 22-Jun-2026"


def _patches(rows=ROWS):
    return (
        patch("agentzero.imap_mail.find_pdf_attachment", new=AsyncMock(return_value=ATT)),
        patch("agentzero.statements._extract_tables", MagicMock(return_value=(COLUMNS, rows, PERIOD))),
    )


@pytest.mark.asyncio
async def test_saves_everything_verbatim(mock_db):
    p1, p2 = _patches()
    with p1, p2:
        out = await statements.import_momo_statement(CHAT_ID)
    assert "Saved 2 transactions" in out
    docs = await mock_db.momo_transactions.find({"chat_id": CHAT_ID}).to_list(None)
    assert len(docs) == 2
    # exact columns preserved, content not altered (incl. money-in types like CASH_OUT)
    d = next(x for x in docs if x["f_id"] == "83739953756")
    assert d["columns"] == COLUMNS                      # exact column names
    assert d["values"][4] == "TRANSFER"                  # TRANS. TYPE verbatim
    assert d["values"][11] == "NAFISA\nAWUDU"            # wrapped cell kept verbatim
    assert d["values"][14] == "G"                        # the REF column, faithfully
    assert d["statement_period"] == PERIOD
    assert any(x["values"][4] == "CASH_OUT" for x in docs)  # money-movement rows saved too


@pytest.mark.asyncio
async def test_dedup_by_fid_on_reimport(mock_db):
    p1, p2 = _patches()
    with p1, p2:
        await statements.import_momo_statement(CHAT_ID)
    p1, p2 = _patches()
    with p1, p2:
        out2 = await statements.import_momo_statement(CHAT_ID)
    assert "Skipped 2 already saved" in out2
    assert await mock_db.momo_transactions.count_documents({"chat_id": CHAT_ID}) == 2


@pytest.mark.asyncio
async def test_no_attachment(mock_db):
    with patch("agentzero.imap_mail.find_pdf_attachment", new=AsyncMock(return_value=None)):
        out = await statements.import_momo_statement(CHAT_ID)
    assert "couldn't find" in out.lower()


@pytest.mark.asyncio
async def test_no_table_extracted(mock_db):
    with patch("agentzero.imap_mail.find_pdf_attachment", new=AsyncMock(return_value=ATT)), \
         patch("agentzero.statements._extract_tables", MagicMock(return_value=([], [], ""))):
        out = await statements.import_momo_statement(CHAT_ID)
    assert "couldn't extract" in out.lower()


@pytest.mark.asyncio
async def test_tool_routes_to_importer(mock_db):
    with patch("agentzero.statements.import_momo_statement", new=AsyncMock(return_value="SAVED")) as imp:
        out = await execute_tool(CHAT_ID, ToolCall(name="import_momo_statement", args={}))
    imp.assert_awaited_once()
    assert out == "SAVED"


@pytest.mark.asyncio
async def test_add_momo_alias_still_works(mock_db):
    out = await execute_tool(CHAT_ID, ToolCall(name="add_momo_alias", args={"code": "K", "name": "Kofi's shop", "category": "food"}))
    assert "kofi's shop" in out.lower()
    aliases = await statements._load_aliases(CHAT_ID)
    assert aliases["k"]["name"] == "Kofi's shop"
    assert aliases["g"]["name"] == "MaryJ"  # built-in default still present
