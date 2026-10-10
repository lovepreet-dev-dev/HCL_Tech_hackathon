"""Background evaluation runs, tracked in the application database.

The run executes in a thread of the instance that received ``POST /evaluate``, but
its progress is a row in ``evaluation_runs``. So ``GET /evaluate`` reports the same
run whichever instance answers it, and a unique index lets only one run be
"running" at a time across all instances.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.copilot import EvaluationRun
from app.models.database import _get_session_factory

# A run whose heartbeat has not moved for this long belongs to an instance that
# stopped (restart, crash). One case takes at most a few LLM calls of 60 s each.
STALE_AFTER = timedelta(minutes=15)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class EvaluationRuns:
    def __init__(self, session_factory: Callable[[], Session] | None = None) -> None:
        self._session_factory = session_factory

    def _session(self) -> Session:
        return (self._session_factory or _get_session_factory())()

    def start(self, use_llm: bool) -> tuple[dict[str, Any], bool]:
        """Create a running run. Returns (run, created); when one is already
        running anywhere, returns that run and ``False``."""
        with self._session() as db:
            with db.begin():
                db.execute(
                    update(EvaluationRun)
                    .where(EvaluationRun.state == "running", EvaluationRun.heartbeat_at < _now() - STALE_AFTER)
                    .values(state="error", error="The instance running this evaluation stopped.", finished_at=_now())
                )
            try:
                with db.begin():
                    run = EvaluationRun(state="running", use_llm=use_llm)
                    db.add(run)
                    db.flush()
                    return run.to_dict(), True
            except IntegrityError:  # the unique "one running" index: someone else started first
                running = db.scalars(select(EvaluationRun).where(EvaluationRun.state == "running")).first()
                return (running.to_dict() if running else {"state": "idle"}), False

    def progress(self, run_id: int, done: int, total: int, current: str) -> None:
        with self._session() as db, db.begin():
            db.execute(
                update(EvaluationRun).where(EvaluationRun.id == run_id)
                .values(done=done, total=total, current=current[:100], heartbeat_at=_now())
            )

    def finish(self, run_id: int, mode: str | None = None, error: str | None = None) -> None:
        with self._session() as db, db.begin():
            db.execute(
                update(EvaluationRun).where(EvaluationRun.id == run_id)
                .values(state="error" if error else "done", mode=mode, error=error,
                        finished_at=_now(), heartbeat_at=_now())
            )

    def latest(self) -> dict[str, Any]:
        """The current run, or the last one; ``{"state": "idle"}`` before the first."""
        with self._session() as db:
            run = db.scalars(select(EvaluationRun).order_by(EvaluationRun.id.desc()).limit(1)).first()
            return run.to_dict() if run else {"state": "idle"}


_runs = EvaluationRuns()


def get_evaluation_runs() -> EvaluationRuns:
    return _runs
