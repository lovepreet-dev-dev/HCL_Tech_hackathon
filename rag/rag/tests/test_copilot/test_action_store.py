"""Drafted copilot actions stored in the application database."""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from app.copilot.actions import ActionStore
from app.copilot.workflow import WorkflowAgent
from app.models.copilot import CopilotAction
from app.models.database import Base, normalize_database_url
from app.models.schemas import ChatRequest, FeedbackRequest

TICKET = {
    "type": "ticket", "id": "TCK-1A2B3C4D", "queue": "inventory_priority_queue", "category": "Inventory and Replenishment",
    "priority": "P2", "store_id": "ST002", "sku": "SKU1001", "status": "draft",
}
NOTIFICATION = {"type": "notification", "priority": "P2", "store_id": "ST002", "to": "Store Manager", "status": "awaiting human acknowledgement"}


@pytest.fixture
def store() -> ActionStore:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine, tables=[CopilotAction.__table__])
    return ActionStore(sessionmaker(bind=engine))


def test_recent_returns_newest_first_in_the_old_api_shape(store: ActionStore) -> None:
    store.save([TICKET, NOTIFICATION])
    store.save([{**TICKET, "id": "TCK-NEWER000"}])
    rows = store.recent(limit=2)
    assert [r.get("id") for r in rows] == ["TCK-NEWER000", None]  # notifications carry no id
    assert rows[1]["to"] == "Store Manager"
    assert datetime.fromisoformat(rows[0]["created_at"]).utcoffset() is not None  # browsers get UTC, not a guess


def test_pandas_and_numpy_values_are_stored_as_plain_json(store: ActionStore) -> None:
    claim = {
        "type": "vendor_claim_draft", "id": "VCL-1", "status": "draft", "shortfall_units": np.int64(57),
        "receipts": [{"receipt_id": "RCPT-001", "receipt_date": pd.Timestamp("2026-10-09"), "po_qty": np.int64(50)}],
    }
    store.save([claim])
    (row,) = store.recent()
    assert row["shortfall_units"] == 57
    assert row["receipts"][0] == {"receipt_id": "RCPT-001", "receipt_date": "2026-10-09T00:00:00", "po_qty": 50}


def test_database_outage_does_not_fail_the_answer() -> None:
    class DownStore(ActionStore):
        def save(self, actions):  # noqa: ANN001, ANN201
            raise OperationalError("INSERT", {}, Exception("connection refused"))

    agent = WorkflowAgent(data=None, confidence_threshold=0.6, store=DownStore())  # type: ignore[arg-type]
    agent.persist([TICKET])  # logged, not raised


@pytest.mark.parametrize(("url", "expected"), [
    ("postgres://u:p@db:5432/rag", "postgresql+psycopg://u:p@db:5432/rag"),
    ("postgresql://u:p@db:5432/rag", "postgresql+psycopg://u:p@db:5432/rag"),
    ("postgresql+psycopg://u:p@db/rag", "postgresql+psycopg://u:p@db/rag"),
    ("sqlite:///./data/rag.db", "sqlite:///./data/rag.db"),
])
def test_postgres_urls_use_psycopg3(url: str, expected: str) -> None:
    assert normalize_database_url(url) == expected


def test_client_ids_longer_than_their_columns_are_rejected_at_the_edge() -> None:
    with pytest.raises(ValidationError):
        ChatRequest(message="hi", session_id="x" * 37)
    with pytest.raises(ValidationError):
        FeedbackRequest(query_id="x" * 37, rating=5)
    assert ChatRequest(message="hi", session_id="4f8c2a54-0a0e-4b8e-9d0a-6f1f8b7c2d11").session_id
