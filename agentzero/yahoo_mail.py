"""
Yahoo Mail — read-only access over IMAP.

Yahoo doesn't need OAuth like Google; it exposes standard IMAP. The user generates
a Yahoo *app password* (Account Security → Generate app password) and we log in with
that. Everything here is READ-ONLY by construction: the mailbox is opened with
`readonly=True` and messages are fetched with BODY.PEEK, so nothing is marked read,
moved, or deleted.

imaplib is blocking, so the sync work runs in a thread via asyncio.to_thread; the
public `yahoo_search` / `yahoo_read` coroutines return plain strings the tool loop
feeds back to the model (same pattern as web_search / Gmail).
"""
from __future__ import annotations

import asyncio
import email
import imaplib
import logging
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime

from agentzero.config import (
    YAHOO_MAIL_APP_PASSWORD,
    YAHOO_MAIL_ENABLED,
    YAHOO_MAIL_USER,
)
from agentzero.web import _html_to_text

logger = logging.getLogger(__name__)

IMAP_HOST = "imap.mail.yahoo.com"
IMAP_PORT = 993
_MAX_BODY_CHARS = 6000
_NOT_CONFIGURED = (
    "Yahoo Mail isn't connected yet. Set YAHOO_MAIL_ENABLED=true, YAHOO_MAIL_USER and "
    "YAHOO_MAIL_APP_PASSWORD (a Yahoo app password, not your login password) in the env."
)


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _connect() -> imaplib.IMAP4_SSL:
    M = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    M.login(YAHOO_MAIL_USER, YAHOO_MAIL_APP_PASSWORD)
    return M


def _extract_body(msg: email.message.Message) -> str:
    """Pull readable text from a message — prefer text/plain, fall back to stripped HTML."""
    plain, html = "", ""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition") or "")
            if "attachment" in disp.lower():
                continue
            try:
                payload = part.get_payload(decode=True)
                if payload is None:
                    continue
                text = payload.decode(part.get_content_charset() or "utf-8", "replace")
            except Exception:
                continue
            if ctype == "text/plain" and not plain:
                plain = text
            elif ctype == "text/html" and not html:
                html = text
    else:
        try:
            payload = msg.get_payload(decode=True)
            text = payload.decode(msg.get_content_charset() or "utf-8", "replace") if payload else ""
        except Exception:
            text = ""
        if msg.get_content_type() == "text/html":
            html = text
        else:
            plain = text
    body = plain or (_html_to_text(html) if html else "")
    return body.strip()


# ---------------------------------------------------------------------------
# Blocking IMAP work (run in a thread)
# ---------------------------------------------------------------------------

def _sync_search(query: str, folder: str, limit: int) -> list[dict]:
    M = _connect()
    try:
        M.select(folder, readonly=True)
        if query:
            typ, data = M.uid("search", None, "TEXT", query)
        else:
            typ, data = M.uid("search", None, "ALL")
        if typ != "OK" or not data or not data[0]:
            return []
        uids = data[0].split()[-limit:][::-1]  # most recent first
        out: list[dict] = []
        for uid in uids:
            typ, msgdata = M.uid(
                "fetch", uid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])"
            )
            if typ != "OK" or not msgdata or not msgdata[0]:
                continue
            hdr = email.message_from_bytes(msgdata[0][1])
            date = ""
            if hdr.get("Date"):
                try:
                    date = parsedate_to_datetime(hdr["Date"]).strftime("%Y-%m-%d %H:%M")
                except Exception:
                    date = _decode(hdr.get("Date"))
            out.append(
                {
                    "uid": uid.decode() if isinstance(uid, bytes) else str(uid),
                    "from": _decode(hdr.get("From")),
                    "subject": _decode(hdr.get("Subject")) or "(no subject)",
                    "date": date,
                }
            )
        return out
    finally:
        try:
            M.logout()
        except Exception:
            pass


def _sync_fetch_recent(folder: str, limit: int, since_uid: str | None) -> list[dict]:
    """Fetch recent messages (newer than since_uid) WITH a body snippet, in one session.
    Used by the job-application scanner so it isn't opening a connection per message."""
    M = _connect()
    try:
        M.select(folder, readonly=True)
        if since_uid:
            typ, data = M.uid("search", None, f"UID {int(since_uid) + 1}:*")
        else:
            typ, data = M.uid("search", None, "ALL")
        if typ != "OK" or not data or not data[0]:
            return []
        uids = data[0].split()
        if since_uid:
            # "UID n:*" always returns at least the highest uid even if ≤ n — filter it.
            uids = [u for u in uids if int(u) > int(since_uid)]
        uids = uids[-limit:]  # oldest → newest
        out: list[dict] = []
        for uid in uids:
            typ, md = M.uid("fetch", uid, "(BODY.PEEK[])")
            if typ != "OK" or not md or not md[0]:
                continue
            msg = email.message_from_bytes(md[0][1])
            date = ""
            if msg.get("Date"):
                try:
                    date = parsedate_to_datetime(msg["Date"]).strftime("%Y-%m-%d %H:%M")
                except Exception:
                    date = _decode(msg.get("Date"))
            out.append(
                {
                    "uid": uid.decode() if isinstance(uid, bytes) else str(uid),
                    "from": _decode(msg.get("From")),
                    "subject": _decode(msg.get("Subject")) or "(no subject)",
                    "date": date,
                    "snippet": _extract_body(msg)[:600],
                }
            )
        return out
    finally:
        try:
            M.logout()
        except Exception:
            pass


def _sync_read(uid: str, folder: str) -> dict | None:
    M = _connect()
    try:
        M.select(folder, readonly=True)
        typ, data = M.uid("fetch", uid, "(BODY.PEEK[])")
        if typ != "OK" or not data or not data[0]:
            return None
        msg = email.message_from_bytes(data[0][1])
        date = ""
        if msg.get("Date"):
            try:
                date = parsedate_to_datetime(msg["Date"]).strftime("%Y-%m-%d %H:%M")
            except Exception:
                date = _decode(msg.get("Date"))
        return {
            "from": _decode(msg.get("From")),
            "to": _decode(msg.get("To")),
            "subject": _decode(msg.get("Subject")) or "(no subject)",
            "date": date,
            "body": _extract_body(msg),
        }
    finally:
        try:
            M.logout()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Public async tools
# ---------------------------------------------------------------------------

async def yahoo_search(query: str = "", folder: str = "INBOX", limit: int = 10) -> str:
    if not YAHOO_MAIL_ENABLED:
        return _NOT_CONFIGURED
    limit = max(1, min(int(limit or 10), 25))
    try:
        rows = await asyncio.to_thread(_sync_search, (query or "").strip(), folder or "INBOX", limit)
    except imaplib.IMAP4.error:
        logger.exception("Yahoo IMAP search auth/protocol error")
        return "Couldn't log in to Yahoo Mail — double-check YAHOO_MAIL_USER and the app password."
    except Exception:
        logger.exception("Yahoo IMAP search failed")
        return "Yahoo Mail search failed — the server didn't respond. Try again shortly."

    if not rows:
        scope = f' matching "{query}"' if query else ""
        return f"No messages found in {folder}{scope}."
    header = f'{folder} — {len(rows)} ' + (f'matching "{query}"' if query else "most recent") + ":"
    lines = [header]
    for r in rows:
        lines.append(
            f"[uid {r['uid']}] {r['date']} — From: {r['from']} | {r['subject']}"
        )
    lines.append('(call yahoo_read with a uid to read that message in full)')
    return "\n".join(lines)


async def fetch_recent(
    folder: str = "INBOX", limit: int = 25, since_uid: str | None = None
) -> list[dict]:
    """Structured recent messages (uid/from/subject/date/snippet) for the scanner. Returns
    [] if Yahoo isn't configured or on error — callers treat that as 'nothing new'."""
    if not YAHOO_MAIL_ENABLED:
        return []
    try:
        return await asyncio.to_thread(
            _sync_fetch_recent, folder or "INBOX", max(1, min(int(limit or 25), 50)), since_uid
        )
    except Exception:
        logger.exception("Yahoo IMAP fetch_recent failed")
        return []


async def yahoo_read(uid: str, folder: str = "INBOX") -> str:
    if not YAHOO_MAIL_ENABLED:
        return _NOT_CONFIGURED
    if not uid:
        return "Give me the uid of the message to read (from yahoo_search)."
    try:
        msg = await asyncio.to_thread(_sync_read, str(uid).strip(), folder or "INBOX")
    except imaplib.IMAP4.error:
        logger.exception("Yahoo IMAP read auth/protocol error")
        return "Couldn't log in to Yahoo Mail — double-check the app password."
    except Exception:
        logger.exception("Yahoo IMAP read failed")
        return "Couldn't read that Yahoo message — try again shortly."

    if not msg:
        return f"No message with uid {uid} in {folder}."
    body = msg["body"] or "(no readable text body)"
    if len(body) > _MAX_BODY_CHARS:
        body = body[:_MAX_BODY_CHARS] + "\n\n…[truncated]"
    return (
        f"From: {msg['from']}\nTo: {msg['to']}\nDate: {msg['date']}\n"
        f"Subject: {msg['subject']}\n\n{body}"
    )
