"""Integration tests against a real PostgreSQL server.

Skipped unless ``TEST_DATABASE_URL`` points at an empty, disposable database, e.g.

    TEST_DATABASE_URL=postgresql+psycopg://rag:rag@localhost:5432/rag_test pytest tests/test_postgres.py

The tables are dropped at the end.
"""

from __future__ import annotations

import os
import threading

import pytest
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import sessionmaker

import app.models.database as database
from app.copilot.actions import ActionStore
from app.models.copilot import CopilotAction
from app.models.database import Base, normalize_database_url
from app.models.user import AuditLog, Role
from app.services.auth_service import create_user
from app.utils.audit import log_audit

URL = os.environ.get("TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL.startswith(("postgres://", "postgresql")), reason="TEST_DATABASE_URL not set")


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch):
    eng = create_engine(normalize_database_url(URL), pool_pre_ping=True)
    monkeypatch.setattr(database, "_engine", eng)  # init_db() uses this engine
    database._register_models()
    Base.metadata.drop_all(bind=eng)
    yield eng
    Base.metadata.drop_all(bind=eng)
    eng.dispose()


def test_concurrent_startups_create_the_schema_once(engine) -> None:
    errors: list[BaseException] = []

    def start() -> None:
        try:
            database.init_db()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=start) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert {"users", "chat_sessions", "audit_logs", "copilot_actions"} <= set(inspect(engine).get_table_names())


def test_actions_round_trip_through_jsonb(engine) -> None:
    database.init_db()
    store = ActionStore(sessionmaker(bind=engine))
    store.save([
        {"type": "ticket", "id": "TCK-PG000001", "priority": "P1", "store_id": "ST003", "status": "draft",
         "category": "POS and Payments", "scope": "all registers at ST003"},
        {"type": "notification", "priority": "P1", "store_id": "ST003", "status": "awaiting human acknowledgement"},
    ])
    rows = store.recent()
    assert rows[0]["type"] == "notification" and rows[1]["id"] == "TCK-PG000001"
    with sessionmaker(bind=engine)() as db:
        # The payload is JSONB, so it can be queried by key.
        hits = db.scalars(select(CopilotAction).where(CopilotAction.payload["scope"].as_string() =="all registers at ST003")).all()
        assert [h.ref for h in hits] == ["TCK-PG000001"]
        assert engine.dialect.name == "postgresql"


def test_users_and_long_audit_headers(engine) -> None:
    database.init_db()
    with sessionmaker(bind=engine)() as db:
        user = create_user(db=db, email="pg@example.com", name="PG", password="Pass1234!", role=Role.ADMIN)
        assert user.role is Role.ADMIN
        # PostgreSQL enforces VARCHAR(500); a client can send any User-Agent.
        log_audit(db, user_id=user.id, action="login", user_agent="Mozilla/5.0 " + "x" * 2000, ip_address="203.0.113.7")
        entry = db.scalars(select(AuditLog)).one()
        assert len(entry.user_agent) == 500
