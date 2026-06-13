"""Thin Telegram send helper — splits messages at 4096 chars."""
from __future__ import annotations

from telegram import Bot

from agentzero.config import TELEGRAM_BOT_TOKEN

MAX_MSG = 4096
_bot: Bot | None = None


def get_bot() -> Bot:
    global _bot
    if _bot is None:
        _bot = Bot(token=TELEGRAM_BOT_TOKEN)
    return _bot


async def send(chat_id: int, text: str) -> None:
    bot = get_bot()
    text = text or "​"  # zero-width space — Telegram rejects empty strings
    for i in range(0, len(text), MAX_MSG):
        await bot.send_message(chat_id=chat_id, text=text[i : i + MAX_MSG])
