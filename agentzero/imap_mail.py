"""
Generic, multi-account IMAP reader — used by the background scanners (expenses, and
potentially others) to pull recent messages from several mailboxes uniformly.

Yahoo's interactive chat tools live in yahoo_mail.py; this module is the account-agnostic
batch fetcher. It reuses yahoo_mail's body/header helpers. Everything is READ-ONLY
(readonly select + BODY.PEEK).
"""
from __future__ import annotations

import asyncio
import email
import imaplib
import logging
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from agentzero.config import (
    GMAIL_IMAP_APP_PASSWORD,
    GMAIL_IMAP_ENABLED,
    GMAIL_IMAP_USER,
    YAHOO_MAIL_APP_PASSWORD,
    YAHOO_MAIL_ENABLED,
    YAHOO_MAIL_USER,
)
from agentzero.yahoo_mail import _decode, _extract_body

logger = logging.getLogger(__name__)


def mail_accounts() -> list[dict]:
    """The IMAP mailboxes that are configured and enabled, for background scanning."""
    spec = [
        {
            "source": "yahoo",
            "host": "imap.mail.yahoo.com",
            "enabled": YAHOO_MAIL_ENABLED,
            "user": YAHOO_MAIL_USER,
            "password": YAHOO_MAIL_APP_PASSWORD,
            "sent_folder": "Sent",
        },
        {
            "source": "gmail",
            "host": "imap.gmail.com",
            "enabled": GMAIL_IMAP_ENABLED,
            "user": GMAIL_IMAP_USER,
            "password": GMAIL_IMAP_APP_PASSWORD,
            "sent_folder": "[Gmail]/Sent Mail",
        },
    ]
    return [a for a in spec if a["enabled"] and a["user"] and a["password"]]


def _fetch_uid_list(M: imaplib.IMAP4_SSL, uids: list) -> list[dict]:
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
        attachments = []
        if msg.is_multipart():
            for part in msg.walk():
                if "attachment" in str(part.get("Content-Disposition") or "").lower():
                    fn = part.get_filename()
                    if fn:
                        attachments.append(_decode(fn))
        full_body = _extract_body(msg)
        out.append(
            {
                "uid": uid.decode() if isinstance(uid, bytes) else str(uid),
                "from": _decode(msg.get("From")),
                "to": _decode(msg.get("To")),
                "subject": _decode(msg.get("Subject")) or "(no subject)",
                "date": date,
                "snippet": full_body[:700],
                "body": full_body[:10000],
                "attachments": attachments,
            }
        )
    return out


def _sync_fetch_recent(
    host: str, user: str, password: str, folder: str, limit: int, since_uid: str | None
) -> list[dict]:
    M = imaplib.IMAP4_SSL(host, 993)
    M.login(user, password)
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
            uids = [u for u in uids if int(u) > int(since_uid)]
        return _fetch_uid_list(M, uids[-limit:])
    finally:
        try:
            M.logout()
        except Exception:
            pass


def _sync_fetch_since(
    host: str, user: str, password: str, folder: str, limit: int, days: int
) -> list[dict]:
    """Fetch messages received within the last `days` (IMAP SINCE), most recent first capped."""
    since_str = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%d-%b-%Y")
    M = imaplib.IMAP4_SSL(host, 993)
    M.login(user, password)
    try:
        M.select(folder, readonly=True)
        typ, data = M.uid("search", None, "SINCE", since_str)
        if typ != "OK" or not data or not data[0]:
            return []
        uids = data[0].split()[-limit:]
        return _fetch_uid_list(M, uids)
    finally:
        try:
            M.logout()
        except Exception:
            pass


async def fetch_recent(
    account: dict, folder: str = "INBOX", limit: int = 30, since_uid: str | None = None
) -> list[dict]:
    """Recent messages (newer than since_uid) from one account. [] on error."""
    try:
        return await asyncio.to_thread(
            _sync_fetch_recent,
            account["host"], account["user"], account["password"],
            folder or "INBOX", max(1, min(int(limit or 30), 50)), since_uid,
        )
    except Exception:
        logger.exception("IMAP fetch_recent failed for %s", account.get("source"))
        return []


def _sync_read_uid(host: str, user: str, password: str, folder: str, uid: str) -> dict | None:
    M = imaplib.IMAP4_SSL(host, 993)
    M.login(user, password)
    try:
        M.select(folder, readonly=True)
        out = _fetch_uid_list(M, [uid.encode() if isinstance(uid, str) else uid])
        return out[0] if out else None
    finally:
        try:
            M.logout()
        except Exception:
            pass


async def read_uid(account: dict, folder: str, uid: str) -> dict | None:
    """Read one message by uid from a given account+folder. None on error/not found."""
    try:
        return await asyncio.to_thread(
            _sync_read_uid, account["host"], account["user"], account["password"], folder, str(uid)
        )
    except Exception:
        logger.exception("IMAP read_uid failed for %s/%s", account.get("source"), folder)
        return None


async def fetch_since(account: dict, days: int = 30, limit: int = 600) -> list[dict]:
    """Messages from the last `days` for one account (for historical backfill). [] on error."""
    try:
        return await asyncio.to_thread(
            _sync_fetch_since,
            account["host"], account["user"], account["password"],
            "INBOX", max(1, min(int(limit), 1500)), max(1, int(days)),
        )
    except Exception:
        logger.exception("IMAP fetch_since failed for %s", account.get("source"))
        return []
