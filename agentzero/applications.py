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
            if created:
                new_docs.append(doc)
        elif cat == "update" and company:
            status = (r.get("new_status") or "replied").lower()
            if status not in _VALID_STATUS:
                status = "replied"
            doc, created = await upsert_application(
                chat_id, company, role, status, source=src, uid=uid
            )
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
            if created:
                new_tracked.append(doc)
        await _set_sent_cursor(chat_id, source, max_uid)
    return new_tracked


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


def serialize_application(doc: dict) -> dict:
    applied = _aware(doc.get("applied_at"))
    updated = _aware(doc.get("last_update_at"))
    src = doc.get("source", "")
    return {
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
    }


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
