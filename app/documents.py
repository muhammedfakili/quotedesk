"""What leaves the building: the quote PDF, the reply email, supplier enquiries.

All three are rendered by code from the stored quote. The only model-written text is
the cover note's prose, which is checked for money before use (app/safety.py).
Supplier enquiries are pure templates: item, size, quantity, our reference. They
have no field for a customer or a selling price, and the leak check confirms it.
"""

from __future__ import annotations

import io
import re

from fpdf import FPDF

from app.catalogue.index import Catalogue
from app.config import settings
from app.ports import OutgoingEmail
from app.pricing import load_rules
from app.schema import Attachment, Quote

COMPANY = "Northgate Industrial Supply Ltd"
ADDRESS = "Unit 7, Calder Park Trading Estate, Wakefield WF2 7XX, United Kingdom"
CONTACT = "Tel 01924 000 100  |  quotes@northgate-supply.example  |  VAT GB 000 0000 00"
TERMS = {"prepaid": "Payment in advance", "net30": "30 days from invoice", "net60": "60 days from invoice"}
ACCENT = (232, 119, 34)
INK = (28, 31, 36)
MUTED = (110, 116, 125)


def _latin1(text: str) -> str:
    """fpdf's core fonts are Latin-1; replace what they cannot draw."""
    text = text.replace("€", "EUR ").replace("–", "-").replace("—", "-").replace("’", "'").replace("“", '"')
    text = text.replace("”", '"').replace("²", "2").replace("×", "x")
    return text.encode("latin-1", errors="replace").decode("latin-1")


def _amount(value: float | None) -> str:
    return "" if value is None else f"{value:,.2f}"


class _QuotePDF(FPDF):
    def __init__(self, ref: str) -> None:
        super().__init__(format="A4")
        self.ref = ref
        self.set_auto_page_break(auto=True, margin=22)
        self.set_margins(16, 16, 16)

    def header(self) -> None:
        # Logo: an orange square with a white N, drawn so the repo needs no image files.
        self.set_fill_color(*ACCENT)
        self.rect(16, 13, 13, 13, style="F")
        self.set_xy(16, 14.2)
        self.set_font("Helvetica", "B", 17)
        self.set_text_color(255, 255, 255)
        self.cell(13, 10, "N", align="C")
        self.set_xy(32, 13)
        self.set_text_color(*INK)
        self.set_font("Helvetica", "B", 13)
        self.cell(0, 6, COMPANY.upper())
        self.set_xy(32, 19.5)
        self.set_font("Helvetica", "", 7.5)
        self.set_text_color(*MUTED)
        self.cell(0, 4, ADDRESS)
        self.set_xy(32, 23.5)
        self.cell(0, 4, CONTACT)
        self.set_draw_color(*ACCENT)
        self.set_line_width(0.6)
        self.line(16, 30, 194, 30)
        self.set_y(36)

    def footer(self) -> None:
        self.set_y(-15)
        self.set_font("Helvetica", "", 7.5)
        self.set_text_color(*MUTED)
        self.cell(0, 5, f"Quotation {self.ref}  |  page {self.page_no()}/{{nb}}  |  "
                        "Fictional company - portfolio demo", align="C")


def quote_pdf(quote: Quote, draft: bool = False) -> bytes:
    rules = load_rules()
    c = quote.customer
    pdf = _QuotePDF(quote.ref)
    pdf.alias_nb_pages()
    pdf.add_page()
    pdf.set_text_color(*INK)
    pdf.set_font("Helvetica", "B", 20)
    pdf.cell(100, 10, "QUOTATION" + (" (DRAFT)" if draft else ""))
    pdf.set_font("Helvetica", "", 9)
    meta = [("Quotation no.", quote.ref), ("Date", quote.created_at[:10]), ("Valid until", quote.valid_until),
            ("Your reference", quote.customer_reference or "-"), ("Currency", quote.currency)]
    y = pdf.get_y()
    for i, (k, v) in enumerate(meta):
        pdf.set_xy(130, y + i * 5)
        pdf.set_text_color(*MUTED)
        pdf.cell(28, 5, k)
        pdf.set_text_color(*INK)
        pdf.cell(36, 5, _latin1(str(v)), align="R")
    pdf.set_xy(16, y + 13)
    pdf.set_font("Helvetica", "B", 9)
    pdf.cell(0, 5, "Prepared for")
    pdf.ln(5)
    pdf.set_font("Helvetica", "", 9)
    for text in (c.get("company"), c.get("contact") if c.get("contact") != c.get("company") else None,
                 c.get("email")):
        if text:
            pdf.cell(0, 4.6, _latin1(text))
            pdf.ln(4.6)
    pdf.set_y(max(pdf.get_y(), y + 30) + 4)

    cols = [("#", 8, "C"), ("Item", 84, "L"), ("SKU", 20, "L"), ("Qty", 12, "R"), ("Unit", 20, "L"),
            ("Unit price", 17, "R"), ("Total", 17, "R")]
    pdf.set_fill_color(*INK)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 8)
    for title, width, align in cols:
        pdf.cell(width, 7, title, align=align, fill=True)
    pdf.ln(7)
    pdf.set_text_color(*INK)
    quoted = [line for line in quote.lines if not line.excluded and line.line_total is not None]
    for n, line in enumerate(quoted, start=1):
        item = _latin1(f"{line.product_name}\nYour item: {line.description}")
        row_top = pdf.get_y()
        if row_top > 255:
            pdf.add_page()
            row_top = pdf.get_y()
        fill = n % 2 == 0
        pdf.set_fill_color(246, 247, 249)
        pdf.set_font("Helvetica", "", 8)
        pdf.set_xy(16 + 8, row_top + 1)
        pdf.multi_cell(84, 4, item, fill=False)
        height = max(pdf.get_y() - row_top + 1, 9)
        if fill:
            pdf.rect(16, row_top, 178, height, style="F")
            pdf.set_xy(16 + 8, row_top + 1)
            pdf.multi_cell(84, 4, item)
        pdf.set_xy(16, row_top)
        values = [str(n), None, line.sku or "", f"{line.sell_qty:g}", _latin1(line.sell_unit or ""),
                  _amount(line.unit_price), _amount(line.line_total)]
        for (_title, width, align), value in zip(cols, values, strict=True):
            if value is None:
                pdf.set_x(pdf.get_x() + width)
                continue
            pdf.cell(width, height, value, align=align)
        pdf.set_y(row_top + height)
    pdf.ln(3)
    box_x = 130
    rows = [("Subtotal", quote.subtotal)]
    if quote.currency == "GBP":
        rows.append((f"VAT {rules.vat_rate_gbp:.0%}", quote.vat))
    rows.append((f"Total {quote.currency}", quote.total))
    for i, (label, value) in enumerate(rows):
        last = i == len(rows) - 1
        pdf.set_x(box_x)
        pdf.set_font("Helvetica", "B" if last else "", 10 if last else 9)
        pdf.cell(32, 6.5, label)
        pdf.cell(32, 6.5, _amount(value), align="R")
        pdf.ln(6.5)

    not_quoted = [line for line in quote.lines if line.excluded or line.line_total is None]
    notes = []
    if not_quoted:
        notes.append("Not included yet (we will come back to you on these): "
                     + "; ".join(line.description for line in not_quoted) + ".")
    slow = [line for line in quoted if any(f.code == "out_of_stock" for f in line.flags)]
    for line in slow:
        lead = next(f.message for f in line.flags if f.code == "out_of_stock").split("lead time ")[-1]
        notes.append(f"Line {quoted.index(line) + 1} ({line.sku}): lead time {lead}.")
    rounded = [line for line in quoted if line.conversion_note]
    for line in rounded:
        notes.append(f"Line {quoted.index(line) + 1}: {line.conversion_note}.")
    notes.append(f"Payment terms: {TERMS.get(c.get('terms', ''), c.get('terms', ''))}. Prices exclude delivery.")
    if quote.currency != "GBP":
        notes.append(f"Prices converted from GBP at our rate of {rules.fx_date}. Export: VAT not charged.")
    notes.append(f"This quotation is valid until {quote.valid_until}.")
    pdf.ln(4)
    pdf.set_font("Helvetica", "B", 9)
    pdf.cell(0, 5, "Notes")
    pdf.ln(5)
    pdf.set_font("Helvetica", "", 8.5)
    for note in notes:
        pdf.set_x(pdf.l_margin)
        pdf.multi_cell(0, 4.4, _latin1("- " + note), new_x="LMARGIN", new_y="NEXT")
    buf = io.BytesIO()
    pdf.output(buf)
    return buf.getvalue()


def fallback_cover(quote: Quote) -> tuple[str, str]:
    first = (quote.customer.get("contact") or "").split(" ")[0]
    not_quoted = [line.description for line in quote.lines if line.excluded or line.line_total is None]
    body = [f"Dear {first}," if first and "@" not in first else "Hello,", "",
            (f"Thank you for your enquiry. Please find our quotation {quote.ref} attached, "
            f"valid until {quote.valid_until}.")]
    if not_quoted:
        body += ["", "We could not quote the following items yet and will come back to you on them:"]
        body += [f"  - {d}" for d in not_quoted]
    body += ["", "Kind regards,", "Sales Desk, Northgate Industrial Supply"]
    return f"Quotation {quote.ref} - Northgate Industrial Supply", "\n".join(body)


def quote_email(quote: Quote) -> OutgoingEmail:
    return OutgoingEmail(
        to=quote.reply_to, sender=settings.company_email, subject=quote.cover_subject, body=quote.cover_body,
        kind="quote", in_reply_to=quote.email_id, quote_ref=quote.ref,
        attachments=[Attachment(f"{quote.ref}.pdf", "application/pdf", quote_pdf(quote))],
    )


def _supplier_address(supplier: str) -> str:
    return "purchasing@" + re.sub(r"[^a-z0-9]+", "-", supplier.lower()).strip("-") + ".example"


def supplier_enquiries(quote: Quote, catalogue: Catalogue) -> list[OutgoingEmail]:
    """Ask suppliers for price/availability of what we could not quote or do not have in stock.

    Only the item and the quantity go out; the reference is our internal RFQ number.
    """
    by_supplier: dict[str, list[str]] = {}
    category_supplier = {p.category: p.supplier for p in catalogue.products}
    for line in quote.lines:
        not_found = line.sku is None or any(f.code == "not_in_catalogue" for f in line.flags)
        short = any(f.code == "out_of_stock" for f in line.flags) and not line.excluded
        if not (not_found or short):
            continue
        product = catalogue.get(line.sku) if line.sku and not not_found else None
        supplier = product.supplier if product else category_supplier.get(line.category)
        if supplier is None:
            continue
        qty = f"{line.requested_qty:g} {line.requested_unit or 'pcs'}" if line.requested_qty else "qty to confirm"
        item = product.name if product else line.description + (f" ({line.specification})"
                                                                if line.specification else "")
        by_supplier.setdefault(supplier, []).append(f"  - {item}: {qty}")
    rfq = quote.ref.replace("Q-", "RFQ-")
    out = []
    for supplier, items in by_supplier.items():
        body = "\n".join([
            "Hello,", "",
            f"Please quote your best price and lead time for the following (our ref {rfq}):", "",
            *items, "",
            "Delivery to our Wakefield warehouse. Please reply to this email.", "",
            "Thank you,", "Purchasing, Northgate Industrial Supply",
        ])
        out.append(OutgoingEmail(to=_supplier_address(supplier), sender="purchasing@northgate-supply.example",
                                 subject=f"Price and availability request {rfq}", body=body,
                                 kind="supplier_enquiry", quote_ref=quote.ref))
    return out
