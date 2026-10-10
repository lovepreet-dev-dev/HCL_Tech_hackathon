"""Workflow agent: routing, escalation decision and mock actions (bonus B3).

Routing comes from ``escalation_matrix.csv``; the escalation decision follows
the SOP escalation policy and the human-in-the-loop list in
``05_responsible_ai_policy.md``. Actions are structured payloads stored in the
application database (``copilot_actions`` table): nothing is sent to a real system.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from app.copilot.actions import ActionStore
from app.copilot.data_agent import DataResult, Entities, RetailData
from app.copilot.safety import SafetyResult
from app.copilot.schemas import Escalation
from app.copilot.triage import SEVERITY, TriageResult
from app.utils.logging import get_logger

logger = get_logger(__name__)

HITL = "05_responsible_ai_policy.md#human-in-the-loop"


@dataclass
class WorkflowResult:
    escalation: Escalation
    route: dict[str, Any] = field(default_factory=dict)
    approvals: list[str] = field(default_factory=list)
    citations: list[str] = field(default_factory=list)
    actions: list[dict[str, Any]] = field(default_factory=list)
    scope: str = "unspecified"


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]+", text.lower()) if len(w) > 3}


class WorkflowAgent:
    name = "Workflow agent"

    def __init__(self, data: RetailData, confidence_threshold: float, store: ActionStore | None = None) -> None:
        self.data = data
        self.threshold = confidence_threshold
        self.store = store

    def _route(self, triage: TriageResult, context: str) -> dict[str, Any]:
        rows = self.data.escalation[self.data.escalation.category == triage.category]
        if rows.empty:
            rows = self.data.escalation[self.data.escalation.category == "Other or Security"]
        ctx = _words(context)
        best, best_score = None, -1e9
        for row in rows.itertuples():
            score = -abs(SEVERITY[row.priority] - SEVERITY[triage.priority]) * 3
            trig = _words(row.trigger)
            score += len(trig & ctx) / max(len(trig), 1) * 2
            if score > best_score:
                best, best_score = row, score
        return best._asdict()

    def _scope(self, triage: TriageResult, e: Entities, data: DataResult, text: str) -> str:
        low = text.lower()
        if "store-wide" in triage.signals.get("priority_rule", ""):
            return f"all registers at {e.store_id or 'the store'}"
        if data.facts.get("stores_affected", 0) > 1 or re.search(r"\b(stores|across)\b", low):
            return "multiple stores"
        if e.register_id:
            return f"one register ({e.register_id})"
        if triage.category == "Returns and Refunds":
            return "one customer"
        if e.sku:
            return f"one SKU ({e.sku}) at {e.store_id or 'unknown store'}"
        return "unspecified"

    def run(
        self,
        text: str,
        triage: TriageResult,
        data: DataResult,
        safety: SafetyResult,
        entities: Entities,
    ) -> WorkflowResult:
        context = " ".join([text, *data.findings, data.likely_cause or ""])
        route = self._route(triage, context)
        approvals: list[str] = []
        citations = [route["policy_source"]]
        team = route["team"]

        if triage.priority == "P1":
            approvals.append("P1 needs an immediate human response")
        conflict = triage.signals.get("policy_conflict")
        if conflict:
            rules = ", ".join(f"{c} {p}" for c, p in conflict["rules"].items())
            approvals.append(f"policy rules point different ways ({rules}); a person confirms the priority")
            citations.append("01_retail_operations_sop.md#escalation-policy")
        if triage.confidence < self.threshold:
            approvals.append(f"confidence {triage.confidence:.2f} is below {self.threshold:.2f}")
            citations.append(HITL)
        if safety.refusal:
            approvals.append("restricted-data or prompt-injection request")
            team = "Security"
            if any(i in safety.restricted_items for i in ("customer card numbers", "credentials")):
                citations.append("02_pos_pricing_returns_policy.md#payment-security")
        if route["priority"] == triage.priority and str(route["human_approval_required"]).lower() == "yes":
            approvals.append(f"escalation matrix requires approval for '{route['trigger']}'")

        active = triage.priority != "P4"  # a policy question or report has nothing to approve
        if triage.category == "Returns and Refunds" and active:
            amount, receipt = entities.amount_inr, entities.receipt_present
            if amount is not None and (amount >= 5000 or (receipt is False and amount >= 1000)):
                approvals.append(f"refund of INR {amount:,.0f} needs manager approval; the copilot cannot approve refunds")
                citations.append(HITL)
            elif receipt is False:
                approvals.append("no-receipt return needs manager review")
        if data.facts.get("consecutive_short"):
            approvals.append("vendor claim review needs Supply Chain approval")
            citations.append(HITL)
            if "Supply Chain" not in team:
                team = f"{team} + Supply Chain"
        if triage.category == "Product Quality and Safety" and triage.priority in ("P1", "P2") and not triage.analytics:
            approvals.append("quality hold/quarantine needs Store Manager and Product Quality approval")

        scope = self._scope(triage, entities, data, text)
        required = bool(approvals)
        reason = "; ".join(approvals) if required else f"routine {triage.priority}: handled through {route['channel']}"
        summary = (
            f"{triage.category} | {triage.priority} | store {entities.store_id or 'not given'} | scope {scope} | "
            f"policy {route['policy_source']} | team {team}"
            + (f" | data: {data.likely_cause}" if data.likely_cause else "")
        )
        escalation = Escalation(
            required=required,
            team=team,
            channel=route["channel"],
            sla_minutes=int(route["sla_minutes"]),
            reason=reason,
            summary=summary,
        )
        result = WorkflowResult(
            escalation=escalation,
            route={k: route[k] for k in ("category", "trigger", "priority", "team", "channel", "sla_minutes", "human_approval_required", "policy_source")},
            approvals=approvals,
            citations=list(dict.fromkeys(citations)),
            scope=scope,
        )
        result.actions = self._actions(text, triage, data, safety, entities, escalation, scope)
        return result

    # ── Mock actions ───────────────────────────────────────────────────

    def _actions(
        self, text: str, triage: TriageResult, data: DataResult, safety: SafetyResult,
        entities: Entities, esc: Escalation, scope: str,
    ) -> list[dict[str, Any]]:
        if triage.analytics and triage.priority == "P4":
            return []
        ref = hashlib.sha1(f"{text}{datetime.now().isoformat()}".encode()).hexdigest()[:8].upper()
        actions: list[dict[str, Any]] = [{
            "type": "security_incident" if safety.refusal else "ticket",
            "id": f"TCK-{ref}",
            "queue": esc.channel,
            "category": triage.category,
            "priority": triage.priority,
            "store_id": entities.store_id,
            "sku": entities.sku,
            "register_id": entities.register_id,
            "scope": scope,
            "description": text,  # already masked by the safety agent
            "sla_minutes": esc.sla_minutes,
            "status": "draft",
        }]
        if esc.required:
            actions.append({
                "type": "notification",
                "priority": triage.priority,
                "category": triage.category,
                "store_id": entities.store_id,
                "to": esc.team,
                "channel": esc.channel,
                "message": f"[{triage.priority}] {esc.summary}. Reason: {esc.reason}.",
                "status": "awaiting human acknowledgement",
            })
        if data.facts.get("consecutive_short"):
            receipts = [r for r in data.tables.get("receipts", []) if r["received_qty"] < r["po_qty"]]
            vendor_id = receipts[0]["vendor_id"] if receipts else None
            actions.append({
                "type": "vendor_claim_draft",
                "priority": triage.priority,
                "category": triage.category,
                "id": f"VCL-{ref}",
                "vendor_id": vendor_id,
                "store_id": entities.store_id,
                "sku": entities.sku,
                "receipts": [{k: r[k] for k in ("receipt_id", "receipt_date", "po_qty", "received_qty")} for r in receipts],
                "shortfall_units": int(sum(r["po_qty"] - r["received_qty"] for r in receipts)),
                "status": "draft: needs Supply Chain approval before submission",
            })
        if triage.category == "Product Quality and Safety" and triage.priority in ("P1", "P2") and not triage.analytics:
            actions.append({
                "type": "quality_hold_request",
                "priority": triage.priority,
                "category": triage.category,
                "id": f"QH-{ref}",
                "skus": sorted({r["sku"] for r in data.tables.get("quality_complaints", []) if r.get("sku")}) or ([entities.sku] if entities.sku else []),
                "stores": sorted({r["store_id"] for r in data.tables.get("quality_complaints", [])}) or ([entities.store_id] if entities.store_id else []),
                "action": "remove from sale and hold for Product Quality review",
                "status": "pending Store Manager + Product Quality approval",
            })
        if triage.category == "Returns and Refunds" and esc.required:
            actions.append({
                "type": "refund_approval_request",
                "priority": triage.priority,
                "category": triage.category,
                "store_id": entities.store_id,
                "id": f"RFA-{ref}",
                "amount_inr": entities.amount_inr,
                "receipt_present": entities.receipt_present,
                "status": "pending Store Manager approval",
            })
        return actions

    def persist(self, actions: list[dict[str, Any]]) -> None:
        """Store the drafts. A database outage is logged, not raised: the answer and its
        drafts still reach the user, as they do when the LLM is down."""
        if self.store is None or not actions:
            return
        try:
            self.store.save(actions)
        except SQLAlchemyError as exc:
            logger.error("copilot_actions_persist_failed", error=str(exc), count=len(actions))
