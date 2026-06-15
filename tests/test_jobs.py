"""
Job hunter tests — profile save, fetch dedupe/seen, and find_jobs.
The HTTP sources are patched so no network is hit.
"""
import pytest
from unittest.mock import AsyncMock, patch

from agentzero import jobs
from agentzero.executor import execute_tool
from agentzero.llm import ToolCall

CHAT_ID = 999


def _tc(tool, **kwargs):
    return ToolCall(name=tool, args=kwargs)


def _job(url, title="Backend Engineer", company="Acme"):
    return {"source": "Remotive", "title": title, "company": company,
            "location": "Remote", "url": url, "snippet": "Build things."}


@pytest.mark.asyncio
async def test_set_job_profile(mock_db):
    result = await execute_tool(
        CHAT_ID, _tc("set_job_profile", cv="10y Python/React", criteria="remote senior backend")
    )
    assert "saved" in result.lower()
    doc = await mock_db.profile.find_one({"chat_id": CHAT_ID})
    assert doc["cv"] == "10y Python/React"
    assert doc["criteria"] == "remote senior backend"


@pytest.mark.asyncio
async def test_fetch_jobs_dedupes_and_marks_seen(mock_db):
    batch = [_job("https://j/1"), _job("https://j/2"), _job("https://j/1")]  # dup url
    with patch("agentzero.jobs._remotive", new_callable=AsyncMock, return_value=batch), \
         patch("agentzero.jobs._remoteok", new_callable=AsyncMock, return_value=[]), \
         patch("agentzero.jobs._wwr", new_callable=AsyncMock, return_value=[]):
        first = await jobs.fetch_jobs(CHAT_ID, query=None, limit=10)
    assert {j["url"] for j in first} == {"https://j/1", "https://j/2"}  # deduped
    assert await mock_db.seen_jobs.count_documents({"chat_id": CHAT_ID}) == 2

    # Second fetch of the same postings returns nothing new (already seen)
    with patch("agentzero.jobs._remotive", new_callable=AsyncMock, return_value=batch), \
         patch("agentzero.jobs._remoteok", new_callable=AsyncMock, return_value=[]), \
         patch("agentzero.jobs._wwr", new_callable=AsyncMock, return_value=[]):
        second = await jobs.fetch_jobs(CHAT_ID, query=None, limit=10)
    assert second == []


@pytest.mark.asyncio
async def test_fetch_survives_a_failing_source(mock_db):
    with patch("agentzero.jobs._remotive", new_callable=AsyncMock, side_effect=RuntimeError("down")), \
         patch("agentzero.jobs._remoteok", new_callable=AsyncMock, return_value=[_job("https://j/9")]), \
         patch("agentzero.jobs._wwr", new_callable=AsyncMock, return_value=[]):
        out = await jobs.fetch_jobs(CHAT_ID, query=None, limit=10)
    assert [j["url"] for j in out] == ["https://j/9"]


@pytest.mark.asyncio
async def test_find_jobs_tool(mock_db):
    with patch("agentzero.jobs._remotive", new_callable=AsyncMock, return_value=[_job("https://j/5", title="Go Dev")]), \
         patch("agentzero.jobs._remoteok", new_callable=AsyncMock, return_value=[]), \
         patch("agentzero.jobs._wwr", new_callable=AsyncMock, return_value=[]):
        result = await execute_tool(CHAT_ID, _tc("find_jobs", query="go"))
    assert "Go Dev" in result
    assert "https://j/5" in result


@pytest.mark.asyncio
async def test_job_digest_skips_without_profile(mock_db):
    # No profile saved → digest returns None (nothing to match against)
    out = await jobs.send_job_digest(CHAT_ID)
    assert out is None
