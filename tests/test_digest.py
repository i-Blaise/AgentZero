"""
Morning digest tests — gather, narrate, empty plate, and LLM-failure fallback.
"""
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from agentzero import digest

CHAT_ID = 999


async def _seed_task(mock_db, title, days_until_due=None):
    proj = await mock_db.projects.find_one({"name": "Work"})
    if not proj:
        res = await mock_db.projects.insert_one(
            {"name": "Work", "scope": "work", "created_at": datetime.utcnow(), "updated_at": datetime.utcnow()}
        )
        pid = res.inserted_id
    else:
        pid = proj["_id"]
    due = None
    if days_until_due is not None:
        due = datetime.utcnow() + timedelta(days=days_until_due)
    await mock_db.tasks.insert_one(
        {
            "project_id": pid,
            "title": title,
            "status": "open",
            "due_date": due,
            "snoozed_until": None,
            "last_nudged_at": None,
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
        }
    )


def _provider(reply: str):
    p = MagicMock()
    p.chat = AsyncMock(return_value=reply)
    return p


@pytest.mark.asyncio
async def test_digest_includes_tasks(mock_db):
    await _seed_task(mock_db, "ship the thing", days_until_due=-1)  # overdue
    await _seed_task(mock_db, "no-date task", days_until_due=None)

    captured = {}

    async def fake_send(cid, txt):
        captured["text"] = txt

    with patch("agentzero.digest.get_provider", return_value=_provider("Morning. Two things, both your fault.")), \
         patch("agentzero.digest.send", side_effect=fake_send):
        out = await digest.send_morning_digest(CHAT_ID)

    assert out  # something was returned/sent
    assert captured["text"] == out


@pytest.mark.asyncio
async def test_digest_empty_plate_still_sends(mock_db):
    with patch("agentzero.digest.get_provider", return_value=_provider("Nothing on the plate. Suspicious.")), \
         patch("agentzero.digest.send", new_callable=AsyncMock) as mock_send:
        out = await digest.send_morning_digest(CHAT_ID)
    mock_send.assert_called_once()
    assert "suspicious" in out.lower()


@pytest.mark.asyncio
async def test_digest_falls_back_on_llm_error(mock_db):
    await _seed_task(mock_db, "file taxes", days_until_due=-2)
    prov = MagicMock()
    prov.chat = AsyncMock(side_effect=RuntimeError("api down"))
    with patch("agentzero.digest.get_provider", return_value=prov), \
         patch("agentzero.digest.send", new_callable=AsyncMock) as mock_send:
        out = await digest.send_morning_digest(CHAT_ID)
    # Plain fallback still carries the facts
    assert "file taxes" in out
    assert "Morning rundown" in out
    mock_send.assert_called_once()


@pytest.mark.asyncio
async def test_evening_digest_includes_tomorrow(mock_db):
    await _seed_task(mock_db, "submit invoice", days_until_due=1)  # due tomorrow
    with patch("agentzero.digest.get_provider", return_value=_provider("Wind down. One thing due tomorrow.")), \
         patch("agentzero.digest.send", new_callable=AsyncMock) as mock_send:
        out = await digest.send_evening_digest(CHAT_ID)
    mock_send.assert_called_once()
    assert out
    # "due tomorrow" task is surfaced in the gathered data
    data = await digest._gather(CHAT_ID)
    assert any("submit invoice" in i for i in data["due_tomorrow"])


@pytest.mark.asyncio
async def test_evening_digest_falls_back_on_llm_error(mock_db):
    await _seed_task(mock_db, "prep slides", days_until_due=1)
    prov = MagicMock()
    prov.chat = AsyncMock(side_effect=RuntimeError("api down"))
    with patch("agentzero.digest.get_provider", return_value=prov), \
         patch("agentzero.digest.send", new_callable=AsyncMock) as mock_send:
        out = await digest.send_evening_digest(CHAT_ID)
    assert "prep slides" in out
    assert "Wind-down" in out
    mock_send.assert_called_once()
