"""Triage agent: category, priority, confidence and reasoning.

Three signals, combined in code:

1. Keyword rules taken from the SOP category list and priority table.
2. A nearest-neighbour vote over the 550 labelled ``support_cases.csv`` tickets
   (BM25 from the RAG engine), which handles wording the rules do not list.
3. Optionally the LLM, prompted with the SOP priority rules and the most similar
   historic tickets as few-shot examples. It may raise a priority, never lower one.

When the embedding model is available, the ticket vote also matches by meaning,
and messages close to an example of a P1 trigger (``semantic.py``) get that P1.
Rules read the normalised text (typos fixed, Hinglish translated).

Hard rules (store-wide outage, injury/recall, payment-data requests, injection)
always win over the model, and the data agent can raise priority later with
facts such as "high-demand SKU at zero stock" (see ``refine``).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from langchain_core.documents import Document

from app.copilot.data_agent import DataResult, Entities, RetailData
from app.copilot.safety import SafetyResult
from app.copilot.schemas import CATEGORIES
from app.retriever.bm25_search import BM25Search
from app.utils.logging import get_logger

logger = get_logger(__name__)

SOP = "01_retail_operations_sop.md"
PRIORITY_RULES = f"{SOP}#priority-rules"

# (pattern, weight) per category, from the SOP category examples.
CATEGORY_TERMS: dict[str, list[tuple[str, float]]] = {
    "POS and Payments": [
        (r"card ?readers?|card machine|card terminal|edc|swipe", 3), (r"\bregisters?\b|\btills?\b|billing counter", 2),
        (r"\bpos\b", 1), (r"card payments?|payments?|upi|tender", 2), (r"error( code)? ?\d+", 1.5),
        (r"receipt printer|cash drawer|printer", 2.5),
    ],
    "Inventory and Replenishment": [
        (r"out of stock|stock-?outs?|out of stok|sold out|not available|unavailable|no stock|zero stock", 3),
        (r"reorder|cycle count|on hand|on-hand|system (shows|qty|quantity)|shelf count|replenish", 2.5),
        (r"\bstock\b|inventory|availability|transfer", 1.5), (r"root cause", 1),
    ],
    "Pricing and Promotions": [
        (r"shelf (label|price)|price tag|label|\btags?\b|rack price", 3), (r"\bprices?\b|pricing|mrp|scans?\b|scanning", 2),
        (r"\b(bill|till|pos|register|counter) (shows|says|charg|rings)", 2),
        (r"promotions?|promo|discount|coupon|offer", 2.5),
    ],
    "Returns and Refunds": [
        (r"\breturn(s|ed|ing)?\b|\brefund(s|ed|ing)?\b|exchange|money back", 3),
        (r"without (a )?receipt|no receipt|no bill", 2),
    ],
    "Vendor and Supply Chain": [
        (r"vendors?|suppliers?|distributor", 2), (r"shipments?|deliver(y|ies|ed)|consignment|\bpo\b|purchase order", 2.5),
        (r"short (shipment|by|delivery)|received only|arrived \d+ ?(%|percent)|partial (delivery|receipt)", 3),
        (r"fill rate|on-time|late delivery|damaged cartons?", 2.5),
    ],
    "Product Quality and Safety": [
        (r"quality", 3), (r"defect|defective|crack|split|seams?|warped|torn|broken|broke|stitching", 3),
        (r"injur|hurt|unsafe|recall|hazard|malfunction|safety|loosened|slipped|tripped|fell (off|over|down|from)|fainted|shock|fire|smoke", 3.5),
        (r"complaints?", 1),
    ],
    "Customer Service": [
        (r"upset|angry|rude|shout|yell|abusive|misbehav|complain(ed|ing)? about|wait time|long queue|queue", 2.5),
        (r"goodwill|apolog|service recovery|compensation|feedback|escalated by customer", 2.5),
    ],
    "Other or Security": [
        (r"password|credential|hack|phishing|suspicious|restricted|confidential", 3),
        (r"incidents?|dashboard|report|grouped by|by store|by region|summar", 1.5),
    ],
}

_ANALYTICS = re.compile(
    r"^(show|list|which|how many|summari[sz]e|give me|what are the|report|count|top \d+|trend)\b"
    r"|grouped by|group by|by store|by region|dashboard|\btrends?\b|how many|which (vendor|store|sku|product)s?\b",
    re.IGNORECASE,
)
_QUESTION = re.compile(r"^(what is|what's|what are|how do|how does|how should|can i|can we|is it|are we|explain|tell me)\b", re.I)
_SYMPTOM = re.compile(
    r"error|fail|not working|down\b|broken|out of stock|unavailable|not available|wants? to|asking|complain|damaged|short|"
    r"missing|mismatch|cannot|can't|isn't|stuck|injur|split|crack|slipp|loosen|upset|scans",
    re.IGNORECASE,
)

# P1 triggers from the SOP priority table, POS runbook and recall policy.
_STORE_WIDE = re.compile(
    r"\b(all|every|entire|whole|none of the|multiple|several|many|most)\b[^.]{0,30}\b(registers?|tills?|counters?|pos|terminals?|card readers?)\b"
    r"[^.]{0,40}\b(cannot|can't|can not|unable|not working|down|failing|failed|offline|outage|not (accepting|processing))"
    r"|\b(none of the|no)\b[^.]{0,15}\b(registers?|tills?|counters?|terminals?)\b[^.]{0,30}\b(work|accept|process|tak)"
    r"|store-?wide|whole store|entire store[^.]{0,30}(card|payment|pos)|no card payments?",
    re.IGNORECASE,
)
_DEGRADED = re.compile(
    r"\b(multiple|several|many|some|two|three|four|five|\d+)\b[^.]{0,20}\b(registers?|tills?|counters?|terminals?)\b[^.]{0,30}\b(slow|intermittent|lag|degraded|delay|freez)",
    re.IGNORECASE,
)
_SAFETY = re.compile(
    r"injur|hurt|bleed|electric shock|caught fire|smoke|burn|recall|unsafe|hazard|safety risk|"
    r"(treadmill|exercise bike|bike|rowing machine|cross trainer|machine|equipment|demo unit|helmet)[^.]{0,40}"
    r"(slipped|loosen|collapsed|broke|snapped|malfunction|sparking|fell|came off)|"
    # A person getting hurt or nearly hurt is an "active customer safety issue" (SOP P1).
    r"\b(customer|shopper|child|kid|boy|girl|person|someone|staff|employee|associate|cashier)\b[^.]{0,30}"
    r"\b(slipped|tripped|fell|fainted|collapsed|was hit|got hit|got cut|was cut)\b",
    re.IGNORECASE,
)
_LEGAL = re.compile(r"legal|lawsuit|police|consumer court|compliance (risk|breach)", re.IGNORECASE)
_URGENT_STOCK = re.compile(r"again|repeated|keeps|tournament|school|academy|team order|club order|bulk order", re.I)
_MANY = re.compile(
    r"\b(multiple|many|several|all|across|lots of|numerous|two|three|four|five|six|ten|dozens? of|"
    r"(?<!inr )(?<!rs )(?<!rs\. )(?<![₹\d,])[2-9]\d?)\b[^.\d]{0,14}?"
    r"\b(?!(?:days|hours|weeks|months|minutes|mins|hrs|times|years)\b)(skus?|items|products|customers|labels|stores|\w{3,}s)\b"
    r"|\b(whole|entire|full)\b[^.]{0,20}\b(aisle|department|section|category|store|range)\b"
    r"|\bdepartment\b|repeated|again and again|keeps? happening",
    re.IGNORECASE,
)
_STOCK_OUT = re.compile(r"out of stock|stock-?out|out of stok|sold out|unavailable|not available|zero stock|no stock|shelf (count )?is zero", re.I)
# "only 30 of 100 arrived", "delivered 40 out of 120": read only for vendor messages that mention a delivery.
_RECEIVED_OF = re.compile(r"\b(\d+)\s*(?:of|out of|/)\s*(\d+)\b")
_DELIVERY_WORD = re.compile(r"\b(arrived|delivered|received|came|sent|ordered|shipped|delivery|shipment)\b", re.I)
_HALF = re.compile(r"\b(only )?half (the|of the|our)?\b[^.]{0,40}\b(ordered|order|shipment|delivery|po)\b", re.I)
_SHORT = re.compile(r"received only (\d+) of (\d+)|arrived (\d+) ?(%|percent)|short by (\d+)|short shipment|partial (delivery|receipt)", re.I)

SEVERITY = {"P1": 1, "P2": 2, "P3": 3, "P4": 4}


@dataclass
class TriageResult:
    category: str
    priority: str
    confidence: float
    reasons: list[str] = field(default_factory=list)
    citations: list[str] = field(default_factory=list)
    analytics: bool = False
    hard_rule: bool = False
    signals: dict[str, Any] = field(default_factory=dict)
    llm_used: bool = False

    @property
    def reasoning(self) -> str:
        return " ".join(self.reasons)


class TriageAgent:
    name = "Triage agent"

    def __init__(self, data: RetailData, llm: Any = None, semantic: Any = None) -> None:
        self.data = data
        self.llm = llm
        self.semantic = semantic
        self._cases = [
            Document(page_content=row.issue_text, metadata={"category": row.category_label, "priority": row.priority_label})
            for row in data.cases.drop_duplicates("issue_text").itertuples()
        ]
        self._bm25 = BM25Search()
        self._bm25.build_index(self._cases)

    # ── Signals ────────────────────────────────────────────────────────

    def _lexicon(self, text: str) -> dict[str, float]:
        scores = {c: 0.0 for c in CATEGORIES}
        for cat, terms in CATEGORY_TERMS.items():
            for pattern, weight in terms:
                if re.search(pattern, text, re.IGNORECASE):
                    scores[cat] += weight
        return scores

    def similar_cases(self, text: str, k: int = 7) -> list[tuple[Document, float]]:
        return self._bm25.search(text, k=k)

    def _knn(self, text: str) -> dict[str, float]:
        """Category votes of the most similar historic tickets: by words (BM25) and,
        when embeddings are available, by meaning; the two are averaged."""
        hits = self.similar_cases(text)
        votes = {c: 0.0 for c in CATEGORIES}
        if hits:
            top = hits[0][1] or 1.0
            for doc, score in hits:
                votes[doc.metadata["category"]] += score / top
            total = sum(votes.values()) or 1.0
            votes = {c: v / total for c, v in votes.items()}
        dense: dict[str, float] = {}
        if self.semantic is not None:
            try:
                dense = self.semantic.ticket_votes(text)
            except Exception as exc:  # embedding failure: words only
                logger.warning("copilot_semantic_votes_failed", error=str(exc)[:200])
        if not dense:
            return votes
        if not hits:
            return {c: dense.get(c, 0.0) for c in CATEGORIES}
        return {c: (votes[c] + dense.get(c, 0.0)) / 2 for c in CATEGORIES}

    # ── Main ───────────────────────────────────────────────────────────

    def run(self, safety: SafetyResult, entities: Entities) -> TriageResult:
        text = safety.rules_text
        analytics = bool(_ANALYTICS.search(text.strip()))

        if safety.refusal:
            return TriageResult(
                category="Other or Security",
                priority="P1",
                confidence=0.95,
                reasons=[f"Safety agent flagged a {safety.reason}; the SOP treats restricted-data requests as P1 security escalations."],
                citations=[PRIORITY_RULES, "05_responsible_ai_policy.md#prompt-injection"],
                hard_rule=True,
            )

        lex, knn = self._lexicon(text), self._knn(text)
        # A P1 trigger is never skipped for lack of category keywords.
        p1_signal = bool(
            safety.payment_data_request or _SAFETY.search(text) or _STORE_WIDE.search(text) or _LEGAL.search(text)
            or any(h.group in ("equipment_injury", "store_wide_payment_outage") for h in safety.semantic_hits)
        )
        if not any(lex.values()) and max(knn.values()) < 0.5 and not p1_signal:
            # Nothing recognisable: do not guess. Low confidence triggers a follow-up and human review.
            return TriageResult(
                category="Other or Security", priority="P4", confidence=0.35,
                reasons=["No retail-operations signal found in the message; asking for details instead of guessing."],
                citations=[PRIORITY_RULES], analytics=analytics,
                signals={"similar_case_votes": {c: round(v, 2) for c, v in knn.items() if v}},
            )
        combined = {c: lex[c] / max(max(lex.values()), 1.0) + 0.8 * knn[c] for c in CATEGORIES}
        category = max(combined, key=combined.get)
        reasons: list[str] = []
        lex_hits = [c for c, s in sorted(lex.items(), key=lambda x: -x[1]) if s > 0][:3]

        # Generic analytics over cases goes to Other or Security / P4 (SOP analytics rule).
        if analytics and lex[category] < 3 and not _SYMPTOM.search(text):
            category = "Other or Security"

        result = TriageResult(category=category, priority="P3", confidence=0.0, analytics=analytics)
        result.signals = {
            "keyword_scores": {c: round(s, 1) for c, s in lex.items() if s},
            "similar_case_votes": {c: round(v, 2) for c, v in knn.items() if v},
        }
        if lex_hits:
            reasons.append(f"Keywords point to {', '.join(lex_hits)}.")
        best_knn = max(knn, key=knn.get)
        if knn[best_knn]:
            reasons.append(f"Similar historic tickets are mostly {best_knn} ({knn[best_knn]:.0%}).")

        self._apply_priority_rules(result, text, entities, safety, reasons)
        if not result.hard_rule:
            self._resolve_multi_category(result, text, entities, safety, lex, combined, reasons)

        # Confidence: how clearly the keyword and similar-ticket signals agree.
        lex_total = sum(lex.values())
        lex_share = lex[result.category] / lex_total if lex_total else 0.0
        conf = 0.35 + 0.35 * lex_share + 0.25 * knn[result.category]
        if result.hard_rule:
            conf = max(conf, 0.9)
        elif result.signals.get("priority_rule"):
            conf += 0.05
        if result.category == "Other or Security" and analytics:
            conf = max(conf, 0.75)
        result.confidence = round(min(max(conf, 0.3), 0.95), 2)
        result.reasons = reasons

        if self.llm is not None:
            self._llm_triage(result, text)
        return result

    def _resolve_multi_category(
        self,
        r: TriageResult,
        text: str,
        e: Entities,
        safety: SafetyResult,
        lex: dict[str, float],
        combined: dict[str, float],
        reasons: list[str],
    ) -> None:
        """A request that spans two categories goes to the one with the immediate risk (SOP).

        When a second category has a strong keyword signal of its own and its rule gives a
        different priority, the more urgent one wins and the conflict is flagged, so the
        workflow agent escalates it for a human decision (SOP escalation policy).
        """
        runner_up = max((c for c in CATEGORIES if c != r.category), key=combined.get)
        if lex[runner_up] < 3 or combined[runner_up] < 0.5 * combined[r.category]:
            return
        alt = TriageResult(category=runner_up, priority="P3", confidence=0.0, analytics=r.analytics)
        self._apply_priority_rules(alt, text, e, safety, [])
        if alt.priority == r.priority or alt.hard_rule:
            return
        first, second = (alt, r) if SEVERITY[alt.priority] < SEVERITY[r.priority] else (r, alt)
        r.signals["policy_conflict"] = {
            "categories": [r.category, runner_up],
            "rules": {r.category: r.priority, runner_up: alt.priority},
        }
        if first is alt:
            r.category, r.priority = alt.category, alt.priority
            r.signals["priority_rule"] = alt.signals.get("priority_rule", "")
            for c in alt.citations:
                if c not in r.citations:
                    r.citations.insert(0, c)
        reasons.append(
            f"Spans {first.category} ({first.priority}) and {second.category} ({second.priority}); "
            f"the SOP takes the category with the immediate risk, so {first.priority} applies and a person confirms it."
        )
        if f"{SOP}#service-categories" not in r.citations:
            r.citations.append(f"{SOP}#service-categories")

    def _set(self, r: TriageResult, priority: str, reason: str, citation: str = PRIORITY_RULES, hard: bool = False) -> None:
        r.priority = priority
        r.signals["priority_rule"] = reason
        r.hard_rule = r.hard_rule or hard
        if citation not in r.citations:
            r.citations.append(citation)

    def _apply_priority_rules(
        self, r: TriageResult, text: str, e: Entities, safety: SafetyResult, reasons: list[str]
    ) -> None:
        low = text.lower()
        pos = "02_pos_pricing_returns_policy.md"

        # ── P1: immediate human escalation ──
        if safety.payment_data_request:
            r.category = "POS and Payments"
            self._set(r, "P1", "payment data requested outside the POS", f"{pos}#payment-security", hard=True)
            reasons.append("Card data is being collected outside the POS: a P1 payment-security risk.")
            return
        if _SAFETY.search(text):
            r.category = "Product Quality and Safety"
            self._set(r, "P1", "injury, recall or unsafe equipment", "04_vendor_quality_policy.md#recall-and-safety", hard=True)
            reasons.append("Injury, recall or unsafe-equipment wording: the SOP makes this P1 Product Quality and Safety.")
            return
        if _STORE_WIDE.search(text) and r.category in ("POS and Payments", "Other or Security", "Customer Service"):
            r.category = "POS and Payments"
            self._set(r, "P1", "store-wide or multi-register payment outage", hard=True)
            r.citations.append(f"{pos}#pos-card-reader-runbook")
            reasons.append("Multiple or all registers cannot take card payments: store-wide outage is P1.")
            return
        if _LEGAL.search(text):
            self._set(r, "P1", "legal or compliance risk", hard=True)
            reasons.append("Legal/compliance risk is P1 under the SOP.")
            return
        # The same P1 triggers, matched by meaning when the wording differs.
        for hit in safety.semantic_hits:
            if hit.group == "equipment_injury":
                r.category = "Product Quality and Safety"
                self._set(r, "P1", "injury or unsafe equipment (matched by meaning)", "04_vendor_quality_policy.md#recall-and-safety", hard=True)
                reasons.append(f"Close to the P1 example \"{hit.example}\" (similarity {hit.similarity:.2f}): "
                               "an injury or unsafe-equipment report is P1 Product Quality and Safety.")
                return
            if hit.group == "store_wide_payment_outage" and r.category in ("POS and Payments", "Other or Security", "Customer Service"):
                r.category = "POS and Payments"
                self._set(r, "P1", "store-wide payment outage (matched by meaning)", hard=True)
                r.citations.append(f"{pos}#pos-card-reader-runbook")
                reasons.append(f"Close to the P1 example \"{hit.example}\" (similarity {hit.similarity:.2f}): "
                               "a store-wide payment outage is P1.")
                return

        # ── P2 / P3 by category ──
        cat = r.category
        many = bool(_MANY.search(text)) or len(e.store_ids) > 1
        if cat == "POS and Payments":
            if _DEGRADED.search(text):
                self._set(r, "P2", "multiple registers degraded")
                reasons.append("Several registers are degraded but still working: P2.")
            else:
                self._set(r, "P3", "single register or device issue", f"{pos}#pos-card-reader-runbook")
                reasons.append("Only one register/device is affected and others work: P3 per the POS runbook.")
        elif cat == "Inventory and Replenishment":
            if _STOCK_OUT.search(text) and (self.data.is_high_demand(e.sku) or _URGENT_STOCK.search(text)):
                self._set(r, "P2", "high-demand or order-critical stock-out", "03_inventory_replenishment_policy.md#stock-outs-and-replenishment")
                reasons.append("Stock-out of a high-demand or order-critical item affects sales: P2.")
            else:
                self._set(r, "P3", "routine reorder or single-SKU issue", "03_inventory_replenishment_policy.md#stock-outs-and-replenishment")
                reasons.append("Routine single-SKU inventory issue: P3 unless data shows a high-demand stock-out.")
        elif cat == "Pricing and Promotions":
            if many:
                self._set(r, "P2", "pricing error affecting many SKUs/customers", f"{pos}#pricing-and-promotions")
                reasons.append("Mismatch affects many SKUs, a department or many customers: P2.")
            else:
                self._set(r, "P3", "single shelf-vs-register mismatch", f"{pos}#pricing-and-promotions")
                reasons.append("Isolated shelf-vs-register mismatch for one SKU: P3.")
        elif cat == "Returns and Refunds":
            amount = e.amount_inr
            if amount is not None and (amount >= 5000 or (e.receipt_present is False and amount >= 1000)):
                self._set(r, "P2", "refund needs manager approval", f"{pos}#returns-and-refunds")
                reasons.append(f"Refund of INR {amount:,.0f}{' without receipt' if e.receipt_present is False else ''} is above the approval threshold: P2, manager approval.")
            else:
                self._set(r, "P3", "standard return guidance", f"{pos}#returns-and-refunds")
                reasons.append("Standard return guidance: P3.")
        elif cat == "Vendor and Supply Chain":
            m = _SHORT.search(text)
            ratio = None
            if m and m.group(1):
                ratio = int(m.group(1)) / max(int(m.group(2)), 1)
            elif m and m.group(3):
                ratio = int(m.group(3)) / 100
            elif (got := _RECEIVED_OF.search(text)) and _DELIVERY_WORD.search(text):
                ratio = int(got.group(1)) / max(int(got.group(2)), 1)
            elif _HALF.search(text):
                ratio = 0.5
            if (ratio is not None and ratio < 0.9) or many or re.search(r"damaged cartons?|damaged on receipt", low):
                self._set(r, "P2", "short shipment below 90% or repeated vendor issue", "04_vendor_quality_policy.md#short-shipments")
                reasons.append("Short shipment below 90% of PO (or repeated) affects replenishment: P2.")
            else:
                self._set(r, "P3", "routine vendor follow-up", "04_vendor_quality_policy.md#short-shipments")
                reasons.append("Routine vendor follow-up: P3.")
        elif cat == "Product Quality and Safety":
            if many or (r.analytics and "complaint" in low):
                self._set(r, "P2", "repeated quality complaints, no safety risk", "04_vendor_quality_policy.md#product-quality-complaints")
                reasons.append("Repeated quality complaints without reported injury: P2.")
            else:
                self._set(r, "P3", "single quality complaint", "04_vendor_quality_policy.md#product-quality-complaints")
                reasons.append("Single quality complaint with no safety risk: P3 unless data shows repeats.")
        elif cat == "Customer Service":
            self._set(r, "P3", "routine service recovery", f"{pos}#customer-recovery")
            reasons.append("Routine service recovery: P3.")
        else:
            self._set(r, "P4", "general inquiry or analytics", f"{SOP}#analytics-rule" if r.analytics else PRIORITY_RULES)
            reasons.append("General inquiry or analytics request with no active issue: P4.")

        # Policy questions with no active issue are P4 whatever the topic.
        if _QUESTION.search(text) and not _SYMPTOM.search(text) and r.priority == "P3":
            self._set(r, "P4", "policy question without an active issue")
            reasons.append("This is a policy question, not an active issue: P4.")

    # ── Data-informed refinement ───────────────────────────────────────

    def refine(self, r: TriageResult, data: DataResult) -> None:
        """Raise priority when the data agent's facts meet a P2 rule."""
        f = data.facts
        if r.hard_rule or SEVERITY[r.priority] <= 2:
            return
        rule = None
        if r.category == "Inventory and Replenishment" and f.get("stockout_high_demand"):
            rule = ("data: high-demand SKU at zero on hand", "03_inventory_replenishment_policy.md#stock-outs-and-replenishment")
        elif r.category in ("Inventory and Replenishment", "Vendor and Supply Chain") and f.get("high_demand") and f.get("worst_fill", 1) < 0.9:
            rule = ("data: high-demand SKU received below 90% of PO", "04_vendor_quality_policy.md#short-shipments")
        elif r.category == "Product Quality and Safety" and f.get("quality_complaints", 0) >= 2:
            rule = ("data: repeated quality complaints in the last 30 days", "04_vendor_quality_policy.md#product-quality-complaints")
        if rule:
            self._set(r, "P2", rule[0], rule[1])
            r.reasons.append(f"Raised to P2 by the data ({rule[0][6:]}).")

    # ── Optional LLM classifier ────────────────────────────────────────

    def _llm_triage(self, r: TriageResult, text: str) -> None:
        examples = "\n".join(
            f'- "{d.page_content}" -> {d.metadata["category"]}, {d.metadata["priority"]}'
            for d, _ in self.similar_cases(text, k=5)
        )
        prompt = (
            "You triage store issues for a sports retail chain. Return ONLY JSON: "
            '{"category": one of ' + json.dumps(CATEGORIES) + ', "priority": "P1"|"P2"|"P3"|"P4", '
            '"confidence": 0-1, "reasoning": "one sentence"}.\n'
            "Priority rules: P1 = store-wide POS outage, payment-data request, injury/recall/unsafe equipment. "
            "P2 = repeated high-demand stock-out, short shipment below 90%, pricing error affecting many, "
            "multiple registers degraded, repeated quality complaints, refund needing manager approval. "
            "P3 = one register, one SKU, single label, standard return. P4 = policy question or report request.\n"
            f"Similar past tickets:\n{examples}\n\nIssue: {text}"
        )
        try:
            raw = self.llm.invoke(prompt).content
            parsed = json.loads(re.search(r"\{.*\}", raw, re.S).group(0))
            cat, pri = parsed.get("category"), parsed.get("priority")
            conf = float(parsed.get("confidence", 0.5))
        except Exception as exc:  # malformed JSON, timeout, no key...
            logger.warning("copilot_llm_triage_failed", error=str(exc))
            r.signals["llm_error"] = str(exc)[:200]
            return
        if cat not in CATEGORIES or pri not in SEVERITY:
            return
        r.llm_used = True
        r.signals["llm"] = {"category": cat, "priority": pri, "confidence": conf, "reasoning": parsed.get("reasoning")}
        if r.hard_rule:
            # Hard rules are policy; the model only adjusts confidence.
            r.confidence = round(min(0.97, (r.confidence + conf) / 2 + (0.05 if cat == r.category else 0)), 2)
            return
        # The model may make a case more urgent, never less: its mistakes cost a human
        # review, not a missed P1.
        more_urgent = SEVERITY[pri] < SEVERITY[r.priority]
        why = parsed.get("reasoning", "")
        if cat == r.category:
            r.confidence = round(min(0.97, max(r.confidence, conf) + 0.05), 2)
            if more_urgent:
                r.priority = pri
                r.reasons.append(f"LLM judged it more urgent, {pri}: {why}")
        elif r.confidence < 0.55 and conf >= 0.7:
            # Rules were unsure and the model is confident: take its category; priority only goes up.
            r.reasons.append(f"Rules were unsure; LLM classified it as {cat}, {pri}: {why}")
            r.category = cat
            if more_urgent:
                r.priority = pri
            r.confidence = round((r.confidence + conf) / 2, 2)
        else:
            r.reasons.append(f"LLM suggested {cat}, {pri} ({conf:.2f}); rules kept {r.category}.")
            if more_urgent:
                r.priority = pri
                r.reasons.append(f"Priority raised to {pri} on the LLM's reading; a person confirms it.")
            r.confidence = round(max(0.3, min(r.confidence, conf) - 0.1), 2)
