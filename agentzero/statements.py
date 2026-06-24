"""
Mobile-money statement import (MTN MoMo PDF).

Pulls the statement PDF from the inbox, extracts the transaction text, and the LLM picks out
SPENDING only — payments to merchants, airtime/data, bills — EXCLUDING money received,
deposits, cash-outs, reversals, and person-to-person transfers the user sent. Each is logged
as an expense (source "momo"), deduped by the MoMo transaction reference so re-importing the
same statement never double-counts.

pdfplumber is imported lazily so the rest of the app doesn't depend on it.
"""
from __future__ import annotations

import asyncio
import io
import logging

from agentzero.config import DEFAULT_CURRENCY
from agentzero.db import get_db
from agentzero.expenses import (
    _CATEGORIES,
    _is_duplicate,
    _log_expense,
    _parse_amount,
    _parse_json,
    _resolve_date,
)
from agentzero.llm import get_provider

logger = logging.getLogger(__name__)

# Built-in reference decodings (the user's shorthand). Keys are lowercased reference text.
# `vendor` aliases are purchases; merged with any stored in profile.momo_aliases.
_DEFAULT_ALIASES = {"g": {"name": "MaryJ", "category": "entertainment"}}


async def _load_aliases(chat_id: int) -> dict:
    db = get_db()
    prof = await db.profile.find_one({"chat_id": chat_id}) or await db.profile.find_one({}) or {}
    aliases = dict(_DEFAULT_ALIASES)
    for code, info in (prof.get("momo_aliases") or {}).items():
        if isinstance(info, dict) and info.get("name"):
            aliases[str(code).strip().lower()] = {
                "name": info["name"],
                "category": (info.get("category") or "other"),
            }
    return aliases


def _aliases_text(aliases: dict) -> str:
    if not aliases:
        return ""
    lines = [
        f"  - reference \"{code}\" means {info['name']} (a known vendor → merchant \"{info['name']}\", "
        f"category \"{info['category']}\", INCLUDE as a purchase)"
        for code, info in aliases.items()
    ]
    return "Known reference shorthands (decode these):\n" + "\n".join(lines) + "\n"


def _extract_pdf_text(pdf_bytes: bytes) -> str:
    import pdfplumber  # lazy — heavy dependency, only needed for this feature

    parts: list[str] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            parts.append(page.extract_text() or "")
    return "\n".join(parts)


def _chunks(text: str, size: int = 6000):
    buf: list[str] = []
    cur = 0
    for line in text.splitlines():
        buf.append(line)
        cur += len(line) + 1
        if cur >= size:
            yield "\n".join(buf)
            buf, cur = [], 0
    if buf:
        yield "\n".join(buf)


async def _parse_chunk(text: str, aliases: dict) -> list[dict]:
    system = (
        "You extract transactions from an MTN Mobile Money (MoMo) statement for a personal spend "
        "tracker. INCLUDE every transaction where money LEFT the user's account (debits): payments, "
        "purchases, airtime/data, bills, cash-outs/withdrawals, AND person-to-person transfers the "
        "user SENT. EXCLUDE only money that came IN (credits): money received, deposits/cash-in, and "
        "reversals — those are not spending.\n"
        "Use each transaction's REFERENCE / narration as the main signal for who or what it was.\n"
        "For every included transaction extract: merchant (who/what the money went to — from the "
        f"reference/recipient), amount (number only), currency (default {DEFAULT_CURRENCY}), date "
        "(YYYY-MM-DD), ref (the numeric MoMo transaction ID, for dedupe), ref_text (the reference/"
        "narration the user typed, e.g. \"G\", \"Mwin\"), expense_category, and a short description.\n\n"
        f"Categories: {_CATEGORIES}. Rules:\n"
        "  - a transfer/send whose recipient is a PERSON'S NAME (e.g. Mwin, BM, Douglas, Felix) → "
        "category \"charity\".\n"
        "  - airtime/data/telecom (MTN, Telecel, etc.) → \"bills\".\n"
        f"{_aliases_text(aliases)}"
        'Return ONLY JSON, no prose: {"transactions":[{"merchant":"","amount":"","currency":"",'
        '"date":"","ref":"","ref_text":"","expense_category":"","description":""}]}'
    )
    try:
        raw = await get_provider().chat([{"role": "user", "content": text}], system)
    except Exception:
        logger.exception("MoMo statement parse failed")
        return []
    data = _parse_json(raw)
    txns = data.get("transactions") if isinstance(data, dict) else None
    return txns if isinstance(txns, list) else []


async def import_momo_statement(chat_id: int, name_substr: str = "momo") -> str:
    from agentzero import imap_mail

    att = await imap_mail.find_pdf_attachment(name_substr)
    if not att:
        return ("Couldn't find a MoMo statement PDF in your inbox (looked for a recent PDF "
                "whose name contains 'momo'). Make sure the statement email is in the inbox.")
    try:
        text = await asyncio.to_thread(_extract_pdf_text, att["bytes"])
    except Exception:
        logger.exception("PDF text extraction failed")
        return f"Found {att['filename']} but couldn't read the PDF — it may be password-protected or corrupted."
    if not text.strip():
        return f"Read {att['filename']} but found no extractable text — it may be a scanned image rather than a text PDF."

    aliases = await _load_aliases(chat_id)
    txns: list[dict] = []
    for chunk in _chunks(text):
        txns.extend(await _parse_chunk(chunk, aliases))

    db = get_db()
    logged: list[dict] = []
    seen_refs: set[str] = set()
    for t in txns:
        if not isinstance(t, dict):
            continue
        amount = _parse_amount(t.get("amount"))
        if amount is None or amount <= 0:
            continue
        ref = str(t.get("ref") or "").strip()
        merchant = (t.get("merchant") or "MoMo").strip()
        cat = (t.get("expense_category") or "other").lower()
        # Deterministic alias override: a typed reference matching a known shorthand wins.
        alias = aliases.get((t.get("ref_text") or "").strip().lower())
        if alias:
            merchant = alias["name"]
            cat = (alias.get("category") or cat).lower()
        currency = (t.get("currency") or DEFAULT_CURRENCY).strip().upper()[:3]
        spent_at = _resolve_date(t.get("date"), att.get("date"))
        if ref:
            if ref in seen_refs or await db.expenses.find_one({"chat_id": chat_id, "momo_ref": ref}):
                continue
            seen_refs.add(ref)
        elif await _is_duplicate(chat_id, merchant, amount, currency, spent_at):
            continue
        doc = await _log_expense(
            chat_id,
            {
                "chat_id": chat_id,
                "merchant": merchant,
                "amount": amount,
                "currency": currency,
                "category": cat if cat in _CATEGORIES else "other",
                "description": (t.get("description") or "").strip(),
                "spent_at": spent_at,
                "source": "momo",
                "email_id": f"momo:{att.get('uid', '')}",
                "momo_ref": ref,
                "created_at": _resolve_date(None, None),
            },
        )
        logged.append(doc)

    if not logged:
        return (f"Read {att['filename']} — no new spending found to add (it's already imported, "
                "or contained only money-in / transfers, which we don't count as expenses).")

    totals: dict[str, float] = {}
    for d in logged:
        totals[d["currency"]] = round(totals.get(d["currency"], 0.0) + float(d["amount"]), 2)
    lines = [f"📄 Imported {len(logged)} spending transactions from {att['filename']}:"]
    for d in logged[:20]:
        lines.append(f"  • {d['currency']} {float(d['amount']):,.2f} · {d['merchant']} [{d['category']}]")
    if len(logged) > 20:
        lines.append(f"  …and {len(logged) - 20} more.")
    lines.append("Total: " + ", ".join(f"{c} {a:,.2f}" for c, a in sorted(totals.items())))
    return "\n".join(lines)
