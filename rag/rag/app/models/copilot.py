"""Retail Operations Copilot tables.

- ``copilot_actions``: drafted actions (tickets, notifications, vendor-claim
  drafts, quality holds, refund approval requests), one row per draft.
- ``copilot_action_decisions``: a person's approve/reject decision on a draft
  that needs one. At most one per draft, enforced by a unique key.
- ``evaluation_runs``: progress of background evaluation runs, so every app
  instance reports the same run.

Rows live in the application database, so every app instance sees the same ones.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.database import Base

# Drafts that wait for a person's approval (05_responsible_ai_policy.md#human-in-the-loop).
NEEDS_DECISION = {"refund_approval_request", "vendor_claim_draft", "quality_hold_request"}


def _utc(value: datetime | None) -> str | None:
    """ISO timestamp with offset. SQLite keeps none; values are written in UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat(timespec="seconds")


def _plain(value: Any) -> Any:
    """JSON fallback for the pandas and NumPy values the data agent puts in drafts."""
    if hasattr(value, "isoformat"):  # datetime, pandas Timestamp
        return value.isoformat()
    if hasattr(value, "item"):  # NumPy scalars such as int64
        return value.item()
    return str(value)


class CopilotAction(Base):
    """One drafted action. The full draft is kept in ``payload``; the columns
    copied out of it are the ones worth filtering on."""

    __tablename__ = "copilot_actions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # The draft's own reference (TCK-…, VCL-…, QH-…, RFA-…). Notifications have none.
    ref: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(200), nullable=False)
    priority: Mapped[str | None] = mapped_column(String(2), nullable=True, index=True)
    category: Mapped[str | None] = mapped_column(String(60), nullable=True)
    store_id: Mapped[str | None] = mapped_column(String(10), nullable=True, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
        index=True,
    )
    decision: Mapped[CopilotActionDecision | None] = relationship(
        back_populates="action", uselist=False, lazy="selectin"
    )

    @classmethod
    def from_draft(cls, draft: dict[str, Any], created_at: datetime | None = None) -> CopilotAction:
        payload = json.loads(json.dumps(draft, default=_plain))
        return cls(
            ref=payload.get("id"),
            type=payload["type"],
            status=payload.get("status", ""),
            priority=payload.get("priority"),
            category=payload.get("category"),
            store_id=payload.get("store_id"),
            payload=payload,
            created_at=created_at or datetime.now(timezone.utc),
        )

    @property
    def needs_decision(self) -> bool:
        return self.type in NEEDS_DECISION and self.decision is None

    def to_dict(self) -> dict[str, Any]:
        """The draft as the console shows it: the stored draft plus its current state.

        ``id`` stays the draft's own reference (TCK-…); ``action_id`` is the row id
        the approve/reject endpoints take.
        """
        return {
            "created_at": _utc(self.created_at),
            **self.payload,
            "action_id": self.id,
            "status": self.status,
            "needs_decision": self.needs_decision,
            "decision": self.decision.to_dict() if self.decision else None,
        }

    def __repr__(self) -> str:
        return f"<CopilotAction {self.type} {self.ref or self.id}>"


class CopilotActionDecision(Base):
    """Who approved or rejected a draft, when and why. Never updated or deleted."""

    __tablename__ = "copilot_action_decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # unique: two people deciding at once cannot both win, on any app instance.
    action_id: Mapped[int] = mapped_column(
        ForeignKey("copilot_actions.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    decision: Mapped[str] = mapped_column(String(10), nullable=False)  # approved | rejected
    decided_by: Mapped[str] = mapped_column(String(100), nullable=False)
    role: Mapped[str | None] = mapped_column(String(50), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )

    action: Mapped[CopilotAction] = relationship(back_populates="decision")

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "decided_by": self.decided_by,
            "role": self.role,
            "note": self.note,
            "decided_at": _utc(self.decided_at),
        }


class EvaluationRun(Base):
    """One background evaluation run and its progress."""

    __tablename__ = "evaluation_runs"
    __table_args__ = (
        # At most one running run, across every app instance.
        Index(
            "uq_evaluation_runs_one_running", "state", unique=True,
            postgresql_where=text("state = 'running'"), sqlite_where=text("state = 'running'"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    state: Mapped[str] = mapped_column(String(10), nullable=False)  # running | done | error
    use_llm: Mapped[bool] = mapped_column(Boolean, nullable=False)
    done: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    current: Mapped[str] = mapped_column(String(100), default="", nullable=False)
    mode: Mapped[str | None] = mapped_column(String(200), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    # Moved forward on every progress step; a running row whose heartbeat stops is stale.
    heartbeat_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def to_dict(self) -> dict[str, Any]:
        """The shape ``GET /api/v1/copilot/evaluate`` has always returned."""
        out: dict[str, Any] = {
            "state": self.state, "run_id": self.id, "use_llm": self.use_llm, "done": self.done,
            "total": self.total, "current": self.current, "started_at": _utc(self.started_at),
        }
        if self.finished_at is not None:
            out["finished_at"] = _utc(self.finished_at)
        if self.mode:
            out["mode"] = self.mode
        if self.error:
            out["error"] = self.error
        return out
