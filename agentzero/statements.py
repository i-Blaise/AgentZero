"""
MoMo statement import — faithful, lossless capture of the full statement.

Pulls the MoMo PDF from the inbox and extracts the transaction table EXACTLY using pdfplumber's
ruled-line table extraction (clean, 16-column-aligned cells), saving EVERY transaction (money in
and out) verbatim into the `momo_transactions` collection with the statement's exact column names
and unaltered cell values. This is the canonical raw store — we can derive categorised views from
it later (the REF column carries the purpose; aliases like G→MaryJ map onto it).

Nothing here rewrites cell content: values are stored as pdfplumber returns them (None → "" for
empty cells; wrapped multi-line cells keep their embedded newlines). Dedup is by F_ID (the
statement's own per-transaction ID), so re-importing the same statement never duplicates.

pdfplumber is imported lazily so the rest of the app doesn't depend on it.
"""
from __future__ import annotations

import asyncio
import io
import logging
import re
from datetime import datetime, timezone

from agentzero.db import get_db

logger = logging.getLogger(__name__)

_FIRST_COL = "TRANSACTION DATE"  # the header row starts with this

# Reference shorthands for a future categorised view (kept here so add_momo_alias has a home).
_DEFAULT_ALIASES = {"g": {"name": "MaryJ", "category": "entertainment"}}


async def _load_aliases(chat_id: int) -> dict:
    db = get_db()
    prof = await db.profile.find_one({"chat_id": chat_id}) or await db.profile.find_one({}) or {}
    aliases = dict(_DEFAULT_ALIASES)
    for code, info in (prof.get("momo_aliases") or {}).items():
        if isinstance(info, dict) and info.get("name"):
            aliases[str(code).strip().lower()] = {
                "name": info["name"], "category": info.get("category") or "other",
            }
    return aliases


def _cell(value) -> str:
    """Faithful cell value — empty cells become '', everything else verbatim (newlines kept)."""
    return "" if value is None else value


def _extract_tables(pdf_bytes: bytes) -> tuple[list[str], list[list[str]], str]:
    """Return (columns, rows, statement_period), all verbatim from the PDF's ruled table.
    columns = exact header names; rows = list of 16-cell lists; period = the 'From: … To: …' line."""
    import pdfplumber  # lazy — heavy dependency

    columns: list[str] = []
    rows: list[list[str]] = []
    period = ""
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                for cells in table:
                    if not cells:
                        continue
                    # Banner/title row: only the first cell is populated (rest None).
                    if cells[0] and all(c is None for c in cells[1:]):
                        if not period:
                            for line in str(cells[0]).splitlines():
                                if line.strip().startswith("From:"):
                                    period = line.strip()
                                    break
                        continue
                    # Header row (repeats per page) — capture the exact column names once.
                    if cells[0] == _FIRST_COL:
                        if not columns:
                            columns = [_cell(c) for c in cells]
                        continue
                    rows.append([_cell(c) for c in cells])
    return columns, rows, period


async def import_momo_statement(chat_id: int, name_substr: str = "momo") -> str:
    """Find the MoMo PDF and save its FULL transaction table verbatim into momo_transactions."""
    from agentzero import imap_mail

    att = await imap_mail.find_pdf_attachment(name_substr)
    if not att:
        return ("Couldn't find a MoMo statement PDF in your inbox (looked for a recent PDF whose "
                "name contains 'momo'). Make sure the statement email is in the inbox.")
    try:
        columns, rows, period = await asyncio.to_thread(_extract_tables, att["bytes"])
    except Exception:
        logger.exception("MoMo table extraction failed")
        return f"Found {att['filename']} but couldn't read the transaction table from the PDF."
    if not columns or not rows:
        return (f"Read {att['filename']} but couldn't extract a transaction table — it may be a "
                "scanned image rather than a text PDF.")

    fid_idx = columns.index("F_ID") if "F_ID" in columns else None
    db = get_db()
    saved = 0
    skipped = 0
    for cells in rows:
        fid = (cells[fid_idx].strip() if fid_idx is not None and fid_idx < len(cells) else "")
        if fid and await db.momo_transactions.find_one({"chat_id": chat_id, "f_id": fid}):
            skipped += 1
            continue
        await db.momo_transactions.insert_one(
            {
                "chat_id": chat_id,
                "source_file": att["filename"],
                "statement_period": period,
                "columns": columns,        # exact 16 column names, verbatim
                "values": cells,           # raw cell values, unaltered
                "f_id": fid,               # dedup key (the statement's own transaction ID)
                "imported_at": datetime.now(timezone.utc),
            }
        )
        saved += 1

    msg = f"📄 Saved {saved} transactions from {att['filename']}"
    if period:
        msg += f" ({period})"
    if skipped:
        msg += f". Skipped {skipped} already saved"
    return msg + "."
