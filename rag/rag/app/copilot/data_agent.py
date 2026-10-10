"""Data agent: answers from the structured retail CSVs with pandas.

Every number in an answer comes from here, never from the model. Each tool
returns plain findings, the CSV files it read, and the policy section whose
rule it applied, so the composer can cite both the data and the policy.
"""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from app.utils.logging import get_logger

logger = get_logger(__name__)

WAREHOUSE_ID = "WH001"
OPEN_STATUSES = {"Open", "In Progress"}
QUALITY_WORDS = r"seam|split|crack|defect|quality|broke|broken|torn|warp|slipp|loosen|unsafe|injur|malfunction|lost air|missing pad"

PRODUCT_WORDS = {
    "football", "bat", "ball", "treadmill", "helmet", "glove", "stick", "racquet",
    "bike", "pad", "shoe", "shuttlecock", "net", "guard", "kit",
}

_STORE = re.compile(r"\b(ST\d{3}|WH\d{3})\b", re.IGNORECASE)
_STORE_NUM = re.compile(r"\bstore\s*(?:no\.?|number|#)?\s*(\d{1,3})\b", re.IGNORECASE)
_SKU = re.compile(r"\bSKU\s?-?(\d{4})\b", re.IGNORECASE)
_AMOUNT = re.compile(r"(?:INR|Rs\.?|₹)\s?([\d,]+(?:\.\d+)?)|([\d,]{3,})\s?(?:INR|rupees|rs\b)", re.IGNORECASE)
_REGISTER = re.compile(r"\b(?:register|reg|till|counter)[\s#-]*(\d{1,2})\b", re.IGNORECASE)
_NAME_STOPWORDS = {"size", "pack", "of", "the", "and", "for", "with"}


@dataclass
class Entities:
    store_ids: list[str] = field(default_factory=list)
    skus: list[str] = field(default_factory=list)
    product_names: list[str] = field(default_factory=list)
    amount_inr: float | None = None
    amounts: list[float] = field(default_factory=list)
    receipt_present: bool | None = None
    register_id: str | None = None
    window_hours: int | None = None
    priorities: list[str] = field(default_factory=list)

    @property
    def store_id(self) -> str | None:
        stores = [s for s in self.store_ids if not s.startswith("WH")]
        return stores[0] if stores else None

    @property
    def sku(self) -> str | None:
        return self.skus[0] if self.skus else None


@dataclass
class DataResult:
    findings: list[str] = field(default_factory=list)
    data_used: list[str] = field(default_factory=list)
    citations: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)
    tables: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    likely_cause: str | None = None
    tools: list[str] = field(default_factory=list)
    # Typed, JSON-ready blocks the console draws as bars, gauges, chains and tables.
    visuals: list[dict[str, Any]] = field(default_factory=list)

    def use(self, *files: str) -> None:
        for f in files:
            if f not in self.data_used:
                self.data_used.append(f)

    def cite(self, *citations: str) -> None:
        for c in citations:
            if c not in self.citations:
                self.citations.append(c)


def _tokens(text: str) -> set[str]:
    return {t.rstrip("s") for t in re.findall(r"[a-z]+", text.lower()) if t not in _NAME_STOPWORDS}


class RetailData:
    """All nine CSVs loaded once, plus the anchor timestamp."""

    def __init__(self, data_dir: str | Path, anchor_ts: str) -> None:
        d = Path(data_dir)
        self.anchor = pd.Timestamp(anchor_ts)
        self.cases = pd.read_csv(d / "support_cases.csv", parse_dates=["created_ts"])
        self.products = pd.read_csv(d / "product_master.csv")
        self.inventory = pd.read_csv(d / "inventory.csv", parse_dates=["last_count_date"])
        self.stores = pd.read_csv(d / "store_master.csv")
        self.employees = pd.read_csv(d / "employee_master.csv")
        self.receipts = pd.read_csv(d / "receipts.csv", parse_dates=["receipt_date"])
        self.vendors = pd.read_csv(d / "vendor_master.csv")
        self.returns = pd.read_csv(d / "returns_quality.csv", parse_dates=["created_ts"])
        self.escalation = pd.read_csv(d / "escalation_matrix.csv")
        for frame in (self.cases, self.returns):
            frame.fillna("", inplace=True)
        self.receipts["damage_notes"] = self.receipts["damage_notes"].fillna("None")
        # Product names tokenised once for fuzzy matching against free text.
        self._name_tokens = [
            (row.sku, row.name, _tokens(re.sub(r"\d+", "", row.name)))
            for row in self.products.itertuples()
        ]

    # ── Entity extraction ──────────────────────────────────────────────

    def match_products(self, text: str) -> list[tuple[str, str]]:
        """Find product names mentioned in free text, tolerating plurals and word order."""
        # An exact name (ignoring trailing model numbers) beats any fuzzy match.
        low = " " + re.sub(r"\s+", " ", text.lower()) + " "
        exact = [
            (sku, name) for sku, name, _ in self._name_tokens
            if f" {re.sub(r'\s*\d+$', '', name.lower())} " in low or f" {name.lower()} " in low
        ]
        if exact:
            exact.sort(key=lambda x: -len(x[1]))
            return exact[:3]
        words = _tokens(text)
        scored = []
        for sku, name, toks in self._name_tokens:
            hit = len(toks & words)
            if hit >= 2 and hit / len(toks) >= 0.6:
                scored.append((hit / len(toks), hit, -len(toks), sku, name))
        scored.sort(reverse=True)
        if not scored:
            return []
        # Every equally good candidate, core-range SKUs (lowest numbers) first.
        return sorted((s[3], s[4]) for s in scored if s[:3] == scored[0][:3])

    def extract_entities(self, text: str, store_hint: str | None = None) -> Entities:
        e = Entities()
        e.store_ids = list(dict.fromkeys(m.upper() for m in _STORE.findall(text)))
        for num in _STORE_NUM.findall(text):
            sid = f"ST{int(num):03d}"
            if sid not in e.store_ids:
                e.store_ids.append(sid)
        if store_hint and store_hint.upper() not in e.store_ids:
            e.store_ids.append(store_hint.upper())

        e.skus = [f"SKU{m}" for m in _SKU.findall(text)]
        matches = self.match_products(text)
        if e.store_id and len(matches) > 1:
            # Tie between similar names: prefer the SKU the mentioned store actually stocks.
            stocked = set(self.inventory[self.inventory.store_id == e.store_id].sku)
            matches.sort(key=lambda m: m[0] not in stocked)
        for sku, _name in matches[:3]:
            if sku not in e.skus:
                e.skus.append(sku)
        e.product_names = [self.product_name(s) for s in e.skus if self.product_name(s)]

        amounts = []
        for a, b in _AMOUNT.findall(text):
            raw = (a or b).replace(",", "")
            with contextlib.suppress(ValueError):
                amounts.append(float(raw))
        e.amounts = amounts
        e.amount_inr = max(amounts) if amounts else None

        low = text.lower()
        if re.search(r"\b(without|no|lost|missing)\b.{0,15}\b(receipt|bill|invoice)\b", low):
            e.receipt_present = False
        elif re.search(r"\bwith (the |a |their )?(receipt|bill|invoice)\b", low):
            e.receipt_present = True

        reg = _REGISTER.search(text)
        e.register_id = f"REG-{int(reg.group(1)):02d}" if reg else None

        if re.search(r"last 24 hours|past 24 hours|\btoday\b|last day", low):
            e.window_hours = 24
        elif re.search(r"last 7 days|this week|past week", low):
            e.window_hours = 24 * 7
        elif re.search(r"last 30 days|this month|past month|last month", low):
            e.window_hours = 24 * 30
        e.priorities = sorted(set(re.findall(r"\bP([1-4])\b", text.upper())))
        e.priorities = [f"P{p}" for p in e.priorities]
        if not e.priorities and re.search(r"high[- ]priority|urgent|critical", low):
            e.priorities = ["P1", "P2"]
        return e

    # ── Lookups ────────────────────────────────────────────────────────

    def product(self, sku: str) -> pd.Series | None:
        rows = self.products[self.products.sku == sku]
        return rows.iloc[0] if len(rows) else None

    def product_name(self, sku: str) -> str | None:
        p = self.product(sku)
        return None if p is None else str(p["name"])

    def is_high_demand(self, sku: str | None) -> bool:
        p = self.product(sku) if sku else None
        return p is not None and str(p["high_demand"]).lower() == "yes"

    def store(self, store_id: str) -> pd.Series | None:
        rows = self.stores[self.stores.store_id == store_id]
        return rows.iloc[0] if len(rows) else None

    def vendor(self, vendor_id: str) -> pd.Series | None:
        rows = self.vendors[self.vendors.vendor_id == vendor_id]
        return rows.iloc[0] if len(rows) else None

    def stock(self, store_id: str, sku: str) -> pd.Series | None:
        rows = self.inventory[(self.inventory.store_id == store_id) & (self.inventory.sku == sku)]
        return rows.iloc[0] if len(rows) else None

    def since(self, hours: int) -> pd.Timestamp:
        return self.anchor - pd.Timedelta(hours=hours)


class DataAgent:
    """Chooses and runs the CSV tools an issue needs."""

    name = "Data agent"

    def __init__(self, data: RetailData) -> None:
        self.data = data

    def run(self, text: str, category: str, entities: Entities, analytics: bool) -> DataResult:
        r = DataResult()
        low = text.lower()
        sku, store_id = entities.sku, entities.store_id

        if analytics and re.search(r"incident|case|ticket|unresolved|open issue", low) and not re.search(
            r"vendor|supplier", low
        ):
            self.unresolved_cases(r, entities)
        if re.search(r"vendor|supplier", low) and re.search(r"quality|complaint|defect|return", low):
            self.vendor_quality(r, entities.window_hours or 24 * 30)

        if category == "Inventory and Replenishment" and sku:
            self.stockout_root_cause(r, sku, store_id)
        elif category == "Vendor and Supply Chain" and (sku or store_id):
            self.short_shipments(r, sku, store_id)
        elif category == "Product Quality and Safety" and "vendor_quality" not in r.tools:
            self.quality_complaints(r, text, sku, entities.window_hours or 24 * 30)
        elif category == "Pricing and Promotions" and sku:
            self.price_check(r, sku, entities)
        elif category == "POS and Payments" and store_id:
            self.store_context(r, store_id)

        if analytics and not r.tools and "stock" in low:
            self.repeated_stockouts(r)
        return r

    # ── Tools ──────────────────────────────────────────────────────────

    def store_context(self, r: DataResult, store_id: str) -> None:
        s = self.data.store(store_id)
        if s is None:
            return
        r.tools.append("store_context")
        r.use("store_master.csv")
        r.facts["pos_registers"] = int(s.pos_registers)
        r.findings.append(
            f"{store_id} ({s.store_name}, {s.city}) has {int(s.pos_registers)} POS registers "
            f"and about {int(s.avg_daily_traffic)} customers a day."
        )
        r.visuals.append({
            "kind": "stats", "title": f"{store_id} · {s.store_name}",
            "items": [
                {"label": "POS registers", "value": int(s.pos_registers)},
                {"label": "Customers a day", "value": int(s.avg_daily_traffic)},
                {"label": "Region", "value": str(s.region)},
            ],
        })

    def stockout_root_cause(self, r: DataResult, sku: str, store_id: str | None) -> None:
        """Follows the check order in 03_inventory_replenishment_policy.md#root-cause-hints."""
        d = self.data
        r.tools.append("stockout_root_cause")
        r.use("product_master.csv", "inventory.csv")
        r.cite("03_inventory_replenishment_policy.md#root-cause-hints", "01_retail_operations_sop.md#analytics-rule")
        name = d.product_name(sku) or sku
        product = d.product(sku)
        high_demand = d.is_high_demand(sku)
        r.facts["high_demand"] = high_demand

        if store_id is None:
            inv = d.inventory[(d.inventory.sku == sku) & (d.inventory.store_id.str.startswith("ST"))]
            out = inv[inv.on_hand == 0]
            r.facts["stores_out"] = out.store_id.tolist()
            r.findings.append(
                f"{name} ({sku}) is at zero on-hand in {len(out)} of {len(inv)} stores"
                + (f": {', '.join(out.store_id)}." if len(out) else ".")
            )
            r.tables["stock_by_store"] = inv[["store_id", "on_hand", "system_qty", "reorder_point"]].to_dict("records")
            r.visuals.append({
                "kind": "table", "title": f"{name} by store",
                "columns": ["Store", "On hand", "System", "Reorder point"],
                "rows": [[x.store_id, int(x.on_hand), int(x.system_qty), int(x.reorder_point)]
                         for x in inv.sort_values("on_hand").head(12).itertuples()],
            })
            return

        causes: list[str] = []
        row = d.stock(store_id, sku)
        if row is None:
            r.findings.append(f"No inventory record for {name} ({sku}) at {store_id}.")
            return
        on_hand, system_qty, rop = int(row.on_hand), int(row.system_qty), int(row.reorder_point)
        r.facts.update(on_hand=on_hand, system_qty=system_qty, reorder_point=rop)
        r.findings.append(
            f"{store_id} {name} ({sku}): on_hand {on_hand}, system_qty {system_qty}, "
            f"reorder_point {rop}, last counted {row.last_count_date:%Y-%m-%d}."
            + (" High-demand item." if high_demand else "")
        )
        r.visuals.append({
            "kind": "stock", "title": f"{name} at {store_id}", "sku": sku, "store_id": store_id,
            "on_hand": on_hand, "system_qty": system_qty, "reorder_point": rop, "high_demand": high_demand,
        })
        if on_hand <= rop:
            r.facts["below_reorder"] = True
        gap = abs(system_qty - on_hand)
        if gap > 10 or (system_qty and gap / system_qty > 0.20):
            r.facts["discrepancy"] = True
            r.cite("03_inventory_replenishment_policy.md#inventory-discrepancy")
            r.findings.append(
                f"System shows {system_qty} but {on_hand} on hand: possible false availability; a cycle count is needed."
            )
            r.actions.append("Run a cycle count and correct the system quantity.")

        # Recent receipts at this store for this SKU.
        self._receipts_for(r, sku, store_id, causes)

        # Warehouse position.
        wh = d.stock(WAREHOUSE_ID, sku)
        if wh is not None:
            r.facts["warehouse_on_hand"] = int(wh.on_hand)
            r.findings.append(f"Warehouse {WAREHOUSE_ID} on_hand for {sku}: {int(wh.on_hand)}.")
            if int(wh.on_hand) == 0:
                wh_rcpt = d.receipts[(d.receipts.store_id == WAREHOUSE_ID) & (d.receipts.sku == sku)]
                note = ""
                if len(wh_rcpt):
                    r.use("receipts.csv")
                    last = wh_rcpt.sort_values("receipt_date").iloc[-1]
                    note = f" Last warehouse receipt {last.receipt_id}: {int(last.received_qty)} of {int(last.po_qty)} received."
                    r.facts["warehouse_last_receipt"] = f"{int(last.received_qty)} of {int(last.po_qty)} received"
                r.findings.append(f"Warehouse stock is depleted, so the store cannot be replenished from {WAREHOUSE_ID}.{note}")
                causes.insert(0, f"warehouse stock depleted ({WAREHOUSE_ID} at zero)")
                r.actions.append("Escalate warehouse replenishment for this SKU to the Inventory Lead.")

        # Vendor performance.
        if product is not None:
            self._vendor_signal(r, str(product.vendor_id), causes)

        # A count mismatch is a contributing cause, ranked after supply-side causes.
        if r.facts.get("discrepancy") and on_hand == 0:
            causes.append(f"system quantity mismatch (system shows {system_qty}, shelf has 0: false availability)")

        # Related cases.
        related = d.cases[(d.cases.sku == sku) & (d.cases.store_id == store_id)]
        if len(related):
            r.use("support_cases.csv")
            r.findings.append(
                f"{len(related)} related support case(s) for {sku} at {store_id}: "
                + "; ".join(f"{c.case_id} ({c.status}) {c.root_cause_hint or c.category_label}" for c in related.head(4).itertuples())
            )

        # Transfer option from the nearby store.
        s = d.store(store_id)
        if s is not None and on_hand == 0:
            r.use("store_master.csv")
            near = d.stock(str(s.nearby_store_id), sku)
            if near is not None and int(near.on_hand) > int(near.reorder_point):
                r.cite("03_inventory_replenishment_policy.md#transfer-and-reorder-recommendation")
                r.findings.append(
                    f"Nearby store {s.nearby_store_id} has {int(near.on_hand)} on hand (reorder point {int(near.reorder_point)})."
                )
                r.actions.append(f"Request a manager-approved transfer review from {s.nearby_store_id}.")

        if on_hand == 0 and high_demand:
            r.facts["stockout_high_demand"] = True
            r.cite("03_inventory_replenishment_policy.md#stock-outs-and-replenishment")
        r.likely_cause = causes[0] if causes else ("no data-backed cause found; needs review" if on_hand == 0 else None)
        if causes:
            r.findings.append("Likely root cause: " + "; ".join(causes) + ".")
        r.visuals.append(self._root_cause_chain(r, store_id, sku, on_hand, system_qty, rop, product))

    def _root_cause_chain(
        self, r: DataResult, store_id: str, sku: str, on_hand: int, system_qty: int, rop: int, product: Any,
    ) -> dict[str, Any]:
        """The policy's check order as a chain of states: bad, warn, ok or info."""
        f = r.facts
        steps = [{
            "label": f"{store_id} shelf",
            "value": f"{on_hand} on hand · system {system_qty}",
            "state": "bad" if on_hand == 0 else ("warn" if on_hand <= rop else "ok"),
        }, {
            "label": "Reorder point",
            "value": f"{rop} · {'below' if on_hand <= rop else 'above'}",
            "state": "warn" if on_hand <= rop else "ok",
        }]
        if "short_shipments" in f:
            steps.append({
                "label": "Store receipts",
                "value": f"{f['short_shipments']} short · lowest {f['worst_fill']:.0%} of PO",
                "state": "bad" if f["worst_fill"] < 0.9 else "warn",
            })
        elif "receipts" in r.tables:
            steps.append({"label": "Store receipts", "value": "delivered in full", "state": "ok"})
        else:
            steps.append({"label": "Store receipts", "value": "none in 30 days", "state": "info"})
        if "warehouse_on_hand" in f:
            wh = f["warehouse_on_hand"]
            last = f.get("warehouse_last_receipt")
            steps.append({"label": f"Warehouse {WAREHOUSE_ID}", "value": f"{wh} on hand" + (f" · last delivery {last}" if last else ""),
                          "state": "bad" if wh == 0 else "ok"})
        if product is not None and "vendor_warning" in f:
            v = self.data.vendor(str(product.vendor_id))
            steps.append({
                "label": f"Vendor {product.vendor_id}",
                "value": f"fill {float(v.fill_rate):.0%} · on time {float(v.on_time_rate):.0%}",
                "state": "warn" if f["vendor_warning"] else "ok",
            })
        if f.get("discrepancy"):
            steps.append({"label": "Count check", "value": f"system {system_qty} vs shelf {on_hand}", "state": "warn"})
        return {"kind": "chain", "title": "Root cause, in policy check order", "steps": steps,
                "conclusion": r.likely_cause or "No stock-out at this store."}

    def _receipts_for(self, r: DataResult, sku: str, store_id: str | None, causes: list[str]) -> None:
        d = self.data
        rc = d.receipts[d.receipts.sku == sku]
        if store_id:
            rc = rc[rc.store_id == store_id]
        rc = rc[rc.receipt_date >= d.since(24 * 30)].sort_values("receipt_date", ascending=False)
        if not len(rc):
            return
        r.use("receipts.csv")
        short = rc[rc.received_qty < rc.po_qty]
        lines = [
            f"{x.receipt_id} {x.receipt_date:%Y-%m-%d}: {int(x.received_qty)}/{int(x.po_qty)} received"
            f" ({x.received_qty / x.po_qty:.0%})" for x in rc.head(4).itertuples()
        ]
        r.findings.append("Recent receipts: " + "; ".join(lines) + ".")
        r.tables["receipts"] = rc.head(10).assign(receipt_date=rc.receipt_date.dt.strftime("%Y-%m-%d")).to_dict("records")
        r.visuals.append({
            "kind": "receipts", "title": "Recent deliveries" + (f" to {store_id}" if store_id else ""), "threshold": 0.9,
            "rows": [{"id": x.receipt_id, "date": f"{x.receipt_date:%Y-%m-%d}", "store_id": x.store_id,
                      "ordered": int(x.po_qty), "received": int(x.received_qty)} for x in rc.head(6).itertuples()],
        })
        if len(short):
            worst = float((short.received_qty / short.po_qty).min())
            r.facts["short_shipments"] = len(short)
            r.facts["worst_fill"] = round(worst, 2)
            r.cite("04_vendor_quality_policy.md#short-shipments")
            last_two = rc.head(2)
            consecutive = len(last_two) == 2 and bool((last_two.received_qty < last_two.po_qty).all())
            r.facts["consecutive_short"] = consecutive
            if worst < 0.9:
                causes.append(
                    f"short shipments ({len(short)} of the last {len(rc)} receipts short, lowest {worst:.0%} of PO)"
                )
            if consecutive:
                r.actions.append("Prepare a vendor claim review for Supply Chain (two consecutive short receipts).")

    def _vendor_signal(self, r: DataResult, vendor_id: str, causes: list[str]) -> None:
        v = self.data.vendor(vendor_id)
        if v is None:
            return
        r.use("vendor_master.csv")
        r.cite("04_vendor_quality_policy.md#vendor-performance")
        warn = float(v.fill_rate) < 0.80 or float(v.on_time_rate) < 0.85
        r.facts["vendor_warning"] = warn
        r.findings.append(
            f"Vendor {vendor_id} {v['name']}: fill_rate {float(v.fill_rate):.0%}, on_time_rate "
            f"{float(v.on_time_rate):.0%}, status {v.status}"
            + (" (below the 80% fill / 85% on-time warning level; a signal for review, not proof of fault)." if warn else ".")
        )
        r.visuals.append({
            "kind": "vendor", "title": f"Vendor {vendor_id} · {v['name']}", "status": str(v.status),
            "metrics": [
                {"label": "Fill rate", "value": float(v.fill_rate), "warning": 0.80},
                {"label": "On-time rate", "value": float(v.on_time_rate), "warning": 0.85},
            ],
        })
        if warn and causes and causes[-1].startswith("short shipments"):
            causes[-1] += f", likely linked to low vendor fill rate ({float(v.fill_rate):.0%})"

    def short_shipments(self, r: DataResult, sku: str | None, store_id: str | None) -> None:
        r.tools.append("short_shipments")
        causes: list[str] = []
        if sku:
            self._receipts_for(r, sku, store_id, causes)
            p = self.data.product(sku)
            if p is not None:
                r.use("product_master.csv")
                r.facts["high_demand"] = self.data.is_high_demand(sku)
                self._vendor_signal(r, str(p.vendor_id), causes)
        else:
            d = self.data
            rc = d.receipts[(d.receipts.store_id == store_id) & (d.receipts.received_qty < d.receipts.po_qty)]
            r.use("receipts.csv")
            r.findings.append(f"{len(rc)} short receipt(s) recorded at {store_id} in the data.")
        if causes:
            r.likely_cause = causes[0]

    def price_check(self, r: DataResult, sku: str, entities: Entities) -> None:
        p = self.data.product(sku)
        if p is None:
            return
        r.tools.append("price_check")
        r.use("product_master.csv")
        price = int(p.list_price)
        r.findings.append(f"Product master list price for {p['name']} ({sku}): INR {price}.")
        r.visuals.append({
            "kind": "price", "title": f"{p['name']} ({sku})", "list_price": price,
            "seen": sorted({int(a) for a in entities.amounts}),
        })
        others = [a for a in entities.amounts if int(a) != price]
        if price in {int(a) for a in entities.amounts} and others:
            r.findings.append(
                f"The INR {price} price matches the product master; the INR {int(others[0])} figure does not, "
                "so verify the shelf label and any promotion dates before adjusting."
            )

    def quality_complaints(self, r: DataResult, text: str, sku: str | None, hours: int) -> None:
        """Count recent quality complaints for the SKU, or for the words in the message."""
        d = self.data
        r.tools.append("quality_complaints")
        r.cite("01_retail_operations_sop.md#analytics-rule")
        since = d.since(hours)
        cases = d.cases[(d.cases.created_ts >= since) & (d.cases.created_ts <= d.anchor)]
        rets = d.returns[(d.returns.created_ts >= since) & (d.returns.created_ts <= d.anchor)]
        if sku:
            cases, rets = cases[cases.sku == sku], rets[rets.sku == sku]
            label = d.product_name(sku) or sku
        else:
            # No SKU: match on the product word and defect word the user used.
            items = [w.rstrip("s") for w in re.findall(r"[a-z]+", text.lower()) if w.rstrip("s") in PRODUCT_WORDS]
            pattern = "|".join(items) or QUALITY_WORDS
            label = " ".join(items) or "product"
            cases = cases[cases.issue_text.str.lower().str.contains(pattern) & cases.issue_text.str.lower().str.contains(QUALITY_WORDS)]
            rets = rets[(rets.reason + " " + rets.notes).str.lower().str.contains(pattern)
                        & (rets.reason + " " + rets.notes).str.lower().str.contains(QUALITY_WORDS)]
        cases = cases[cases.category_label == "Product Quality and Safety"]
        r.use("support_cases.csv", "returns_quality.csv")
        stores = sorted(set(cases.store_id) | set(rets.store_id))
        total = len(cases) + len(rets)
        r.facts.update(quality_complaints=total, stores_affected=len(stores))
        days = hours // 24
        r.findings.append(
            f"{total} {label} quality complaint(s) in the last {days} days "
            f"({len(cases)} support cases, {len(rets)} returns) across {len(stores)} store(s): {', '.join(stores) or 'none'}."
        )
        skus = sorted((set(cases.sku) | set(rets.sku)) - {""})
        qc = d.receipts[d.receipts.sku.isin(skus) & (d.receipts.quality_check_ok == "No")]
        if len(qc):
            r.use("receipts.csv")
            r.findings.append(
                "Receipts that failed quality check: "
                + "; ".join(f"{x.receipt_id} {x.store_id} ({x.damage_notes})" for x in qc.head(4).itertuples()) + "."
            )
        if total >= 2:
            r.cite("04_vendor_quality_policy.md#product-quality-complaints")
            r.actions.append("Place affected stock on quality hold and request a vendor/product review.")
        r.tables["quality_complaints"] = (
            [{"source": "support_cases", "id": c.case_id, "store_id": c.store_id, "sku": c.sku, "text": c.issue_text} for c in cases.head(10).itertuples()]
            + [{"source": "returns_quality", "id": x.return_id, "store_id": x.store_id, "sku": x.sku, "text": x.reason} for x in rets.head(10).itertuples()]
        )
        if r.tables["quality_complaints"]:
            r.visuals.append({
                "kind": "table", "title": f"Quality complaints, last {days} days",
                "columns": ["Source", "ID", "Store", "SKU", "Detail"],
                "rows": [["Case" if q["source"] == "support_cases" else "Return", q["id"], q["store_id"], q["sku"], q["text"]]
                         for q in r.tables["quality_complaints"]],
            })

    def vendor_quality(self, r: DataResult, hours: int) -> None:
        """Rank vendors by quality complaints in the window (support cases + returns)."""
        d = self.data
        r.tools.append("vendor_quality")
        r.use("support_cases.csv", "returns_quality.csv", "product_master.csv", "vendor_master.csv")
        r.cite("04_vendor_quality_policy.md#vendor-performance", "01_retail_operations_sop.md#analytics-rule")
        since = d.since(hours)
        cases = d.cases[(d.cases.created_ts >= since) & (d.cases.created_ts <= d.anchor)
                        & (d.cases.category_label == "Product Quality and Safety")][["sku", "store_id"]]
        rets = d.returns[(d.returns.created_ts >= since) & (d.returns.created_ts <= d.anchor)]
        rets = rets[(rets.reason + " " + rets.notes).str.lower().str.contains(QUALITY_WORDS)][["sku", "store_id"]]
        both = pd.concat([cases.assign(source="case"), rets.assign(source="return")])
        both = both.merge(d.products[["sku", "vendor_id"]], on="sku", how="left").dropna(subset=["vendor_id"])
        if both.empty:
            r.findings.append(f"No quality complaints linked to a vendor in the last {hours // 24} days.")
            return
        ranked = (
            both.groupby("vendor_id")
            .agg(complaints=("sku", "size"), skus=("sku", "nunique"), stores=("store_id", "nunique"))
            .reset_index()
            .merge(d.vendors[["vendor_id", "name", "fill_rate", "on_time_rate", "status"]], on="vendor_id")
            .sort_values(["complaints", "stores"], ascending=False)
        )
        top = ranked.iloc[0]
        r.facts.update(top_vendor=top.vendor_id, quality_complaints=int(top.complaints))
        r.findings.append(
            f"In the last {hours // 24} days (to {d.anchor:%Y-%m-%d %H:%M} IST), {top.vendor_id} {top['name']} is linked to the most "
            f"quality complaints: {int(top.complaints)} across {int(top.skus)} SKU(s) and {int(top.stores)} store(s) "
            f"(fill_rate {float(top.fill_rate):.0%}, status {top.status})."
        )
        others = ranked.iloc[1:4]
        if len(others):
            r.findings.append("Next: " + "; ".join(f"{x.vendor_id} {x.name} {int(x.complaints)}" for x in others.itertuples()) + ".")
        r.findings.append("This shows where to focus a review; complaint counts alone do not prove vendor fault.")
        r.actions.append(f"Request a product/vendor quality review for {top.vendor_id} with Product Quality.")
        r.tables["vendor_quality"] = ranked.to_dict("records")
        r.visuals.append({
            "kind": "ranking", "title": f"Quality complaints by vendor, last {hours // 24} days", "unit": "complaints",
            "rows": [{"label": f"{x.vendor_id} · {x.name}", "value": int(x.complaints),
                      "note": f"fill {float(x.fill_rate):.0%} · {x.status}"} for x in ranked.head(8).itertuples()],
        })

    def unresolved_cases(self, r: DataResult, e: Entities) -> None:
        d = self.data
        r.tools.append("unresolved_cases")
        r.use("support_cases.csv")
        r.cite("01_retail_operations_sop.md#analytics-rule")
        hours = e.window_hours or 24
        priorities = e.priorities or ["P1", "P2"]
        since = d.since(hours)
        df = d.cases[
            (d.cases.created_ts > since)
            & (d.cases.created_ts <= d.anchor)
            & d.cases.status.isin(OPEN_STATUSES)
            & d.cases.priority_label.isin(priorities)
        ]
        r.facts.update(unresolved=len(df), window_hours=hours)
        window = f"{since:%Y-%m-%d %H:%M} and {d.anchor:%Y-%m-%d %H:%M} IST"
        if df.empty:
            r.findings.append(f"No unresolved {'/'.join(priorities)} cases between {window}.")
            return
        teams = d.escalation.drop_duplicates(["category", "priority"]).set_index(["category", "priority"])["team"]
        df = df.assign(team=[teams.get((c, p), "") for c, p in zip(df.category_label, df.priority_label, strict=False)])
        r.findings.append(f"{len(df)} unresolved {'/'.join(priorities)} case(s) between {window}:")
        for store_id, g in df.sort_values(["store_id", "priority_label"]).groupby("store_id"):
            items = ", ".join(f"{x.case_id} {x.priority_label} {x.category_label} ({x.status}, {x.team})" for x in g.itertuples())
            r.findings.append(f"{store_id}: {items}")
        r.visuals.append({
            "kind": "table", "title": f"Unresolved {'/'.join(priorities)} cases · {window}",
            "columns": ["Case", "Store", "Priority", "Category", "Status", "Team"],
            "rows": [[x.case_id, x.store_id, x.priority_label, x.category_label, x.status, x.team]
                     for x in df.sort_values(["priority_label", "store_id"]).itertuples()],
        })
        r.tables["unresolved_cases"] = df[
            ["case_id", "created_ts", "store_id", "priority_label", "category_label", "status", "team", "issue_text"]
        ].assign(created_ts=df.created_ts.dt.strftime("%Y-%m-%d %H:%M")).to_dict("records")

    def repeated_stockouts(self, r: DataResult) -> None:
        d = self.data
        r.tools.append("repeated_stockouts")
        r.use("inventory.csv", "product_master.csv")
        r.cite("03_inventory_replenishment_policy.md#stock-outs-and-replenishment", "01_retail_operations_sop.md#analytics-rule")
        inv = d.inventory[(d.inventory.on_hand == 0) & d.inventory.store_id.str.startswith("ST")]
        inv = inv.merge(d.products[["sku", "name", "high_demand"]], on="sku")
        r.findings.append(f"{len(inv)} store/SKU combinations are at zero on hand ({(inv.high_demand == 'Yes').sum()} high-demand).")
        r.tables["stockouts"] = inv[["store_id", "sku", "name", "system_qty", "reorder_point", "high_demand"]].to_dict("records")
        r.visuals.append({
            "kind": "table", "title": "Store/SKU combinations at zero on hand",
            "columns": ["Store", "SKU", "Product", "System", "Reorder point", "High demand"],
            "rows": [[x.store_id, x.sku, x.name, int(x.system_qty), int(x.reorder_point), x.high_demand]
                     for x in inv.head(20).itertuples()],
        })

    # ── Dashboard (bonus B2) ───────────────────────────────────────────

    def dashboard(self) -> dict[str, Any]:
        d = self.data
        cases = d.cases.merge(d.stores[["store_id", "region"]], on="store_id", how="left")
        open_cases = cases[cases.status.isin(OPEN_STATUSES)]
        start = (d.anchor - pd.Timedelta(days=28)).normalize()
        week0 = start - pd.Timedelta(days=start.dayofweek)  # Monday, so the first week is complete
        return {
            "anchor": f"{d.anchor:%Y-%m-%d %H:%M} IST",
            "open_cases": int(len(open_cases)),
            "total_cases": int(len(cases)),
            "open_p1": int((open_cases.priority_label == "P1").sum()),
            "priority": open_cases.priority_label.value_counts().sort_index().to_dict(),
            "category": open_cases.category_label.value_counts().to_dict(),
            "region": open_cases.region.fillna("Unknown").value_counts().to_dict(),
            # Open P1/P2 raised in the 24 hours to the anchor, newest first: the regional manager's queue.
            "hot_cases": [
                {"case_id": x.case_id, "created": f"{x.created_ts:%d %b %H:%M}", "store_id": x.store_id,
                 "priority": x.priority_label, "category": x.category_label, "status": x.status, "issue": x.issue_text}
                for x in open_cases[(open_cases.created_ts > d.since(24)) & (open_cases.created_ts <= d.anchor)
                                    & open_cases.priority_label.isin(["P1", "P2"])]
                .sort_values(["priority_label", "created_ts"], ascending=[True, False]).head(10).itertuples()
            ],
            # Weekly new cases per region up to the anchor, for small-multiple trend charts.
            "region_by_week": (
                cases[(cases.created_ts >= week0) & (cases.created_ts <= d.anchor)]
                .assign(week=lambda x: (x.created_ts - pd.to_timedelta(x.created_ts.dt.dayofweek, unit="D")).dt.strftime("%Y-%m-%d"))
                .groupby(["week", "region"]).size().unstack(fill_value=0).to_dict("index")
            ),
        }
