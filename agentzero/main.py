"""
FastAPI application entry point.

TELEGRAM_MODE=webhook  — Telegram pushes updates to POST /webhook (production)
TELEGRAM_MODE=polling  — bot polls get_updates in a background asyncio task (dev)
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, Request, Response
from telegram import Update

from agentzero.config import (
    ALLOWED_CHAT_ID,
    TELEGRAM_MODE,
    WEBHOOK_SECRET,
    WEBHOOK_URL,
)
from agentzero.db import close as close_db
from agentzero.db import create_indexes, get_db
from agentzero.executor import execute_tool, get_status, undo_last
from agentzero.llm import ToolCall, get_provider
from agentzero.prompts import build_system_prompt
from agentzero.telegram_io import get_bot, send
from agentzero.tools import TOOLS

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    await create_indexes()
    bot = get_bot()

    if TELEGRAM_MODE == "webhook" and WEBHOOK_URL:
        await bot.set_webhook(
            url=f"{WEBHOOK_URL.rstrip('/')}/webhook",
            secret_token=WEBHOOK_SECRET or None,
        )
        logger.info("Webhook registered at %s/webhook", WEBHOOK_URL)
    else:
        await bot.delete_webhook(drop_pending_updates=True)
        asyncio.create_task(_polling_loop())
        logger.info("Polling mode started")

    yield

    await close_db()
    if TELEGRAM_MODE == "webhook":
        await bot.delete_webhook()


app = FastAPI(lifespan=lifespan)


# ---------------------------------------------------------------------------
# Polling loop (dev / local)
# ---------------------------------------------------------------------------

async def _polling_loop() -> None:
    bot = get_bot()
    offset = 0
    while True:
        try:
            updates = await bot.get_updates(offset=offset, timeout=30)
            for upd in updates:
                offset = upd.update_id + 1
                asyncio.create_task(_safe_process(upd))
        except Exception:
            logger.exception("Polling error — retrying in 5 s")
            await asyncio.sleep(5)


async def _safe_process(update: Update) -> None:
    try:
        await process_update(update)
    except Exception:
        logger.exception("Error processing update %s", update.update_id)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.post("/webhook")
async def webhook(request: Request) -> Response:
    if WEBHOOK_SECRET:
        token = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if token != WEBHOOK_SECRET:
            return Response(status_code=403)

    bot = get_bot()
    data = await request.json()
    update = Update.de_json(data, bot)
    asyncio.create_task(_safe_process(update))
    return Response(status_code=200)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Core update handler
# ---------------------------------------------------------------------------

async def process_update(update: Update) -> None:
    msg = update.message
    if not msg or not msg.text:
        return

    chat_id = msg.chat_id
    if chat_id != ALLOWED_CHAT_ID:
        return  # silently drop unauthorised messages

    text = msg.text.strip()

    # ---- bot commands -------------------------------------------------------
    if text == "/start":
        await send(chat_id, "AgentZero online. Tell me what to track.")
        return

    if text.startswith("/undo"):
        await send(chat_id, await undo_last(chat_id))
        return

    if text.startswith("/status"):
        args = text[7:].strip()
        scope = args if args in ("work", "personal") else "all"
        await send(chat_id, await get_status(scope=scope))
        return

    if text.startswith("/done "):
        tc = ToolCall(name="mark_done", args={"task_query": text[6:].strip()})
        await send(chat_id, await execute_tool(chat_id, tc))
        return

    if text.startswith("/add "):
        # /add <project> | <task title>
        parts = text[5:].split("|", 1)
        if len(parts) == 2:
            tc = ToolCall(
                name="add_task",
                args={
                    "project_name": parts[0].strip(),
                    "title": parts[1].strip(),
                },
            )
            await send(chat_id, await execute_tool(chat_id, tc))
        else:
            await send(chat_id, "Usage: /add <project> | <task title>")
        return

    if text.startswith("/snooze "):
        # /snooze <task> until <YYYY-MM-DD>
        parts = text[8:].split(" until ", 1)
        if len(parts) == 2:
            tc = ToolCall(
                name="snooze",
                args={"task_query": parts[0].strip(), "until": parts[1].strip()},
            )
            await send(chat_id, await execute_tool(chat_id, tc))
        else:
            await send(chat_id, "Usage: /snooze <task> until <YYYY-MM-DD>")
        return

    # ---- NL path ------------------------------------------------------------
    db = get_db()

    # Load last 10 messages (oldest first)
    history_docs = (
        await db.chat_history.find({"chat_id": chat_id})
        .sort("created_at", -1)
        .limit(10)
        .to_list(None)
    )
    history_docs.reverse()
    history = [{"role": d["role"], "content": d["content"]} for d in history_docs]
    history.append({"role": "user", "content": text})

    # Persist user message before LLM call
    await db.chat_history.insert_one(
        {"chat_id": chat_id, "role": "user", "content": text, "created_at": datetime.utcnow()}
    )

    system = await build_system_prompt()
    llm = get_provider()
    response = await llm.chat_with_tools(history, system, TOOLS)

    # Execute all tool calls
    confirmations: list[str] = []
    for tc in response.tool_calls:
        confirmations.append(await execute_tool(chat_id, tc))

    if confirmations:
        reply = "\n".join(confirmations)
    elif response.content:
        reply = response.content
    else:
        reply = "Done."

    # Persist assistant reply
    await db.chat_history.insert_one(
        {
            "chat_id": chat_id,
            "role": "assistant",
            "content": reply,
            "created_at": datetime.utcnow(),
        }
    )

    await send(chat_id, reply)
