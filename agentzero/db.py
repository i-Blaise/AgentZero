from __future__ import annotations
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from agentzero.config import MONGODB_URI, MONGODB_DB

_client: AsyncIOMotorClient | None = None
_db: AsyncIOMotorDatabase | None = None


def get_client() -> AsyncIOMotorClient:
    global _client
    if _client is None:
        _client = AsyncIOMotorClient(MONGODB_URI)
    return _client


def get_db() -> AsyncIOMotorDatabase:
    global _db
    if _db is None:
        _db = get_client()[MONGODB_DB]
    return _db


async def create_indexes() -> None:
    db = get_db()
    await db.projects.create_index([("name", 1)])
    await db.tasks.create_index([("project_id", 1)])
    await db.tasks.create_index([("status", 1)])
    await db.tasks.create_index([("snoozed_until", 1)])
    await db.events.create_index([("chat_id", 1), ("created_at", -1)])
    await db.chat_history.create_index([("chat_id", 1), ("created_at", -1)])
    await db.disambiguation.create_index([("chat_id", 1)], unique=True)
    await db.reminders.create_index([("status", 1), ("fire_at", 1)])
    await db.reminders.create_index([("chat_id", 1), ("status", 1)])
    await db.memory.create_index([("chat_id", 1)])
    await db.system_state.create_index([("chat_id", 1)], unique=True)


async def close() -> None:
    global _client, _db
    if _client:
        _client.close()
        _client = None
        _db = None
