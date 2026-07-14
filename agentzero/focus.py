"""
Daily focus — the day's committed slate of 3-4 tasks.

Once a day (normally at the morning digest) the agent commits to a small slate:
yesterday's unfinished focus tasks carry over first, then the most urgent open tasks
fill the remaining seats. Proactive heartbeat nudges are FENCED to this slate for the
rest of the day, so the user is never pinged about five different projects back-to-back
— the cap limits nudging, not awareness: overdue/due-today tasks that don't make the
slate are disclosed as "overflow" in the digest with an offer to swap.

Selection is deterministic (urgency-ranked) unless there are genuinely more candidates
than seats — only then is the LLM consulted to judge which 3 vs 4 fit the day (it can
weigh effort and the user's goals). The executor never triggers the LLM path
(allow_llm=False) — the brain proposes, the deterministic executor disposes.

daily_focus collection:
  _id           ObjectId
  chat_id       int
  date          "YYYY-MM-DD"   (local date, TIMEZONE)
  task_ids      list[ObjectId] — today's slate (order = presentation order)
  carryover_ids list[ObjectId] — subset of task_ids inherited from the previous slate
  overflow_ids  list[ObjectId] — overdue/due-today tasks that did NOT make the slate
  created_at    datetime
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from agentzero.config import TIMEZONE
from agentzero.db import get_db
from agentzero.llm import get_provider

logger = logging.getLogger(__name__)

MAX_SLATE = 4    # hard ceiling — the fence never widens past this
TARGET_SLATE = 3 # deterministic size when the LLM isn't consulted / fails


def _today_str() -> str:
    return datetime.now(timezone.utc).astimezone(ZoneInfo(TIMEZONE)).strftime("%Y-%m-%d")


def _due_date(t: dict):
    d = t.get("due_date")
    return d.date() if hasattr(d, "date") else d


def _rank_key(t: dict):
    """Urgency sort: overdue (most overdue first) → due today → dated (soonest) → undated
    (oldest first, i.e. most stalled)."""
    today = datetime.now(timezone.utc).astimezone(ZoneInfo(TIMEZONE)).date()
    d = _due_date(t)
    if d is None:
        created = t.get("created_at") or datetime.max
        return (3, created)
    if d < today:
        return (0, d)
    if d == today:
        return (1, d)
    return (2, d)


async def _open_actionables() -> list[dict]:
    """Open tasks that are actionable focus units. A GOAL with open steps is represented
    by its steps (the actual next actions), never listed alongside them. Timed tasks
    (remind_at set) are excluded — their ping fires at the exact time the user asked for,
    so they don't need a slate seat (the user can still swap one in explicitly)."""
    db = get_db()
    open_tasks = await db.tasks.find({"status": "open"}).to_list(None)
    goals_with_open_steps = {t["parent_task_id"] for t in open_tasks if t.get("parent_task_id")}
    return [
        t for t in open_tasks
        if t["_id"] not in goals_with_open_steps and t.get("remind_at") is None
    ]


async def _label_maps() -> tuple[dict, dict]:
    db = get_db()
    projects = {p["_id"]: p for p in await db.projects.find({}).to_list(None)}
    titles = {t["_id"]: t["title"] for t in await db.tasks.find({}).to_list(None)}
    return projects, titles


def _label(t: dict, projects: dict, titles: dict) -> str:
    proj = projects.get(t.get("project_id"))
    ctx = proj["name"] if proj else "?"
    goal = titles.get(t.get("parent_task_id"))
    if goal:
        ctx = f"{ctx} ▸ {goal}"
    today = datetime.now(timezone.utc).astimezone(ZoneInfo(TIMEZONE)).date()
    d = _due_date(t)
    if d is None:
        tag = ""
    elif d < today:
        tag = f" — was due {d.strftime('%d %b')} (OVERDUE)"
    elif d == today:
        tag = " — due TODAY"
    else:
        tag = f" — due {d.strftime('%d %b')}"
    return f"{t['title']} ({ctx}){tag}"


async def get_today_focus(chat_id: int) -> dict | None:
    db = get_db()
    return await db.daily_focus.find_one({"chat_id": chat_id, "date": _today_str()})


async def ensure_today_focus(chat_id: int, allow_llm: bool = True) -> dict:
    """Return today's slate, selecting one if none exists yet. First caller of the day
    (morning digest, or an early heartbeat) fixes the slate; everyone else reads it."""
    doc = await get_today_focus(chat_id)
    if doc:
        return doc
    return await _select_today_focus(chat_id, allow_llm=allow_llm)


async def _llm_pick(carry_lines: list[str], pool_lines: list[str], seats: int) -> list[int] | None:
    """Ask the LLM which pool items (by number) join today's slate. Returns indices,
    [] for an explicit 'none', or None on any failure (caller falls back)."""
    carry = "\n".join(f"  - {ln}" for ln in carry_lines) or "  (none)"
    pool = "\n".join(f"  {i + 1}. {ln}" for i, ln in enumerate(pool_lines))
    system = (
        "You pick the user's DAILY FOCUS: the few tasks they should actually work on today. "
        "Already committed (carryovers from yesterday) are listed first; then numbered "
        "candidates. Choose which candidates to ADD so the day's total is 3, or 4 only if "
        "they're light. Prefer hard deadlines (overdue / due today), then what advances the "
        "user's goals or income. Multi-hour tasks fill a day fast — don't overpack. "
        f"Add at most {seats}. Reply with ONLY the chosen candidate numbers, comma-separated "
        "(e.g. 1,3), or NONE to add nothing."
    )
    try:
        reply = (await get_provider().chat(
            [{"role": "user", "content": f"Carryovers:\n{carry}\n\nCandidates:\n{pool}"}],
            system,
        )).strip()
    except Exception:
        logger.exception("Focus slate LLM pick failed — falling back to deterministic fill")
        return None
    if reply.upper().startswith("NONE"):
        return []
    picked = [int(n) - 1 for n in re.findall(r"\d+", reply)]
    picked = [i for i in picked if 0 <= i < len(pool_lines)]
    return picked[:seats] if picked else None


async def _select_today_focus(chat_id: int, allow_llm: bool = True) -> dict:
    db = get_db()
    candidates = await _open_actionables()
    cand_by_id = {t["_id"]: t for t in candidates}

    # Carryovers: still-open tasks from the most recent previous slate keep their seats.
    prev = await db.daily_focus.find(
        {"chat_id": chat_id, "date": {"$lt": _today_str()}}
    ).sort("date", -1).limit(1).to_list(1)
    carryover_ids = [tid for tid in (prev[0]["task_ids"] if prev else []) if tid in cand_by_id]

    pool = sorted(
        (t for t in candidates if t["_id"] not in set(carryover_ids)), key=_rank_key
    )
    seats = max(0, MAX_SLATE - len(carryover_ids))
    chosen_ids = list(carryover_ids)

    if pool and seats:
        if len(carryover_ids) + len(pool) <= TARGET_SLATE or not allow_llm:
            # No real choice to make (or executor context) — deterministic fill.
            chosen_ids += [t["_id"] for t in pool[: max(0, TARGET_SLATE - len(carryover_ids))]]
        else:
            projects, titles = await _label_maps()
            carry_lines = [_label(cand_by_id[i], projects, titles) for i in carryover_ids]
            pool_lines = [_label(t, projects, titles) for t in pool]
            picked = await _llm_pick(carry_lines, pool_lines, seats)
            if picked is None:
                chosen_ids += [t["_id"] for t in pool[: max(0, TARGET_SLATE - len(carryover_ids))]]
            else:
                chosen_ids += [pool[i]["_id"] for i in picked]

    # Overflow: deadline-pressured tasks that didn't make the slate — never hidden silently.
    chosen_set = set(chosen_ids)
    overflow_ids = [t["_id"] for t in sorted(candidates, key=_rank_key)
                    if t["_id"] not in chosen_set and _rank_key(t)[0] <= 1]

    doc = {
        "chat_id": chat_id,
        "date": _today_str(),
        "task_ids": chosen_ids,
        "carryover_ids": carryover_ids,
        "overflow_ids": overflow_ids,
        "created_at": datetime.utcnow(),
    }
    result = await db.daily_focus.insert_one(doc)
    doc["_id"] = result.inserted_id
    return doc


async def focus_overview(chat_id: int) -> dict | None:
    """Today's slate hydrated for display. None if no slate exists yet.
    {'lines', 'overflow_lines', 'done', 'total', 'open_lines', 'done_lines'}"""
    db = get_db()
    doc = await get_today_focus(chat_id)
    if not doc:
        return None
    projects, titles = await _label_maps()
    carry = set(doc.get("carryover_ids", []))
    lines, open_lines, done_lines, done = [], [], [], 0
    for tid in doc.get("task_ids", []):
        t = await db.tasks.find_one({"_id": tid})
        if not t:
            continue
        label = _label(t, projects, titles)
        if tid in carry:
            label += " [carried over]"
        if t.get("status") == "done":
            done += 1
            done_lines.append(label)
            label += " ✓ done"
        else:
            open_lines.append(label)
        lines.append(label)
    overflow_lines = []
    for tid in doc.get("overflow_ids", []):
        t = await db.tasks.find_one({"_id": tid})
        if t and t.get("status") == "open":
            overflow_lines.append(_label(t, projects, titles))
    return {
        "lines": lines,
        "open_lines": open_lines,
        "done_lines": done_lines,
        "overflow_lines": overflow_lines,
        "done": done,
        "total": len(lines),
    }


async def next_focus_candidates(chat_id: int, limit: int = 2) -> list[str]:
    """Deterministic 'what's next' suggestions once the slate is cleared: overflow
    deadline tasks first, then the ranked backlog."""
    doc = await get_today_focus(chat_id)
    in_slate = set(doc.get("task_ids", [])) if doc else set()
    candidates = sorted(
        (t for t in await _open_actionables() if t["_id"] not in in_slate), key=_rank_key
    )
    projects, titles = await _label_maps()
    return [_label(t, projects, titles) for t in candidates[:limit]]


async def add_to_focus(chat_id: int, task_id) -> None:
    db = get_db()
    await ensure_today_focus(chat_id, allow_llm=False)
    await db.daily_focus.update_one(
        {"chat_id": chat_id, "date": _today_str()},
        {"$addToSet": {"task_ids": task_id}, "$pull": {"overflow_ids": task_id}},
    )


async def remove_from_focus(chat_id: int, task_id) -> None:
    db = get_db()
    await db.daily_focus.update_one(
        {"chat_id": chat_id, "date": _today_str()},
        {"$pull": {"task_ids": task_id, "carryover_ids": task_id}},
    )
