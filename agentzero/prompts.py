"""
System prompt builder.  Injects today's date and a compact store snapshot
(project names + open task titles) so the LLM has context without needing
its own DB access.
"""
from __future__ import annotations

from datetime import datetime

from agentzero.db import get_db


async def build_system_prompt() -> str:
    db = get_db()
    today = datetime.utcnow().strftime("%Y-%m-%d")

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

    return f"""You are AgentZero, a personal work-tracking assistant available via Telegram.
Today's date: {today}

Current store:
{snapshot}

Rules:
- Parse the user's message and call the appropriate tool(s).
- You may call multiple tools in one turn (e.g. two add_task calls).
- If the message is not actionable (questions, chitchat), reply conversationally — do not call tools.
- If a required field is missing and cannot be inferred, ask one focused clarifying question.
- Scope inference: work-related context → "work"; personal → "personal". If the project already exists, carry its scope — never re-ask.
- Date resolution: interpret relative expressions ("tomorrow", "next Friday", "end of week") against today's date above.
- Keep replies concise — this is a chat interface."""
