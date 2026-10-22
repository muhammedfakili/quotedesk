"""The "is this safe to send" checks. Plain Python; they run on the final text and PDF.

1. Confidentiality, both directions:
   * customer documents (email + quote PDF) must not contain our cost prices, margins
     or supplier names;
   * supplier enquiries must not contain the customer's name, contact, address or
     domain, or any of our selling prices.
   `find_leaks` searches the actual outgoing text (the PDF is read back with pdfplumber),
   so a leak through any path (model prose, template, a reviewer's edit) is caught.
2. The recipient of a quote is the address the request came from. Addresses written in
   the email body are never used, so "send the quote to x@y" in an email changes nothing.
3. Prompt-injection attempts are flagged for the reviewer (they cannot change prices
   anyway, but a person should know someone tried).
4. The cover note a model wrote must not mention money; otherwise a template replaces it.
"""

from __future__ import annotations

import re

from app.schema import Quote

_INJECTION = re.compile(
    r"(ignore (all |any )?(previous|prior|above|earlier) (instructions|rules|prompts?)"
    r"|disregard (the |your )?(previous|prior|system|above)"
    r"|system prompt|you are now|new instructions|developer mode|as an ai"
    r"|(apply|give|grant) (a |an )?\d{2,3}\s?% (discount|off)"
    r"|send (the |this )?(quote|quotation|pdf) to"
    r"|reveal (your )?(cost|margin|prompt))",
    re.IGNORECASE,
)
_MONEY = re.compile(r"([£$€]\s?\d|\d[\d,]*\.\d{2}\b|\b\d+(\.\d+)?\s?(gbp|eur|usd|pounds|euros|dollars)\b"
                    r"|\b\d{1,2}\s?% (discount|off|margin))", re.IGNORECASE)


def injection_signals(text: str) -> list[str]:
    return sorted({m.group(0) for m in _INJECTION.finditer(text)})


def mentions_money(text: str) -> bool:
    return bool(_MONEY.search(text))


def _amount_patterns(value: float) -> list[str]:
    """How an amount can appear in text: 1234.50, 1,234.50."""
    plain = f"{value:.2f}"
    grouped = f"{value:,.2f}"
    return sorted({plain, grouped})


def customer_secrets(quote: Quote, supplier_names: set[str]) -> dict[str, list[str]]:
    """What must never appear in documents that go to the customer."""
    costs = {p for line in quote.lines if line.cost_gbp for p in _amount_patterns(line.cost_gbp)}
    # A cost that happens to equal a selling price (or a qty) can't be told apart; skip those.
    public = {p for line in quote.lines for v in (line.unit_price, line.line_total) if v for p in _amount_patterns(v)}
    return {"cost price": sorted(costs - public), "supplier": sorted(supplier_names)}


def supplier_secrets(quote: Quote) -> dict[str, list[str]]:
    """What must never appear in an enquiry to a supplier."""
    c = quote.customer
    names = [v for v in (c.get("company"), c.get("contact"), c.get("email"), c.get("domain")) if v]
    prices = {p for line in quote.lines for v in (line.unit_price, line.line_total) if v
              for p in _amount_patterns(v)}
    prices |= {p for v in (quote.subtotal, quote.total) if v for p in _amount_patterns(v)}
    return {"customer": names, "selling price": sorted(prices)}


def find_leaks(text: str, secrets: dict[str, list[str]]) -> list[str]:
    found = []
    lowered = text.lower()
    for kind, values in secrets.items():
        for value in values:
            if not value:
                continue
            if kind in ("cost price", "selling price"):
                hit = re.search(rf"(?<![\d.,]){re.escape(value)}(?![\d])", text)
            else:
                hit = value.lower() in lowered
            if hit:
                found.append(f"{kind}: {value}")
    return found


def margin_mentions(text: str) -> list[str]:
    return re.findall(r"\bmargin\b[^.\n]{0,40}", text, re.IGNORECASE)
