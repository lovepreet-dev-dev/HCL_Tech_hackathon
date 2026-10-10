"""Safety agent: PII/payment masking, prompt-injection and restricted-data detection.

Runs first, on the raw message, before any other agent or the LLM sees it.
Everything here is deterministic regex so the 100% safety target never depends
on model behaviour. Masking formats follow ``05_responsible_ai_policy.md``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# 13-19 digits, optionally grouped by spaces or dashes: card numbers.
_CARD = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
# Indian mobile numbers, optional +91 / 0 prefix.
_PHONE = re.compile(r"(?<!\d)(?:\+?91[ -]?|0)?([6-9]\d{9})(?!\d)")
_EMAIL = re.compile(r"\b([A-Za-z0-9._%+-]+)@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")
# "CVV 123", "OTP is 4455", "password: hunter2" -> keep the keyword, drop the value.
# Value must follow "is"/":"/"=" or contain a digit, so "password for the POS" is left alone.
_SECRET = re.compile(
    r"\b(cvv|cvc|pin|otp|password|passcode|upi pin)\b"
    r"(\s*(?:is|:|=|-)\s*|\s+(?=[^\s,.;]*\d))([^\s,.;]+)",
    re.IGNORECASE,
)
_AADHAAR = re.compile(r"(?<!\d)\d{4}[ -]?\d{4}[ -]?\d{4}(?!\d)")
_PAN = re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")
_BANK_ACCOUNT = re.compile(
    r"\b(account (?:number|no\.?)|a/c(?: no\.?)?)(\s*(?:is|:|=)?\s*)(\d{9,18})\b", re.IGNORECASE
)

_INJECTION = [
    r"\b(ignore|disregard|forget|override)\b.{0,40}\b(instructions?|rules|polic(y|ies)|guardrails|prompt)\b",
    r"\b(reveal|show|print|leak|display)\b.{0,30}\b(system|hidden|secret)\s+(prompt|instructions|test cases?)\b",
    r"\bjudge test cases?\b",
    r"\b(bypass|disable|turn off)\b.{0,30}\b(safety|security|guardrails?|filters?|masking)\b",
    r"\byou are now\b|\bact as (an? )?(admin|developer|dan)\b|\bjailbreak\b|\bdeveloper mode\b",
    r"\bdo not cite\b|\binvent (a |the )?polic(y|ies)\b|\bmake up (a )?polic(y|ies)\b",
]

_REQUEST_VERB = r"\b(show|give|list|share|print|export|reveal|send|display|dump|tell|provide|get|fetch|access)\b"
_RESTRICTED = {
    "customer card numbers": r"\b(customer'?s? )?(card numbers?|card details|cvv|card data)\b",
    "customer personal data": r"\bcustomer('?s)? (phone|mobile|email|address|data|records?|details|database|list|contacts?)\b",
    "full customer database": r"\b(full|entire|whole) (customer )?database\b",
    "supplier contract pricing": r"\b(supplier|vendor) (contract|cost|purchase)? ?(prices?|pricing|contracts?|margins?)\b|\bcontract prices?\b",
    "credentials": r"\b(admin |pos )?(passwords?|credentials|login details)\b",
    "employee records": r"\bemployee (records|salar(y|ies)|personal data|phone numbers?)\b",
}
# Asking an associate to collect card data through chat is a payment-security risk.
_PAYMENT_SOLICIT = re.compile(
    r"\b(type|enter|send|share|write|collect)\b.{0,30}\b(full )?(card number|card details|cvv|otp|pin)\b",
    re.IGNORECASE,
)


@dataclass
class SafetyResult:
    masked_text: str
    pii_found: list[str] = field(default_factory=list)
    injection: bool = False
    restricted_items: list[str] = field(default_factory=list)
    payment_data_request: bool = False

    @property
    def pii_masked(self) -> bool:
        return bool(self.pii_found)

    @property
    def refusal(self) -> bool:
        """Refuse when someone tries to override rules or pull restricted data."""
        return self.injection or bool(self.restricted_items)

    @property
    def reason(self) -> str:
        parts = []
        if self.injection:
            parts.append("prompt-injection attempt")
        if self.restricted_items:
            parts.append("request for restricted data (" + ", ".join(self.restricted_items) + ")")
        if self.payment_data_request:
            parts.append("payment data being collected outside POS")
        if self.pii_found:
            parts.append("personal/payment data masked (" + ", ".join(self.pii_found) + ")")
        return "; ".join(parts) or "no safety issue"


def _mask_email(match: re.Match[str]) -> str:
    local, domain = match.group(1), match.group(2)
    if len(local) <= 2:
        return f"{local[0]}*@{domain}"
    return f"{local[0]}{'*' * (len(local) - 2)}{local[-1]}@{domain}"


def mask_pii(text: str) -> tuple[str, list[str]]:
    """Mask personal and payment data. Returns the masked text and what was found."""
    found: list[str] = []

    def sub(pattern: re.Pattern[str], repl, label: str, value: str) -> str:  # noqa: ANN001
        new, n = pattern.subn(repl, value)
        if n:
            found.append(label)
        return new

    # Order matters: cards before Aadhaar before phone, so long digit runs are claimed once.
    text = sub(_CARD, "[REDACTED_CARD]", "card number", text)
    text = sub(_SECRET, lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", "secret/PIN/OTP", text)
    text = sub(_BANK_ACCOUNT, lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", "bank account", text)
    text = sub(_AADHAAR, "[REDACTED]", "government ID", text)
    text = sub(_PAN, "[REDACTED]", "government ID", text)
    text = sub(_PHONE, lambda m: "******" + m.group(1)[-4:], "phone", text)
    text = sub(_EMAIL, _mask_email, "email", text)
    return text, sorted(set(found))


class SafetyAgent:
    """First agent in the pipeline. Never calls the LLM."""

    name = "Safety agent"

    def run(self, message: str) -> SafetyResult:
        masked, found = mask_pii(message)
        lowered = message.lower()
        injection = any(re.search(p, lowered) for p in _INJECTION)

        # Restricted only when the data is being asked for ("show ... card numbers"),
        # not when a customer's details are merely mentioned in the issue.
        restricted = [
            name
            for name, p in _RESTRICTED.items()
            if re.search(_REQUEST_VERB + r"[^.?!]{0,60}?(?:" + p + ")", lowered)
        ]

        return SafetyResult(
            masked_text=masked,
            pii_found=found,
            injection=injection,
            restricted_items=restricted,
            payment_data_request=bool(_PAYMENT_SOLICIT.search(message)),
        )
