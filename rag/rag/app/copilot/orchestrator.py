"""Orchestrator: runs the cooperating agents in a fixed order.

    message
      → Safety agent     mask PII/card data, detect injection & restricted requests
      → Triage agent     category, priority, confidence (rules + similar tickets [+ LLM])
      → Data agent       pandas tools over the CSVs (root cause, incidents, vendors, ...)
      → Triage.refine    data facts can raise priority (e.g. high-demand SKU at zero)
      → Workflow agent   escalation matrix routing, approvals, mock ticket/claim payloads
      → Retrieval agent  policy sections the decision rests on + hybrid search
      → Composer         answer from evidence only, output re-masked

Each agent owns one job and passes a typed result to the next; the trace of
those results is returned with every answer.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from app.config.settings import LLMProvider, Settings, get_settings
from app.copilot.actions import get_action_store
from app.copilot.composer import Composer
from app.copilot.data_agent import DataAgent, DataResult, RetailData
from app.copilot.retrieval import KnowledgeBase, RetrievalAgent
from app.copilot.safety import SafetyAgent
from app.copilot.schemas import AgentStep, CopilotResponse
from app.copilot.triage import TriageAgent
from app.copilot.workflow import WorkflowAgent
from app.utils.logging import get_logger

logger = get_logger(__name__)
MODEL_FIELD = {
    LLMProvider.OPENAI: "openai_model",
    LLMProvider.ANTHROPIC: "anthropic_model",
    LLMProvider.OPENROUTER: "openrouter_model",
    LLMProvider.OLLAMA: "ollama_model",
}
T = TypeVar("T")


class Copilot:
    def __init__(self, settings: Settings | None = None, use_llm: bool | None = None) -> None:
        self.settings = settings or get_settings()
        root = Path(self.settings.copilot_dir)
        self.data = RetailData(root / "data", self.settings.copilot_anchor_ts)
        self.kb = KnowledgeBase(root / "knowledge_base", use_embeddings=self.settings.copilot_use_embeddings)

        llm = None
        self.llm_error: str | None = None
        # The copilot's own LLM settings: its model override and a short, fail-fast call budget.
        overrides: dict[str, object] = {
            "llm_max_tokens": self.settings.copilot_llm_max_tokens,
            "llm_timeout_seconds": self.settings.copilot_llm_timeout_seconds,
            "llm_max_retries": self.settings.copilot_llm_max_retries,
        }
        if self.settings.copilot_llm_model.strip():
            overrides[MODEL_FIELD[self.settings.llm_provider]] = self.settings.copilot_llm_model.strip()
        if self.settings.copilot_llm_reasoning_effort.strip():
            overrides["openrouter_reasoning_effort"] = self.settings.copilot_llm_reasoning_effort.strip()
        self.llm_settings = self.settings.model_copy(update=overrides)
        if self.settings.copilot_use_llm if use_llm is None else use_llm:
            from app.llm.factory import get_llm

            try:
                llm = get_llm(self.llm_settings)
            except Exception as exc:  # missing key, unknown provider: keep working on templates
                self.llm_error = str(exc)
                logger.warning("copilot_llm_unavailable", error=self.llm_error)

        self.safety = SafetyAgent()
        self.triage = TriageAgent(self.data, llm=llm if self.settings.copilot_llm_triage else None)
        self.data_agent = DataAgent(self.data)
        self.workflow = WorkflowAgent(
            self.data,
            self.settings.copilot_confidence_threshold,
            store=get_action_store(),
        )
        self.retrieval = RetrievalAgent(self.kb)
        self.composer = Composer(llm=llm, anchor_label=f"{self.data.anchor:%Y-%m-%d %H:%M} IST", retail=self.data)
        self.llm_enabled = llm is not None

    def ask(
        self,
        message: str,
        persona: str | None = None,
        store_id: str | None = None,
        persist: bool = True,
        on_step: Callable[[AgentStep], None] | None = None,
    ) -> CopilotResponse:
        """Run the agents in order. ``on_step`` receives each agent's result as it finishes."""
        trace: list[AgentStep] = []

        def record(item: AgentStep) -> None:
            trace.append(item)
            if on_step is not None:
                on_step(item)

        def step(agent: str, fn: Callable[[], T], summary: Callable[[T], str], details: Callable[[T], dict[str, Any]]) -> T:
            start = time.perf_counter()
            out = fn()
            record(AgentStep(
                agent=agent, summary=summary(out),
                duration_ms=round((time.perf_counter() - start) * 1000, 1), details=details(out),
            ))
            return out

        safety = step(
            self.safety.name, lambda: self.safety.run(message),
            lambda s: s.reason,
            lambda s: {"injection": s.injection, "restricted": s.restricted_items, "pii": s.pii_found,
                       "payment_data_request": s.payment_data_request, "masked_text": s.masked_text,
                       "refusal": s.refusal},
        )
        entities = self.data.extract_entities(safety.masked_text, store_hint=store_id)

        triage = step(
            self.triage.name, lambda: self.triage.run(safety, entities),
            lambda t: f"{t.category} / {t.priority} / confidence {t.confidence:.2f}",
            lambda t: {"reasons": t.reasons, "signals": t.signals, "analytics": t.analytics,
                       "entities": {"stores": entities.store_ids, "skus": entities.skus,
                                    "amount_inr": entities.amount_inr, "receipt_present": entities.receipt_present}},
        )

        if safety.refusal:
            data = DataResult()  # restricted requests get no data access at all
        else:
            data = step(
                self.data_agent.name,
                lambda: self.data_agent.run(safety.masked_text, triage.category, entities, triage.analytics),
                lambda d: f"tools {', '.join(d.tools) or 'none'}; files {', '.join(d.data_used) or 'none'}",
                lambda d: {"findings": d.findings, "facts": d.facts, "likely_cause": d.likely_cause},
            )
            before = triage.priority
            self.triage.refine(triage, data)
            if triage.priority != before:
                record(AgentStep(agent=self.triage.name, summary=f"priority {before} → {triage.priority} from data facts"))

        workflow = step(
            self.workflow.name,
            lambda: self.workflow.run(safety.masked_text, triage, data, safety, entities),
            lambda w: (f"escalate to {w.escalation.team}" if w.escalation.required else f"route to {w.escalation.team}")
            + f" via {w.escalation.channel}",
            lambda w: {"route": w.route, "approvals": w.approvals, "scope": w.scope},
        )

        wanted = list(triage.citations)
        if safety.pii_masked:
            wanted.append("05_responsible_ai_policy.md#pii-and-payment-data-masking")
        wanted += workflow.citations + data.citations
        policy_question = triage.priority == "P4" and not triage.analytics
        sections, search = step(
            self.retrieval.name,
            lambda: self.retrieval.run(safety.masked_text, triage.category, wanted, policy_question),
            lambda out: f"{len(out[0])} section(s) cited; top search hit {out[1][0].citation if out[1] else 'none'}",
            lambda out: {"cited": [x.citation for x in out[0]],
                         "search": [{"citation": x.citation, "score": x.score} for x in out[1]]},
        )

        composed = step(
            self.composer.name,
            lambda: self.composer.run(triage, sections, data, workflow, safety, entities),
            lambda c: "LLM wrote the answer" if c.llm_used else "template answer from cited sections",
            lambda c: {"llm_used": c.llm_used},
        )

        if persist:
            self.workflow.persist(workflow.actions)

        return CopilotResponse(
            category=triage.category,
            priority=triage.priority,
            confidence=triage.confidence,
            reasoning=triage.reasoning,
            answer=composed.answer,
            citations=[s.citation for s in sections],
            data_used=data.data_used,
            findings=data.findings,
            escalation=workflow.escalation,
            pii_masked=safety.pii_masked,
            safety_refusal=safety.refusal,
            follow_up_question=composed.follow_up,
            mode="analytics" if triage.analytics else "triage",
            actions=workflow.actions,
            trace=trace,
            llm_used=triage.llm_used or composed.llm_used,
            masked_text=safety.masked_text,
            pii_found=safety.pii_found,
            store_id=entities.store_id,
            ticket_id=workflow.actions[0]["id"] if workflow.actions else None,
            steps=composed.steps,
            highlights=composed.highlights,
            quick_replies=composed.quick_replies,
            visuals=data.visuals,
        )


_instance: Copilot | None = None
_instance_lock = threading.Lock()


def get_copilot() -> Copilot:
    """The process-wide copilot, built once.

    A lock, not ``lru_cache``: the startup warm-up thread and the first request can arrive
    together, and building twice at once loads the embedding model twice concurrently.
    """
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = Copilot()
    return _instance
