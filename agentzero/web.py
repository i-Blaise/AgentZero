"""
Web search + fetch — gives the bot eyes on the internet so it can research,
look things up, and read pages, all inside the existing tool loop.

`web_fetch` needs no API key (plain httpx GET + a light HTML→text strip).
`web_search` picks a backend: Tavily or Brave when a key is configured
(reliable from a datacenter IP), else a best-effort keyless DuckDuckGo fallback.

Both return plain strings — the tool loop feeds them back to the model, which
reasons over them and writes the final reply.
"""
from __future__ import annotations

import logging
import re

import httpx

from agentzero.config import (
    BRAVE_API_KEY,
    TAVILY_API_KEY,
    WEB_SEARCH_PROVIDER,
)

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; AgentZero/1.0; personal assistant)"
    )
}
_TIMEOUT = 20
_MAX_PAGE_CHARS = 6000


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

_SCRIPT_STYLE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\r\f\v]+")
_BLANKS = re.compile(r"\n\s*\n\s*\n+")


def _html_to_text(html: str) -> str:
    """Crude but dependency-free: drop scripts/styles/tags, collapse whitespace."""
    text = _SCRIPT_STYLE.sub(" ", html)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</(p|div|li|h[1-6]|tr)>", "\n", text, flags=re.IGNORECASE)
    text = _TAG.sub("", text)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&#39;", "'")
        .replace("&quot;", '"')
    )
    text = _WS.sub(" ", text)
    text = _BLANKS.sub("\n\n", text)
    return text.strip()


async def web_fetch(url: str) -> str:
    """Fetch a URL and return readable text (truncated)."""
    if not url or not url.lower().startswith(("http://", "https://")):
        return f"Invalid URL: {url!r}. Give me a full http(s):// URL."
    try:
        async with httpx.AsyncClient(
            timeout=_TIMEOUT, headers=_HEADERS, follow_redirects=True
        ) as c:
            r = await c.get(url)
            r.raise_for_status()
            ctype = r.headers.get("content-type", "")
            body = r.text
    except httpx.HTTPStatusError as e:
        return f"Couldn't fetch {url} — HTTP {e.response.status_code}."
    except Exception:
        logger.exception("web_fetch failed for %s", url)
        return f"Couldn't fetch {url} — the request failed."

    text = body if "html" not in ctype.lower() else _html_to_text(body)
    if not text:
        return f"Fetched {url} but found no readable text."
    if len(text) > _MAX_PAGE_CHARS:
        text = text[:_MAX_PAGE_CHARS] + "\n\n…[truncated]"
    return f"Content of {url}:\n\n{text}"


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def _fmt_results(results: list[dict]) -> str:
    if not results:
        return "No results found."
    lines = []
    for i, r in enumerate(results, 1):
        title = (r.get("title") or "").strip() or "(untitled)"
        url = (r.get("url") or "").strip()
        snippet = (r.get("snippet") or "").strip()
        block = f"{i}. {title}\n   {url}"
        if snippet:
            block += f"\n   {snippet[:300]}"
        lines.append(block)
    return "\n".join(lines)


async def _tavily(query: str, max_results: int) -> list[dict]:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.post(
            "https://api.tavily.com/search",
            json={
                "api_key": TAVILY_API_KEY,
                "query": query,
                "max_results": max_results,
                "search_depth": "basic",
            },
        )
        r.raise_for_status()
        data = r.json()
    return [
        {"title": it.get("title"), "url": it.get("url"), "snippet": it.get("content")}
        for it in data.get("results", [])
    ]


async def _brave(query: str, max_results: int) -> list[dict]:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": max_results},
            headers={
                "Accept": "application/json",
                "X-Subscription-Token": BRAVE_API_KEY,
            },
        )
        r.raise_for_status()
        data = r.json()
    return [
        {"title": it.get("title"), "url": it.get("url"), "snippet": it.get("description")}
        for it in data.get("web", {}).get("results", [])[:max_results]
    ]


_DDG_RESULT = re.compile(
    r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>.*?'
    r'(?:class="result__snippet"[^>]*>(.*?)</a>)?',
    re.DOTALL | re.IGNORECASE,
)


async def _duckduckgo(query: str, max_results: int) -> list[dict]:
    async with httpx.AsyncClient(
        timeout=_TIMEOUT, headers=_HEADERS, follow_redirects=True
    ) as c:
        r = await c.post(
            "https://html.duckduckgo.com/html/", data={"q": query}
        )
        r.raise_for_status()
        html = r.text
    out: list[dict] = []
    for m in _DDG_RESULT.finditer(html):
        url, title_html, snippet_html = m.group(1), m.group(2), m.group(3) or ""
        out.append(
            {
                "title": _html_to_text(title_html),
                "url": url,
                "snippet": _html_to_text(snippet_html),
            }
        )
        if len(out) >= max_results:
            break
    return out


async def web_search(query: str, max_results: int = 5) -> str:
    """Search the web and return a ranked list of results (title, url, snippet)."""
    query = (query or "").strip()
    if not query:
        return "Give me something to search for."
    max_results = max(1, min(int(max_results or 5), 10))

    provider = WEB_SEARCH_PROVIDER.lower()
    if provider == "auto":
        if TAVILY_API_KEY:
            provider = "tavily"
        elif BRAVE_API_KEY:
            provider = "brave"
        else:
            provider = "duckduckgo"

    try:
        if provider == "tavily":
            if not TAVILY_API_KEY:
                return "Web search isn't configured — set TAVILY_API_KEY (free tier at tavily.com)."
            results = await _tavily(query, max_results)
        elif provider == "brave":
            if not BRAVE_API_KEY:
                return "Web search isn't configured — set BRAVE_API_KEY."
            results = await _brave(query, max_results)
        else:
            results = await _duckduckgo(query, max_results)
    except Exception:
        logger.exception("web_search failed (provider=%s)", provider)
        return "Web search failed — the backend didn't respond. Try again shortly."

    return f'Search results for "{query}":\n\n{_fmt_results(results)}'
