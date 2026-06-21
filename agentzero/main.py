"""
FastAPI application entry point.

TELEGRAM_MODE=webhook  — Telegram pushes updates to POST /webhook (production)
TELEGRAM_MODE=polling  — bot polls get_updates in a background asyncio task (dev)
"""
from __future__ import annotations

import asyncio
import logging
import random
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from telegram import Update
from telegram.constants import ChatAction

from agentzero.config import (
    ALLOWED_CHAT_ID,
    AUTONOMY_ENABLED,
    EVENING_DIGEST_ENABLED,
    EXPENSE_TRACKING_ENABLED,
    JOB_HUNT_ENABLED,
    JOB_TRACKING_ENABLED,
    MCP_ENABLED,
    MORNING_DIGEST_ENABLED,
    TELEGRAM_MODE,
    WEBHOOK_SECRET,
    WEBHOOK_URL,
)
from agentzero.db import close as close_db
from agentzero.db import create_indexes, get_db
from agentzero.executor import (
    complete_reminder_by_id,
    execute_tool,
    get_status,
    mark_done_by_id,
    mute_task_nudge_by_id,
    snooze_reminder_by_id,
    undo_last,
)
from agentzero.llm import ToolCall, get_provider
from agentzero.mcp_client import call_mcp_tool, get_mcp_tools, is_mcp_tool, load_mcp_tools
from agentzero.prompts import THINKING_FILLERS, build_system_prompt
from agentzero.scheduler import (
    load_pending_reminders,
    load_recurring_reminders,
    schedule_evening_digest,
    schedule_heartbeat,
    schedule_job_digest,
    schedule_application_scan,
    schedule_expense_summary,
    schedule_morning_digest,
    schedule_receipt_scan,
    schedule_reminder_followups,
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
    await load_recurring_reminders()
    schedule_reminder_followups(ALLOWED_CHAT_ID)
    if AUTONOMY_ENABLED:
        schedule_heartbeat(ALLOWED_CHAT_ID)
    if MORNING_DIGEST_ENABLED:
        schedule_morning_digest(ALLOWED_CHAT_ID)
    if EVENING_DIGEST_ENABLED:
        schedule_evening_digest(ALLOWED_CHAT_ID)
    if JOB_HUNT_ENABLED:
        schedule_job_digest(ALLOWED_CHAT_ID)
    if JOB_TRACKING_ENABLED:
        schedule_application_scan(ALLOWED_CHAT_ID)
    if EXPENSE_TRACKING_ENABLED:
        schedule_receipt_scan(ALLOWED_CHAT_ID)
        schedule_expense_summary(ALLOWED_CHAT_ID)
    if MCP_ENABLED:
        await load_mcp_tools()

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

# Dashboard API (read-only expense JSON under /api, gated by DASHBOARD_API_KEY).
from agentzero.api import router as api_router  # noqa: E402
from agentzero.config import DASHBOARD_ORIGINS  # noqa: E402

app.add_middleware(
    CORSMiddleware,
    allow_origins=DASHBOARD_ORIGINS,
    allow_methods=["GET"],
    allow_headers=["X-API-Key", "Content-Type"],
)
app.include_router(api_router)


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
    # ---- inline button tap (callback query) --------------------------------
    if update.callback_query:
        await _handle_callback(update.callback_query)
        return

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

    if text.startswith("/winddown"):
        from agentzero.digest import send_evening_digest

        await send_evening_digest(chat_id)
        return

    if text.startswith("/jobs"):
        from agentzero.jobs import send_job_digest

        result = await send_job_digest(chat_id)
        if result is None:
            await send(chat_id, "No new postings, or no job profile yet — send me your CV and what you're after.")
        return

    if text.startswith("/applications"):
        from agentzero.applications import _load_apps, format_applications

        await send(chat_id, format_applications(await _load_apps(chat_id)))
        return

    if text.startswith("/expenses"):
        from agentzero.expenses import expense_summary

        await send(chat_id, await expense_summary(chat_id, "month"))
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
    await _handle_nl(chat_id, text, reply_to=_quoted_context(msg))


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
    await _handle_nl(chat_id, text, image=image_bytes, reply_to=_quoted_context(msg))


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
    await _handle_nl(chat_id, text, reply_to=_quoted_context(msg))


# Tools slow enough (they hit the internet) to warrant a "working on it" filler. Fast local
# tools and quick replies don't get one — a filler there just looks odd.
_FILLER_TOOLS = {"web_search", "web_fetch"}


async def _handle_callback(cq) -> None:
    """An inline button was tapped. callback_data is a compact 'kind:action:id[:arg]' string."""
    bot = get_bot()
    chat_id = cq.message.chat_id if cq.message else (cq.from_user.id if cq.from_user else 0)
    if chat_id != ALLOWED_CHAT_ID:
        try:
            await bot.answer_callback_query(cq.id)
        except Exception:
            pass
        return

    parts = (cq.data or "").split(":")
    toast = "Done."
    try:
        if parts[:2] == ["rem", "done"]:
            toast = await complete_reminder_by_id(chat_id, parts[2])
        elif parts[:2] == ["rem", "snz"]:
            toast = await snooze_reminder_by_id(chat_id, parts[2], int(parts[3]))
        elif parts[:2] == ["tsk", "done"]:
            toast = await mark_done_by_id(chat_id, parts[2])
        elif parts[:2] == ["tsk", "mute"]:
            toast = await mute_task_nudge_by_id(chat_id, parts[2], int(parts[3]))
        else:
            toast = "Unknown action."
    except Exception:
        logger.exception("Callback handling failed for data %r", cq.data)
        toast = "Couldn't do that — try typing it instead."

    # Acknowledge (stops the button spinner / shows a toast).
    try:
        await bot.answer_callback_query(cq.id, text=toast[:200])
    except Exception:
        pass
    # Annotate the original message and drop the keyboard so it can't be tapped twice.
    try:
        original = cq.message.text if cq.message and cq.message.text else ""
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=cq.message.message_id,
            text=f"{original}\n— {toast}".strip(),
        )
    except Exception:
        pass


def _quoted_context(msg) -> str | None:
    """If the user replied to a specific message (Telegram's reply feature), return a short
    description of that quoted message so the model knows what's being referenced."""
    q = getattr(msg, "reply_to_message", None)
    if not q:
        return None
    body = (q.text or q.caption or "").strip()
    if not body:
        if getattr(q, "photo", None):
            body = "[an image]"
        elif getattr(q, "voice", None):
            body = "[a voice message]"
        else:
            body = "[a non-text message]"
    by_bot = bool(getattr(q, "from_user", None) and q.from_user.is_bot)
    whose = "your own earlier message" if by_bot else "an earlier message"
    return f'{whose}: "{body}"'


async def _handle_nl(
    chat_id: int,
    text: str,
    image: bytes | None = None,
    image_mime: str = "image/jpeg",
    reply_to: str | None = None,
) -> None:
    db = get_db()
    # Immediate "typing…" so the user sees activity; a witty text filler follows only if
    # the reply genuinely takes a while (THINKING_FILLER_SECONDS).
    try:
        await get_bot().send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    except Exception:
        pass

    # When the user replied to a specific message, prepend that quoted context so the model
    # (and stored history) knows exactly what they're referring to.
    user_content = f'[Replying to {reply_to}]\n{text}' if reply_to else text

    # Load last 10 messages (oldest first)
    history_docs = (
        await db.chat_history.find({"chat_id": chat_id})
        .sort("created_at", -1)
        .limit(10)
        .to_list(None)
    )
    history_docs.reverse()
    history = [{"role": d["role"], "content": d["content"]} for d in history_docs]
    history.append({"role": "user", "content": user_content})

    await db.chat_history.insert_one(
        {"chat_id": chat_id, "role": "user", "content": user_content, "created_at": datetime.utcnow()}
    )

    system = await build_system_prompt()
    llm = get_provider()
    tools = TOOLS + get_mcp_tools()

    filler_sent = False

    async def _execute(name: str, args: dict) -> str:
        # Drop a witty "working on it" line the first time the model reaches for the internet
        # (web search/fetch) — those take a beat. Fast local tools stay quiet.
        nonlocal filler_sent
        if not filler_sent and name in _FILLER_TOOLS:
            filler_sent = True
            try:
                await send(chat_id, random.choice(THINKING_FILLERS))
            except Exception:
                logger.exception("Thinking filler failed for chat %s", chat_id)
        # Local tools run on the executor; MCP tools route to their server.
        if is_mcp_tool(name):
            return await call_mcp_tool(name, args)
        return await execute_tool(chat_id, ToolCall(name=name, args=args))

    # Agentic loop: the model can call tools, see results, and call more (e.g. search
    # Gmail for ids, then fetch each body) before producing its final answer in voice.
    try:
        result = await llm.run_tool_loop(
            history, system, tools, _execute, image=image, image_mime=image_mime,
            max_iters=10,  # headroom for self-directed research (chained search → read → act)
        )
        reply = result.text or (
            "\n".join(result.last_results) if result.last_results else "Done."
        )
    except Exception:
        logger.exception("Tool loop failed for chat %s", chat_id)
        reply = "Something went wrong handling that — give it another go in a moment."

    await db.chat_history.insert_one(
        {
            "chat_id": chat_id,
            "role": "assistant",
            "content": reply,
            "created_at": datetime.utcnow(),
        }
    )

    await send(chat_id, reply)
