"""Dashboard API — auth gating + read-only expense JSON endpoints.
Uses a minimal app mounting just the router (no lifespan), against mock_db."""
import pytest
from datetime import datetime, timezone
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
    now = datetime(2026, 6, 16, 12, 0, tzinfo=timezone.utc)
    docs = [
        {"chat_id": CHAT_ID, "merchant": "Uber", "amount": 38.5, "currency": "GHS",
         "category": "transport", "description": "trip", "spent_at": now, "source": "yahoo", "email_id": "yahoo:1"},
        {"chat_id": CHAT_ID, "merchant": "Cafe", "amount": 50.0, "currency": "GHS",
         "category": "food", "description": "", "spent_at": now, "source": "gmail", "email_id": "gmail:2"},
        {"chat_id": CHAT_ID, "merchant": "Steam", "amount": 10.0, "currency": "USD",
         "category": "entertainment", "description": "", "spent_at": now, "source": "manual", "email_id": ""},
    ]
    await mock_db.expenses.insert_many(docs)


def test_api_disabled_without_key(mock_db):
    with patch("agentzero.api.DASHBOARD_API_KEY", ""):
        client = TestClient(_app())
        assert client.get("/api/expenses").status_code == 404


def test_api_rejects_bad_key(mock_db):
    with patch("agentzero.api.DASHBOARD_API_KEY", KEY):
        client = TestClient(_app())
        assert client.get("/api/expenses").status_code == 401
        assert client.get("/api/expenses", headers={"X-API-Key": "nope"}).status_code == 401


@pytest.mark.asyncio
async def test_expenses_endpoint(mock_db):
    await _seed(mock_db)
    with patch("agentzero.api.DASHBOARD_API_KEY", KEY), \
         patch("agentzero.api.ALLOWED_CHAT_ID", CHAT_ID):
        client = TestClient(_app())
        r = client.get("/api/expenses?period=month", headers={"X-API-Key": KEY})
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 3
    merchants = {e["merchant"] for e in body["expenses"]}
    assert merchants == {"Uber", "Cafe", "Steam"}
    assert all("spent_at" in e and "_id" not in e for e in body["expenses"])


@pytest.mark.asyncio
async def test_summary_endpoint_groups_per_currency(mock_db):
    await _seed(mock_db)
    with patch("agentzero.api.DASHBOARD_API_KEY", KEY), \
         patch("agentzero.api.ALLOWED_CHAT_ID", CHAT_ID):
        client = TestClient(_app())
        r = client.get("/api/expenses/summary?period=month", headers={"X-API-Key": KEY})
    body = r.json()
    assert body["by_currency"] == {"GHS": 88.5, "USD": 10.0}
    assert body["by_category"]["transport"] == {"GHS": 38.5}


@pytest.mark.asyncio
async def test_timeseries_and_categories(mock_db):
    await _seed(mock_db)
    with patch("agentzero.api.DASHBOARD_API_KEY", KEY), \
         patch("agentzero.api.ALLOWED_CHAT_ID", CHAT_ID):
        client = TestClient(_app())
        ts = client.get("/api/expenses/timeseries?bucket=day&period=month",
                        headers={"X-API-Key": KEY}).json()
        cats = client.get("/api/expenses/categories", headers={"X-API-Key": KEY}).json()
    assert ts["bucket"] == "day"
    assert ts["series"][0]["date"] == "2026-06-16"
    assert ts["series"][0]["totals"] == {"GHS": 88.5, "USD": 10.0}
    assert "food" in cats["categories"]
