from dataclasses import replace

import pytest

from app.catalogue.index import Product
from app.pricing import (
    check_totals,
    convert_quantity,
    find_customer,
    load_rules,
    margin_for,
    parse_requested_unit,
    price_quote,
    unit_price,
)
from app.schema import Quote, QuoteLine

BOLT = Product("X-1", "HEX HD BOLT M10X50", "fasteners", "each", 1, "each", 0.50, 1000, 5, "S")
NUTS = Product("X-2", "NUT HEX M10", "fasteners", "box of 100", 100, "each", 8.00, 50, 5, "S")
CABLE = Product("X-3", "6242Y T&E 2.5MM2 100M", "electrical", "drum (100 m)", 100, "m", 78.0, 10, 5, "S")
FLEX = Product("X-4", "3183Y FLEX", "electrical", "m", 1, "m", 0.74, 1000, 5, "S")
BOOTS = Product("X-5", "SAFETY BOOT SZ 9", "ppe", "pair", 1, "pair", 21.0, 10, 5, "S")


def test_margin_table_terms_volume_and_floor():
    rules = load_rules()
    assert margin_for(rules, "trade", "net30", "fasteners", 10) == 0.35
    assert margin_for(rules, "trade", "prepaid", "fasteners", 10) == 0.33
    assert margin_for(rules, "oem", "net60", "electrical", 10) == 0.20
    assert margin_for(rules, "oem", "net30", "electrical", 600) == 0.15  # 0.18 - 0.03 volume break
    # never below the floor, whatever the rules add up to
    tight = replace(rules, margins={"oem": {"electrical": 0.10}})
    assert margin_for(tight, "oem", "prepaid", "electrical", 600) == rules.minimum_margin


def test_unit_price_rounds_half_up_and_converts_currency():
    assert unit_price(1.00, 0.30, 1.0) == 1.43  # 1.428571...
    assert unit_price(10.0, 0.20, 1.17) == 14.63  # 12.5 * 1.17 = 14.625 -> half up
    with pytest.raises(ValueError):
        unit_price(1.0, 1.0, 1.0)


@pytest.mark.parametrize(("qty", "unit", "product", "sell", "flag"), [
    (200, "pcs", BOLT, 200, None),
    (200, None, NUTS, 2, None),  # 200 pieces of nuts = 2 boxes
    (150, "pcs", NUTS, 2, "rounded_up"),
    (2, "boxes of 100", NUTS, 2, None),
    (3, "boxes", NUTS, 3, None),
    (5, None, NUTS, 5, "unit_assumed"),  # 5 with no unit on a box item: read as boxes, but ask
    (300, "m", CABLE, 3, None),
    (250, "m", CABLE, 3, "rounded_up"),
    (2, "drums", CABLE, 2, None),
    (150, "ft", FLEX, 46, "rounded_up"),  # 45.72 m
    (4, "pairs", BOOTS, 4, None),
    (10, "kg", BOLT, None, "unit_mismatch"),
    (None, None, BOLT, None, "missing_quantity"),
    (24, "Stk", BOLT, 24, None),
    (10, None, Product("X-7", "TAPE", "electrical", "roll", 1, "each", 0.5, 9, 5, "S"), 10, None),
    (10, "pak", Product("X-6", "TIES", "electrical", "pack of 100", 100, "each", 2.0, 9, 5, "S"), 10, None),
])
def test_convert_quantity(qty, unit, product, sell, flag):
    conv = convert_quantity(qty, unit, product)
    assert conv.sell_qty == sell
    assert (conv.flag.code if conv.flag else None) == flag


def test_parse_requested_unit():
    assert parse_requested_unit("boxes of 100").kind == "box"
    assert parse_requested_unit("boxes of 100").size == 100
    assert parse_requested_unit("stuks").kind == "each"
    assert parse_requested_unit("meter").kind == "m"
    assert parse_requested_unit("ft").kind == "ft"
    assert parse_requested_unit("bananas").kind == "unknown"


def _quote(customer, lines):
    return Quote(ref="Q-1", email_id="e", created_at="2026-10-22T09:00:00+00:00", customer=customer, lines=lines,
                 reply_to=customer["email"])


def test_price_quote_gbp_with_vat(catalogue):
    customer = find_customer("x@airevalleymech.example", "X")
    q = _quote(customer, [QuoteLine(line_no=1, description="bolt", requested_qty=100, requested_unit="pcs",
                                    sku="NG-10023"),
                          QuoteLine(line_no=2, description="bolt", requested_qty=200, requested_unit="pcs",
                                    sku="NG-10023")])
    price_quote(q, catalogue)
    line = q.lines[0]
    assert line.margin == 0.30  # contractor, fasteners, net30
    assert q.lines[1].margin == 0.27  # same, but the line costs over £500: volume break
    assert line.line_total == round(line.unit_price * 100, 2)
    assert q.vat == round(q.subtotal * 0.2, 2)
    assert q.total == round(q.subtotal + q.vat, 2)
    assert q.valid_until == "2026-11-21"
    assert check_totals(q) == []


def test_price_quote_eur_export_has_no_vat(catalogue):
    customer = find_customer("x@kessler-anlagenbau.example", "X")
    q = _quote(customer, [QuoteLine(line_no=1, description="bolt", requested_qty=10, sku="NG-10023")])
    price_quote(q, catalogue)
    assert q.currency == "EUR"
    assert q.vat == 0.0
    gbp = unit_price(catalogue.get("NG-10023").cost_gbp, q.lines[0].margin, 1.0)
    assert q.lines[0].unit_price > gbp  # converted at 1.17


def test_margin_override_below_floor_is_flagged(catalogue):
    customer = find_customer("x@airevalleymech.example", "X")
    q = _quote(customer, [QuoteLine(line_no=1, description="bolt", requested_qty=10, sku="NG-10023",
                                    margin_override=0.05)])
    price_quote(q, catalogue)
    assert q.lines[0].margin == 0.05
    assert any(f.code == "below_floor" and f.blocking for f in q.lines[0].flags)


def test_check_totals_catches_tampering(catalogue):
    customer = find_customer("x@airevalleymech.example", "X")
    q = _quote(customer, [QuoteLine(line_no=1, description="bolt", requested_qty=10, sku="NG-10023")])
    price_quote(q, catalogue)
    q.lines[0].line_total += 1
    assert check_totals(q)


def test_unknown_sender_is_new_retail_customer():
    c = find_customer("someone@gmail.example", "Someone")
    assert not c["known"]
    assert c["type"] == "retail"
    assert c["terms"] == "prepaid"
