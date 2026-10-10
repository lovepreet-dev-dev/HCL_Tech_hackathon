"""Retail Operations Copilot endpoints (Use Case 3).

All routes live under ``/api/v1/copilot``. The web console in ``app/web/copilot``
is a client of exactly these routes.
"""

from __future__ import annotations

import csv
import json
import queue
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.api.deps import get_current_user_optional
from app.config.settings import get_settings
from app.copilot.actions import DecisionNotAllowedError, DraftNotFoundError, get_action_store
from app.copilot.eval_runs import get_evaluation_runs
from app.copilot.orchestrator import Copilot, get_copilot
from app.copilot.schemas import CopilotRequest, CopilotResponse
from app.models.user import User
from app.utils.logging import get_logger

logger = get_logger(__name__)
router = APIRouter(prefix="/copilot", tags=["copilot"])


def _eval_dir() -> Path:
    return Path(get_settings().copilot_dir) / "evaluation"


@router.post("/ask", response_model=CopilotResponse, summary="Triage an issue or answer an analytics question")
def ask(request: CopilotRequest) -> CopilotResponse:
    """Run the full agent pipeline. PII is masked before anything is stored or returned."""
    return get_copilot().ask(request.message, persona=request.persona, store_id=request.store_id)


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


@router.post("/ask/stream", summary="Same as /ask, streamed: one `step` event per agent, then `result`")
def ask_stream(request: CopilotRequest) -> StreamingResponse:
    """Server-Sent Events, so a client can show each agent finishing as it happens.

    Events: ``step`` (an AgentStep), then ``result`` (the CopilotResponse) or ``error``.
    """
    events: queue.Queue[tuple[str, Any] | None] = queue.Queue()

    def run() -> None:
        try:
            response = get_copilot().ask(
                request.message, persona=request.persona, store_id=request.store_id,
                on_step=lambda s: events.put(("step", s.model_dump())),
            )
            events.put(("result", response.model_dump()))
        except Exception as exc:  # the stream reports it; the server keeps running
            logger.error("copilot_stream_failed", error=str(exc))
            events.put(("error", {"detail": "The copilot could not answer. Please try again."}))
        finally:
            events.put(None)

    def stream() -> Iterator[str]:
        threading.Thread(target=run, name="copilot-ask", daemon=True).start()
        while (item := events.get()) is not None:
            yield _sse(*item)

    return StreamingResponse(
        stream(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/status", summary="Which LLM is configured and what the knowledge base holds")
def status() -> dict[str, Any]:
    c = get_copilot()
    return {
        "llm_enabled": c.llm_enabled,
        "llm_provider": c.llm_settings.llm_provider.value,
        "llm_model": c.llm_settings.get_llm_model_name(),
        "llm_error": c.llm_error,
        "anchor": f"{c.data.anchor:%Y-%m-%d %H:%M} IST",
        "kb_sections": len(c.kb.sections),
        "dense_search": c.kb.dense_enabled,
        "csv_files": 9,
    }


@router.get("/stores", summary="Stores, for the console's store picker")
def stores() -> list[dict[str, Any]]:
    s = get_copilot().data.stores
    return [
        {"store_id": x.store_id, "name": x.store_name, "city": x.city, "region": x.region, "registers": int(x.pos_registers)}
        for x in s.sort_values("store_id").itertuples()
    ]


@router.get("/kb", summary="List the knowledge-base sections that can be cited")
def kb_sections() -> list[dict[str, str]]:
    return [
        {"citation": d.metadata["citation"], "section": d.metadata["section"], "title": d.metadata["title"]}
        for d in get_copilot().kb.sections
    ]


@router.get("/kb/section", summary="Full text of one cited section")
def kb_section(citation: str = Query(..., examples=["04_vendor_quality_policy.md#short-shipments"])) -> dict[str, str]:
    doc = get_copilot().kb.get(citation)
    if doc is None:
        raise HTTPException(status_code=404, detail=f"No knowledge-base section {citation!r}.")
    body = doc.page_content.split("\n", 1)[1] if "\n" in doc.page_content else doc.page_content
    return {
        "citation": citation,
        "section": doc.metadata["section"],
        "title": doc.metadata["title"],
        "source": doc.metadata["source"],
        "text": body.strip(),
    }


@router.get("/dashboard", summary="Open cases, priority, category and region trends")
def dashboard() -> dict[str, Any]:
    return get_copilot().data_agent.dashboard()


@router.get("/actions", summary="Mock tickets, notifications and claims created so far")
def actions(limit: int = Query(default=50, ge=1, le=500)) -> list[dict[str, Any]]:
    return get_action_store().recent(limit)


@router.get("/metrics", summary="Latest evaluation run on the public test cases")
def metrics() -> dict[str, Any]:
    path = _eval_dir() / "metrics.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="No evaluation yet. POST /api/v1/copilot/evaluate or run scripts/run_uc3_eval.py.")
    return json.loads(path.read_text(encoding="utf-8"))


@router.get("/predictions", summary="Rows of the latest predictions.csv")
def predictions() -> list[dict[str, str]]:
    path = _eval_dir() / "predictions.csv"
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


# ── Background evaluation job ──────────────────────────────────────────────
# The run's progress lives in the database (evaluation_runs), so any app instance
# can report it; the run itself executes in a thread of the instance that started it.


class EvaluateRequest(BaseModel):
    use_llm: bool = True


def _run_job(run_id: int, use_llm: bool) -> None:
    from app.copilot.evaluation import evaluate

    runs = get_evaluation_runs()
    try:
        shared = get_copilot()
        copilot = shared if shared.llm_enabled == use_llm else Copilot(use_llm=use_llm)
        doc = evaluate(
            copilot, _eval_dir() / "public_test_cases.csv",
            progress=lambda done, total, current: runs.progress(run_id, done, total, current),
        )
        runs.finish(run_id, mode=doc["mode"])
    except Exception as exc:  # report, never crash the server
        logger.error("copilot_evaluation_failed", error=str(exc), run_id=run_id)
        runs.finish(run_id, error=str(exc))


@router.post("/evaluate", summary="Start an evaluation run in the background")
def start_evaluation(request: EvaluateRequest) -> dict[str, Any]:
    run, created = get_evaluation_runs().start(request.use_llm)
    if created:
        threading.Thread(target=_run_job, args=(run["run_id"], request.use_llm), daemon=True).start()
    return run


@router.get("/evaluate", summary="Progress of the current or last evaluation run")
def evaluation_status() -> dict[str, Any]:
    return get_evaluation_runs().latest()


# ── Decisions on drafts (human in the loop) ────────────────────────────────


class DecisionRequest(BaseModel):
    decided_by: str = Field(..., min_length=1, max_length=100, description="Who decides, e.g. a name or persona")
    role: str | None = Field(default=None, max_length=50, examples=["Store Manager"])
    note: str | None = Field(default=None, max_length=1000, description="Why; required to reject")


def _decide(action_id: int, decision: str, body: DecisionRequest, user: User | None) -> dict[str, Any]:
    decided_by, user_id = body.decided_by.strip(), None
    if get_settings().enable_auth:
        # With login on, the decision belongs to the signed-in person, not to a typed name.
        if user is None:
            raise HTTPException(status_code=401, detail="Sign in to approve or reject drafts.")
        decided_by, user_id = user.name, user.id
    note = (body.note or "").strip() or None
    if decision == "rejected" and not note:
        raise HTTPException(status_code=422, detail="Give a reason to reject a draft.")
    try:
        return get_action_store().decide(action_id, decision, decided_by, body.role, note, user_id)
    except DraftNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except DecisionNotAllowedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/actions/{action_id}/approve", summary="Approve a draft that waits for a person's approval")
def approve_action(
    action_id: int, body: DecisionRequest, user: User | None = Depends(get_current_user_optional),
) -> dict[str, Any]:
    return _decide(action_id, "approved", body, user)


@router.post("/actions/{action_id}/reject", summary="Reject a draft that waits for a person's approval (reason required)")
def reject_action(
    action_id: int, body: DecisionRequest, user: User | None = Depends(get_current_user_optional),
) -> dict[str, Any]:
    return _decide(action_id, "rejected", body, user)
