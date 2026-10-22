"""Pricing: margins, the floor, units, currency, VAT, validity. Plain Python, unit tested.

No model ever produces a price. The rules live in fixtures/pricing.json:
  margin  = table[customer type][category] + payment-terms adjustment
            - volume break (line cost >= £500), never below the minimum margin
  price   = cost / (1 - margin)          (margin is gross margin on the selling price)
  in currency: x fx rate, rounded half-up to 2 decimals; line total = unit price x qty
A reviewer may override a line's margin. Below the floor that becomes an exception:
the quote cannot be approved until someone records a reason for it.

Units: customers ask in pieces, boxes, metres, feet, drums; we sell in our own units
(each, box of 100, 100 m drum, 3 m length). `convert_quantity` turns one into the other,
rounds up to whole sell units, and raises a flag when the two cannot be reconciled.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from functools import cache
from pathlib import Path

from app.catalogue.index import Catalogue, Product
from app.config import FIXTURES
from app.schema import Flag, Quote, QuoteLine

FEET_TO_M = 0.3048


@dataclass(frozen=True)
class Rules:
    margins: dict[str, dict[str, float]]
    terms_adjustment: dict[str, float]
    volume_cost_gbp: float
    volume_reduction: float
    minimum_margin: float
    fx: dict[str, float]
    fx_date: str
    vat_rate_gbp: float
    validity_days: int


@cache
def load_rules(path: str = str(FIXTURES / "pricing.json")) -> Rules:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return Rules(
        margins=raw["margin_by_customer_type"], terms_adjustment=raw["payment_terms_adjustment"],
        volume_cost_gbp=raw["volume_break"]["line_cost_gbp_at_least"],
        volume_reduction=raw["volume_break"]["margin_reduction"], minimum_margin=raw["minimum_margin"],
        fx=raw["fx_rates_per_gbp"], fx_date=raw["fx_rates_date"], vat_rate_gbp=raw["vat_rate_gbp"],
        validity_days=raw["quote_validity_days"],
    )


def money(value: float | Decimal) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


# --- customers ------------------------------------------------------------------------------
@cache
def _customers() -> tuple[dict, ...]:
    raw = json.loads((FIXTURES / "customers.json").read_text(encoding="utf-8"))
    return tuple(raw["customers"])


def find_customer(sender: str, sender_name: str = "") -> dict:
    """Known customer by email domain; anyone else is a new retail customer on prepaid terms."""
    domain = sender.rsplit("@", 1)[-1].lower()
    for c in _customers():
        if c["domain"] == domain:
            return {**c, "contact": sender_name, "email": sender, "known": True}
    return {"id": None, "company": sender_name or domain, "domain": domain, "type": "retail", "terms": "prepaid",
            "currency": "GBP", "contact": sender_name, "email": sender, "known": False}


# --- margin -------------------------------------------------------------------------------------
def margin_for(rules: Rules, customer_type: str, terms: str, category: str, line_cost_gbp: float) -> float:
    table = rules.margins.get(customer_type) or rules.margins.get("retail") or {}
    m = table.get(category, min(table.values(), default=rules.minimum_margin))
    m += rules.terms_adjustment.get(terms, 0.0)
    if line_cost_gbp >= rules.volume_cost_gbp:
        m -= rules.volume_reduction
    return round(max(m, rules.minimum_margin), 4)


def unit_price(cost_gbp: float, margin: float, fx: float) -> float:
    if not 0 <= margin < 1:
        raise ValueError(f"margin must be in [0, 1), got {margin}")
    return money(Decimal(str(cost_gbp)) / (Decimal(1) - Decimal(str(margin))) * Decimal(str(fx)))


# --- units --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class SellUnit:
    kind: str  # each | box | pack | m | drum | length | pair | roll | kg
    size: float | None  # base units per sell unit when the unit is a container


def parse_requested_unit(unit: str | None) -> SellUnit:
    """'boxes of 100' -> box/100, 'pcs' -> each, 'ft' -> ft, None -> each."""
    if not unit or not unit.strip():
        return SellUnit("none", None)
    u = unit.lower().strip().rstrip(".")
    size_m = re.search(r"(\d+(?:\.\d+)?)", u)
    size = float(size_m.group(1)) if size_m else None
    patterns = (
        ("each", (r"^(pcs?|pieces?|nos?|no|ea|each|units?|off|items?|x|qty|tubes?|cartridges?|tins?|tubs?|sets?"
                 r"|stk|stck|st|stuks?|stueck|stück)$")),
        ("box", r"\b(box|boxes|bx|bxs|carton|cartons|ctn)\b"),
        ("pack", r"\b(pack|packs|pk|pks|pkt|pkts|packets?|bags?|pak|pakken)\b"),
        ("drum", r"\b(drums?|reels?|coils?)\b"),
        ("length", r"\b(lengths?|lgths?|lgs?|bars?|sticks?)\b"),
        ("pair", r"\b(pairs?|prs?|pr)\b"),
        ("roll", r"\b(rolls?)\b"),
        ("kg", r"^(kg|kgs|kilos?)$"),
        ("ft", r"^(ft|feet|foot|')$"),
        ("m", r"^(m|mtrs?|metres?|meters?|lm|lin\.? ?m)$"),
    )
    for kind, pattern in patterns:
        if re.search(pattern, u):
            return SellUnit(kind, size if kind in ("box", "pack", "drum", "length") else None)
    return SellUnit("unknown", None)


def product_unit(product: Product) -> SellUnit:
    u = product.unit.lower()
    for kind in ("box", "pack", "drum", "length", "pair", "roll"):
        if u.startswith(kind):
            return SellUnit(kind, product.pack_qty)
    if u == "m":
        return SellUnit("m", 1)
    return SellUnit("each", 1)


@dataclass(frozen=True)
class Conversion:
    sell_qty: float | None
    note: str | None = None
    flag: Flag | None = None


def _whole(x: float) -> float:
    return float(math.ceil(round(x, 6)))


def convert_quantity(qty: float | None, unit: str | None, product: Product) -> Conversion:
    if qty is None or qty <= 0:
        return Conversion(None, flag=Flag(code="missing_quantity", message="No quantity stated."))
    req = parse_requested_unit(unit)
    sell = product_unit(product)
    container = sell.kind in ("box", "pack", "drum", "length")
    per = product.pack_qty  # base units in one sell unit
    sold_as = product.unit

    def from_base(base: float, what: str) -> Conversion:
        n = _whole(base / per)
        note = None
        if abs(n * per - base) > 1e-6:
            note = f"{what} requested; sold as {sold_as}, rounded up to {n:g}"
            return Conversion(n, note, Flag(code="rounded_up", message=note, blocking=False))
        if per != 1:
            note = f"{what} = {n:g} x {sold_as}"
        return Conversion(n, note)

    # Asked in the unit we sell in.
    if req.kind == sell.kind and (req.size is None or req.size == per):
        return Conversion(_whole(qty), None if qty == _whole(qty) else f"rounded up to {_whole(qty):g}")
    if req.kind in ("each", "none") and sell.kind in ("each", "pair", "roll"):
        return Conversion(_whole(qty))
    if req.kind == "length" and sell.kind == "each":  # "10 lengths" of 1 m studding: each length is one item
        return Conversion(_whole(qty))
    # Pieces (or no unit) against a box/pack of pieces.
    if req.kind in ("each", "none") and container and product.base_unit in ("each", "pair"):
        if req.kind == "none" and qty < per:
            note = f"no unit given; {qty:g} read as {qty:g} x {sold_as}"
            return Conversion(_whole(qty), note, Flag(code="unit_assumed", message=note))
        return from_base(qty, f"{qty:g} pcs")
    if req.kind == "pair" and product.base_unit == "pair":
        return from_base(qty, f"{qty:g} pairs")
    # A box/pack of a stated size, against our own boxes or single items.
    if req.kind in ("box", "pack") and req.size and product.base_unit == "each":
        return from_base(qty * req.size, f"{qty:g} x {req.kind} of {req.size:g}")
    if req.kind in ("box", "pack") and container and sell.kind in ("box", "pack") and req.size is None:
        return Conversion(_whole(qty), f"{qty:g} {req.kind}(es) read as {sold_as}")
    # Lengths of cable/tube in metres or feet.
    if req.kind in ("m", "ft") and product.base_unit == "m":
        metres = qty * (FEET_TO_M if req.kind == "ft" else 1)
        what = f"{qty:g} ft ({metres:.1f} m)" if req.kind == "ft" else f"{qty:g} m"
        return from_base(metres, what)
    if req.kind == "length" and req.size and product.base_unit == "m":
        return from_base(qty * req.size, f"{qty:g} x {req.size:g} m lengths")
    if req.kind == "none" and container and product.base_unit == "m":
        note = f"no unit given; {qty:g} read as {qty:g} x {sold_as}"
        return Conversion(_whole(qty), note, Flag(code="unit_assumed", message=note))
    if req.kind == "kg" and product.base_unit == "kg":
        return from_base(qty, f"{qty:g} kg")
    msg = f"asked for {qty:g} {unit or ''}".strip() + f", but this item is sold as {sold_as}"
    return Conversion(None, flag=Flag(code="unit_mismatch", message=msg))


# --- the whole quote ----------------------------------------------------------------------------------
def price_line(line: QuoteLine, product: Product, customer: dict, rules: Rules) -> QuoteLine:
    line.flags = [f for f in line.flags if f.code not in ("missing_quantity", "unit_mismatch", "unit_assumed",
                                                           "rounded_up", "below_floor", "out_of_stock")
                  or f.resolved]
    conv = convert_quantity(line.requested_qty, line.requested_unit, product)
    line.sell_unit = product.unit
    line.conversion_note = conv.note
    if conv.flag and not any(f.code == conv.flag.code for f in line.flags):
        line.flags.append(conv.flag)
    line.sell_qty = conv.sell_qty
    line.cost_gbp = product.cost_gbp
    if conv.sell_qty is None:
        line.margin = line.unit_price = line.line_total = None
        return line
    fx = rules.fx[customer["currency"]]
    auto = margin_for(rules, customer["type"], customer["terms"], product.category, product.cost_gbp * conv.sell_qty)
    line.margin = line.margin_override if line.margin_override is not None else auto
    if line.margin < rules.minimum_margin and not any(f.code == "below_floor" for f in line.flags):
        line.flags.append(Flag(code="below_floor", message=(
            f"margin {line.margin:.0%} is below the {rules.minimum_margin:.0%} floor: needs an approved exception")))
    line.unit_price = unit_price(product.cost_gbp, line.margin, fx)
    line.line_total = money(Decimal(str(line.unit_price)) * Decimal(str(conv.sell_qty)))
    if product.stock < conv.sell_qty and not any(f.code == "out_of_stock" for f in line.flags):
        line.flags.append(Flag(code="out_of_stock", blocking=False, message=(
            f"{product.stock:g} in stock, {conv.sell_qty:g} needed: lead time {product.lead_days} working days")))
    return line


def price_quote(quote: Quote, catalogue: Catalogue, rules: Rules | None = None, today: date | None = None) -> Quote:
    rules = rules or load_rules()
    quote.currency = quote.customer["currency"]
    for line in quote.lines:
        product = catalogue.get(line.sku) if line.sku else None
        if product is None or line.excluded:
            line.sell_qty = line.unit_price = line.line_total = line.margin = None
            continue
        price_line(line, product, quote.customer, rules)
    subtotal = sum((Decimal(str(line.line_total)) for line in quote.lines
                    if not line.excluded and line.line_total is not None), Decimal(0))
    quote.subtotal = money(subtotal)
    quote.vat = money(subtotal * Decimal(str(rules.vat_rate_gbp))) if quote.currency == "GBP" else 0.0
    quote.total = money(Decimal(str(quote.subtotal)) + Decimal(str(quote.vat)))
    issued = today or date.fromisoformat(quote.created_at[:10])
    quote.valid_until = (issued + timedelta(days=rules.validity_days)).isoformat()
    return quote


def check_totals(quote: Quote) -> list[str]:
    """Recompute every line and the totals from scratch; any difference is a bug, so block sending."""
    problems = []
    subtotal = Decimal(0)
    for line in quote.lines:
        if line.excluded or line.line_total is None:
            continue
        expected = money(Decimal(str(line.unit_price)) * Decimal(str(line.sell_qty)))
        if expected != line.line_total:
            problems.append(f"line {line.line_no}: total {line.line_total} != {expected}")
        subtotal += Decimal(str(line.line_total))
    if money(subtotal) != quote.subtotal:
        problems.append(f"subtotal {quote.subtotal} != {money(subtotal)}")
    if money(Decimal(str(quote.subtotal)) + Decimal(str(quote.vat))) != quote.total:
        problems.append("total != subtotal + VAT")
    return problems
