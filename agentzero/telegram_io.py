"""Thin Telegram send helper — strips un-rendered markup, splits at 4096 chars.

Telegram messages are sent as plain text (no parse_mode), because free-form LLM
output can't be safely escaped into MarkdownV2 without frequent send failures.
So instead we strip the Markdown/LaTeX the model sometimes emits, leaving clean
readable text — the user never sees raw `*…*` or `\\[ … \\]`.
"""
from __future__ import annotations

import re

from telegram import Bot

from agentzero.config import TELEGRAM_BOT_TOKEN

MAX_MSG = 4096
_bot: Bot | None = None


def get_bot() -> Bot:
    global _bot
    if _bot is None:
        _bot = Bot(token=TELEGRAM_BOT_TOKEN)
    return _bot


# LaTeX command → readable symbol (matched only when not followed by another letter,
# so \to doesn't clobber \token).
_LATEX_SYMBOLS = {
    r"\approx": "≈", r"\times": "×", r"\div": "÷", r"\cdot": "·", r"\pm": "±",
    r"\leq": "≤", r"\geq": "≥", r"\le": "≤", r"\ge": "≥", r"\neq": "≠",
    r"\rightarrow": "→", r"\to": "→", r"\Rightarrow": "⇒", r"\ldots": "…",
    r"\dots": "…", r"\%": "%", r"\$": "$", r"\&": "&", r"\deg": "°",
}


def _to_plain(text: str) -> str:
    """Strip Markdown + LaTeX the channel can't render, leaving clean text."""
    if not text:
        return text
    t = text

    # --- LaTeX ---
    # \text{...}, \mathrm{...} etc. → inner contents
    t = re.sub(
        r"\\(?:text|mathrm|mathbf|mathit|mathsf|operatorname)\s*\{([^{}]*)\}",
        r"\1",
        t,
    )
    # \frac{a}{b} → (a)/(b)
    t = re.sub(r"\\frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}", r"(\1)/(\2)", t)
    # named symbols
    for cmd, sym in _LATEX_SYMBOLS.items():
        t = re.sub(re.escape(cmd) + r"(?![a-zA-Z])", sym, t)
    # thin/medium spaces and \quad
    t = re.sub(r"\\[,;:! ]", " ", t)
    t = re.sub(r"\\q?quad", " ", t)
    # math delimiters \[ \] \( \) and $$ display math
    t = re.sub(r"\\[\[\]()]", " ", t)
    t = t.replace("$$", " ")
    # any remaining \command token → drop the backslash, keep the word
    t = re.sub(r"\\([a-zA-Z]+)", r"\1", t)

    # --- Markdown ---
    t = re.sub(r"```[a-zA-Z0-9]*\n?", "", t)  # code fences
    t = t.replace("`", "")                      # inline code ticks
    t = re.sub(r"\*\*([^*]+)\*\*", r"\1", t)    # **bold**
    t = re.sub(r"\*([^*\n]+)\*", r"\1", t)       # *italic/bold*
    t = re.sub(r"__([^_]+)__", r"\1", t)          # __bold__
    t = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", t)  # [text](url)
    t = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", t)   # # headings

    # tidy whitespace
    t = re.sub(r"[ \t]+\n", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


async def send(chat_id: int, text: str) -> None:
    bot = get_bot()
    text = _to_plain(text or "") or "​"  # zero-width space — Telegram rejects empty strings
    for i in range(0, len(text), MAX_MSG):
        await bot.send_message(chat_id=chat_id, text=text[i : i + MAX_MSG])
