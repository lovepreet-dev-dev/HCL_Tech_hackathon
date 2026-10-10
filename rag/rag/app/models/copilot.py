"""Retail Operations Copilot tables.

Drafted actions (tickets, notifications, vendor-claim drafts, quality holds,
refund approval requests) used to be appended to ``data/copilot_actions.jsonl``.
A file only works for one process on one machine; a table is shared by every
app instance and can be queried by store, type or priority.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, DateTime, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.database import Base


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

    def to_dict(self) -> dict[str, Any]:
        """The shape ``GET /api/v1/copilot/actions`` has always returned."""
        created = self.created_at
        if created.tzinfo is None:  # SQLite keeps no offset; the value was written in UTC
            created = created.replace(tzinfo=timezone.utc)
        return {"created_at": created.isoformat(timespec="seconds"), **self.payload}

    def __repr__(self) -> str:
        return f"<CopilotAction {self.type} {self.ref or self.id}>"
