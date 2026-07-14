"""Board API — tasks + reminders + overview endpoints (read-only, key-gated)."""
import pytest
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI
from fastapi.testclient import TestClient
from unittest.mock import patch

from agentzero import api

CHAT_ID = 999
KEY = "secret-key-123"


def _app():
    app = FastAPI()
    app.include_router(api.router)
    return app


async def _seed(mock_db):
    now = datetime.now(timezone.utc)
    naive = now.replace(tzinfo=None)
    work = (await mock_db.projects.insert_one(
        {"name": "Jobotron", "scope": "work", "created_at": now, "updated_at": now}
    )).inserted_id
    await mock_db.tasks.insert_many([
        {"project_id": work, "title": "ship demo", "status": "open",
         "due_date": now - timedelta(days=2), "created_at": now, "updated_at": now},   # overdue
        {"project_id": work, "title": "write tests", "status": "done",
         "due_date": None, "created_at": now, "updated_at": now},
        {"project_id": work, "title": "later thing", "status": "snoozed",
         "snoozed_until": now + timedelta(days=5), "created_at": now, "updated_at": now},
    ])
    # Timed pings — tasks with remind_at (the merged "reminders"), naive-UTC datetimes.
    await mock_db.tasks.insert_many([
        {"project_id": None, "title": "call the bank", "status": "open",
         "remind_at": naive, "reminded_at": naive, "next_nudge_at": naive,
         "nudge_count": 2, "created_at": naive, "updated_at": naive},          # → awaiting_ack
        {"project_id": None, "title": "stretch", "status": "open",
         "remind_at": naive + timedelta(hours=1), "created_at": naive, "updated_at": naive},  # → pending
        {"project_id": None, "title": "old one", "status": "done",
         "remind_at": naive - timedelta(days=1), "completed_at": naive,
         "created_at": naive, "updated_at": naive},                            # → done
    ])
    await mock_db.recurring_reminders.insert_one(
        {"chat_id": CHAT_ID, "text": "standup", "hour": 9, "minute": 0, "day_of_week": "mon-fri", "active": True}
    )


def _patches():
    return patch("agentzero.api.DASHBOARD_API_KEY", KEY), patch("agentzero.api.ALLOWED_CHAT_ID", CHAT_ID)


@pytest.mark.asyncio
async def test_tasks_endpoint(mock_db):
    await _seed(mock_db)
    p1, p2 = _patches()
    with p1, p2:
        r = TestClient(_app()).get("/api/tasks", headers={"X-API-Key": KEY})
    body = r.json()
    # Timed pings are tasks too since the merge — they show on the board.
    assert body["count"] == 6
    assert body["by_status"] == {"open": 3, "done": 2, "snoozed": 1}
    demo = next(t for t in body["tasks"] if t["title"] == "ship demo")
    assert demo["project"] == "Jobotron" and demo["scope"] == "work"
    assert demo["is_overdue"] is True
    assert demo["remind_at"] is None
    bank = next(t for t in body["tasks"] if t["title"] == "call the bank")
    assert bank["remind_at"] is not None and bank["reminded_at"] is not None


@pytest.mark.asyncio
async def test_tasks_status_filter(mock_db):
    await _seed(mock_db)
    p1, p2 = _patches()
    with p1, p2:
        r = TestClient(_app()).get("/api/tasks?status=open", headers={"X-API-Key": KEY})
    body = r.json()
    assert body["count"] == 3
    assert {t["title"] for t in body["tasks"]} == {"ship demo", "call the bank", "stretch"}


@pytest.mark.asyncio
async def test_reminders_endpoint(mock_db):
    await _seed(mock_db)
    p1, p2 = _patches()
    with p1, p2:
        r = TestClient(_app()).get("/api/reminders", headers={"X-API-Key": KEY})
    body = r.json()
    # Same pre-merge response shape, now derived from timed tasks.
    assert body["count"] == 3
    assert body["by_status"] == {"awaiting_ack": 1, "pending": 1, "done": 1}
    awaiting = next(x for x in body["reminders"] if x["text"] == "call the bank")
    assert awaiting["awaiting_ack"] is True and awaiting["nudge_count"] == 2
    assert awaiting["fire_at"] is not None and awaiting["fired_at"] is not None
    assert body["recurring"][0]["schedule"] == "every weekday at 09:00"


@pytest.mark.asyncio
async def test_overview_endpoint(mock_db):
    await _seed(mock_db)
    p1, p2 = _patches()
    with p1, p2:
        r = TestClient(_app()).get("/api/overview", headers={"X-API-Key": KEY})
    body = r.json()
    assert body["tasks"] == {"open": 3, "done": 2, "snoozed": 1}
    assert body["reminders"] == {"awaiting_ack": 1, "pending": 1, "done": 1}
    assert body["reminders_active"] == 2
    assert body["projects"] == 1


def test_board_requires_key(mock_db):
    with patch("agentzero.api.DASHBOARD_API_KEY", KEY):
        client = TestClient(_app())
        assert client.get("/api/tasks").status_code == 401
        assert client.get("/api/reminders").status_code == 401


# ---------------------------------------------------------------------------
# Goal → step hierarchy + reminder-lifecycle mirroring
# ---------------------------------------------------------------------------

async def _seed_hierarchy(mock_db):
    """A goal with 2 steps (1 done) + a standalone task."""
    now = datetime.now(timezone.utc)
    work = (await mock_db.projects.insert_one(
        {"name": "Jobotron", "scope": "work", "created_at": now, "updated_at": now}
    )).inserted_id
    goal = (await mock_db.tasks.insert_one(
        {"project_id": work, "parent_task_id": None, "title": "Deploy the website",
         "status": "open", "created_at": now, "updated_at": now}
    )).inserted_id
    await mock_db.tasks.insert_many([
        {"project_id": work, "parent_task_id": goal, "title": "pull latest",
         "status": "done", "created_at": now, "updated_at": now},
        {"project_id": work, "parent_task_id": goal, "title": "prep ENV vars",
         "status": "open", "created_at": now, "updated_at": now},
        {"project_id": work, "parent_task_id": None, "title": "standalone thing",
         "status": "open", "created_at": now, "updated_at": now},
    ])
    return goal


@pytest.mark.asyncio
async def test_tasks_hierarchy_fields_and_tree(mock_db):
    goal_id = await _seed_hierarchy(mock_db)
    p1, p2 = _patches()
    with p1, p2:
        r = TestClient(_app()).get("/api/tasks", headers={"X-API-Key": KEY})
    body = r.json()

    # Flat list rows carry their goal/step relations.
    goal = next(t for t in body["tasks"] if t["title"] == "Deploy the website")
    assert goal["is_goal"] is True
    assert goal["steps_done"] == 1 and goal["steps_total"] == 2
    assert goal["parent_task_id"] is None and goal["parent_title"] is None
    step = next(t for t in body["tasks"] if t["title"] == "prep ENV vars")
    assert step["parent_task_id"] == str(goal_id)
    assert step["parent_title"] == "Deploy the website"
    assert step["is_goal"] is False
    solo = next(t for t in body["tasks"] if t["title"] == "standalone thing")
    assert solo["is_goal"] is False and solo["parent_task_id"] is None

    # Nested tree: 2 top-level nodes (goal + standalone), steps nested under the goal.
    tree = body["tree"]
    assert len(tree) == 2
    tree_goal = next(n for n in tree if n["title"] == "Deploy the website")
    assert [s["title"] for s in tree_goal["steps"]] == ["pull latest", "prep ENV vars"]
    tree_solo = next(n for n in tree if n["title"] == "standalone thing")
    assert tree_solo["steps"] == []


@pytest.mark.asyncio
async def test_tasks_status_filter_keeps_tree_progress_truthful(mock_db):
    """status=open filters the FLAT list, but the tree still counts done steps."""
    await _seed_hierarchy(mock_db)
    p1, p2 = _patches()
    with p1, p2:
        r = TestClient(_app()).get("/api/tasks?status=open", headers={"X-API-Key": KEY})
    body = r.json()
    titles = [t["title"] for t in body["tasks"]]
    assert "pull latest" not in titles  # done → filtered from the flat list
    # …yet the annotated goal row and the tree still show 1/2 progress.
    goal = next(t for t in body["tasks"] if t["title"] == "Deploy the website")
    assert goal["steps_done"] == 1 and goal["steps_total"] == 2
    tree_goal = next(n for n in body["tree"] if n["title"] == "Deploy the website")
    assert len(tree_goal["steps"]) == 2


@pytest.mark.asyncio
async def test_reminders_mirror_lifecycle(mock_db):
    """The /api/reminders view derives the old lifecycle vocabulary from timed tasks:
    open+reminded_at → awaiting_ack, open → pending, done/cancelled pass through."""
    now = datetime.utcnow()
    await mock_db.tasks.insert_many([
        {"project_id": None, "title": "fired ghost", "status": "open",
         "remind_at": now, "reminded_at": now, "next_nudge_at": now + timedelta(hours=3),
         "created_at": now, "updated_at": now},
        {"project_id": None, "title": "fresh ping", "status": "open",
         "remind_at": now + timedelta(hours=1), "created_at": now, "updated_at": now},
        {"project_id": None, "title": "closed", "status": "done",
         "remind_at": now - timedelta(days=1), "completed_at": now,
         "created_at": now, "updated_at": now},
        {"project_id": None, "title": "dropped", "status": "cancelled",
         "remind_at": now - timedelta(days=1), "created_at": now, "updated_at": now},
    ])
    p1, p2 = _patches()
    with p1, p2:
        client = TestClient(_app())
        body = client.get("/api/reminders", headers={"X-API-Key": KEY}).json()
        active = client.get("/api/reminders?status=active", headers={"X-API-Key": KEY}).json()

    ghost = next(x for x in body["reminders"] if x["text"] == "fired ghost")
    assert ghost["status"] == "awaiting_ack"
    assert ghost["awaiting_ack"] is True
    assert ghost["is_active"] is True
    assert ghost["next_nudge_at"] is not None
    closed = next(x for x in body["reminders"] if x["text"] == "closed")
    assert closed["status"] == "done"
    assert closed["awaiting_ack"] is False and closed["is_active"] is False
    dropped = next(x for x in body["reminders"] if x["text"] == "dropped")
    assert dropped["status"] == "cancelled" and dropped["is_active"] is False

    # status=active pseudo-filter = still-open timed tasks.
    assert {x["text"] for x in active["reminders"]} == {"fired ghost", "fresh ping"}


@pytest.mark.asyncio
async def test_overview_goals_rollup(mock_db):
    await _seed_hierarchy(mock_db)
    p1, p2 = _patches()
    with p1, p2:
        body = TestClient(_app()).get("/api/overview", headers={"X-API-Key": KEY}).json()
    assert body["goals"] == {"count": 1, "steps_done": 1, "steps_total": 2}
    assert body["reminders_active"] == 0  # no timed tasks in this seed


# ---------------------------------------------------------------------------
# Daily focus in /api/overview
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_overview_focus_null_before_selection(mock_db):
    """No slate yet today → overview.focus is null (and older APIs won't have the key
    at all) — Cockpit must tolerate both."""
    await _seed(mock_db)
    p1, p2 = _patches()
    with p1, p2:
        body = TestClient(_app()).get("/api/overview", headers={"X-API-Key": KEY}).json()
    assert body["focus"] is None


@pytest.mark.asyncio
async def test_overview_focus_block(mock_db):
    from agentzero import focus as focus_mod

    await _seed_hierarchy(mock_db)
    step = await mock_db.tasks.find_one({"title": "prep ENV vars"})
    done_step = await mock_db.tasks.find_one({"title": "pull latest"})
    standalone = await mock_db.tasks.find_one({"title": "standalone thing"})
    now = datetime.now(timezone.utc)
    await mock_db.daily_focus.insert_one(
        {"chat_id": CHAT_ID, "date": focus_mod._today_str(),
         "task_ids": [step["_id"], done_step["_id"]],
         "carryover_ids": [step["_id"]],
         "overflow_ids": [standalone["_id"]],
         "created_at": now}
    )
    p1, p2 = _patches()
    with p1, p2:
        body = TestClient(_app()).get("/api/overview", headers={"X-API-Key": KEY}).json()

    f = body["focus"]
    assert f["total"] == 2 and f["done"] == 1
    assert f["date"] == focus_mod._today_str()
    by_title = {i["title"]: i for i in f["items"]}
    assert by_title["prep ENV vars"]["carried_over"] is True
    assert by_title["prep ENV vars"]["status"] == "open"
    assert by_title["prep ENV vars"]["parent_title"] == "Deploy the website"
    assert by_title["pull latest"]["status"] == "done"
    # Overflow rides along with full task serialization (open tasks only).
    assert [o["title"] for o in f["overflow"]] == ["standalone thing"]
