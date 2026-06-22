"""
Job-application tracking.

The bot watches the (Yahoo) inbox: when it sees an application *confirmation* it starts
tracking that application; when it sees a *reply* about one it's tracking (interview,
rejection, offer, recruiter follow-up) it updates the status. A scheduled scan
(`scheduler._application_scan_job`) runs this and proactively messages the user when
something changes, and flags applications that have gone quiet.

Emails are classified by the LLM (subjects/snippets are too varied for rules). On the
very first scan we just set a UID baseline and track *forward* — we don't trawl history.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from difflib import SequenceMatcher
from urllib.parse import quote

from agentzero.config import APPLICATION_STALE_DAYS, JOB_TRACKING_ENABLED
from agentzero.db import get_db
from agentzero.llm import get_provider

logger = logging.getLogger(__name__)

# applied → replied → interview → offer | rejected (closed = manually archived)
_STATUS_LABEL = {
    "applied": "applied — no reply yet",
    "replied": "got a reply",
    "interview": "interview stage 🎯",
    "offer": "offer 🎉",
    "rejected": "rejected",
    "closed": "closed",
}
_VALID_STATUS = set(_STATUS_LABEL)


def _sim(a: str, b: str) -> float:
    return SequenceMatcher(None, (a or "").lower(), (b or "").lower()).ratio()


def _pick_cv(attachments: list[str]) -> str:
    """Pick the CV/résumé filename from a sent email's attachments."""
    if not attachments:
        return ""
    for a in attachments:
        if re.search(r"cv|resume|r[eé]sum", a, re.I):
            return a
    for a in attachments:
        if a.lower().endswith((".pdf", ".doc", ".docx")):
            return a
    return attachments[0]


_QUOTE_PATTERNS = [
    r"\nOn .{0,160}wrote:",            # "On Mon, ... <x> wrote:"
    r"\n-{2,}\s*Original Message\s*-{2,}",
    r"\nFrom:.{0,160}\nSent:",          # Outlook-style quoted header
    r"\n_{5,}",
    r"\n>{1,}\s",                        # quoted lines
]


def _strip_quoted(text: str) -> str:
    """Best-effort: drop quoted reply history / forwarded headers so we keep the new message."""
    if not text:
        return ""
    cut = len(text)
    for pat in _QUOTE_PATTERNS:
        m = re.search(pat, text, re.IGNORECASE | re.DOTALL)
        if m:
            cut = min(cut, m.start())
    return text[:cut].strip()


def _parse_email_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


async def _suggested_action(body: str | None, direction: str | None, sender: str | None) -> dict | None:
    """LLM-suggested next action from the latest EMPLOYER message. None when there's nothing to
    act on (outbound, automated acks/no-reply, dead-end rejections) or on any error. Never raises."""
    body = (body or "").strip()
    if direction != "inbound" or not body:
        return None  # we're waiting on them, or nothing to read — no action
    system = (
        "You advise a job applicant on their single best NEXT ACTION given the latest message an "
        "EMPLOYER sent them. Return ONLY JSON.\n"
        "If the message is an automated 'application received' acknowledgement, a no-reply/FYI "
        "notification, or a rejection with no follow-up value → {\"actionable\": false}.\n"
        "If there IS something worth doing (schedule/confirm an interview, send requested info or "
        "documents, complete an assessment, answer a question, respond to an offer) → "
        '{"actionable": true, "headline": "<short imperative>", "summary": "<1-2 sentences grounded '
        'in the email>", "steps": ["concrete step", ...], "priority": "high|normal|low"}.\n'
        "Ground everything strictly in the actual message; do not invent dates, names, or requirements."
    )
    user = f"From: {sender or '(unknown)'}\n\nMessage:\n{body[:4000]}"
    try:
        raw = await get_provider().chat([{"role": "user", "content": user}], system)
    except Exception:
        logger.exception("suggested_action generation failed")
        return None
    data = _parse_json(raw)
    if not isinstance(data, dict) or not data.get("actionable"):
        return None
    headline = (data.get("headline") or "").strip()
    if not headline:
        return None
    pri = data.get("priority")
    return {
        "headline": headline,
        "summary": (data.get("summary") or "").strip() or None,
        "steps": [s.strip() for s in (data.get("steps") or []) if isinstance(s, str) and s.strip()][:6],
        "priority": pri if pri in ("high", "normal", "low") else "normal",
        "generated_at": datetime.now(timezone.utc),
    }


async def _attach_message(chat_id: int, app_id, email: dict, direction: str) -> None:
    """Record the email that touched an application onto its `messages` thread + last_message_*
    fields, so the dashboard can act on what was actually said (not just the subject)."""
    db = get_db()
    if not email:
        return
    uid = str(email.get("uid") or "")
    body = _strip_quoted(email.get("body") or email.get("snippet") or "")
    msg = {
        "from": email.get("from") or "",
        "direction": direction,
        "sent_at": _parse_email_dt(email.get("date")),
        "body": body,
        "uid": uid,
    }
    app = await db.applications.find_one({"_id": app_id})
    if not app:
        return
    msgs = list(app.get("messages") or [])
    if not any(m.get("uid") == uid and m.get("direction") == direction for m in msgs):
        msgs.append(msg)
        msgs = msgs[-15:]  # keep the thread bounded
    latest = max(msgs, key=lambda m: _aware(m.get("sent_at")) or datetime.min.replace(tzinfo=timezone.utc))
    action = await _suggested_action(latest.get("body"), latest.get("direction"), latest.get("from"))
    await db.applications.update_one(
        {"_id": app_id},
        {"$set": {
            "messages": msgs,
            "last_message_body": latest.get("body"),
            "last_message_snippet": (latest.get("body") or "")[:200] or None,
            "last_message_from": latest.get("from"),
            "last_message_direction": latest.get("direction"),
            "last_message_at": latest.get("sent_at"),
            "suggested_action": action,
        }},
    )


def _aware(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Scan-cursor (per chat) in system_state
# ---------------------------------------------------------------------------

async def _get_last_scan_uid(chat_id: int) -> str | None:
    db = get_db()
    doc = await db.system_state.find_one({"chat_id": chat_id})
    return (doc or {}).get("last_app_scan_uid")


async def _set_last_scan_uid(chat_id: int, uid: str) -> None:
    db = get_db()
    await db.system_state.update_one(
        {"chat_id": chat_id}, {"$set": {"last_app_scan_uid": str(uid)}}, upsert=True
    )


async def _get_sent_cursor(chat_id: int, source: str) -> str | None:
    db = get_db()
    doc = await db.system_state.find_one({"chat_id": chat_id})
    return (doc or {}).get(f"sent_app_cursor_{source}")


async def _set_sent_cursor(chat_id: int, source: str, uid: str) -> None:
    db = get_db()
    await db.system_state.update_one(
        {"chat_id": chat_id}, {"$set": {f"sent_app_cursor_{source}": str(uid)}}, upsert=True
    )


# ---------------------------------------------------------------------------
# LLM classification
# ---------------------------------------------------------------------------

def _parse_json(text: str) -> dict:
    """Best-effort extract a JSON object from an LLM reply (handles code fences/prose)."""
    text = text.strip()
    text = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", text).strip()
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


async def _classify(emails: list[dict], apps: list[dict]) -> list[dict]:
    tracked = "\n".join(
        f"- {a.get('company','?')} — {a.get('role') or '(role unknown)'}" for a in apps
    ) or "(none yet)"
    listing = "\n".join(
        f'[uid {e["uid"]}] From: {e["from"]} | Subject: {e["subject"]}\n  {e.get("snippet","")[:400]}'
        for e in emails
    )
    system = (
        "You classify inbox emails for a job-application tracker. For EACH email decide its "
        "category:\n"
        "- \"confirmation\": confirms the user SUBMITTED an application (e.g. 'thanks for "
        "applying', 'we received your application', 'application submitted'). Extract company "
        "and role.\n"
        "- \"update\": a reply/update about a job the user applied to — interview invite, "
        "assessment/test, rejection, offer, or a recruiter following up. Extract company, role "
        "if present, and new_status as one of: interview, rejected, offer, replied. IMPORTANT: "
        "classify by the email's CONTENT, not by whether the company is already in the tracked "
        "list — an interview invite or rejection from a company NOT yet tracked is still an "
        "\"update\" (we'll start tracking it from that reply).\n"
        "- \"other\": anything else (newsletters, receipts, personal, security notices, etc.).\n\n"
        f"Applications already tracked (to help match updates):\n{tracked}\n\n"
        'Return ONLY JSON, no prose: {"results":[{"uid":"<uid>","category":"confirmation|update|'
        'other","company":"","role":"","new_status":""}]} — exactly one entry per email, reusing '
        "the given uids. Leave fields you can't fill as empty strings."
    )
    try:
        raw = await get_provider().chat([{"role": "user", "content": listing}], system)
    except Exception:
        logger.exception("Application classifier LLM call failed")
        return []
    data = _parse_json(raw)
    results = data.get("results") if isinstance(data, dict) else None
    return results if isinstance(results, list) else []


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

async def _classify_sent(emails: list[dict]) -> list[dict]:
    """Classify emails the USER SENT — find the ones that are job applications."""
    listing = "\n".join(
        f'[uid {e["uid"]}] To: {e.get("to","")} | Subject: {e["subject"]}\n  {e.get("snippet","")[:400]}'
        for e in emails
    )
    system = (
        "These are emails the USER SENT. Find JOB APPLICATIONS — emails where the user is applying "
        "for a job: sending a CV/résumé or cover letter, expressing interest in a specific role, "
        "responding to a job posting, or emailing a recruiter/company to apply. For EACH email set "
        "category:\n"
        "- \"application\": the user is applying for a job. Extract the company (from the recipient "
        "or the body) and the role/title if stated.\n"
        "- \"other\": anything else the user sent — normal work email, personal mail, replies to "
        "friends, newsletters they forwarded, etc.\n\n"
        "Only mark \"application\" when it's genuinely the user applying for a job. Return ONLY JSON, "
        'no prose: {"results":[{"uid":"<uid>","category":"application|other","company":"","role":""}]}'
        " — one entry per email, reusing the given uids."
    )
    try:
        raw = await get_provider().chat([{"role": "user", "content": listing}], system)
    except Exception:
        logger.exception("Sent-application classifier LLM call failed")
        return []
    data = _parse_json(raw)
    results = data.get("results") if isinstance(data, dict) else None
    return results if isinstance(results, list) else []


async def _load_apps(chat_id: int) -> list[dict]:
    db = get_db()
    return await db.applications.find({"chat_id": chat_id}).to_list(None)


async def _find_app(chat_id: int, company: str, role: str = "") -> dict | None:
    apps = await _load_apps(chat_id)
    if not apps:
        return None
    scored = []
    for a in apps:
        s = _sim(company, a.get("company", ""))
        if role and a.get("role"):
            s = 0.7 * s + 0.3 * _sim(role, a["role"])
        scored.append((s, a))
    best_s, best = max(scored, key=lambda x: x[0])
    return best if best_s >= 0.55 else None


async def upsert_application(
    chat_id: int, company: str, role: str = "", status: str = "applied",
    applied_at: datetime | None = None, source: str = "", uid: str = "", cv_used: str = "",
) -> tuple[dict, bool]:
    """Create or update an application. Returns (doc, created)."""
    db = get_db()
    company = (company or "").strip()
    role = (role or "").strip()
    status = status if status in _VALID_STATUS else "applied"
    now = datetime.now(timezone.utc)
    existing = await _find_app(chat_id, company, role) if company else None
    if existing:
        updates = {"last_update_at": now}
        if status and status != existing.get("status"):
            updates["status"] = status
        if role and not existing.get("role"):
            updates["role"] = role
        if uid:
            updates["last_email_uid"] = uid
        if cv_used and not existing.get("cv_used"):
            updates["cv_used"] = cv_used
        await db.applications.update_one({"_id": existing["_id"]}, {"$set": updates})
        existing.update(updates)
        return existing, False
    doc = {
        "chat_id": chat_id,
        "company": company,
        "role": role,
        "status": status,
        "applied_at": applied_at or now,
        "last_update_at": now,
        "last_email_uid": uid,
        "source": source,
        "cv_used": cv_used,
        "stale_notified": False,
        "created_at": now,
    }
    res = await db.applications.insert_one(doc)
    doc["_id"] = res.inserted_id
    return doc, True


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------

async def scan_inbox(chat_id: int) -> dict:
    """Pull new inbox mail, classify, update tracked applications. Returns what changed:
    {"new": [docs], "updates": [(doc, status)]}."""
    from agentzero import yahoo_mail

    empty = {"new": [], "updates": []}
    if not JOB_TRACKING_ENABLED:
        return empty

    last_uid = await _get_last_scan_uid(chat_id)
    emails = await yahoo_mail.fetch_recent("INBOX", 25, last_uid)
    if not emails:
        return empty

    max_uid = str(max(int(e["uid"]) for e in emails))

    # First run: set a baseline and track forward only — don't classify history.
    if last_uid is None:
        await _set_last_scan_uid(chat_id, max_uid)
        logger.info("Application tracker baseline set at uid %s for chat %s", max_uid, chat_id)
        return empty

    apps = await _load_apps(chat_id)
    results = await _classify(emails, apps)
    by_uid = {str(e["uid"]): e for e in emails}

    new_docs: list[dict] = []
    updates: list[tuple] = []
    for r in results:
        if not isinstance(r, dict):
            continue
        cat = (r.get("category") or "").lower()
        company = (r.get("company") or "").strip()
        role = (r.get("role") or "").strip()
        uid = str(r.get("uid") or "")
        src = by_uid.get(uid, {}).get("subject", "")
        if cat == "confirmation" and company:
            doc, created = await upsert_application(
                chat_id, company, role, "applied", source=src, uid=uid
            )
            await _attach_message(chat_id, doc["_id"], by_uid.get(uid, {}), "inbound")
            if created:
                new_docs.append(doc)
        elif cat == "update" and company:
            status = (r.get("new_status") or "replied").lower()
            if status not in _VALID_STATUS:
                status = "replied"
            doc, created = await upsert_application(
                chat_id, company, role, status, source=src, uid=uid
            )
            await _attach_message(chat_id, doc["_id"], by_uid.get(uid, {}), "inbound")
            updates.append((doc, status, created))

    await _set_last_scan_uid(chat_id, max_uid)
    return {"new": new_docs, "updates": updates}


async def scan_sent(chat_id: int) -> list[dict]:
    """Scan the SENT folder of every configured mailbox for outgoing job applications
    (applying by email) and start tracking them. Returns the newly-tracked application docs.
    Per-mailbox cursor; first scan of a mailbox sets a baseline (tracks forward)."""
    from agentzero import imap_mail

    if not JOB_TRACKING_ENABLED:
        return []
    new_tracked: list[dict] = []
    for acc in imap_mail.mail_accounts():
        source = acc["source"]
        folder = acc.get("sent_folder", "Sent")
        cursor = await _get_sent_cursor(chat_id, source)
        emails = await imap_mail.fetch_recent(acc, folder, 25, cursor)
        if not emails:
            continue
        max_uid = str(max(int(e["uid"]) for e in emails))
        if cursor is None:  # first run for this mailbox — baseline forward only
            await _set_sent_cursor(chat_id, source, max_uid)
            logger.info("Sent-application baseline for %s set at uid %s", source, max_uid)
            continue
        by_uid = {str(e["uid"]): e for e in emails}
        for r in await _classify_sent(emails):
            if not isinstance(r, dict) or (r.get("category") or "").lower() != "application":
                continue
            company = (r.get("company") or "").strip()
            if not company:
                continue
            uid = str(r.get("uid") or "")
            cv_used = _pick_cv(by_uid.get(uid, {}).get("attachments", []))
            doc, created = await upsert_application(
                chat_id, company, (r.get("role") or "").strip(), "applied",
                source=f"{source}:sent", uid=uid, cv_used=cv_used,
            )
            await _attach_message(chat_id, doc["_id"], by_uid.get(uid, {}), "outbound")
            if created:
                new_tracked.append(doc)
        await _set_sent_cursor(chat_id, source, max_uid)
    return new_tracked


async def backfill_application_messages(chat_id: int) -> int:
    """One-off: for each tracked application without captured message content, fetch the most
    recent email (by last_email_uid) and attach it. Returns how many were populated.
    Folder/direction are derived: ':sent' source → that account's Sent (outbound); otherwise
    it came from the Yahoo job inbox (inbound)."""
    from agentzero import imap_mail

    db = get_db()
    accounts = {a["source"]: a for a in imap_mail.mail_accounts()}
    populated = 0
    for app in await db.applications.find({"chat_id": chat_id}).to_list(None):
        has_body = bool(app.get("last_message_body"))
        has_action = "suggested_action" in app
        if has_body and has_action:
            continue
        if has_body and not has_action:
            # Body already captured earlier — just generate the suggested action from stored fields.
            action = await _suggested_action(
                app.get("last_message_body"), app.get("last_message_direction"),
                app.get("last_message_from"),
            )
            await db.applications.update_one(
                {"_id": app["_id"]}, {"$set": {"suggested_action": action}}
            )
            populated += 1
            continue
        uid = app.get("last_email_uid")
        src = app.get("source", "") or ""
        if not uid or src == "manual":
            continue
        if src.endswith(":sent"):
            acct = accounts.get(src.split(":")[0])
            folder = acct.get("sent_folder", "Sent") if acct else "Sent"
            direction = "outbound"
        else:
            acct = accounts.get("yahoo")  # inbox-tracked apps come from the Yahoo job inbox
            folder, direction = "INBOX", "inbound"
        if not acct:
            continue
        email = await imap_mail.read_uid(acct, folder, str(uid))
        if not email:
            continue
        await _attach_message(chat_id, app["_id"], email, direction)  # also generates the action
        populated += 1
    return populated


# ---------------------------------------------------------------------------
# Proactive update message
# ---------------------------------------------------------------------------

async def _stale_followups(chat_id: int) -> list[dict]:
    """Applications still 'applied' past the stale window, not yet flagged — warn once."""
    db = get_db()
    now = datetime.now(timezone.utc)
    flagged = []
    for a in await db.applications.find(
        {"chat_id": chat_id, "status": "applied", "stale_notified": {"$ne": True}}
    ).to_list(None):
        applied = _aware(a.get("applied_at"))
        if applied and (now - applied).days >= APPLICATION_STALE_DAYS:
            await db.applications.update_one({"_id": a["_id"]}, {"$set": {"stale_notified": True}})
            a["_days"] = (now - applied).days
            flagged.append(a)
    return flagged


async def gather_application_update(chat_id: int) -> str | None:
    """Scan + build the update text (new tracked apps / status changes / stale follow-ups),
    WITHOUT sending. Returns the message text, or None if nothing's worth saying."""
    changes = await scan_inbox(chat_id)
    sent_new = await scan_sent(chat_id)
    lines: list[str] = []
    for d in changes["new"]:
        role = f" — {d['role']}" if d.get("role") else ""
        lines.append(f"📋 Now tracking: {d['company']}{role}")
    for d in sent_new:
        role = f" — {d['role']}" if d.get("role") else ""
        lines.append(f"📋 Now tracking (you applied by email): {d['company']}{role}")
    for d, status, created in changes["updates"]:
        role = f" ({d['role']})" if d.get("role") else ""
        label = _STATUS_LABEL.get(status, status)
        if created:
            # No prior confirmation seen — we're picking this one up straight from the reply.
            lines.append(f"📋 Now tracking: {d['company']}{role} — {label} (their reply came in first)")
        else:
            lines.append(f"✉️ {d['company']}{role}: {label}")
    for a in await _stale_followups(chat_id):
        role = f" ({a['role']})" if a.get("role") else ""
        lines.append(f"⏳ {a['company']}{role} has gone quiet ({a['_days']}d) — want to follow up?")

    if not lines:
        return None
    return "📋 Job tracker\n\n" + "\n".join(lines)


async def send_application_update(chat_id: int) -> str | None:
    """Scheduled path: gather the update and proactively send it. Returns the message or None."""
    from agentzero.telegram_io import send

    msg = await gather_application_update(chat_id)
    if msg:
        await send(chat_id, msg)
    return msg


# ---------------------------------------------------------------------------
# Formatting (for the list tool)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Structured data access (for the dashboard API)
# ---------------------------------------------------------------------------

_MAILBOX_NAMES = {"yahoo": "Yahoo", "gmail": "Gmail", "manual": "Manual entry"}


def _mailbox_label(source: str) -> str:
    if not source or source == "manual":
        return "Manual entry"
    parts = source.split(":")
    acct = _MAILBOX_NAMES.get(parts[0], parts[0].title())
    folder = "Sent" if len(parts) > 1 and parts[1] == "sent" else "Inbox"
    return f"{acct} · {folder}"


def _mailbox_url(source: str, company: str) -> str | None:
    """A deep search link that opens the right webmail pre-searched for the company —
    IMAP gives no stable per-message URL, so this lands the user near the email."""
    acct = (source or "").split(":")[0]
    q = quote(company or "")
    if acct == "yahoo":
        return f"https://mail.yahoo.com/d/search/keyword={q}"
    if acct == "gmail":
        return f"https://mail.google.com/mail/u/0/#search/{q}"
    return None


async def query_applications(chat_id: int, status: str | None = None) -> list[dict]:
    db = get_db()
    q: dict = {"chat_id": chat_id}
    if status:
        q["status"] = status
    rows = await db.applications.find(q).to_list(None)
    order = {"offer": 0, "interview": 1, "replied": 2, "applied": 3, "rejected": 4, "closed": 5}
    rows.sort(key=lambda r: order.get(r.get("status"), 9))
    return rows


def _iso(dt) -> str | None:
    dt = _aware(dt)
    return dt.isoformat() if dt else None


def serialize_application(doc: dict) -> dict:
    applied = _aware(doc.get("applied_at"))
    updated = _aware(doc.get("last_update_at"))
    src = doc.get("source", "")
    out = {
        "id": str(doc.get("_id")),
        "company": doc.get("company"),
        "title": doc.get("role") or "",
        "status": doc.get("status"),
        "status_label": _STATUS_LABEL.get(doc.get("status"), doc.get("status")),
        "applied_at": applied.isoformat() if applied else None,
        "last_update_at": updated.isoformat() if updated else None,
        "source": src,
        "mailbox": _mailbox_label(src),
        "mailbox_url": _mailbox_url(src, doc.get("company", "")),
        "cv_used": doc.get("cv_used") or "",
        "notes": doc.get("notes") or "",
        # Most-recent message content (additive; null when not captured yet).
        "last_message_body": doc.get("last_message_body"),
        "last_message_snippet": doc.get("last_message_snippet"),
        "last_message_from": doc.get("last_message_from"),
        "last_message_direction": doc.get("last_message_direction"),
        "last_message_at": _iso(doc.get("last_message_at")),
    }
    sa = doc.get("suggested_action")
    out["suggested_action"] = (
        {**sa, "generated_at": _iso(sa.get("generated_at"))} if isinstance(sa, dict) else None
    )
    msgs = doc.get("messages")
    if msgs:
        msgs_sorted = sorted(
            msgs, key=lambda m: _aware(m.get("sent_at")) or datetime.min.replace(tzinfo=timezone.utc)
        )
        out["messages"] = [
            {
                "from": m.get("from"),
                "direction": m.get("direction"),
                "sent_at": _iso(m.get("sent_at")),
                "body": m.get("body"),
            }
            for m in msgs_sorted
        ]
    return out


def status_counts(rows: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for r in rows:
        s = r.get("status", "applied")
        counts[s] = counts.get(s, 0) + 1
    return counts


async def profile_cv(chat_id: int) -> str | None:
    db = get_db()
    prof = await db.profile.find_one({"chat_id": chat_id}) or await db.profile.find_one({}) or {}
    return (prof.get("cv") or "").strip() or None


def format_applications(apps: list[dict], status_filter: str | None = None) -> str:
    apps = [a for a in apps if a.get("status") != "closed"]
    if status_filter:
        apps = [a for a in apps if a.get("status") == status_filter]
    if not apps:
        return "No tracked job applications yet."
    order = {"offer": 0, "interview": 1, "replied": 2, "applied": 3, "rejected": 4}
    apps.sort(key=lambda a: order.get(a.get("status"), 9))
    lines = ["Job applications:"]
    for a in apps:
        role = f" — {a['role']}" if a.get("role") else ""
        applied = _aware(a.get("applied_at"))
        when = f" (applied {applied.strftime('%d %b')})" if applied else ""
        lines.append(f"  • {a['company']}{role}: {_STATUS_LABEL.get(a.get('status'), a.get('status'))}{when}")
    return "\n".join(lines)
