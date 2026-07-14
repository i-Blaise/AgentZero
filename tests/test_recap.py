"""
get_recap tests — the "brief me on what I completed this week / last N days" tool.

Covers: window filtering (in vs out), the legacy fallback (done tasks without completed_at
use updated_at), step-of-goal context, project grouping, reminders confirmed done, the
empty case, days clamping/garbage input, and that mark_done now stamps completed_at
(including cascade-closed steps).

Uses mongomock-motor (via conftest).
"""
from datetime import datetime, timedelta, timezone

import pytest

from agentzero.db import get_db
from agentzero.executor import execute_tool
from agentzero.llm import ToolCall

CHAT_ID = 777


def _tc(tool: str, **kwargs) -> ToolCall:
    return ToolCall(name=tool, args=kwargs)


async def _seed_project(mock_db, name="Deploy Proj", scope="work"):
    await execute_tool(CHAT_ID, _tc("create_project", name=name, scope=scope))
    return await mock_db.projects.find_one({"name": name})


async def _seed_done_task(mock_db, project, title, days_ago, parent_id=None,
                          with_completed_at=True):
    when = datetime.utcnow() - timedelta(days=days_ago)
    doc = {
        "project_id": project["_id"],
        "parent_task_id": parent_id,
        "title": title,
        "status": "done",
        "due_date": None,
        "snoozed_until": None,
        "last_nudged_at": None,
        "created_at": when - timedelta(days=1),
        "updated_at": when,
    }
    if with_completed_at:
        doc["completed_at"] = when
    result = await mock_db.tasks.insert_one(doc)
    return result.inserted_id


@pytest.mark.asyncio
async def test_recap_includes_recent_excludes_old(mock_db):
    proj = await _seed_project(mock_db)
    await _seed_done_task(mock_db, proj, "Ship the fix", days_ago=2)
    await _seed_done_task(mock_db, proj, "Ancient chore", days_ago=30)
    out = await execute_tool(CHAT_ID, _tc("get_recap", days=7))
    assert "Ship the fix" in out
    assert "Ancient chore" not in out


@pytest.mark.asyncio
async def test_recap_open_tasks_never_listed(mock_db):
    proj = await _seed_project(mock_db)
    await _seed_done_task(mock_db, proj, "Finished thing", days_ago=1)
    await execute_tool(CHAT_ID, _tc("add_task", project_name="Deploy Proj", title="Still open thing"))
    out = await execute_tool(CHAT_ID, _tc("get_recap", days=7))
    assert "Finished thing" in out
    assert "Still open thing" not in out


@pytest.mark.asyncio
async def test_recap_legacy_done_task_falls_back_to_updated_at(mock_db):
    """Done rows from before completed_at existed must still show up via updated_at."""
    proj = await _seed_project(mock_db)
    await _seed_done_task(mock_db, proj, "Legacy win", days_ago=3, with_completed_at=False)
    out = await execute_tool(CHAT_ID, _tc("get_recap", days=7))
    assert "Legacy win" in out


@pytest.mark.asyncio
async def test_recap_shows_goal_context_and_project(mock_db):
    proj = await _seed_project(mock_db)
    goal_id = await _seed_done_task(mock_db, proj, "Deploy the website", days_ago=1)
    await _seed_done_task(mock_db, proj, "prep the ENV vars", days_ago=2, parent_id=goal_id)
    out = await execute_tool(CHAT_ID, _tc("get_recap", days=7))
    assert "[work] Deploy Proj" in out
    assert 'step of "Deploy the website"' in out


@pytest.mark.asyncio
async def test_recap_includes_completed_reminders(mock_db):
    now = datetime.now(timezone.utc)
    await mock_db.reminders.insert_one(
        {"chat_id": CHAT_ID, "text": "call the bank", "status": "done",
         "fire_at": now - timedelta(days=1), "completed_at": now - timedelta(days=1)}
    )
    await mock_db.reminders.insert_one(
        {"chat_id": CHAT_ID, "text": "old errand", "status": "done",
         "fire_at": now - timedelta(days=20), "completed_at": now - timedelta(days=20)}
    )
    await mock_db.reminders.insert_one(
        {"chat_id": CHAT_ID, "text": "still pending", "status": "pending",
         "fire_at": now + timedelta(days=1)}
    )
    out = await execute_tool(CHAT_ID, _tc("get_recap", days=7))
    assert "call the bank" in out
    assert "old errand" not in out
    assert "still pending" not in out


@pytest.mark.asyncio
async def test_recap_empty_window(mock_db):
    proj = await _seed_project(mock_db)
    await _seed_done_task(mock_db, proj, "Ancient chore", days_ago=30)
    out = await execute_tool(CHAT_ID, _tc("get_recap", days=2))
    assert "Nothing was marked done" in out


@pytest.mark.asyncio
async def test_recap_days_defaults_and_garbage(mock_db):
    proj = await _seed_project(mock_db)
    await _seed_done_task(mock_db, proj, "Recent win", days_ago=1)
    # No days at all → default 7.
    out = await execute_tool(CHAT_ID, _tc("get_recap"))
    assert "Recent win" in out
    # Garbage days → default 7, not a crash.
    out = await execute_tool(CHAT_ID, _tc("get_recap", days="a fortnight"))
    assert "Recent win" in out


@pytest.mark.asyncio
async def test_mark_done_stamps_completed_at(mock_db):
    await _seed_project(mock_db)
    await execute_tool(CHAT_ID, _tc("add_task", project_name="Deploy Proj", title="Write tests"))
    await execute_tool(CHAT_ID, _tc("mark_done", task_query="Write tests"))
    task = await mock_db.tasks.find_one({"title": "Write tests"})
    assert task["status"] == "done"
    assert task.get("completed_at") is not None


@pytest.mark.asyncio
async def test_goal_cascade_close_stamps_steps_completed_at(mock_db):
    await _seed_project(mock_db)
    await execute_tool(CHAT_ID, _tc("add_task", project_name="Deploy Proj", title="Deploy the website"))
    await execute_tool(
        CHAT_ID,
        _tc("add_task", project_name="Deploy Proj", title="prep the ENV vars",
            parent_task_query="Deploy the website"),
    )
    await execute_tool(CHAT_ID, _tc("mark_done", task_query="Deploy the website"))
    step = await mock_db.tasks.find_one({"title": "prep the ENV vars"})
    assert step["status"] == "done"
    assert step.get("completed_at") is not None


@pytest.mark.asyncio
async def test_recap_then_full_flow(mock_db):
    """End to end: create → close via the normal tool → recap reports it."""
    await _seed_project(mock_db, name="Errands", scope="personal")
    await execute_tool(CHAT_ID, _tc("add_task", project_name="Errands", title="Buy groceries"))
    await execute_tool(CHAT_ID, _tc("mark_done", task_query="Buy groceries"))
    out = await execute_tool(CHAT_ID, _tc("get_recap", days=1))
    assert "Buy groceries" in out
    assert "[personal] Errands" in out
