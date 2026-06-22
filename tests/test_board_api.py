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
    await mock_db.reminders.insert_many([
        {"chat_id": CHAT_ID, "text": "call the bank", "status": "awaiting_ack",
         "fire_at": now, "created_at": now, "fired_at": now, "nudge_count": 2},
        {"chat_id": CHAT_ID, "text": "stretch", "status": "pending", "fire_at": now + timedelta(hours=1), "created_at": now},
        {"chat_id": CHAT_ID, "text": "old one", "status": "done", "fire_at": now - timedelta(days=1),
         "created_at": now, "completed_at": now},
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
    assert body["count"] == 3
    assert body["by_status"] == {"open": 1, "done": 1, "snoozed": 1}
    demo = next(t for t in body["tasks"] if t["title"] == "ship demo")
    assert demo["project"] == "Jobotron" and demo["scope"] == "work"
    assert demo["is_overdue"] is True


@pytest.mark.asyncio
async def test_tasks_status_filter(mock_db):
    await _seed(mock_db)
    p1, p2 = _patches()
    with p1, p2:
        r = TestClient(_app()).get("/api/tasks?status=open", headers={"X-API-Key": KEY})
    body = r.json()
    assert body["count"] == 1 and body["tasks"][0]["title"] == "ship demo"


@pytest.mark.asyncio
async def test_reminders_endpoint(mock_db):
    await _seed(mock_db)
    p1, p2 = _patches()
    with p1, p2:
        r = TestClient(_app()).get("/api/reminders", headers={"X-API-Key": KEY})
    body = r.json()
    assert body["count"] == 3
    assert body["by_status"] == {"awaiting_ack": 1, "pending": 1, "done": 1}
    awaiting = next(x for x in body["reminders"] if x["text"] == "call the bank")
    assert awaiting["awaiting_ack"] is True and awaiting["nudge_count"] == 2
    assert body["recurring"][0]["schedule"] == "every weekday at 09:00"


@pytest.mark.asyncio
async def test_overview_endpoint(mock_db):
    await _seed(mock_db)
    p1, p2 = _patches()
    with p1, p2:
        r = TestClient(_app()).get("/api/overview", headers={"X-API-Key": KEY})
    body = r.json()
    assert body["tasks"] == {"open": 1, "done": 1, "snoozed": 1}
    assert body["reminders"] == {"awaiting_ack": 1, "pending": 1, "done": 1}
    assert body["projects"] == 1


def test_board_requires_key(mock_db):
    with patch("agentzero.api.DASHBOARD_API_KEY", KEY):
        client = TestClient(_app())
        assert client.get("/api/tasks").status_code == 401
        assert client.get("/api/reminders").status_code == 401
