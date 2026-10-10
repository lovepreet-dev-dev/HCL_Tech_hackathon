"""Normalise how store staff actually type, before the rules read it.

Three cheap, deterministic fixes:

- **Typos** in store words: a word close to a known product or domain word is
  corrected ("treadmil" -> "treadmill", "recipt" -> "receipt").
- **Hinglish**: common words become English ("saare counters" -> "all counters",
  "band hai" -> "not working", "shelf khali hai" -> "shelf empty").
- **Idioms** the rules do not list ("run dry", "stock finished", "shelf empty").

The normalised text is only what the rules and matchers read. Tickets, the answer
and the trace keep the staff member's own (masked) words.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Iterable

# Applied in order, as whole words; longer phrases first.
_PHRASES: tuple[tuple[str, str], ...] = (
    # Hinglish
    (r"nahi ho raha|nahin ho raha|nahi chal raha|nahin chal raha|kaam nahi kar raha|band (?:hai|hain|ho gaya|ho gaye)", "not working"),
    (r"paisa wapas|paise wapas|paisa vapas|paise vapas|refund chahiye", "refund"),
    (r"chot (?:lagi|lag gayi|aayi)|zakhmi|ghayal|chot", "injured"),
    (r"khatam(?: ho gaya| ho gaye| hai)?", "out of stock"),
    (r"khali(?: hai| hain)?", "empty"),
    (r"toot gaya|toot gayi|tut gaya|toota|tuta", "broke"),
    (r"kharab(?: hai| ho gaya)?", "broken"),
    (r"saare|saari|sare|sabhi|sab", "all"),
    (r"nahi|nahin", "not"),
    (r"dukaan|dukan", "store"),
    (r"grahak", "customer"),
    # Idioms
    (r"(?:run|ran|running|gone) (?:dry|out)(?: of)?", "out of stock of"),
    (r"stock (?:has )?(?:finished|over|exhausted)|(?:finished|exhausted) stock", "out of stock"),
    (r"(?:shelf|shelves|rack) (?:is |are )?empty|empty (?:shelf|shelves|rack)", "out of stock on the shelf"),
    (r"came of", "came off"),
)
_COMPILED = [(re.compile(rf"\b(?:{p})\b", re.IGNORECASE), r) for p, r in _PHRASES]

# Store words worth correcting. Product-name words are added from product_master.csv.
DOMAIN_WORDS = (
    "treadmill register registers receipt refund refunds injured injury loosened payment payments terminal "
    "reader shipment shipments delivery delivered vendor supplier warehouse inventory discount promotion "
    "defective cracked broken customer customers cricket football badminton shuttlecock dumbbell exercise "
    "helmet racquet stitching quality replenish transfer invoice approval manager counter counters machine "
    "sparking collapsed snapped malfunction warranty exchange damaged shortage available unavailable "
    "electric shock bleeding stock-out label shelf"
).split()

_WORD = re.compile(r"[A-Za-z]{6,}")


class Normalizer:
    def __init__(self, extra_words: Iterable[str] = ()) -> None:
        words = {w.lower() for w in DOMAIN_WORDS}
        words |= {w.lower() for w in extra_words if len(w) >= 5 and w.isalpha()}
        self.vocabulary = sorted(words)
        self._known = set(self.vocabulary)

    def _fix_typo(self, match: re.Match[str]) -> str:
        word = match.group(0)
        low = word.lower()
        if low in self._known:
            return word
        close = difflib.get_close_matches(low, self.vocabulary, n=1, cutoff=0.88)
        # Same first letter: typos rarely change it, and it blocks most wrong "corrections".
        return close[0] if close and close[0][0] == low[0] else word

    def __call__(self, text: str) -> str:
        out = _WORD.sub(self._fix_typo, text)
        for pattern, replacement in _COMPILED:
            out = pattern.sub(replacement, out)
        return out
