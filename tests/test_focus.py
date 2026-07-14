"""
Daily focus tests — the day's committed 3-4 task slate (focus.py) and its integrations.

Covers: deterministic selection (≤3 candidates, no LLM), LLM pick when there's a real
choice, LLM-failure fallback, carryover of yesterday's unfinished slate, goal-with-steps
represented by its steps, overflow (due-today tasks that missed the slate), the heartbeat
fence, set_daily_focus add/remove/swap/show, the slate-cleared suggestion on mark_done,
add_task due-today + overbooking notices, and idempotent ensure.

Uses mongomock-motor (via conftest). Focus's LLM is patched where the pick path is hit.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentzero import autonomy, focus
from agentzero.executor import execute_tool
from agentzero.llm import ToolCall

CHAT_ID = 555


def _tc(tool: str, **kwargs) -> ToolCall:
    return ToolCall(name=tool, args=kwargs)


def _provider(reply: str):
    p = MagicMock()
    p.chat = AsyncMock(return_value=reply)
    return p


async def _seed_project(mock_db, name="Work", scope="work"):
    proj = await mock_db.projects.find_one({"name": name})
    if proj:
        return proj["_id"]
    res = await mock_db.projects.insert_one(
        {"name": name, "scope": scope, "created_at": datetime.utcnow(), "updated_at": datetime.utcnow()}
    )
    return res.inserted_id


async def _seed_task(mock_db, pid, title, days_until_due=None, parent_id=None, status="open"):
    due = None
    if days_until_due is not None:
        due = datetime.utcnow() + timedelta(days=days_until_due)
    res = await mock_db.tasks.insert_one(
        {
            "project_id": pid,
            "parent_task_id": parent_id,
            "title": title,
            "status": status,
            "due_date": due,
            "snoozed_until": None,
            "last_nudged_at": None,
            "created_at": datetime.utcnow() - timedelta(days=1),
            "updated_at": datetime.utcnow(),
        }
    )
    return res.inserted_id


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_small_pool_selected_deterministically_no_llm(mock_db):
    pid = await _seed_project(mock_db)
    t1 = await _seed_task(mock_db, pid, "only task", days_until_due=0)
    with patch("agentzero.focus.get_provider") as mock_get:
        doc = await focus.ensure_today_focus(CHAT_ID)
    mock_get.assert_not_called()
    assert doc["task_ids"] == [t1]


@pytest.mark.asyncio
async def test_llm_picks_when_pool_exceeds_target(mock_db):
    pid = await _seed_project(mock_db)
    ids = [await _seed_task(mock_db, pid, f"task {i}", days_until_due=0) for i in range(6)]
    with patch("agentzero.focus.get_provider", return_value=_provider("1, 3, 4")):
        doc = await focus.ensure_today_focus(CHAT_ID)
    # All six are due today with equal rank; picks are by ranked-pool position.
    assert len(doc["task_ids"]) == 3
    assert set(doc["task_ids"]).issubset(set(ids))
    # The three deadline tasks that missed the slate are disclosed as overflow.
    assert len(doc["overflow_ids"]) == 3
    assert set(doc["overflow_ids"]).isdisjoint(set(doc["task_ids"]))


@pytest.mark.asyncio
async def test_llm_failure_falls_back_to_deterministic_top3(mock_db):
    pid = await _seed_project(mock_db)
    for i in range(6):
        await _seed_task(mock_db, pid, f"task {i}", days_until_due=i - 2)  # two overdue first
    prov = MagicMock()
    prov.chat = AsyncMock(side_effect=RuntimeError("api down"))
    with patch("agentzero.focus.get_provider", return_value=prov):
        doc = await focus.ensure_today_focus(CHAT_ID)
    assert len(doc["task_ids"]) == 3
    titles = [
        (await mock_db.tasks.find_one({"_id": tid}))["title"] for tid in doc["task_ids"]
    ]
    assert "task 0" in titles and "task 1" in titles  # the overdue ones made it


@pytest.mark.asyncio
async def test_carryover_keeps_seats(mock_db):
    pid = await _seed_project(mock_db)
    carried = await _seed_task(mock_db, pid, "unfinished from yesterday")
    finished = await _seed_task(mock_db, pid, "finished yesterday", status="done")
    fresh = await _seed_task(mock_db, pid, "new urgent thing", days_until_due=0)
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    await mock_db.daily_focus.insert_one(
        {"chat_id": CHAT_ID, "date": yesterday, "task_ids": [carried, finished],
         "carryover_ids": [], "overflow_ids": [], "created_at": datetime.utcnow()}
    )
    doc = await focus.ensure_today_focus(CHAT_ID)
    assert carried in doc["task_ids"]          # unfinished carries over
    assert carried in doc["carryover_ids"]
    assert finished not in doc["task_ids"]     # done yesterday doesn't
    assert fresh in doc["task_ids"]            # seats topped up from the backlog


@pytest.mark.asyncio
async def test_goal_with_open_steps_represented_by_steps(mock_db):
    pid = await _seed_project(mock_db)
    goal = await _seed_task(mock_db, pid, "Deploy the website")
    step = await _seed_task(mock_db, pid, "prep the ENV vars", parent_id=goal)
    doc = await focus.ensure_today_focus(CHAT_ID)
    assert step in doc["task_ids"]
    assert goal not in doc["task_ids"]


@pytest.mark.asyncio
async def test_ensure_is_idempotent(mock_db):
    pid = await _seed_project(mock_db)
    await _seed_task(mock_db, pid, "a task")
    d1 = await focus.ensure_today_focus(CHAT_ID)
    d2 = await focus.ensure_today_focus(CHAT_ID)
    assert d1["_id"] == d2["_id"]
    assert await mock_db.daily_focus.count_documents({}) == 1


# ---------------------------------------------------------------------------
# Heartbeat fence
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_heartbeat_only_sees_focus_tasks(mock_db):
    pid = await _seed_project(mock_db)
    in_focus = await _seed_task(mock_db, pid, "focus deadline task", days_until_due=-1)
    outside = await _seed_task(mock_db, pid, "other project fire", days_until_due=-2)
    today = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
    # Pin the slate to just one task, bypassing selection.
    await mock_db.daily_focus.insert_one(
        {"chat_id": CHAT_ID, "date": focus._today_str(), "task_ids": [in_focus],
         "carryover_ids": [], "overflow_ids": [outside], "created_at": datetime.utcnow()}
    )
    prov = _provider("Your focus deadline task is overdue — go.")
    with patch("agentzero.autonomy.get_provider", return_value=prov), \
         patch("agentzero.autonomy.send", new_callable=AsyncMock):
        result = await autonomy.run_heartbeat(CHAT_ID, force=True)
    assert result is not None
    # The candidate summary handed to the LLM contains ONLY the focus task.
    summary = prov.chat.call_args[0][0][0]["content"]
    assert "focus deadline task" in summary
    assert "other project fire" not in summary


@pytest.mark.asyncio
async def test_heartbeat_silent_when_focus_all_done(mock_db):
    pid = await _seed_project(mock_db)
    done_task = await _seed_task(mock_db, pid, "already finished", status="done")
    await _seed_task(mock_db, pid, "outside the slate", days_until_due=-3)
    await mock_db.daily_focus.insert_one(
        {"chat_id": CHAT_ID, "date": focus._today_str(), "task_ids": [done_task],
         "carryover_ids": [], "overflow_ids": [], "created_at": datetime.utcnow()}
    )
    with patch("agentzero.autonomy.get_provider") as mock_get, \
         patch("agentzero.autonomy.send", new_callable=AsyncMock) as mock_send:
        result = await autonomy.run_heartbeat(CHAT_ID, force=True)
    assert result is None
    mock_send.assert_not_called()


# ---------------------------------------------------------------------------
# set_daily_focus tool
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_set_daily_focus_add_and_show(mock_db):
    pid = await _seed_project(mock_db)
    t1 = await _seed_task(mock_db, pid, "slate seed")
    await focus.ensure_today_focus(CHAT_ID)
    extra = await _seed_task(mock_db, pid, "the invoice task")
    out = await execute_tool(CHAT_ID, _tc("set_daily_focus", add_task_query="invoice"))
    assert "Added" in out and "invoice" in out
    doc = await focus.get_today_focus(CHAT_ID)
    assert extra in doc["task_ids"] and t1 in doc["task_ids"]
    shown = await execute_tool(CHAT_ID, _tc("set_daily_focus"))
    assert "Today's focus" in shown and "invoice task" in shown


@pytest.mark.asyncio
async def test_set_daily_focus_swap(mock_db):
    pid = await _seed_project(mock_db)
    old = await _seed_task(mock_db, pid, "boring chore")
    await focus.ensure_today_focus(CHAT_ID)
    new = await _seed_task(mock_db, pid, "urgent invoice")
    out = await execute_tool(
        CHAT_ID, _tc("set_daily_focus", add_task_query="urgent invoice", remove_task_query="boring chore")
    )
    assert "Swapped" in out
    doc = await focus.get_today_focus(CHAT_ID)
    assert new in doc["task_ids"] and old not in doc["task_ids"]


@pytest.mark.asyncio
async def test_set_daily_focus_remove_not_in_slate(mock_db):
    pid = await _seed_project(mock_db)
    await _seed_task(mock_db, pid, "slate task")
    await focus.ensure_today_focus(CHAT_ID)
    await _seed_task(mock_db, pid, "never focused")
    out = await execute_tool(CHAT_ID, _tc("set_daily_focus", remove_task_query="never focused"))
    assert "isn't in today's focus" in out


# ---------------------------------------------------------------------------
# Executor integrations
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_clearing_slate_suggests_next(mock_db):
    pid = await _seed_project(mock_db)
    t1 = await _seed_task(mock_db, pid, "the one focus task")
    await _seed_task(mock_db, pid, "backlog item", days_until_due=2)
    await mock_db.daily_focus.insert_one(
        {"chat_id": CHAT_ID, "date": focus._today_str(), "task_ids": [t1],
         "carryover_ids": [], "overflow_ids": [], "created_at": datetime.utcnow()}
    )
    out = await execute_tool(CHAT_ID, _tc("mark_done", task_query="the one focus task"))
    assert "clears today's focus" in out
    assert "backlog item" in out  # suggested, not silently added
    doc = await focus.get_today_focus(CHAT_ID)
    assert len(doc["task_ids"]) == 1  # nothing was auto-added


@pytest.mark.asyncio
async def test_closing_non_focus_task_no_suffix(mock_db):
    pid = await _seed_project(mock_db)
    t1 = await _seed_task(mock_db, pid, "slate task")
    await _seed_task(mock_db, pid, "side quest")
    await mock_db.daily_focus.insert_one(
        {"chat_id": CHAT_ID, "date": focus._today_str(), "task_ids": [t1],
         "carryover_ids": [], "overflow_ids": [], "created_at": datetime.utcnow()}
    )
    out = await execute_tool(CHAT_ID, _tc("mark_done", task_query="side quest"))
    assert "clears today's focus" not in out


@pytest.mark.asyncio
async def test_add_task_due_today_focus_full_notice(mock_db):
    pid = await _seed_project(mock_db)
    t1 = await _seed_task(mock_db, pid, "slate task")
    await mock_db.daily_focus.insert_one(
        {"chat_id": CHAT_ID, "date": focus._today_str(), "task_ids": [t1],
         "carryover_ids": [], "overflow_ids": [], "created_at": datetime.utcnow()}
    )
    today = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
    out = await execute_tool(
        CHAT_ID, _tc("add_task", project_name="Work", title="surprise same-day job", due_date=today)
    )
    assert "due TODAY" in out and "swap" in out


@pytest.mark.asyncio
async def test_add_task_overbooking_warning(mock_db):
    await _seed_project(mock_db)
    date_str = (datetime.now(timezone.utc) + timedelta(days=3)).strftime("%Y-%m-%d")
    titles = ["send invoice", "renew domain", "prep slides", "call landlord", "deploy hotfix"]
    out = ""
    for title in titles:
        out = await execute_tool(
            CHAT_ID, _tc("add_task", project_name="Work", title=title, due_date=date_str)
        )
    assert "5 tasks now due" in out
    assert "spreading" in out


@pytest.mark.asyncio
async def test_next_candidates_prefer_overflow(mock_db):
    pid = await _seed_project(mock_db)
    t1 = await _seed_task(mock_db, pid, "slate task")
    overflowed = await _seed_task(mock_db, pid, "missed the cut", days_until_due=0)
    await _seed_task(mock_db, pid, "undated filler")
    await mock_db.daily_focus.insert_one(
        {"chat_id": CHAT_ID, "date": focus._today_str(), "task_ids": [t1],
         "carryover_ids": [], "overflow_ids": [overflowed], "created_at": datetime.utcnow()}
    )
    suggestions = await focus.next_focus_candidates(CHAT_ID, limit=1)
    assert len(suggestions) == 1
    assert "missed the cut" in suggestions[0]
