"""
Shared fixtures for all test phases.

Uses mongomock-motor so tests run without a real MongoDB instance.
The mock DB is injected by patching agentzero.db._db before each test.
"""
import pytest
import pytest_asyncio
import mongomock_motor

import agentzero.db as db_module


@pytest.fixture(autouse=True)
def mock_db(monkeypatch):
    """Replace the Motor DB with an in-memory mongomock-motor instance."""
    client = mongomock_motor.AsyncMongoMockClient()
    test_db = client["agentzero_test"]
    monkeypatch.setattr(db_module, "_client", client)
    monkeypatch.setattr(db_module, "_db", test_db)
    return test_db
