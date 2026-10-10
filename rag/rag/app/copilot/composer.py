"""Composer: writes the user-facing answer from evidence only.

Inputs are the retrieved policy sections, the data agent's findings and the
workflow decision. The deterministic composer quotes the policy's own
"Recommended steps"; the optional LLM composer may reword, but its citations
are checked against the retrieved list and its output is re-masked.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.copilot.data_agent import DataResult, Entities, RetailData
from app.copilot.retrieval import RetrievedSection
from app.copilot.safety import SafetyResult, mask_pii
from app.copilot.triage import TriageResult
from app.copilot.workflow import WorkflowResult
from app.utils.logging import get_logger

logger = get_logger(__name__)


def policy_steps(sections: list[RetrievedSection], limit: int = 5) -> list[str]:
    """Bullet points under 'Recommended steps' in the cited sections."""
    steps: list[str] = []
    for s in sections:
        if "recommended steps" not in s.text.lower():
            continue
        after = s.text.lower().split("recommended steps", 1)[1]
        start = len(s.text) - len(after)
        for line in s.text[start:].splitlines():
            if line.strip().startswith("- "):
                steps.append(line.strip()[2:].rstrip("."))
    return steps[:limit]


_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
_RULE_WORDS = re.compile(r"\b(P[1-4]|must|required|requires|approval|escalate|classify|recommend|do not|never)\b", re.I)


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def policy_quotes(
    sections: list[RetrievedSection], query: str, priority: str = "", n: int = 2
) -> list[tuple[str, str]]:
    """The most relevant rule sentence from each of the first ``n`` cited sections.

    Sentences score on word overlap with the issue and triage reason, with a bonus
    for stating the priority that was assigned.
    """
    q = {w for w in re.findall(r"[a-z]+", query.lower()) if len(w) > 3}
    quotes = []
    for s in sections[:n]:
        body = [ln for ln in s.text.splitlines()[1:] if ln.strip() and not ln.strip().startswith(("-", "#", "Recommended"))]
        best, best_score = None, 0.0
        for sent in _SENTENCE_SPLIT.split(" ".join(body)):
            words = set(re.findall(r"[a-z]+", sent.lower()))
            score = len(q & words) + (1.5 if _RULE_WORDS.search(sent) else 0)
            if priority and priority in sent:
                score += 3
            # Skip list lead-ins ("check in this order:" / "...: 1."), not rules ending in "P1."
            fragment = sent.rstrip().endswith(":") or re.search(r"(?:^|\s)\d+\.$", sent.strip())
            if score > best_score and len(sent) > 40 and not fragment:
                best, best_score = sent.strip(), score
        if best:
            quotes.append((best, s.citation))
    return quotes


FIELD_PHRASES = {
    "store": "the store ID",
    "register": "the register ID",
    "error": "the error code shown",
    "time": "the time it started",
    "product": "the SKU or product name",
    "sku": "the SKU",
    "prices": "the shelf and POS prices",
    "amount": "the refund amount",
    "receipt": "whether the customer has the receipt",
    "po": "the PO or receipt number",
    "batch": "the SKU or batch",
    "stores": "the affected stores",
    "hurt": "whether anyone was hurt",
}


def missing_fields(triage: TriageResult, e: Entities, safety: SafetyResult, data: DataResult) -> list[str]:
    """Keys of the SOP's minimum triage fields that the message did not give."""
    if safety.refusal or triage.analytics:
        return []
    c, missing = triage.category, []
    if c == "POS and Payments":
        if not e.store_id:
            missing.append("store")
        if triage.priority == "P1":
            return missing
        if not e.register_id:
            missing.append("register")
        if not re.search(r"error", safety.masked_text, re.I):
            missing.append("error")
        missing.append("time")
    elif c == "Inventory and Replenishment":
        missing += [k for k, ok in (("store", e.store_id), ("product", e.sku)) if not ok]
    elif c == "Pricing and Promotions":
        missing += [k for k, ok in (("store", e.store_id), ("sku", e.sku), ("prices", len(e.amounts) >= 2)) if not ok]
    elif c == "Returns and Refunds":
        missing += [k for k, ok in (("amount", e.amount_inr is not None), ("receipt", e.receipt_present is not None)) if not ok]
    elif c == "Vendor and Supply Chain":
        missing += [k for k, ok in (("store", e.store_id), ("sku", e.sku)) if not ok] + ["po"]
    elif c == "Product Quality and Safety":
        if not e.sku and not data.facts.get("quality_complaints"):
            missing.append("batch")
        if not e.store_id and not data.facts.get("stores_affected"):
            missing.append("stores")
        if triage.priority != "P1":
            missing.append("hurt")
    return missing


def follow_up_question(triage: TriageResult, e: Entities, safety: SafetyResult, data: DataResult) -> str | None:
    """Ask for the minimum triage fields (SOP) that are still missing."""
    if safety.refusal or triage.analytics:
        return None
    if triage.category == "POS and Payments" and triage.priority == "P1":
        return ("Which register IDs and payment types are affected, and what time did it start?" if e.store_id
                else "Which store is affected, and which payment types are failing?")
    missing = missing_fields(triage, e, safety, data)
    if not missing:
        if triage.category == "Other or Security" and triage.confidence < 0.6:
            return "Can you describe the issue with the store, product or register involved?"
        return None
    return f"Could you share {_join([FIELD_PHRASES[k] for k in missing])}?"


def quick_replies(
    triage: TriageResult, e: Entities, safety: SafetyResult, data: DataResult, retail: RetailData | None,
) -> list[dict[str, str]]:
    """One-tap answers to the follow-up question. ``store_id`` sets the store; ``text`` is appended."""
    missing = missing_fields(triage, e, safety, data)
    replies: list[dict[str, str]] = []
    if "store" in missing and retail is not None:
        if e.sku:
            inv = retail.inventory[(retail.inventory.sku == e.sku) & retail.inventory.store_id.str.startswith("ST")]
            for x in inv.sort_values("on_hand").head(4).itertuples():
                replies.append({"label": x.store_id, "hint": f"{int(x.on_hand)} on hand", "store_id": x.store_id})
        else:
            open_cases = retail.cases[retail.cases.status.isin({"Open", "In Progress"})]
            for store_id, n in open_cases.store_id.value_counts().head(4).items():
                replies.append({"label": store_id, "hint": f"{int(n)} open cases", "store_id": store_id})
    if "register" in missing:
        replies += [{"label": "One register", "text": "Only one register is affected."},
                    {"label": "All registers", "text": "All registers are affected."}]
    if "receipt" in missing:
        replies += [{"label": "Has the receipt", "text": "The customer has the receipt."},
                    {"label": "No receipt", "text": "The customer has no receipt."}]
    if "hurt" in missing:
        replies += [{"label": "No one was hurt", "text": "No one was hurt."},
                    {"label": "Someone was hurt", "text": "A customer was hurt."}]
    if triage.category == "Other or Security" and triage.confidence < 0.6 and not safety.refusal:
        replies += [{"label": "Card reader problem", "text": "A card reader is not working."},
                    {"label": "Item out of stock", "text": "An item is out of stock."},
                    {"label": "Return or refund", "text": "A customer wants to return an item."}]
    return replies[:6]


@dataclass
class Composition:
    answer: str
    follow_up: str | None
    llm_used: bool
    steps: list[str] = field(default_factory=list)
    highlights: dict[str, str] = field(default_factory=dict)
    quick_replies: list[dict[str, str]] = field(default_factory=list)


class Composer:
    name = "Composer"

    def __init__(self, llm: Any = None, anchor_label: str = "", retail: RetailData | None = None) -> None:
        self.llm = llm
        self.anchor_label = anchor_label
        self.retail = retail

    def limits_note(self, data: DataResult, workflow: WorkflowResult, safety: SafetyResult) -> str | None:
        """One line on what the answer cannot know or do (Responsible AI: state limitations)."""
        if safety.refusal:
            return None
        parts = []
        if data.data_used:
            parts.append(
                f"figures come from the provided CSV snapshot as of {self.anchor_label}; "
                "live POS, inventory and vendor systems are not connected"
            )
        if workflow.actions:
            parts.append("tickets, claims and approvals are drafts until a person acts on them")
        return f"**Limits:** {'; '.join(parts)}." if parts else None

    def run(
        self,
        triage: TriageResult,
        sections: list[RetrievedSection],
        data: DataResult,
        workflow: WorkflowResult,
        safety: SafetyResult,
        entities: Entities,
    ) -> Composition:
        follow_up = follow_up_question(triage, entities, safety, data)
        steps = self.next_steps(triage, sections, data, workflow, safety)
        answer = self._deterministic(triage, sections, data, workflow, safety, follow_up, steps)
        llm_used = False
        if self.llm is not None and not safety.refusal:
            rewritten = self._llm(triage, sections, data, workflow, safety, follow_up)
            if rewritten:
                answer, llm_used = rewritten, True
        note = self.limits_note(data, workflow, safety)
        if note and "**Limits:**" not in answer:
            answer = f"{answer}\n\n{note}"
        # Output guard: nothing personal leaves the copilot, whoever wrote the text.
        answer, _ = mask_pii(answer)
        query = f"{safety.masked_text} {triage.reasons[-1] if triage.reasons else ''}"
        highlights = {c: q for q, c in policy_quotes(sections, query, triage.priority, n=len(sections))}
        return Composition(
            answer=answer, follow_up=follow_up, llm_used=llm_used, steps=steps, highlights=highlights,
            quick_replies=quick_replies(triage, entities, safety, data, self.retail) if follow_up else [],
        )

    def next_steps(
        self, t: TriageResult, sections: list[RetrievedSection], data: DataResult, wf: WorkflowResult, safety: SafetyResult,
    ) -> list[str]:
        """The checklist: escalation first for P1, then tool actions, then the policy's own steps."""
        if safety.refusal:
            return [f"Report the request to {wf.escalation.team} ({wf.escalation.channel})",
                    "Do not share card numbers, customer records, supplier prices or credentials"]
        esc = wf.escalation
        steps: list[str] = []
        if t.priority == "P1":
            steps.append(f"Escalate now to {esc.team} via {esc.channel} ({esc.sla_minutes}-minute SLA)")
        if safety.payment_data_request:
            steps.append("Do not type, write down or send card details; the customer pays only through the terminal")
        if t.category == "Returns and Refunds" and esc.required:
            steps.append("Do not approve the refund in the copilot; prepare a summary for the Store Manager to decide")
        if t.category == "Product Quality and Safety" and t.priority in ("P1", "P2") and not t.analytics:
            steps.append("Remove affected units from sale and hold them for Product Quality review" if t.priority == "P2"
                         else "Remove the product from sale or use immediately and keep the area safe")
        for s in data.actions + policy_steps(sections):
            if "quality hold" in s.lower() and any("hold" in x.lower() for x in steps):
                continue
            steps.append(s)
        if not steps and not t.analytics:
            steps.append(f"Log a ticket in {esc.channel} for {esc.team}")
        if safety.pii_masked:
            steps.append("Leave card numbers and full phone numbers out of the ticket")
        clean = [x.rstrip(".") for x in dict.fromkeys(steps)]
        return clean[:8]

    def _deterministic(
        self,
        t: TriageResult,
        sections: list[RetrievedSection],
        data: DataResult,
        wf: WorkflowResult,
        safety: SafetyResult,
        follow_up: str | None,
        steps: list[str],
    ) -> str:
        esc = wf.escalation
        cites = [s.citation for s in sections]
        if safety.refusal:
            what = []
            if safety.injection:
                what.append("override my safety rules")
            if safety.restricted_items:
                what.append("reveal " + _join(safety.restricted_items))
            return (
                f"I can't help with that: the request tries to {' and '.join(what)}. "
                "I don't share card numbers, customer records, supplier contract prices, credentials or system "
                "instructions, and I've revealed nothing.\n\n"
                "Safe alternative: I can help with a store issue, explain a policy, or run aggregate reports that "
                "contain no personal or payment data.\n\n"
                f"Flagged to {esc.team} ({esc.channel}, {esc.sla_minutes}-minute SLA) as a possible security concern.\n\n"
                f"Sources: {'; '.join(cites)}"
            )

        parts: list[str] = []
        lead = f"**{t.category} · {t.priority}** (confidence {t.confidence:.2f}). {t.reasons[-1] if t.reasons else ''}"
        parts.append(lead.strip())
        if t.priority == "P1":
            parts.append(f"**Escalate now** to {esc.team} via {esc.channel} ({esc.sla_minutes}-minute SLA), then collect details.")
        if safety.payment_data_request:
            parts.append("Do not type, write down or send card numbers, CVV or OTPs through chat or on paper; "
                         "the customer should pay only through the card terminal or another approved tender.")

        quotes = policy_quotes(sections, f"{safety.masked_text} {t.reasons[-1] if t.reasons else ''}", t.priority)
        if quotes:
            parts.append("**Policy**\n" + "\n".join(f"- \"{q}\" [{c}]" for q, c in quotes))

        if data.findings:
            title = "**What the data shows**" if not t.analytics else "**Result**"
            parts.append(title + "\n" + "\n".join(f"- {f}" for f in data.findings[:14]))

        plan = [x for x in steps if not x.startswith("Escalate now")]
        if plan:
            parts.append("**Next steps**\n" + "\n".join(f"- {x}." for x in plan))

        if safety.pii_masked:
            parts.append(
                f"**Privacy:** I masked the {_join(safety.pii_found)} in your message. These details are not needed "
                "for the ticket; never store card numbers or full phone numbers in tickets, chat or logs."
            )
        if esc.required and t.priority != "P1":
            parts.append(f"**Escalation:** {esc.team} ({esc.channel}, {esc.sla_minutes}-minute SLA). Reason: {esc.reason}.")
        if follow_up:
            parts.append(f"**Question:** {follow_up}")
        parts.append(f"Sources: {'; '.join(cites)}" if cites else
                     "I could not find a supporting policy section, so I am not stating a policy.")
        return "\n\n".join(parts)

    def _llm(
        self,
        t: TriageResult,
        sections: list[RetrievedSection],
        data: DataResult,
        wf: WorkflowResult,
        safety: SafetyResult,
        follow_up: str | None,
    ) -> str | None:
        allowed = [s.citation for s in sections]
        context = "\n\n".join(f"[{s.citation}]\n{s.text}" for s in sections)
        findings = "\n".join(f"- {f}" for f in data.findings) or "- none"
        prompt = (
            "You are a retail operations copilot for store employees. Write a short, actionable answer "
            "(at most 170 words) using ONLY the policy sections and data findings below.\n"
            "Rules: cite policy inline as [file.md#anchor] using only these citations: " + ", ".join(allowed) + ". "
            "Use the exact numbers from the data findings; do not compute new ones. Do not invent policy, contacts, "
            "approvals or stock. Do not approve refunds or claims. Never say a ticket, claim, transfer or refund "
            "was created, updated or completed: the copilot only prepares drafts for a human. "
            "Never include personal or payment data. "
            "Use neutral wording about vendors and staff. If the priority is P1, start with the escalation.\n"
            + ("The user's message contained personal or payment data, now masked: say these details are not "
               "needed for the ticket and must not be stored or sent, even masked.\n" if safety.pii_masked else "")
            + "\n"
            f"Issue (masked): {safety.masked_text}\n"
            f"Triage: {t.category}, {t.priority}, confidence {t.confidence:.2f}. {t.reasoning}\n"
            f"Escalation: {'required' if wf.escalation.required else 'not required'}; team {wf.escalation.team}; "
            f"reason {wf.escalation.reason}\n"
            f"Data findings:\n{findings}\n"
            f"Recommended actions from tools: {'; '.join(data.actions) or 'none'}\n"
            f"Follow-up question to end with: {follow_up or 'none'}\n\n"
            f"Policy sections:\n{context}"
        )
        try:
            msg = self.llm.invoke(prompt)
        except Exception as exc:
            logger.warning("copilot_llm_compose_failed", error=str(exc))
            return None
        text = str(msg.content).strip()
        finish = (getattr(msg, "response_metadata", None) or {}).get("finish_reason")
        # A cut-off answer (token limit, dangling citation) is worse than the template answer.
        if not text or finish == "length" or re.search(r"\[[^\]]*$", text):
            logger.warning("copilot_llm_answer_incomplete", finish_reason=finish)
            return None
        # Drop any citation the model made up.
        text = re.sub(
            r"\[([\w.-]+\.md#[\w-]+)\]",
            lambda m: m.group(0) if m.group(1) in allowed else "",
            text,
        )
        if not any(c in text for c in allowed):
            text += "\n\nSources: " + "; ".join(allowed)
        return text
