"""Where the workflow agent's drafted actions are kept: the application database.

SQLite by default, PostgreSQL in production (``DATABASE_URL``). Every app instance
reads and writes the same rows, so a draft made on one instance is listed by
``GET /api/v1/copilot/actions`` on any other, and a decision recorded on one is
seen by all.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.copilot import NEEDS_DECISION, CopilotAction, CopilotActionDecision
from app.models.database import _get_session_factory
from app.models.user import AuditLog

Decision = Literal["approved", "rejected"]


class DraftNotFoundError(LookupError):
    pass


class DecisionNotAllowedError(ValueError):
    """The draft needs no decision, or already has one."""


class ActionStore:
    def __init__(self, session_factory: Callable[[], Session] | None = None) -> None:
        # Resolved on first use, so building the copilot never opens a connection.
        self._session_factory = session_factory

    def _session(self) -> Session:
        return (self._session_factory or _get_session_factory())()

    def save(self, actions: list[dict[str, Any]]) -> None:
        """Store one request's drafts in a single transaction.

        Each dict gets its row id as ``action_id`` (and ``needs_decision``), so the
        answer that carries these drafts can offer approve/reject straight away.
        """
        if not actions:
            return
        now = datetime.now(timezone.utc)
        rows = [CopilotAction.from_draft(a, created_at=now) for a in actions]
        with self._session() as db, db.begin():
            db.add_all(rows)
            db.flush()  # assigns the ids; read them before the commit expires the rows
            ids = [row.id for row in rows]
        for action, row_id in zip(actions, ids, strict=True):
            action["action_id"] = row_id
            action["needs_decision"] = action["type"] in NEEDS_DECISION

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        """Newest drafts first."""
        with self._session() as db:
            rows = db.scalars(select(CopilotAction).order_by(CopilotAction.id.desc()).limit(limit))
            return [row.to_dict() for row in rows]

    def decide(
        self,
        action_id: int,
        decision: Decision,
        decided_by: str,
        role: str | None = None,
        note: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """Record a person's decision on a draft that waits for one.

        The decision, the draft's new status and an audit-log entry are written in
        one transaction. The unique key on ``copilot_action_decisions.action_id``
        makes a second decision fail even when two arrive at the same moment.
        """
        with self._session() as db:
            try:
                with db.begin():
                    row = db.get(CopilotAction, action_id)
                    if row is None:
                        raise DraftNotFoundError(f"No draft with action_id {action_id}.")
                    if row.type not in NEEDS_DECISION:
                        raise DecisionNotAllowedError(f"A {row.type.replace('_', ' ')} needs no approval.")
                    if row.decision is not None:
                        raise DecisionNotAllowedError(f"This draft was already {row.decision.decision} by {row.decision.decided_by}.")
                    db.add(CopilotActionDecision(
                        action_id=row.id, decision=decision, decided_by=decided_by,
                        role=role, note=note, user_id=user_id,
                    ))
                    row.status = decision
                    db.add(AuditLog(
                        user_id=user_id,
                        action=f"copilot_draft_{decision}",
                        resource_type="copilot_action",
                        resource_id=str(row.id),
                        details=f"{row.type} {row.ref or ''} {decision} by {decided_by}"
                                + (f" ({role})" if role else "") + (f": {note}" if note else ""),
                    ))
            except IntegrityError as exc:  # another decision won the race
                raise DecisionNotAllowedError("This draft was already decided by someone else.") from exc
            db.expire_all()
            return db.get(CopilotAction, action_id).to_dict()


_store = ActionStore()


def get_action_store() -> ActionStore:
    return _store
