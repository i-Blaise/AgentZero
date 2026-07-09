"""
Goal → step task hierarchy tests.

Covers: filing a task as a step (add_task parent_task_query), re-filing (set_task_parent
attach/detach), 2-level flattening, parent-scoped dedup, cascade-close of a goal, the
last-step nudge, the (done/total) tree render in get_status, and undo of a re-file.

Uses mongomock-motor (via conftest) and patches the scheduler where a closer might touch it.
"""
import pytest
from unittest.mock import patch

from agentzero.executor import execute_tool, undo_last, get_status
from agentzero.llm import ToolCall
from agentzero.db import get_db

CHAT_ID = 777


def _tc(tool: str, **kwargs) -> ToolCall:
    return ToolCall(name=tool, args=kwargs)


async def _project(name="Deploy Proj", scope="work"):
    await execute_tool(CHAT_ID, _tc("create_project", name=name, scope=scope))


async def _goal_and_step(mock_db):
    """Create a goal 'Deploy the website' and file 'prep the ENV vars' under it."""
    await _project()
    await execute_tool(CHAT_ID, _tc("add_task", project_name="Deploy Proj", title="Deploy the website"))
    result = await execute_tool(
        CHAT_ID,
        _tc("add_task", project_name="Deploy Proj", title="prep the ENV vars",
            parent_task_query="Deploy the website"),
    )
    return result


@pytest.mark.asyncio
async def test_add_task_as_step_sets_parent(mock_db):
    result = await _goal_and_step(mock_db)
    assert "step under" in result.lower()
    goal = await mock_db.tasks.find_one({"title": "Deploy the website"})
    step = await mock_db.tasks.find_one({"title": "prep the ENV vars"})
    assert step["parent_task_id"] == goal["_id"]


@pytest.mark.asyncio
async def test_add_step_parent_not_found(mock_db):
    await _project()
    result = await execute_tool(
        CHAT_ID,
        _tc("add_task", project_name="Deploy Proj", title="prep env",
            parent_task_query="nonexistent goal"),
    )
    assert "couldn't find" in result.lower()
    # Nothing was created for the step.
    assert await mock_db.tasks.find_one({"title": "prep env"}) is None


@pytest.mark.asyncio
async def test_dedup_scoped_by_parent(mock_db):
    """Same step title under two DIFFERENT goals is allowed; the same title under the SAME
    goal is blocked."""
    await _project()
    await execute_tool(CHAT_ID, _tc("add_task", project_name="Deploy Proj", title="Ship mobile app"))
    await execute_tool(CHAT_ID, _tc("add_task", project_name="Deploy Proj", title="Write blog post"))
    await execute_tool(CHAT_ID, _tc("add_task", project_name="Deploy Proj", title="run tests", parent_task_query="Ship mobile app"))
    # Same step title under the OTHER goal — allowed.
    ok = await execute_tool(CHAT_ID, _tc("add_task", project_name="Deploy Proj", title="run tests", parent_task_query="Write blog post"))
    assert "step under" in ok.lower()
    # Same title under the SAME goal again — blocked.
    dup = await execute_tool(CHAT_ID, _tc("add_task", project_name="Deploy Proj", title="run tests", parent_task_query="Ship mobile app"))
    assert "not adding a duplicate" in dup.lower()
    assert await mock_db.tasks.count_documents({"title": "run tests"}) == 2


@pytest.mark.asyncio
async def test_flatten_to_two_levels(mock_db):
    """Filing under a STEP re-points to that step's goal (max depth 2)."""
    await _goal_and_step(mock_db)  # goal + 'prep the ENV vars' step
    goal = await mock_db.tasks.find_one({"title": "Deploy the website"})
    # Try to nest under the step 'prep the ENV vars'.
    await execute_tool(
        CHAT_ID,
        _tc("add_task", project_name="Deploy Proj", title="export secrets",
            parent_task_query="prep the ENV vars"),
    )
    nested = await mock_db.tasks.find_one({"title": "export secrets"})
    assert nested["parent_task_id"] == goal["_id"]  # flattened to the goal, not the step


@pytest.mark.asyncio
async def test_set_task_parent_attach_and_detach(mock_db):
    await _project()
    await execute_tool(CHAT_ID, _tc("add_task", project_name="Deploy Proj", title="Deploy the website"))
    await execute_tool(CHAT_ID, _tc("add_task", project_name="Deploy Proj", title="pull latest"))
    # Attach.
    r1 = await execute_tool(CHAT_ID, _tc("set_task_parent", task_query="pull latest", parent_task_query="Deploy the website"))
    assert "filed" in r1.lower()
    goal = await mock_db.tasks.find_one({"title": "Deploy the website"})
    step = await mock_db.tasks.find_one({"title": "pull latest"})
    assert step["parent_task_id"] == goal["_id"]
    # Detach.
    r2 = await execute_tool(CHAT_ID, _tc("set_task_parent", task_query="pull latest"))
    assert "standalone" in r2.lower()
    step = await mock_db.tasks.find_one({"title": "pull latest"})
    assert step["parent_task_id"] is None


@pytest.mark.asyncio
async def test_set_task_parent_refuses_moving_a_goal(mock_db):
    """A task that itself has steps can't be filed under another goal (would go 3 deep)."""
    await _goal_and_step(mock_db)  # 'Deploy the website' now has a step
    await execute_tool(CHAT_ID, _tc("add_task", project_name="Deploy Proj", title="Other goal"))
    result = await execute_tool(CHAT_ID, _tc("set_task_parent", task_query="Deploy the website", parent_task_query="Other goal"))
    assert "has its own steps" in result.lower()


@pytest.mark.asyncio
async def test_close_goal_cascades_to_steps(mock_db):
    await _goal_and_step(mock_db)
    await execute_tool(CHAT_ID, _tc("add_task", project_name="Deploy Proj", title="run migrations", parent_task_query="Deploy the website"))
    with patch("agentzero.scheduler.get_scheduler"):
        result = await execute_tool(CHAT_ID, _tc("mark_done", task_query="Deploy the website"))
    assert "closed its 2 open steps" in result.lower()
    # Goal and both steps are all done.
    assert await mock_db.tasks.count_documents({"status": "done"}) == 3
    assert await mock_db.tasks.count_documents({"status": "open"}) == 0


@pytest.mark.asyncio
async def test_last_step_nudges_to_close_goal(mock_db):
    await _goal_and_step(mock_db)  # goal with ONE step
    with patch("agentzero.scheduler.get_scheduler"):
        result = await execute_tool(CHAT_ID, _tc("mark_done", task_query="prep the ENV vars"))
    assert "last open step" in result.lower()
    assert "mark the whole goal done" in result.lower()
    # The goal itself is NOT auto-closed — completion stays the user's call.
    goal = await mock_db.tasks.find_one({"title": "Deploy the website"})
    assert goal["status"] == "open"


@pytest.mark.asyncio
async def test_nonlast_step_reports_progress(mock_db):
    await _goal_and_step(mock_db)
    await execute_tool(CHAT_ID, _tc("add_task", project_name="Deploy Proj", title="run migrations", parent_task_query="Deploy the website"))
    with patch("agentzero.scheduler.get_scheduler"):
        result = await execute_tool(CHAT_ID, _tc("mark_done", task_query="prep the ENV vars"))
    assert "1/2 steps" in result


@pytest.mark.asyncio
async def test_get_status_renders_tree(mock_db):
    await _goal_and_step(mock_db)
    await execute_tool(CHAT_ID, _tc("add_task", project_name="Deploy Proj", title="run migrations", parent_task_query="Deploy the website"))
    status = await get_status()
    assert "Deploy the website (0/2)" in status
    assert "- prep the ENV vars" in status
    assert "- run migrations" in status


@pytest.mark.asyncio
async def test_undo_refile(mock_db):
    await _project()
    await execute_tool(CHAT_ID, _tc("add_task", project_name="Deploy Proj", title="Deploy the website"))
    await execute_tool(CHAT_ID, _tc("add_task", project_name="Deploy Proj", title="pull latest"))
    await execute_tool(CHAT_ID, _tc("set_task_parent", task_query="pull latest", parent_task_query="Deploy the website"))
    goal = await mock_db.tasks.find_one({"title": "Deploy the website"})
    step = await mock_db.tasks.find_one({"title": "pull latest"})
    assert step["parent_task_id"] == goal["_id"]
    await undo_last(CHAT_ID)
    step = await mock_db.tasks.find_one({"title": "pull latest"})
    assert step["parent_task_id"] is None
