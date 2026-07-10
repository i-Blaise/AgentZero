"""
Read-only dashboard API (mounted at /api on the main FastAPI app).

Exposes the expense data as JSON for an external spending dashboard. This is financial
data on a public domain, so every route requires the `X-API-Key` header to match
DASHBOARD_API_KEY; if that key isn't set the whole API is disabled (404). All queries are
scoped to the single owner (ALLOWED_CHAT_ID) and are strictly read-only.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException

from agentzero import applications, board, expenses
from agentzero.config import ALLOWED_CHAT_ID, DASHBOARD_API_KEY


async def require_api_key(x_api_key: str = Header(default="")) -> None:
    if not DASHBOARD_API_KEY:
        raise HTTPException(status_code=404, detail="API disabled")
    if x_api_key != DASHBOARD_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


router = APIRouter(prefix="/api", tags=["dashboard"], dependencies=[Depends(require_api_key)])


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _range(period: str, start: str | None, end: str | None):
    s = _parse_iso(start)
    e = _parse_iso(end)
    if s is None and period:
        s = expenses._period_start(period)
    return s, e


@router.get("/health")
async def api_health() -> dict:
    return {"status": "ok"}


@router.get("/expenses")
async def api_expenses(
    period: str = "month",
    category: str | None = None,
    start: str | None = None,
    end: str | None = None,
    limit: int = 500,
) -> dict:
    s, e = _range(period, start, end)
    rows = await expenses.query_range(ALLOWED_CHAT_ID, s, e, category)
    limit = max(1, min(int(limit), 2000))
    return {
        "count": len(rows),
        "expenses": [expenses.serialize_expense(r) for r in rows[:limit]],
    }


@router.get("/expenses/summary")
async def api_summary(
    period: str = "month", start: str | None = None, end: str | None = None
) -> dict:
    s, e = _range(period, start, end)
    rows = await expenses.query_range(ALLOWED_CHAT_ID, s, e, None)
    out = expenses.summary_data(rows)
    out["period"] = {"period": period, "start": s.isoformat() if s else None, "end": e.isoformat() if e else None}
    return out


@router.get("/expenses/timeseries")
async def api_timeseries(
    bucket: str = "day",
    period: str = "month",
    category: str | None = None,
    start: str | None = None,
    end: str | None = None,
) -> dict:
    if bucket not in ("day", "week", "month"):
        bucket = "day"
    s, e = _range(period, start, end)
    rows = await expenses.query_range(ALLOWED_CHAT_ID, s, e, category)
    return {"bucket": bucket, "series": expenses.timeseries_data(rows, bucket)}


@router.get("/expenses/categories")
async def api_categories() -> dict:
    return {"categories": expenses._CATEGORIES}


# --- Job applications -------------------------------------------------------

@router.get("/applications")
async def api_applications(status: str | None = None) -> dict:
    rows = await applications.query_applications(ALLOWED_CHAT_ID, status)
    return {
        "count": len(rows),
        "by_status": applications.status_counts(rows),
        "cv_on_file": await applications.profile_cv(ALLOWED_CHAT_ID),
        "applications": [applications.serialize_application(r) for r in rows],
    }


# --- Tasks & reminders (the board) ------------------------------------------

@router.get("/tasks")
async def api_tasks(status: str | None = None, scope: str | None = None) -> dict:
    items = await board.query_tasks(status, scope)
    by_id, children = await board.hierarchy_maps()
    return {
        "count": len(items),
        "by_status": board.task_status_counts([t for t, _ in items]),
        # Flat list (respects the status filter), each row annotated with its goal/step
        # relations: parent_task_id/parent_title for steps, is_goal + steps_done/steps_total
        # for goals.
        "tasks": [board.serialize_task(t, p, by_id, children) for t, p in items],
        # Nested goal→steps view (scope-filtered, ALL statuses so progress is truthful).
        "tree": await board.task_tree_view(scope),
    }


@router.get("/reminders")
async def api_reminders(status: str | None = None) -> dict:
    rows = await board.query_reminders(ALLOWED_CHAT_ID, status)
    recurring = await board.query_recurring(ALLOWED_CHAT_ID)
    return {
        "count": len(rows),
        "by_status": board.reminder_status_counts(rows),
        "reminders": [board.serialize_reminder(r) for r in rows],
        "recurring": [board.serialize_recurring(r) for r in recurring],
    }


@router.get("/overview")
async def api_overview() -> dict:
    return await board.overview(ALLOWED_CHAT_ID)
