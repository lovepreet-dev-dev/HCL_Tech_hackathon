"""Where the workflow agent's drafted actions are kept: the application database.

SQLite by default, PostgreSQL in production (``DATABASE_URL``). Every app instance
reads and writes the same rows, so a draft made on one instance is listed by
``GET /api/v1/copilot/actions`` on any other.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.copilot import CopilotAction
from app.models.database import _get_session_factory


class ActionStore:
    def __init__(self, session_factory: Callable[[], Session] | None = None) -> None:
        # Resolved on first use, so building the copilot never opens a connection.
        self._session_factory = session_factory

    def _session(self) -> Session:
        return (self._session_factory or _get_session_factory())()

    def save(self, actions: list[dict[str, Any]]) -> None:
        """Store one request's drafts in a single transaction."""
        if not actions:
            return
        now = datetime.now(timezone.utc)
        with self._session() as db, db.begin():
            db.add_all([CopilotAction.from_draft(a, created_at=now) for a in actions])

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        """Newest drafts first."""
        with self._session() as db:
            rows = db.scalars(select(CopilotAction).order_by(CopilotAction.id.desc()).limit(limit))
            return [row.to_dict() for row in rows]


_store = ActionStore()


def get_action_store() -> ActionStore:
    return _store
