"""
MongoDB document schemas (TypedDicts for type-checking; Motor returns plain dicts).

projects:
  _id          ObjectId
  name         str
  scope        "work" | "personal"
  created_at   datetime
  updated_at   datetime

tasks:
  _id            ObjectId
  project_id     ObjectId  (ref → projects._id)
  title          str
  status         "open" | "done" | "snoozed" | "cancelled"
  due_date       datetime | None
  snoozed_until  datetime | None
  last_nudged_at datetime | None
  completed_at   datetime | None  — set when marked done (added 2026-07-14; older done
                                    rows lack it — recap falls back to updated_at)
  remind_at      datetime | None  — timed ping (merged from the old reminders collection,
                                    2026-07-14): when set, the scheduler pings the user at
                                    this moment and the follow-up loop nags until the task
                                    is closed. None → a plain task (opportunistic nudges
                                    only). All naive UTC like every other task datetime.
  reminded_at    datetime | None  — when the timed ping actually fired; set + still open
                                    means "fired, awaiting the user's confirmation"
  next_nudge_at  datetime | None  — when the follow-up loop may nag again
  nudge_count    int              — follow-up nags sent since the ping fired
  created_at     datetime
  updated_at     datetime

  Tasks with a remind_at that were migrated from the old `reminders` collection keep
  their ORIGINAL ObjectId (so old inline-button callbacks still resolve) and carry
  migrated_from: "reminders". The old collection is left in place as a backup.

events  (undo log):
  _id          ObjectId
  chat_id      int
  operation    str  — create_project | add_task | mark_done | update_task | snooze
  collection   str  — "projects" | "tasks"
  document_id  ObjectId
  prev_state   dict | None  — None for creates (undo = delete); prior doc for updates
  created_at   datetime

chat_history:
  _id        ObjectId
  chat_id    int
  role       "user" | "assistant"
  content    str
  created_at datetime

disambiguation:
  _id           ObjectId
  chat_id       int  (unique index)
  matches       list[dict]  — task docs that matched the query
  original_tool str
  original_args dict
  created_at    datetime

daily_focus  (one doc per local day — today's committed 3-4 task slate; see focus.py):
  _id           ObjectId
  chat_id       int
  date          str "YYYY-MM-DD"  (local date, TIMEZONE)
  task_ids      list[ObjectId]  — the slate; heartbeat nudges are fenced to these
  carryover_ids list[ObjectId]  — subset inherited from the previous unfinished slate
  overflow_ids  list[ObjectId]  — overdue/due-today tasks that did NOT make the slate
  created_at    datetime
"""

from typing import TypedDict, Optional, Any
from datetime import datetime


# Statuses from the LEGACY reminders collection that map to an OPEN task when migrated
# (2026-07-14 merge). "fired" is from an even older lifecycle (pre-awaiting_ack). Kept only
# for the migration and for reading unmigrated legacy data — new code queries tasks.
ACTIVE_REMINDER_STATUSES = ["pending", "awaiting_ack", "fired"]


class ProjectDoc(TypedDict, total=False):
    _id: Any
    name: str
    scope: str
    created_at: datetime
    updated_at: datetime


class TaskDoc(TypedDict, total=False):
    _id: Any
    project_id: Any
    # None → standalone task (or a "goal" once steps are filed under it); set → this is a
    # STEP under the referenced goal. The tree is only ever two levels deep.
    parent_task_id: Optional[Any]
    title: str
    status: str
    due_date: Optional[datetime]
    snoozed_until: Optional[datetime]
    last_nudged_at: Optional[datetime]
    completed_at: Optional[datetime]
    remind_at: Optional[datetime]
    reminded_at: Optional[datetime]
    next_nudge_at: Optional[datetime]
    nudge_count: int
    created_at: datetime
    updated_at: datetime


class EventDoc(TypedDict, total=False):
    _id: Any
    chat_id: int
    operation: str
    collection: str
    document_id: Any
    prev_state: Optional[dict]
    created_at: datetime


class ChatMessageDoc(TypedDict, total=False):
    _id: Any
    chat_id: int
    role: str
    content: str
    created_at: datetime


class DisambiguationDoc(TypedDict, total=False):
    _id: Any
    chat_id: int
    matches: list
    original_tool: str
    original_args: dict
    created_at: datetime
