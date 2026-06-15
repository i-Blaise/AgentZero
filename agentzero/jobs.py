"""
Job hunter — pulls software/remote job postings from free, structured sources
(no scraping, no API keys): RemoteOK, Remotive, We Work Remotely.

fetch_jobs() normalises across sources, drops anything already seen (so daily
drops don't repeat), records new ones, and returns them. The LLM does the
matching against the user's stored CV/criteria (injected into the prompt) — here
we just reliably get structured postings in.

send_job_digest() is the scheduled daily drop: fetch new → LLM ranks against the
profile → send a shortlist (only when there's something new).
"""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import httpx

from agentzero.db import get_db
from agentzero.llm import get_provider
from agentzero.prompts import PERSONALITY
from agentzero.telegram_io import send

logger = logging.getLogger(__name__)

_HEADERS = {"User-Agent": "AgentZero/1.0 (personal job assistant)"}
_TIMEOUT = 15


def _match(query: str | None, text: str) -> bool:
    if not query:
        return True
    terms = [t for t in query.lower().split() if t]
    blob = text.lower()
    return any(t in blob for t in terms)


async def _remoteok(query: str | None) -> list[dict]:
    async with httpx.AsyncClient(timeout=_TIMEOUT, headers=_HEADERS) as c:
        r = await c.get("https://remoteok.com/api")
        r.raise_for_status()
        data = r.json()
    out = []
    for it in data:
        if not isinstance(it, dict) or "position" not in it:
            continue  # first element is a legal notice
        blob = f"{it.get('position','')} {it.get('company','')} {' '.join(it.get('tags',[]))} {it.get('description','')}"
        if not _match(query, blob):
            continue
        out.append(
            {
                "source": "RemoteOK",
                "title": it.get("position", "").strip(),
                "company": (it.get("company") or "").strip(),
                "location": it.get("location") or "Remote",
                "url": it.get("url") or it.get("apply_url") or "",
                "snippet": (it.get("description") or "")[:280],
            }
        )
    return out


async def _remotive(query: str | None) -> list[dict]:
    params = {"search": query} if query else {"category": "software-dev"}
    async with httpx.AsyncClient(timeout=_TIMEOUT, headers=_HEADERS) as c:
        r = await c.get("https://remotive.com/api/remote-jobs", params=params)
        r.raise_for_status()
        data = r.json()
    out = []
    for it in data.get("jobs", []):
        out.append(
            {
                "source": "Remotive",
                "title": (it.get("title") or "").strip(),
                "company": (it.get("company_name") or "").strip(),
                "location": it.get("candidate_required_location") or "Remote",
                "url": it.get("url") or "",
                "snippet": (it.get("description") or "")[:280],
            }
        )
    return out


async def _wwr(query: str | None) -> list[dict]:
    url = "https://weworkremotely.com/categories/remote-programming-jobs.rss"
    async with httpx.AsyncClient(timeout=_TIMEOUT, headers=_HEADERS) as c:
        r = await c.get(url)
        r.raise_for_status()
        root = ET.fromstring(r.text)
    out = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        desc = (item.findtext("description") or "").strip()
        if not _match(query, f"{title} {desc}"):
            continue
        company, _, role = title.partition(":")
        out.append(
            {
                "source": "WeWorkRemotely",
                "title": (role or title).strip(),
                "company": company.strip() if role else "",
                "location": "Remote",
                "url": link,
                "snippet": desc[:280],
            }
        )
    return out


async def fetch_jobs(chat_id: int, query: str | None = None, limit: int = 15) -> list[dict]:
    """Fetch new (unseen) postings across sources, record them as seen, return them."""
    db = get_db()
    collected: list[dict] = []
    for fn in (_remotive, _remoteok, _wwr):
        try:
            collected.extend(await fn(query))
        except Exception:
            logger.exception("Job source %s failed", fn.__name__)

    # Dedupe within this batch and against previously-seen URLs.
    seen_urls = {
        d["url"]
        for d in await db.seen_jobs.find({"chat_id": chat_id}).to_list(None)
    }
    fresh: list[dict] = []
    batch_urls: set[str] = set()
    for j in collected:
        u = j.get("url")
        if not u or u in seen_urls or u in batch_urls:
            continue
        batch_urls.add(u)
        fresh.append(j)
        if len(fresh) >= limit:
            break

    if fresh:
        now = datetime.now(timezone.utc)
        await db.seen_jobs.insert_many(
            [
                {"chat_id": chat_id, "url": j["url"], "title": j["title"],
                 "company": j["company"], "seen_at": now}
                for j in fresh
            ]
        )
    return fresh


def format_jobs(jobs: list[dict]) -> str:
    if not jobs:
        return "(no new postings found)"
    lines = []
    for j in jobs:
        loc = f" — {j['location']}" if j.get("location") else ""
        lines.append(f"[{j['source']}] {j['title']} @ {j['company'] or 'unknown'}{loc}\n  {j['url']}\n  {j['snippet']}")
    return "\n\n".join(lines)


async def _profile_text(chat_id: int) -> tuple[str, str]:
    db = get_db()
    prof = await db.profile.find_one({"chat_id": chat_id}) or {}
    return (prof.get("cv", ""), prof.get("criteria", ""))


async def send_job_digest(chat_id: int) -> str | None:
    """Daily drop: fetch new jobs, rank against the profile, send a shortlist if any."""
    cv, criteria = await _profile_text(chat_id)
    if not cv and not criteria:
        return None  # no profile yet — nothing to match against

    jobs = await fetch_jobs(chat_id, query=criteria or None, limit=20)
    if not jobs:
        return None  # nothing new today — stay quiet

    system = (
        f"{PERSONALITY}\n\n"
        "You're the user's job scout. Below is their CV and what they're looking for, then "
        "a list of NEW postings. Pick the genuinely strong matches (skip weak ones — quality "
        "over quantity), and present a short ranked shortlist: role @ company, one line on why "
        "it fits them, and the apply link. If none are a real fit, say so briefly. Be honest "
        "about fit; don't pad the list."
    )
    user = f"CV:\n{cv}\n\nLooking for:\n{criteria}\n\nNew postings:\n{format_jobs(jobs)}"
    try:
        msg = (await get_provider().chat([{"role": "user", "content": user}], system)).strip()
        if msg:
            await send(chat_id, f"🧭 Job drop\n\n{msg}")
            return msg
    except Exception:
        logger.exception("Job digest narration failed — sending raw list")

    fallback = f"🧭 Job drop — {len(jobs)} new postings\n\n{format_jobs(jobs)}"
    await send(chat_id, fallback)
    return fallback
