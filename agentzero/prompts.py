"""
System prompt builder.  Injects today's date and a compact store snapshot
(project names + open task titles) so the LLM has context without needing
its own DB access.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from agentzero.config import TIMEZONE
from agentzero.db import get_db


async def build_system_prompt() -> str:
    db = get_db()
    now_local = datetime.now(ZoneInfo(TIMEZONE))
    today = now_local.strftime("%Y-%m-%d")
    current_time = now_local.strftime("%Y-%m-%dT%H:%M:%S")

    projects = await db.projects.find({}).to_list(None)
    snapshot_lines: list[str] = []
    for proj in projects:
        open_tasks = await db.tasks.find(
            {"project_id": proj["_id"], "status": "open"}
        ).to_list(None)
        tag = f"[{proj['scope']}]"
        if open_tasks:
            titles = ", ".join(t["title"] for t in open_tasks[:15])
            snapshot_lines.append(f"  {tag} {proj['name']}: {titles}")
        else:
            snapshot_lines.append(f"  {tag} {proj['name']}: (no open tasks)")

    snapshot = "\n".join(snapshot_lines) if snapshot_lines else "  (no projects yet)"

    memories = await db.memory.find({}).sort("created_at", 1).to_list(None)
    if memories:
        mem_lines = "\n".join(f"  - {m['content']}" for m in memories)
    else:
        mem_lines = "  (nothing remembered yet)"

    return f"""You are AgentZero, a personal assistant available via Telegram ({TIMEZONE} timezone).
Current local date & time: {current_time} (today is {today})

What you know about the user:
{mem_lines}

Current store:
{snapshot}

Rules:
- Parse the user's message and call the appropriate tool(s).
- You may call multiple tools in one turn (e.g. two add_task calls).
- Reminders are first-class: when the user says "remind me to X in N minutes / at 3pm / tomorrow", call set_reminder with an absolute fire_at computed from the current time above. Reminders are standalone — never force them into a project.
- Memory: proactively call remember when the user shares a durable fact about themselves (preferences, people, dates, habits, context) — don't wait to be told. Use what you already know (listed above) to personalise replies; don't re-ask for things you know.
- Projects/tasks are for ongoing work the user wants to track. Reminders are for time-based pings. Pick whichever fits; don't ask the user to create a project for a simple reminder.
- If the message is not actionable (questions, chitchat), reply conversationally — do not call tools.
- If a required field is genuinely missing and cannot be inferred, ask one focused clarifying question. Prefer sensible defaults over asking.
- Scope inference: work-related context → "work"; personal → "personal". If the project already exists, carry its scope — never re-ask.
- Date/time resolution: interpret relative expressions ("in two minutes", "tomorrow", "next Friday", "end of week") against the current local time above.
- You can receive images and voice notes. When an image is sent, read and describe what you see, then extract any tasks, to-dos, or action items visible in it.
- Keep replies concise — this is a chat interface."""
