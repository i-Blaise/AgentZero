"""
Expense tracking from payment receipts.

A scheduled scan reads recent mail from every configured IMAP mailbox (Yahoo + personal
Gmail), the LLM picks out payment receipts and extracts merchant/amount/currency/category,
and each is logged to the `expenses` collection. The scan itself is silent (receipts are
frequent — a ping per purchase would be noise); a weekly summary reports spend, and the
user can query anytime ("how much did I spend this week?").

Per-mailbox UID cursors live in system_state; the first scan of a mailbox just sets a
baseline and tracks forward (no history trawl). Each receipt records its source+uid so the
same email never logs twice.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

from agentzero.config import DEFAULT_CURRENCY, EXPENSE_TRACKING_ENABLED
from agentzero.db import get_db
from agentzero.llm import get_provider

logger = logging.getLogger(__name__)

_CATEGORIES = [
    "food", "transport", "shopping", "subscription", "bills",
    "entertainment", "travel", "health", "charity", "other",
]


def _aware(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _sim(a: str, b: str) -> float:
    return SequenceMatcher(None, (a or "").lower(), (b or "").lower()).ratio()


def _parse_amount(raw) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    s = re.sub(r"[^\d.]", "", str(raw).replace(",", ""))
    try:
        return float(s) if s else None
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Per-mailbox scan cursor
# ---------------------------------------------------------------------------

async def _get_cursor(chat_id: int, source: str) -> str | None:
    db = get_db()
    doc = await db.system_state.find_one({"chat_id": chat_id})
    return (doc or {}).get(f"receipt_cursor_{source}")


async def _set_cursor(chat_id: int, source: str, uid: str) -> None:
    db = get_db()
    await db.system_state.update_one(
        {"chat_id": chat_id}, {"$set": {f"receipt_cursor_{source}": str(uid)}}, upsert=True
    )


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def _parse_json(text: str) -> dict:
    text = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", text.strip()).strip()
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
    return {}


async def _classify(emails: list[dict]) -> list[dict]:
    listing = "\n".join(
        f'[uid {e["uid"]}] From: {e["from"]} | Subject: {e["subject"]} | Date: {e.get("date","")}\n'
        f'  {e.get("snippet","")[:450]}'
        for e in emails
    )
    system = (
        "You extract PAYMENT RECEIPTS (money the user actually SPENT) from emails for an expense "
        "tracker. For EACH email set category:\n"
        "- \"receipt\": the user paid money OUT for a good, service, bill, or subscription — a "
        "purchase/order receipt, card (POS) purchase, ride/food-delivery receipt, bill or "
        "subscription payment, an invoice they paid. Extract merchant, amount (number only), "
        f"currency (3-letter ISO; infer from the symbol, default {DEFAULT_CURRENCY}), date "
        f"(YYYY-MM-DD if present), an expense_category from {_CATEGORIES} (infer it from the "
        "merchant; use 'other' only when genuinely unclear), and a short description.\n"
        "- \"other\": everything that is NOT money the user spent.\n\n"
        "CRITICAL — bank / mobile-money transaction alerts (e.g. from a bank like CalBank): an "
        "expense is money paid to a THIRD-PARTY merchant for a good, service, or bill — NOT money "
        "the user simply moved around. Mark \"receipt\" ONLY for a debit that pays a merchant (card "
        "purchase, POS payment, bill, subscription). You MUST mark \"other\" for ALL of: credits, "
        "deposits, money RECEIVED, incoming transfers, salary, refunds, reversals, declined/failed "
        "transactions, OTP/verification codes, low-balance or balance/mini-statement notices, "
        "person-to-person money the user SENT, AND — importantly — the user moving their OWN money "
        "between their OWN accounts or wallets: bank↔mobile-money transfers, mobile-money funding / "
        "top-ups / 'pull' transactions (e.g. a 'CalPay MTN Pull', wallet load/top-up), and ATM or "
        "cash withdrawals. These are NOT purchases even though they debit the account. If the debit "
        "is a transfer/top-up/withdrawal rather than a payment to a merchant, or the direction is "
        "unclear, choose \"other\".\n\n"
        "Be conservative: only \"receipt\" when real money was genuinely spent on a purchase/bill AND "
        "you can find an amount. Return ONLY JSON, no prose: "
        '{"results":[{"uid":"<uid>","category":"receipt|other","merchant":"","amount":"",'
        '"currency":"","date":"","expense_category":"","description":""}]} — one entry per email.'
    )
    try:
        raw = await get_provider().chat([{"role": "user", "content": listing}], system)
    except Exception:
        logger.exception("Expense classifier LLM call failed")
        return []
    data = _parse_json(raw)
    results = data.get("results") if isinstance(data, dict) else None
    return results if isinstance(results, list) else []


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------

async def _already_logged(chat_id: int, email_id: str) -> bool:
    db = get_db()
    return bool(await db.expenses.find_one({"chat_id": chat_id, "email_id": email_id}))


async def _log_expense(chat_id: int, doc: dict) -> dict:
    db = get_db()
    res = await db.expenses.insert_one(doc)
    doc["_id"] = res.inserted_id
    return doc


async def scan_receipts(chat_id: int) -> list[dict]:
    """Scan every configured mailbox for new receipts, log them, return what was logged."""
    from agentzero import imap_mail

    if not EXPENSE_TRACKING_ENABLED:
        return []

    logged: list[dict] = []
    for acc in imap_mail.mail_accounts():
        source = acc["source"]
        cursor = await _get_cursor(chat_id, source)
        emails = await imap_mail.fetch_recent(acc, "INBOX", 30, cursor)
        if not emails:
            continue
        max_uid = str(max(int(e["uid"]) for e in emails))

        if cursor is None:  # first run for this mailbox — baseline forward only
            await _set_cursor(chat_id, source, max_uid)
            logger.info("Expense scan baseline for %s set at uid %s", source, max_uid)
            continue

        logged.extend(await _log_receipts(chat_id, source, emails, await _classify(emails)))
        await _set_cursor(chat_id, source, max_uid)
    return logged


async def _log_receipts(
    chat_id: int, source: str, emails: list[dict], results: list[dict]
) -> list[dict]:
    """Turn classifier results into logged expense docs (receipts only, deduped)."""
    by_uid = {str(e["uid"]): e for e in emails}
    logged: list[dict] = []
    for r in results:
        if not isinstance(r, dict) or (r.get("category") or "").lower() != "receipt":
            continue
        amount = _parse_amount(r.get("amount"))
        if amount is None or amount <= 0:
            continue
        uid = str(r.get("uid") or "")
        email_id = f"{source}:{uid}"
        if await _already_logged(chat_id, email_id):
            continue
        src_email = by_uid.get(uid, {})
        spent_at = _resolve_date(r.get("date"), src_email.get("date"))
        merchant = (r.get("merchant") or "Unknown").strip()
        currency = (r.get("currency") or DEFAULT_CURRENCY).strip().upper()[:3]
        if await _is_duplicate(chat_id, merchant, amount, currency, spent_at):
            continue  # same merchant+amount+currency same day = likely a duplicate alert
        cat = (r.get("expense_category") or "other").lower()
        doc = await _log_expense(
            chat_id,
            {
                "chat_id": chat_id,
                "merchant": merchant,
                "amount": amount,
                "currency": currency,
                "category": cat if cat in _CATEGORIES else "other",
                "description": (r.get("description") or "").strip(),
                "spent_at": spent_at,
                "source": source,
                "email_id": email_id,
                "created_at": datetime.now(timezone.utc),
            },
        )
        logged.append(doc)
    return logged


async def _is_duplicate(
    chat_id: int, merchant: str, amount: float, currency: str, spent_at: datetime
) -> bool:
    """A same merchant+amount+currency on the same calendar day is almost certainly a duplicate
    alert for one transaction (banks sometimes send two). Trade-off: two genuine identical
    same-day purchases would be merged — rare, and the user can add the second manually."""
    db = get_db()
    same = await db.expenses.find(
        {"chat_id": chat_id, "amount": amount, "currency": currency}
    ).to_list(None)
    for ex in same:
        exd = _aware(ex.get("spent_at"))
        if (
            ex.get("merchant", "").strip().lower() == merchant.lower()
            and exd
            and exd.date() == spent_at.date()
        ):
            return True
    return False


def _chunks(seq: list, n: int):
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


async def backfill_receipts(chat_id: int, days: int = 30, per_account_limit: int = 600) -> list[dict]:
    """One-off historical scan: pull the last `days` of mail from every mailbox, classify in
    batches, and log receipts (deduped by email_id). Does NOT move the forward scan cursor."""
    from agentzero import imap_mail

    if not EXPENSE_TRACKING_ENABLED:
        return []
    logged: list[dict] = []
    for acc in imap_mail.mail_accounts():
        source = acc["source"]
        emails = await imap_mail.fetch_since(acc, days, per_account_limit)
        for chunk in _chunks(emails, 25):
            results = await _classify(chunk)
            logged.extend(await _log_receipts(chat_id, source, chunk, results))
        logger.info("Backfill (%s, %dd): scanned %d emails", source, days, len(emails))
    return logged


def _resolve_date(parsed: str | None, email_date: str | None) -> datetime:
    for fmt, val in (("%Y-%m-%d", parsed), ("%Y-%m-%d %H:%M", email_date)):
        if val:
            try:
                return datetime.strptime(val, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Query / summary
# ---------------------------------------------------------------------------

_PERIOD_DAYS = {"today": 1, "week": 7, "month": 30, "all": None}


def _period_start(period: str) -> datetime | None:
    days = _PERIOD_DAYS.get(period, 30)
    if days is None:
        return None
    if period == "today":
        now = datetime.now(timezone.utc)
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    return datetime.now(timezone.utc) - timedelta(days=days)


async def _fetch(chat_id: int, period: str, category: str | None) -> list[dict]:
    db = get_db()
    q: dict = {"chat_id": chat_id}
    if category:
        q["category"] = category
    rows = await db.expenses.find(q).to_list(None)
    start = _period_start(period)
    if start:
        rows = [r for r in rows if _aware(r.get("spent_at")) and _aware(r["spent_at"]) >= start]
    rows.sort(key=lambda r: _aware(r.get("spent_at")) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return rows


def _totals_by_currency(rows: list[dict]) -> dict:
    totals: dict[str, float] = {}
    for r in rows:
        cur = r.get("currency", DEFAULT_CURRENCY)
        totals[cur] = totals.get(cur, 0.0) + float(r.get("amount", 0))
    return totals


def _fmt_money(totals: dict) -> str:
    return ", ".join(f"{cur} {amt:,.2f}" for cur, amt in sorted(totals.items())) or "0"


async def list_expenses(chat_id: int, period: str = "month", category: str | None = None) -> str:
    rows = await _fetch(chat_id, period, category)
    if not rows:
        return f"No expenses logged for '{period}'" + (f" in {category}." if category else ".")
    lines = [f"Expenses ({period}{', ' + category if category else ''}):"]
    for r in rows[:20]:
        when = _aware(r.get("spent_at"))
        d = when.strftime("%d %b") if when else ""
        desc = f" — {r['description']}" if r.get("description") else ""
        lines.append(f"  • {d} {r['currency']} {float(r['amount']):,.2f} · {r['merchant']} [{r['category']}]{desc}")
    lines.append(f"Total: {_fmt_money(_totals_by_currency(rows))}")
    return "\n".join(lines)


async def expense_summary(chat_id: int, period: str = "month") -> str:
    rows = await _fetch(chat_id, period, None)
    if not rows:
        return f"No expenses logged for '{period}' yet."
    by_cat: dict[str, dict] = {}
    for r in rows:
        by_cat.setdefault(r["category"], {})
        cur = r.get("currency", DEFAULT_CURRENCY)
        by_cat[r["category"]][cur] = by_cat[r["category"]].get(cur, 0.0) + float(r["amount"])
    lines = [f"Spending summary ({period}) — {len(rows)} receipts:"]
    for cat in sorted(by_cat, key=lambda c: -sum(by_cat[c].values())):
        lines.append(f"  • {cat}: {_fmt_money(by_cat[cat])}")
    lines.append(f"Total: {_fmt_money(_totals_by_currency(rows))}")
    return "\n".join(lines)


async def add_expense(
    chat_id: int, merchant: str, amount: float, currency: str = "",
    category: str = "other", description: str = "",
) -> dict:
    cat = (category or "other").lower()
    doc = {
        "chat_id": chat_id,
        "merchant": merchant.strip() or "Unknown",
        "amount": float(amount),
        "currency": (currency or DEFAULT_CURRENCY).strip().upper()[:3],
        "category": cat if cat in _CATEGORIES else "other",
        "description": (description or "").strip(),
        "spent_at": datetime.now(timezone.utc),
        "source": "manual",
        "email_id": "",
        "created_at": datetime.now(timezone.utc),
    }
    return await _log_expense(chat_id, doc)


async def delete_expense(chat_id: int, query: str, amount: float | None = None) -> str:
    """Remove a logged expense the user says is wrong / not really an expense. Fuzzy-matches
    by merchant or description; `amount` disambiguates when several merchants match."""
    db = get_db()
    rows = await db.expenses.find({"chat_id": chat_id}).to_list(None)
    if not rows:
        return "No expenses logged to delete."

    def score(r: dict) -> float:
        s = _sim(query, r.get("merchant", ""))
        if r.get("description"):
            s = max(s, _sim(query, r["description"]))
        return s

    candidates = rows
    if amount is not None:
        amt_matches = [r for r in rows if abs(float(r.get("amount", 0)) - amount) < 0.01]
        if amt_matches:
            candidates = amt_matches

    scored = sorted(candidates, key=score, reverse=True)
    best = scored[0]
    if amount is None and score(best) < 0.4:
        return f'No expense matching "{query}".'

    strong = [r for r in scored if score(r) >= 0.6]
    if amount is None and len(strong) > 1:
        listed = "\n".join(
            f"  • {r['currency']} {float(r['amount']):,.2f} · {r['merchant']}"
            + (f" ({_aware(r['spent_at']).strftime('%d %b')})" if _aware(r.get('spent_at')) else "")
            for r in strong[:5]
        )
        return f'Several expenses match "{query}" — tell me the amount too:\n{listed}'

    await db.expenses.delete_one({"_id": best["_id"]})
    return f"🗑️ Removed: {best['currency']} {float(best['amount']):,.2f} · {best['merchant']}."


async def purge_scanned_expenses(chat_id: int) -> int:
    """Delete email-sourced expenses (keep manual entries) — for a clean re-scan. Returns count."""
    db = get_db()
    res = await db.expenses.delete_many(
        {"chat_id": chat_id, "source": {"$in": ["yahoo", "gmail"]}}
    )
    return res.deleted_count


# ---------------------------------------------------------------------------
# Structured data access (for the dashboard API)
# ---------------------------------------------------------------------------

async def query_range(
    chat_id: int, start: datetime | None, end: datetime | None, category: str | None = None
) -> list[dict]:
    """Raw expense docs in [start, end], newest first. None bounds = open-ended."""
    db = get_db()
    q: dict = {"chat_id": chat_id}
    if category:
        q["category"] = category
    out = []
    for r in await db.expenses.find(q).to_list(None):
        sa = _aware(r.get("spent_at"))
        if sa is None or (start and sa < start) or (end and sa > end):
            continue
        out.append(r)
    out.sort(key=lambda r: _aware(r["spent_at"]), reverse=True)
    return out


def serialize_expense(r: dict) -> dict:
    sa = _aware(r.get("spent_at"))
    return {
        "id": str(r.get("_id")),
        "merchant": r.get("merchant"),
        "amount": float(r.get("amount", 0)),
        "currency": r.get("currency"),
        "category": r.get("category"),
        "description": r.get("description", ""),
        "spent_at": sa.isoformat() if sa else None,
        "source": r.get("source"),
    }


def summary_data(rows: list[dict]) -> dict:
    by_currency: dict[str, float] = {}
    by_category: dict[str, dict] = {}
    for r in rows:
        cur = r.get("currency", DEFAULT_CURRENCY)
        amt = float(r.get("amount", 0))
        cat = r.get("category", "other")
        by_currency[cur] = round(by_currency.get(cur, 0.0) + amt, 2)
        by_category.setdefault(cat, {})
        by_category[cat][cur] = round(by_category[cat].get(cur, 0.0) + amt, 2)
    return {"count": len(rows), "by_currency": by_currency, "by_category": by_category}


def timeseries_data(rows: list[dict], bucket: str = "day") -> list[dict]:
    keyfn = {
        "day": lambda d: d.strftime("%Y-%m-%d"),
        "week": lambda d: d.strftime("%G-W%V"),
        "month": lambda d: d.strftime("%Y-%m"),
    }.get(bucket, lambda d: d.strftime("%Y-%m-%d"))
    buckets: dict[str, dict] = {}
    for r in rows:
        sa = _aware(r.get("spent_at"))
        if not sa:
            continue
        k = keyfn(sa)
        cur = r.get("currency", DEFAULT_CURRENCY)
        buckets.setdefault(k, {})
        buckets[k][cur] = round(buckets[k].get(cur, 0.0) + float(r.get("amount", 0)), 2)
    return [{"date": k, "totals": buckets[k]} for k in sorted(buckets)]


# ---------------------------------------------------------------------------
# Proactive weekly summary
# ---------------------------------------------------------------------------

async def send_weekly_summary(chat_id: int) -> str | None:
    from agentzero.telegram_io import send

    rows = await _fetch(chat_id, "week", None)
    if not rows:
        return None
    msg = "💸 This week's spending\n\n" + await expense_summary(chat_id, "week")
    await send(chat_id, msg)
    return msg
