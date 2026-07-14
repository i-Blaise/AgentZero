"""
Daily digests — a morning rundown (08:00) of what's on the user's plate, and an
evening wind-down that tees up tomorrow.

Unlike the autonomy heartbeat (selective, can stay silent), digests are scheduled
rundowns the user asked for, so they ALWAYS send. Narrated in AgentZero's voice,
with a plain-text fallback so a flaky LLM call still delivers the facts.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from agentzero import focus
from agentzero.config import TIMEZONE
from agentzero.db import get_db
from agentzero.llm import get_provider
from agentzero.prompts import PERSONALITY
from agentzero.telegram_io import send

logger = logging.getLogger(__name__)


async def _gather(chat_id: int) -> dict:
    db = get_db()
    tz = ZoneInfo(TIMEZONE)
    now_local = datetime.now(timezone.utc).astimezone(tz)
    today = now_local.date()
    tomorrow = today + timedelta(days=1)

    projects = {p["_id"]: p for p in await db.projects.find({}).to_list(None)}

    # Map every task by id so a step can name the goal it belongs to.
    tasks_by_id = {t["_id"]: t for t in await db.tasks.find({}).to_list(None)}

    overdue, due_today, due_tomorrow, upcoming, undated = [], [], [], [], []
    for t in await db.tasks.find({"status": "open"}).to_list(None):
        proj = projects.get(t["project_id"])
        pname = proj["name"] if proj else "?"
        goal = tasks_by_id.get(t.get("parent_task_id")) if t.get("parent_task_id") else None
        ctx = f"{pname} ▸ {goal['title']}" if goal else pname
        label = f"{t['title']} ({ctx})"
        due = t.get("due_date")
        if due:
            d = due.date() if hasattr(due, "date") else due
            if d < today:
                overdue.append(f"{label} — was due {d.strftime('%d %b')}")
            elif d == today:
                due_today.append(label)
            elif d == tomorrow:
                due_tomorrow.append(label)
            else:
                upcoming.append(f"{label} — due {d.strftime('%d %b')}")
        else:
            undated.append(label)

    reminders = []
    for r in await db.reminders.find(
        {"status": {"$in": ["pending", "awaiting_ack"]}}
    ).sort("fire_at", 1).to_list(None):
        fa = r["fire_at"]
        if fa.tzinfo is None:
            fa = fa.replace(tzinfo=timezone.utc)
        reminders.append(f"{r['text']} — {fa.astimezone(tz).strftime('%a %d %b, %H:%M')}")

    return {
        "now_local": now_local,
        "overdue": overdue,
        "due_today": due_today,
        "due_tomorrow": due_tomorrow,
        "upcoming": upcoming,
        "undated": undated,
        "reminders": reminders,
    }


def _format(data: dict) -> str:
    sections = [
        ("Overdue", data["overdue"]),
        ("Due today", data["due_today"]),
        ("Due tomorrow", data["due_tomorrow"]),
        ("Upcoming", data["upcoming"]),
        ("No due date", data["undated"]),
        ("Reminders", data["reminders"]),
    ]
    lines: list[str] = []
    for title, items in sections:
        if items:
            lines.append(f"{title}:")
            lines.extend(f"  - {i}" for i in items)
    return "\n".join(lines) if lines else "(nothing on the plate)"


def _has_anything(data: dict) -> bool:
    return any(
        data[k]
        for k in ("overdue", "due_today", "due_tomorrow", "upcoming", "undated", "reminders")
    )


async def send_morning_digest(chat_id: int) -> str:
    data = await _gather(chat_id)

    # The selection moment: commit today's focus slate (carryovers first, then the most
    # urgent — LLM-judged when there's a genuine choice), and disclose any deadline tasks
    # that didn't make it so nothing due today is ever silently hidden.
    focus_block = ""
    try:
        await focus.ensure_today_focus(chat_id)
        overview = await focus.focus_overview(chat_id)
        if overview and overview["lines"]:
            focus_block = "TODAY'S FOCUS (the committed slate — lead with this):\n"
            focus_block += "\n".join(f"  {i + 1}. {ln}" for i, ln in enumerate(overview["lines"]))
            if overview["overflow_lines"]:
                focus_block += "\nAlso due today but NOT in focus (offer to swap in):\n"
                focus_block += "\n".join(f"  - {ln}" for ln in overview["overflow_lines"])
            focus_block += "\n\n"
    except Exception:
        logger.exception("Focus selection failed — sending digest without a slate")

    summary = focus_block + _format(data)
    greeting = data["now_local"].strftime("%A %d %B")

    system = (
        f"{PERSONALITY}\n\n"
        f"It's the morning briefing ({greeting}). Below is everything on the user's plate. "
        "Write a concise morning rundown in your voice: LEAD with TODAY'S FOCUS — the 3-4 "
        "tasks committed for today (mark carryovers as such); that slate is the day's plan. "
        "If items are listed as due today but NOT in focus, mention them once and offer to "
        "swap one in. Then the rest of the plate briefly, kept skimmable. Be dry and funny, "
        "but every item must stay clear and accurate; don't drop or mangle any of it. If the "
        "plate is empty, say so with appropriate suspicion. No corporate filler, no sign-off."
    )

    try:
        msg = (await get_provider().chat([{"role": "user", "content": summary}], system)).strip()
        if msg:
            await send(chat_id, msg)
            return msg
    except Exception:
        logger.exception("Morning digest narration failed — sending plain summary")

    # Fallback: deliver the facts even if narration failed
    fallback = f"☀️ Morning rundown — {greeting}\n\n{summary}"
    await send(chat_id, fallback)
    return fallback


async def send_evening_digest(chat_id: int) -> str:
    data = await _gather(chat_id)

    # Focus scoreboard: how today's slate went, and what carries into tomorrow. Read-only —
    # the evening never (re)selects a slate.
    focus_block = ""
    try:
        overview = await focus.focus_overview(chat_id)
        if overview and overview["total"]:
            focus_block = f"TODAY'S FOCUS SCOREBOARD: {overview['done']}/{overview['total']} done.\n"
            if overview["done_lines"]:
                focus_block += "Completed:\n" + "\n".join(f"  ✓ {ln}" for ln in overview["done_lines"]) + "\n"
            if overview["open_lines"]:
                focus_block += "Carrying over to tomorrow:\n" + "\n".join(
                    f"  - {ln}" for ln in overview["open_lines"]
                ) + "\n"
            focus_block += "\n"
    except Exception:
        logger.exception("Focus scoreboard failed — sending digest without it")

    # Overbooking check for tomorrow: more deadline tasks than the slate can hold is a
    # scheduling conflict to resolve tonight, not a surprise at breakfast.
    if len(data["due_tomorrow"]) > 4:
        focus_block += (
            f"HEADS-UP: {len(data['due_tomorrow'])} tasks are due tomorrow but the daily "
            "focus holds at most 4 — suggest re-dating or consciously deprioritising some tonight.\n\n"
        )

    summary = focus_block + _format(data)
    tomorrow = (data["now_local"] + timedelta(days=1)).strftime("%A %d %B")

    system = (
        f"{PERSONALITY}\n\n"
        f"It's the evening wind-down. Tomorrow is {tomorrow}. Below is the user's current "
        "state. Write a short, calm end-of-day message that helps them mentally close out "
        "today and tee up tomorrow: if there's a focus scoreboard, open with it — celebrate "
        "what got done, and note matter-of-factly what carries over to tomorrow's slate. Then "
        "surface anything still OVERDUE worth clearing, what's DUE TOMORROW, and tomorrow's "
        "reminders. If there's an overbooking heads-up for tomorrow, relay it and suggest "
        "spreading things out. Keep it brief and a touch reflective (it's evening, not a war "
        "room). Every item must stay clear and accurate; don't invent or drop anything. If "
        "there's genuinely nothing to prep, tell them to switch off and rest. No corporate "
        "filler, no sign-off."
    )

    try:
        msg = (await get_provider().chat([{"role": "user", "content": summary}], system)).strip()
        if msg:
            await send(chat_id, msg)
            return msg
    except Exception:
        logger.exception("Evening digest narration failed — sending plain summary")

    fallback = f"🌙 Wind-down — before {tomorrow}\n\n{summary}"
    await send(chat_id, fallback)
    return fallback
