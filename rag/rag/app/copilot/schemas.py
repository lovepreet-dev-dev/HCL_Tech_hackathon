"""Request/response models for the Retail Operations Copilot.

The response carries every field in ``usecase3/evaluation/predictions_schema.csv``
plus the agent trace, so the UI, the API and the evaluation runner share one shape.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

CATEGORIES = [
    "POS and Payments",
    "Inventory and Replenishment",
    "Pricing and Promotions",
    "Returns and Refunds",
    "Vendor and Supply Chain",
    "Product Quality and Safety",
    "Customer Service",
    "Other or Security",
]

Priority = Literal["P1", "P2", "P3", "P4"]


class CopilotRequest(BaseModel):
    """A free-text store issue or analytics question."""

    message: str = Field(..., min_length=1, max_length=4000)
    persona: str | None = Field(default=None, description="Cashier, Store Manager, ...")
    store_id: str | None = Field(default=None, description="Optional store context, e.g. ST002")


class Escalation(BaseModel):
    required: bool = False
    team: str | None = None
    channel: str | None = None
    sla_minutes: int | None = None
    reason: str = ""
    summary: str | None = None


class AgentStep(BaseModel):
    """One agent's contribution, shown in the UI so judges can follow the flow."""

    agent: str
    summary: str
    duration_ms: float = 0.0
    details: dict[str, Any] = Field(default_factory=dict)


class CopilotResponse(BaseModel):
    category: str
    priority: Priority
    confidence: float = Field(..., ge=0.0, le=1.0)
    reasoning: str
    answer: str
    citations: list[str] = Field(default_factory=list)
    data_used: list[str] = Field(default_factory=list)
    findings: list[str] = Field(default_factory=list)
    escalation: Escalation = Field(default_factory=Escalation)
    pii_masked: bool = False
    safety_refusal: bool = False
    follow_up_question: str | None = None
    mode: Literal["triage", "analytics"] = "triage"
    actions: list[dict[str, Any]] = Field(default_factory=list)
    trace: list[AgentStep] = Field(default_factory=list)
    llm_used: bool = False
    # For the console: what the agents saw and decided, as structured parts.
    masked_text: str = ""
    pii_found: list[str] = Field(default_factory=list)
    store_id: str | None = None
    ticket_id: str | None = None
    steps: list[str] = Field(default_factory=list, description="Checklist of next steps, built in code")
    highlights: dict[str, str] = Field(default_factory=dict, description="Cited section -> the sentence that applies")
    quick_replies: list[dict[str, str]] = Field(default_factory=list)
    visuals: list[dict[str, Any]] = Field(default_factory=list)
