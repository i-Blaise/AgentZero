"""
Timed-ping tests for the merged tasks/reminders model (2026-07-14).

A "reminder" is a task with remind_at: add_task creates it (Inbox project when none is
named), the scheduler fires the ping and the follow-up loop nags, mark_done/cancel_task
close it. The scheduler's add_job is patched out so no real APScheduler runs during
tests; we only verify DB state and confirmation strings.
"""
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo
from bson import ObjectId

from agentzero.config import TIMEZONE
from agentzero.executor import execute_tool
from agentzero.llm import ToolCall

CHAT_ID = 999


def _tc(tool: str, **kwargs) -> ToolCall:
    return ToolCall(name=tool, args=kwargs)


def _future_local(minutes: int = 5) -> str:
    now = datetime.now(ZoneInfo(TIMEZONE))
    return (now + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S")


def _past_local(minutes: int = 5) -> str:
    now = datetime.now(ZoneInfo(TIMEZONE))
    return (now - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S")


def _naive_utc_now() -> datetime:
    return datetime.utcnow()


async def _seed_timed(db, title: str, *, fired: bool = False, next_nudge_at=None,
                      nudge_count: int = 0, status: str = "open", remind_delta_min: int = 0):
    """Insert a timed task the way add_task + a fire would leave it (naive-UTC datetimes)."""
    now = _naive_utc_now()
    remind_at = now + timedelta(minutes=remind_delta_min)
    doc = {
        "project_id": None,
        "parent_task_id": None,
        "title": title,
        "status": status,
        "due_date": datetime(now.year, now.month, now.day),
        "snoozed_until": None,
        "last_nudged_at": None,
        "remind_at": remind_at,
        "created_at": now,
        "updated_at": now,
    }
    if fired:
        doc["reminded_at"] = remind_at
        doc["next_nudge_at"] = next_nudge_at if next_nudge_at is not None else now
        doc["nudge_count"] = nudge_count
    return (await db.tasks.insert_one(doc)).inserted_id


# ---------------------------------------------------------------------------
# Creating timed tasks (add_task with remind_at)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_add_task_with_remind_at_creates_inbox_task(mock_db):
    with patch("agentzero.scheduler.schedule_reminder") as mock_sched:
        result = await execute_tool(
            CHAT_ID, _tc("add_task", title="take a break", remind_at=_future_local(2))
        )
    assert "ping you at" in result.lower()
    task = await mock_db.tasks.find_one({"title": "take a break"})
    assert task is not None
    assert task["status"] == "open"
    assert task["remind_at"] is not None
    assert task["due_date"] is not None  # a ping implies its day is the deadline
    inbox = await mock_db.projects.find_one({"name": "Inbox"})
    assert inbox is not None and task["project_id"] == inbox["_id"]
    mock_sched.assert_called_once()


@pytest.mark.asyncio
async def test_add_task_remind_at_into_named_project(mock_db):
    await execute_tool(CHAT_ID, _tc("create_project", name="Work", scope="work"))
    with patch("agentzero.scheduler.schedule_reminder"):
        await execute_tool(
            CHAT_ID,
            _tc("add_task", project_name="Work", title="deploy the fix", remind_at=_future_local(30)),
        )
    task = await mock_db.tasks.find_one({"title": "deploy the fix"})
    work = await mock_db.projects.find_one({"name": "Work"})
    assert task["project_id"] == work["_id"]
    assert task["remind_at"] is not None


@pytest.mark.asyncio
async def test_add_task_remind_at_past_time(mock_db):
    with patch("agentzero.scheduler.schedule_reminder"):
        result = await execute_tool(
            CHAT_ID, _tc("add_task", title="too late", remind_at=_past_local(10))
        )
    assert "past" in result.lower()
    assert await mock_db.tasks.find_one({"title": "too late"}) is None


@pytest.mark.asyncio
async def test_add_task_dedup_same_time(mock_db):
    """Two near-identical pings for essentially the same time must collapse to one —
    the classic double-fire when the brain hedges."""
    when = _future_local(3)
    with patch("agentzero.scheduler.schedule_reminder"):
        await execute_tool(CHAT_ID, _tc("add_task", title="call the bank", remind_at=when))
        result = await execute_tool(CHAT_ID, _tc("add_task", title="call the bank", remind_at=when))
    assert "not adding a duplicate" in result.lower()
    assert await mock_db.tasks.count_documents({"title": "call the bank"}) == 1


@pytest.mark.asyncio
async def test_add_task_same_text_different_time_allowed(mock_db):
    """A deliberate second ping for the SAME thing at a clearly different time is allowed."""
    with patch("agentzero.scheduler.schedule_reminder"):
        await execute_tool(CHAT_ID, _tc("add_task", title="drink water", remind_at=_future_local(5)))
        await execute_tool(CHAT_ID, _tc("add_task", title="drink water", remind_at=_future_local(120)))
    assert await mock_db.tasks.count_documents({"title": "drink water"}) == 2


@pytest.mark.asyncio
async def test_remind_at_attaches_to_existing_task(mock_db):
    """'Remind me at 4 to deploy the hotfix' when 'deploy the hotfix' is already tracked
    attaches the ping to the existing task instead of creating a twin."""
    await execute_tool(CHAT_ID, _tc("create_project", name="Work", scope="work"))
    await execute_tool(CHAT_ID, _tc("add_task", project_name="Work", title="deploy the hotfix"))
    with patch("agentzero.scheduler.schedule_reminder") as mock_sched:
        result = await execute_tool(
            CHAT_ID,
            _tc("add_task", project_name="Work", title="deploy the hotfix", remind_at=_future_local(60)),
        )
    assert "already on the list" in result.lower()
    assert await mock_db.tasks.count_documents({"title": "deploy the hotfix"}) == 1
    task = await mock_db.tasks.find_one({"title": "deploy the hotfix"})
    assert task["remind_at"] is not None
    mock_sched.assert_called_once()


@pytest.mark.asyncio
async def test_plain_add_task_without_project_goes_to_inbox(mock_db):
    result = await execute_tool(CHAT_ID, _tc("add_task", title="buy new charger"))
    assert "inbox" in result.lower()
    task = await mock_db.tasks.find_one({"title": "buy new charger"})
    inbox = await mock_db.projects.find_one({"name": "Inbox"})
    assert task["project_id"] == inbox["_id"]


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_reminders(mock_db):
    with patch("agentzero.scheduler.schedule_reminder"):
        await execute_tool(CHAT_ID, _tc("add_task", title="call mum", remind_at=_future_local(30)))
        await execute_tool(CHAT_ID, _tc("add_task", title="stretch", remind_at=_future_local(10)))
    result = await execute_tool(CHAT_ID, _tc("list_reminders"))
    assert "call mum" in result
    assert "stretch" in result


@pytest.mark.asyncio
async def test_list_reminders_empty(mock_db):
    result = await execute_tool(CHAT_ID, _tc("list_reminders"))
    assert "no active reminders" in result.lower()


@pytest.mark.asyncio
async def test_list_reminders_flags_fired(mock_db):
    await _seed_timed(mock_db, "email the client", fired=True)
    result = await execute_tool(CHAT_ID, _tc("list_reminders"))
    assert "awaiting your confirmation" in result.lower()


@pytest.mark.asyncio
async def test_list_reminders_excludes_plain_tasks(mock_db):
    await execute_tool(CHAT_ID, _tc("create_project", name="Work", scope="work"))
    await execute_tool(CHAT_ID, _tc("add_task", project_name="Work", title="refactor the parser"))
    with patch("agentzero.scheduler.schedule_reminder"):
        await execute_tool(CHAT_ID, _tc("add_task", title="stand up", remind_at=_future_local(10)))
    result = await execute_tool(CHAT_ID, _tc("list_reminders"))
    assert "stand up" in result
    assert "refactor the parser" not in result


# ---------------------------------------------------------------------------
# Firing + follow-up nags (scheduler)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fire_reminder_uses_personality(mock_db):
    """Firing renders the title through the LLM and marks the task fired-awaiting-ack."""
    from agentzero import scheduler

    tid = await _seed_timed(mock_db, "take a break")

    prov = MagicMock()
    prov.chat = AsyncMock(return_value="⏰ Your spine called — take a break.")
    with patch("agentzero.llm.get_provider", return_value=prov), \
         patch("agentzero.scheduler.send", new_callable=AsyncMock) as mock_send:
        await scheduler._fire_reminder(str(tid), CHAT_ID, "take a break")

    sent = mock_send.call_args[0][1]
    assert "take a break" in sent.lower()
    assert '— task: "take a break"' in sent  # verbatim title so replies close the RIGHT task
    doc = await mock_db.tasks.find_one({"_id": tid})
    # A fired ping awaits the user's confirmation — the task stays open, nag armed.
    assert doc["status"] == "open"
    assert doc["reminded_at"] is not None
    assert doc.get("next_nudge_at") is not None
    assert doc.get("last_nudged_at") is not None  # heartbeat suppression armed too


@pytest.mark.asyncio
async def test_fire_reminder_falls_back_on_llm_error(mock_db):
    """If the LLM call fails, the plain reminder still goes out."""
    from agentzero import scheduler

    tid = await _seed_timed(mock_db, "call the bank")

    prov = MagicMock()
    prov.chat = AsyncMock(side_effect=RuntimeError("api down"))
    with patch("agentzero.llm.get_provider", return_value=prov), \
         patch("agentzero.scheduler.send", new_callable=AsyncMock) as mock_send:
        await scheduler._fire_reminder(str(tid), CHAT_ID, "call the bank")

    assert mock_send.call_args[0][1] == '⏰ Reminder: call the bank\n— task: "call the bank"'
    doc = await mock_db.tasks.find_one({"_id": tid})
    assert doc["reminded_at"] is not None


@pytest.mark.asyncio
async def test_fire_reminder_does_not_resurrect_closed(mock_db):
    """A stale job firing for an already-done task must NOT ping or re-arm it."""
    from agentzero import scheduler

    tid = await _seed_timed(mock_db, "already done", status="done")
    with patch("agentzero.scheduler.send", new_callable=AsyncMock) as mock_send:
        await scheduler._fire_reminder(str(tid), CHAT_ID, "already done")
    mock_send.assert_not_called()
    doc = await mock_db.tasks.find_one({"_id": tid})
    assert doc["status"] == "done"  # unchanged
    assert doc.get("reminded_at") is None


@pytest.mark.asyncio
async def test_fire_reminder_skips_deleted_task(mock_db):
    """An /undo'd (deleted) task must not be pinged by its leftover job."""
    from agentzero import scheduler

    with patch("agentzero.scheduler.send", new_callable=AsyncMock) as mock_send:
        await scheduler._fire_reminder(str(ObjectId()), CHAT_ID, "ghost")
    mock_send.assert_not_called()


@pytest.mark.asyncio
async def test_followup_renudges_unacknowledged(mock_db):
    """The follow-up loop re-pings a fired-but-unconfirmed timed task when due."""
    from agentzero import scheduler

    past = _naive_utc_now() - timedelta(minutes=5)
    tid = await _seed_timed(mock_db, "email the client", fired=True, next_nudge_at=past)

    prov = MagicMock()
    prov.chat = AsyncMock(return_value="⏰ Still need: email the client.")
    with patch("agentzero.autonomy._in_quiet_hours", return_value=False), \
         patch("agentzero.llm.get_provider", return_value=prov), \
         patch("agentzero.scheduler.send", new_callable=AsyncMock) as mock_send:
        await scheduler._reminder_followup_job(CHAT_ID)

    mock_send.assert_called_once()
    doc = await mock_db.tasks.find_one({"_id": tid})
    assert doc["nudge_count"] == 1
    assert doc["status"] == "open"  # still awaiting until the user confirms


@pytest.mark.asyncio
async def test_followup_silent_in_quiet_hours(mock_db):
    from agentzero import scheduler

    past = _naive_utc_now() - timedelta(minutes=5)
    await _seed_timed(mock_db, "x", fired=True, next_nudge_at=past)
    with patch("agentzero.autonomy._in_quiet_hours", return_value=True), \
         patch("agentzero.scheduler.send", new_callable=AsyncMock) as mock_send:
        await scheduler._reminder_followup_job(CHAT_ID)
    mock_send.assert_not_called()


@pytest.mark.asyncio
async def test_followup_sends_one_at_a_time(mock_db):
    """A backlog trickles out: one nudge per wake, not a 5-8 message dump."""
    from agentzero import scheduler

    now = _naive_utc_now()
    for i, title in enumerate(("email client", "call bank", "submit invoice", "book flight")):
        # Different next_nudge_at so "most overdue" is deterministic.
        await _seed_timed(mock_db, title, fired=True, next_nudge_at=now - timedelta(minutes=10 + i))

    prov = MagicMock()
    prov.chat = AsyncMock(return_value="⏰ book flight — still hanging.")
    with patch("agentzero.autonomy._in_quiet_hours", return_value=False), \
         patch("agentzero.llm.get_provider", return_value=prov), \
         patch("agentzero.scheduler.send", new_callable=AsyncMock) as mock_send:
        await scheduler._reminder_followup_job(CHAT_ID)

    mock_send.assert_called_once()  # exactly one this cycle
    advanced = [d async for d in mock_db.tasks.find({"nudge_count": 1})]
    assert len(advanced) == 1
    # The most-overdue one (book flight, oldest next_nudge_at) is the one that fired.
    assert advanced[0]["title"] == "book flight"


@pytest.mark.asyncio
async def test_followup_ignores_closed_tasks(mock_db):
    from agentzero import scheduler

    past = _naive_utc_now() - timedelta(minutes=5)
    await _seed_timed(mock_db, "done thing", fired=True, next_nudge_at=past, status="done")
    await _seed_timed(mock_db, "dropped thing", fired=True, next_nudge_at=past, status="cancelled")
    with patch("agentzero.autonomy._in_quiet_hours", return_value=False), \
         patch("agentzero.scheduler.send", new_callable=AsyncMock) as mock_send:
        await scheduler._reminder_followup_job(CHAT_ID)
    mock_send.assert_not_called()


@pytest.mark.asyncio
async def test_load_pending_reschedules_only_unfired(mock_db):
    """Startup reload registers a job for each not-yet-fired ping; fired ones are the
    follow-up loop's business and past-due unfired ones fire immediately."""
    from agentzero import scheduler

    future_id = await _seed_timed(mock_db, "future ping", remind_delta_min=60)
    await _seed_timed(mock_db, "already fired", fired=True)
    overdue_id = await _seed_timed(mock_db, "came due while down", remind_delta_min=-30)

    prov = MagicMock()
    prov.chat = AsyncMock(return_value="⏰ came due while down.")
    with patch("agentzero.scheduler.schedule_reminder") as mock_sched, \
         patch("agentzero.llm.get_provider", return_value=prov), \
         patch("agentzero.scheduler.send", new_callable=AsyncMock) as mock_send:
        await scheduler.load_pending_reminders(CHAT_ID)

    assert mock_sched.call_count == 1
    assert mock_sched.call_args[0][0] == str(future_id)
    mock_send.assert_called_once()  # the overdue one fired right away
    doc = await mock_db.tasks.find_one({"_id": overdue_id})
    assert doc["reminded_at"] is not None


# ---------------------------------------------------------------------------
# Closing: done / cancel / snooze
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mark_done_closes_fired_ping_and_stops_nags(mock_db):
    tid = await _seed_timed(mock_db, "submit the proposal", fired=True)
    with patch("agentzero.scheduler.get_scheduler"):
        result = await execute_tool(CHAT_ID, _tc("mark_done", task_query="proposal"))
    assert "done" in result.lower()
    doc = await mock_db.tasks.find_one({"_id": tid})
    assert doc["status"] == "done"
    assert doc["completed_at"] is not None
    assert doc["next_nudge_at"] is None  # cleared, so the nag loop can't resurrect it


@pytest.mark.asyncio
async def test_mark_done_matches_partial_phrase(mock_db):
    """A partial phrase must match a long timed task and actually close it."""
    tid = await _seed_timed(
        mock_db, "Continue with the Gyacity website and add images sent by Brown on Snapchat",
        fired=True,
    )
    with patch("agentzero.scheduler.get_scheduler"):
        result = await execute_tool(CHAT_ID, _tc("mark_done", task_query="gyacity images from brown"))
    assert "done" in result.lower()
    doc = await mock_db.tasks.find_one({"_id": tid})
    assert doc["status"] == "done"
    assert doc["next_nudge_at"] is None


@pytest.mark.asyncio
async def test_cancel_task_kills_fired_ping(mock_db):
    """'remove that reminder' must work on a ping that's already firing/nagging."""
    tid = await _seed_timed(mock_db, "call the dentist", fired=True)
    with patch("agentzero.scheduler.get_scheduler"):
        result = await execute_tool(CHAT_ID, _tc("cancel_task", query="dentist"))
    assert "cancelled" in result.lower()
    doc = await mock_db.tasks.find_one({"_id": tid})
    assert doc["status"] == "cancelled"
    assert doc["next_nudge_at"] is None


@pytest.mark.asyncio
async def test_cancel_task_pending_ping(mock_db):
    with patch("agentzero.scheduler.schedule_reminder"):
        await execute_tool(CHAT_ID, _tc("add_task", title="dentist appointment", remind_at=_future_local(60)))
    with patch("agentzero.scheduler.get_scheduler"):
        result = await execute_tool(CHAT_ID, _tc("cancel_task", query="dentist"))
    assert "cancelled" in result.lower()
    doc = await mock_db.tasks.find_one({"title": "dentist appointment"})
    assert doc["status"] == "cancelled"


@pytest.mark.asyncio
async def test_cancel_task_works_on_plain_task(mock_db):
    await execute_tool(CHAT_ID, _tc("create_project", name="Work", scope="work"))
    await execute_tool(CHAT_ID, _tc("add_task", project_name="Work", title="write the tender doc"))
    with patch("agentzero.scheduler.get_scheduler"):
        result = await execute_tool(CHAT_ID, _tc("cancel_task", query="tender doc"))
    assert "cancelled" in result.lower()
    doc = await mock_db.tasks.find_one({"title": "write the tender doc"})
    assert doc["status"] == "cancelled"


@pytest.mark.asyncio
async def test_cancelled_task_hidden_from_status_and_recap(mock_db):
    await execute_tool(CHAT_ID, _tc("create_project", name="Work", scope="work"))
    await execute_tool(CHAT_ID, _tc("add_task", project_name="Work", title="write the tender doc"))
    with patch("agentzero.scheduler.get_scheduler"):
        await execute_tool(CHAT_ID, _tc("cancel_task", query="tender doc"))
    status = await execute_tool(CHAT_ID, _tc("get_status"))
    assert "tender doc" not in status
    recap = await execute_tool(CHAT_ID, _tc("get_recap", days=7))
    assert "tender doc" not in recap


@pytest.mark.asyncio
async def test_cancel_task_undo_restores(mock_db):
    from agentzero.executor import undo_last

    await execute_tool(CHAT_ID, _tc("create_project", name="Work", scope="work"))
    await execute_tool(CHAT_ID, _tc("add_task", project_name="Work", title="write the tender doc"))
    with patch("agentzero.scheduler.get_scheduler"):
        await execute_tool(CHAT_ID, _tc("cancel_task", query="tender doc"))
    result = await undo_last(CHAT_ID)
    assert "undid" in result.lower()
    doc = await mock_db.tasks.find_one({"title": "write the tender doc"})
    assert doc["status"] == "open"


@pytest.mark.asyncio
async def test_snooze_reminder_pushes_next_nudge(mock_db):
    """'remind me later' on a fired ping pushes its next nudge out."""
    now = _naive_utc_now()
    tid = await _seed_timed(mock_db, "call the plumber", fired=True, nudge_count=1)

    result = await execute_tool(CHAT_ID, _tc("snooze_reminder", query="plumber", minutes=90))
    assert "plumber" in result.lower()
    doc = await mock_db.tasks.find_one({"_id": tid})
    assert doc["next_nudge_at"] > now + timedelta(minutes=80)


@pytest.mark.asyncio
async def test_snooze_reminder_pending_moves_fire_time(mock_db):
    """Snoozing a not-yet-fired ping moves when it first fires (and reschedules the job)."""
    now = _naive_utc_now()
    with patch("agentzero.scheduler.schedule_reminder"):
        await execute_tool(CHAT_ID, _tc("add_task", title="start the laundry", remind_at=_future_local(10)))
    with patch("agentzero.scheduler.schedule_reminder") as mock_sched:
        await execute_tool(CHAT_ID, _tc("snooze_reminder", query="laundry", minutes=120))
    doc = await mock_db.tasks.find_one({"title": "start the laundry"})
    assert doc["remind_at"] > now + timedelta(minutes=110)
    mock_sched.assert_called_once()


@pytest.mark.asyncio
async def test_snooze_reminder_all_when_no_query(mock_db):
    now = _naive_utc_now()
    for title in ("thing a", "thing b"):
        await _seed_timed(mock_db, title, fired=True)
    result = await execute_tool(CHAT_ID, _tc("snooze_reminder", minutes=30))
    assert "all" in result.lower()
    async for doc in mock_db.tasks.find({}):
        assert doc["next_nudge_at"] > now + timedelta(minutes=20)


@pytest.mark.asyncio
async def test_set_reminder_cadence_persists_and_clamps(mock_db):
    """'space them apart' stores a clamped per-chat cadence the loop reads."""
    result = await execute_tool(CHAT_ID, _tc("set_reminder_cadence", minutes=180))
    assert "every" in result.lower()
    state = await mock_db.system_state.find_one({"chat_id": CHAT_ID})
    assert state["nudge_interval_minutes"] == 180

    # below the floor → clamped up, not stored as-is
    await execute_tool(CHAT_ID, _tc("set_reminder_cadence", minutes=1))
    state = await mock_db.system_state.find_one({"chat_id": CHAT_ID})
    assert state["nudge_interval_minutes"] >= 30


# ---------------------------------------------------------------------------
# Interplay with focus / heartbeat
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_timed_tasks_skip_heartbeat_and_focus_slate(mock_db):
    """A timed task has a guaranteed ping — it must not take a focus seat or be
    heartbeat-nudged on top."""
    from agentzero.autonomy import gather_candidates
    from agentzero.focus import ensure_today_focus

    with patch("agentzero.scheduler.schedule_reminder"):
        await execute_tool(CHAT_ID, _tc("add_task", title="take out the chicken", remind_at=_future_local(60)))

    c = await gather_candidates(CHAT_ID)
    titles = [e[0]["title"] for e in c["overdue"] + c["due_soon"] + c["stalled"]]
    assert "take out the chicken" not in titles

    doc = await ensure_today_focus(CHAT_ID, allow_llm=False)
    task = await mock_db.tasks.find_one({"title": "take out the chicken"})
    assert task["_id"] not in doc["task_ids"]


# ---------------------------------------------------------------------------
# Identical-twin duplicates (predate the dedup guard, e.g. migrated legacy data)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mark_done_closes_all_identical_twins(mock_db):
    """Two copies with the SAME title: 'be more specific' would be unanswerable, so
    closing by phrase closes every copy."""
    for _ in range(2):
        await _seed_timed(mock_db, "Log your time on Planorama for ID", fired=True)
    with patch("agentzero.scheduler.get_scheduler"):
        result = await execute_tool(CHAT_ID, _tc("mark_done", task_query="log your time on planorama"))
    assert "every copy" in result.lower()
    assert await mock_db.tasks.count_documents({"status": "done"}) == 2
    assert await mock_db.tasks.count_documents({"status": "open"}) == 0


@pytest.mark.asyncio
async def test_cancel_task_cancels_all_identical_twins(mock_db):
    for _ in range(2):
        await _seed_timed(mock_db, "Log your time on Planorama for ID", fired=True)
    with patch("agentzero.scheduler.get_scheduler"):
        result = await execute_tool(CHAT_ID, _tc("cancel_task", query="log your time on planorama"))
    assert "every copy" in result.lower()
    assert await mock_db.tasks.count_documents({"status": "cancelled"}) == 2


@pytest.mark.asyncio
async def test_mark_done_distinct_titles_still_ask(mock_db):
    """DIFFERENT titles that both match stay an ambiguity prompt — only identical
    twins auto-close together."""
    await _seed_timed(mock_db, "Prepare for the class session for Jade", fired=True)
    await _seed_timed(mock_db, "Prepare for the MBA class session for Jade on AI", fired=True)
    result = await execute_tool(CHAT_ID, _tc("mark_done", task_query="class session for jade"))
    assert "be more specific" in result.lower()
    assert await mock_db.tasks.count_documents({"status": "done"}) == 0


# ---------------------------------------------------------------------------
# Verbatim-title markers + recurring-reminder heads-up (2026-07-17 fixes)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_followup_nag_carries_verbatim_title(mock_db):
    """Follow-up nags append the exact title so replying 'done' resolves precisely."""
    from agentzero import scheduler

    past = _naive_utc_now() - timedelta(minutes=5)
    await _seed_timed(mock_db, "Prepare for the MBA class session for Jade", fired=True,
                      next_nudge_at=past)
    prov = MagicMock()
    prov.chat = AsyncMock(return_value="⏰ That MBA class won't prep itself.")
    with patch("agentzero.autonomy._in_quiet_hours", return_value=False), \
         patch("agentzero.llm.get_provider", return_value=prov), \
         patch("agentzero.scheduler.send", new_callable=AsyncMock) as mock_send:
        await scheduler._reminder_followup_job(CHAT_ID)
    sent = mock_send.call_args[0][1]
    assert '— task: "Prepare for the MBA class session for Jade"' in sent


@pytest.mark.asyncio
async def test_recurring_fire_carries_verbatim_marker(mock_db):
    from agentzero import scheduler

    prov = MagicMock()
    prov.chat = AsyncMock(return_value="⏰ GHIPPS won't deploy itself.")
    with patch("agentzero.llm.get_provider", return_value=prov), \
         patch("agentzero.scheduler.send", new_callable=AsyncMock) as mock_send:
        await scheduler._fire_recurring(CHAT_ID, "Deploy GHIPPS")
    assert '— recurring reminder: "Deploy GHIPPS"' in mock_send.call_args[0][1]


@pytest.mark.asyncio
async def test_close_task_notes_matching_recurring_reminder(mock_db):
    """Closing a task whose title matches an ACTIVE recurring schedule says so —
    otherwise the schedule re-pings later and looks like the close didn't stick."""
    await mock_db.recurring_reminders.insert_one(
        {"chat_id": CHAT_ID, "text": "Deploy GHIPPS", "hour": 10, "minute": 0,
         "day_of_week": "mon,tue,wed", "active": True}
    )
    await execute_tool(CHAT_ID, _tc("create_project", name="Work", scope="work"))
    await execute_tool(CHAT_ID, _tc("add_task", project_name="Work", title="Deploy GHIPPS"))
    with patch("agentzero.scheduler.get_scheduler"):
        result = await execute_tool(CHAT_ID, _tc("mark_done", task_query="deploy ghipps"))
    assert "done" in result.lower()
    assert "recurring" in result.lower()
    task = await mock_db.tasks.find_one({"title": "Deploy GHIPPS"})
    assert task["status"] == "done"
    rec = await mock_db.recurring_reminders.find_one({"text": "Deploy GHIPPS"})
    assert rec["active"] is True  # heads-up only — never auto-cancelled


@pytest.mark.asyncio
async def test_close_task_no_recurring_note_when_unrelated(mock_db):
    await mock_db.recurring_reminders.insert_one(
        {"chat_id": CHAT_ID, "text": "Practice DSA on LeetCode", "hour": 9, "minute": 0,
         "day_of_week": "mon-fri", "active": True}
    )
    await execute_tool(CHAT_ID, _tc("create_project", name="Work", scope="work"))
    await execute_tool(CHAT_ID, _tc("add_task", project_name="Work", title="Send the invoice"))
    with patch("agentzero.scheduler.get_scheduler"):
        result = await execute_tool(CHAT_ID, _tc("mark_done", task_query="send the invoice"))
    assert "recurring" not in result.lower()
