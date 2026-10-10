"""Drafted copilot actions, decisions on them, and evaluation runs in the application database."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import StaticPool, create_engine, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from app.copilot.actions import ActionStore, DecisionNotAllowedError, DraftNotFoundError
from app.copilot.eval_runs import EvaluationRuns
from app.copilot.workflow import WorkflowAgent
from app.models.copilot import EvaluationRun
from app.models.database import Base, _register_models, normalize_database_url
from app.models.schemas import ChatRequest, FeedbackRequest
from app.models.user import AuditLog

TICKET = {
    "type": "ticket", "id": "TCK-1A2B3C4D", "queue": "inventory_priority_queue", "category": "Inventory and Replenishment",
    "priority": "P2", "store_id": "ST002", "sku": "SKU1001", "status": "draft",
}
NOTIFICATION = {"type": "notification", "priority": "P2", "store_id": "ST002", "to": "Store Manager", "status": "awaiting human acknowledgement"}
REFUND = {"type": "refund_approval_request", "id": "RFA-00000001", "priority": "P2", "store_id": "ST001",
          "amount_inr": 24999.0, "receipt_present": False, "status": "pending Store Manager approval"}


def _sessions(url: str = "sqlite://"):
    kwargs = {"connect_args": {"check_same_thread": False}}
    if url == "sqlite://":
        kwargs["poolclass"] = StaticPool
    engine = create_engine(url, **kwargs)
    _register_models()
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)


@pytest.fixture
def sessions():
    return _sessions()


@pytest.fixture
def store(sessions) -> ActionStore:
    return ActionStore(sessions)


# ── Drafts ──────────────────────────────────────────────────────────────────


def test_recent_returns_newest_first_in_the_old_api_shape(store: ActionStore) -> None:
    store.save([TICKET, NOTIFICATION])
    store.save([{**TICKET, "id": "TCK-NEWER000"}])
    rows = store.recent(limit=2)
    assert [r.get("id") for r in rows] == ["TCK-NEWER000", None]  # notifications carry no id
    assert rows[1]["to"] == "Store Manager"
    assert datetime.fromisoformat(rows[0]["created_at"]).utcoffset() is not None  # browsers get UTC, not a guess


def test_save_gives_each_draft_its_row_id(store: ActionStore) -> None:
    drafts = [dict(TICKET), dict(REFUND)]
    store.save(drafts)
    assert all(isinstance(d["action_id"], int) for d in drafts)
    assert [d["needs_decision"] for d in drafts] == [False, True]


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


# ── Decisions ───────────────────────────────────────────────────────────────


def test_approving_records_who_when_why_and_audits_it(store: ActionStore, sessions) -> None:
    drafts = [dict(REFUND)]
    store.save(drafts)
    out = store.decide(drafts[0]["action_id"], "approved", "Priya", role="Store Manager", note="Regular customer")
    assert out["status"] == "approved" and out["needs_decision"] is False
    assert out["decision"]["decided_by"] == "Priya" and out["decision"]["role"] == "Store Manager"
    assert out["id"] == "RFA-00000001" and out["amount_inr"] == 24999.0  # the draft itself is unchanged
    with sessions() as db:
        audit = db.scalars(select(AuditLog)).one()
    assert audit.action == "copilot_draft_approved" and audit.resource_id == str(drafts[0]["action_id"])


def test_a_draft_is_decided_once(store: ActionStore) -> None:
    drafts = [dict(REFUND)]
    store.save(drafts)
    store.decide(drafts[0]["action_id"], "rejected", "Priya", note="No proof of purchase")
    with pytest.raises(DecisionNotAllowedError, match="already rejected by Priya"):
        store.decide(drafts[0]["action_id"], "approved", "Arjun")


def test_only_drafts_that_wait_for_approval_can_be_decided(store: ActionStore) -> None:
    drafts = [dict(TICKET)]
    store.save(drafts)
    with pytest.raises(DecisionNotAllowedError, match="needs no approval"):
        store.decide(drafts[0]["action_id"], "approved", "Priya")
    with pytest.raises(DraftNotFoundError):
        store.decide(99999, "approved", "Priya")


@pytest.fixture
def client(store: ActionStore, monkeypatch: pytest.MonkeyPatch):
    import app.api.copilot as api
    from app.api.deps import get_current_user_optional
    from app.main import app

    monkeypatch.setattr(api, "get_action_store", lambda: store)
    app.dependency_overrides[get_current_user_optional] = lambda: None
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_decision_endpoints(client: TestClient, store: ActionStore) -> None:
    drafts = [dict(REFUND), dict(TICKET)]
    store.save(drafts)
    refund, ticket = drafts[0]["action_id"], drafts[1]["action_id"]
    base = "/api/v1/copilot/actions"
    assert client.post(f"{base}/{refund}/reject", json={"decided_by": "Priya"}).status_code == 422  # reason required
    res = client.post(f"{base}/{refund}/approve", json={"decided_by": "Priya", "role": "Store Manager"})
    assert res.status_code == 200 and res.json()["decision"]["decision"] == "approved"
    assert client.post(f"{base}/{refund}/approve", json={"decided_by": "Arjun"}).status_code == 409
    assert client.post(f"{base}/{ticket}/approve", json={"decided_by": "Priya"}).status_code == 409
    assert client.post(f"{base}/424242/approve", json={"decided_by": "Priya"}).status_code == 404
    listed = {a["action_id"]: a for a in client.get(base).json()}
    assert listed[refund]["status"] == "approved" and listed[ticket]["needs_decision"] is False


def test_with_login_on_a_decision_needs_a_signed_in_user(client: TestClient, store: ActionStore) -> None:
    from app.config.settings import get_settings

    drafts = [dict(REFUND)]
    store.save(drafts)
    settings = get_settings()
    original, settings.enable_auth = settings.enable_auth, True
    try:
        res = client.post(f"/api/v1/copilot/actions/{drafts[0]['action_id']}/approve", json={"decided_by": "Anyone"})
    finally:
        settings.enable_auth = original
    assert res.status_code == 401


# ── Evaluation runs ─────────────────────────────────────────────────────────


def test_every_instance_sees_the_same_run(tmp_path) -> None:
    # Two "workers": separate engines and session factories over one database file.
    url = f"sqlite:///{tmp_path / 'shared.db'}"
    worker_a, worker_b = EvaluationRuns(_sessions(url)), EvaluationRuns(sessionmaker(bind=create_engine(url)))
    assert worker_b.latest() == {"state": "idle"}

    run, created = worker_a.start(use_llm=False)
    assert created and run["state"] == "running"
    worker_a.progress(run["run_id"], 3, 11, "PUB-004")
    seen = worker_b.latest()
    assert (seen["state"], seen["done"], seen["total"], seen["current"]) == ("running", 3, 11, "PUB-004")

    again, created = worker_b.start(use_llm=True)  # one run at a time, across instances
    assert not created and again["run_id"] == run["run_id"]

    worker_a.finish(run["run_id"], mode="rules + retrieval")
    assert worker_b.latest()["state"] == "done" and worker_b.latest()["mode"] == "rules + retrieval"


def test_a_run_left_behind_by_a_stopped_instance_does_not_block_new_runs(sessions) -> None:
    runs = EvaluationRuns(sessions)
    old, _ = runs.start(use_llm=False)
    with sessions() as db, db.begin():
        db.get(EvaluationRun, old["run_id"]).heartbeat_at = datetime.now(timezone.utc) - timedelta(hours=1)
    new, created = runs.start(use_llm=False)
    assert created and new["run_id"] != old["run_id"]
    with sessions() as db:
        assert db.get(EvaluationRun, old["run_id"]).state == "error"


# ── Database URL and column limits ──────────────────────────────────────────


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
