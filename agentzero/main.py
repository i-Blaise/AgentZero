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
    AUTONOMY_ENABLED,
    MORNING_DIGEST_ENABLED,
    TELEGRAM_MODE,
    WEBHOOK_SECRET,
    WEBHOOK_URL,
)
from agentzero.db import close as close_db
from agentzero.db import create_indexes, get_db
from agentzero.executor import execute_tool, get_status, undo_last
from agentzero.llm import ToolCall, get_provider
from agentzero.prompts import build_system_prompt
from agentzero.scheduler import (
    load_pending_reminders,
    schedule_heartbeat,
    schedule_morning_digest,
    start_scheduler,
    stop_scheduler,
)
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

    # Reminders + autonomy heartbeat
    start_scheduler()
    await load_pending_reminders()
    if AUTONOMY_ENABLED:
        schedule_heartbeat(ALLOWED_CHAT_ID)
    if MORNING_DIGEST_ENABLED:
        schedule_morning_digest(ALLOWED_CHAT_ID)

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

    stop_scheduler()
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
    if not msg:
        return

    chat_id = msg.chat_id
    if chat_id != ALLOWED_CHAT_ID:
        return  # silently drop unauthorised messages

    # ---- voice message ------------------------------------------------------
    if msg.voice:
        await _handle_voice(chat_id, msg)
        return

    # ---- photo --------------------------------------------------------------
    if msg.photo:
        await _handle_photo(chat_id, msg)
        return

    if not msg.text:
        return

    text = msg.text.strip()

    # ---- bot commands -------------------------------------------------------
    if text == "/start":
        await send(chat_id, "AgentZero online. Tell me what to track.")
        return

    if text.startswith("/checkin"):
        from agentzero.autonomy import run_heartbeat

        result = await run_heartbeat(chat_id, force=True)
        if result is None:
            await send(chat_id, "All clear — nothing needs your attention right now. 🙂")
        return

    if text.startswith("/brief"):
        from agentzero.digest import send_morning_digest

        await send_morning_digest(chat_id)
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
        parts = text[5:].split("|", 1)
        if len(parts) == 2:
            tc = ToolCall(
                name="add_task",
                args={"project_name": parts[0].strip(), "title": parts[1].strip()},
            )
            await send(chat_id, await execute_tool(chat_id, tc))
        else:
            await send(chat_id, "Usage: /add <project> | <task title>")
        return

    if text.startswith("/snooze "):
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
    await _handle_nl(chat_id, text)


async def _handle_photo(chat_id: int, msg) -> None:
    bot = get_bot()
    # Telegram gives multiple sizes; largest is always the last in the array
    photo = msg.photo[-1]
    try:
        file = await bot.get_file(photo.file_id)
        image_bytes = bytes(await file.download_as_bytearray())
    except Exception:
        logger.exception("Photo download failed for chat %s", chat_id)
        await send(chat_id, "Couldn't download that image — try again.")
        return

    if not image_bytes:
        await send(chat_id, "Received an empty image — try again.")
        return

    # Validate JPEG magic bytes (FF D8 FF); Telegram photos are always JPEG
    if not image_bytes[:3] == b"\xff\xd8\xff":
        logger.error(
            "Photo for chat %s is not a valid JPEG (got %s)",
            chat_id,
            image_bytes[:4].hex(),
        )
        await send(chat_id, "Image format not recognised — try again.")
        return

    logger.info("Photo received for chat %s: %d bytes", chat_id, len(image_bytes))
    text = msg.caption.strip() if msg.caption else "Describe what you see in this image and extract any tasks, to-dos, notes, or action items I should track. Don't worry about naming specific brands or products — focus on what I might need to do."
    await _handle_nl(chat_id, text, image=image_bytes)


async def _handle_voice(chat_id: int, msg) -> None:
    from agentzero.audio import transcribe

    bot = get_bot()
    try:
        file = await bot.get_file(msg.voice.file_id)
        audio = bytes(await file.download_as_bytearray())
        text = await transcribe(audio)
    except Exception:
        logger.exception("Voice transcription failed for chat %s", chat_id)
        await send(chat_id, "Couldn't transcribe that — try again or type your message.")
        return

    if not text:
        await send(chat_id, "Couldn't hear that clearly — try again.")
        return

    # Echo transcription so the user can see what was heard
    await send(chat_id, f'🎤 "{text}"')
    await _handle_nl(chat_id, text)


async def _handle_nl(
    chat_id: int, text: str, image: bytes | None = None, image_mime: str = "image/jpeg"
) -> None:
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

    await db.chat_history.insert_one(
        {"chat_id": chat_id, "role": "user", "content": text, "created_at": datetime.utcnow()}
    )

    system = await build_system_prompt()
    llm = get_provider()
    response = await llm.chat_with_tools(history, system, TOOLS, image=image, image_mime=image_mime)

    confirmations: list[str] = []
    for tc in response.tool_calls:
        confirmations.append(await execute_tool(chat_id, tc))

    if confirmations:
        reply = "\n".join(confirmations)
    elif response.content:
        reply = response.content
    else:
        reply = "Done."

    await db.chat_history.insert_one(
        {
            "chat_id": chat_id,
            "role": "assistant",
            "content": reply,
            "created_at": datetime.utcnow(),
        }
    )

    await send(chat_id, reply)
