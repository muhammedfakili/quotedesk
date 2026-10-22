"""Render eval/cases.py into real emails: .eml files with PDF / Excel / Word / scan / photo attachments.

    uv run python -m eval.make_testset

Writes eval/testset/emails/*.eml and eval/testset/ground_truth.json (catalogue names
resolved to SKUs). Deterministic: same seed, same files. Scans and photos are made
from the rendered page with rotation, noise, blur, perspective and uneven light;
the handwritten lists use the Ink Free font.
"""

from __future__ import annotations

import io
import json
import random
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import numpy as np
from docx import Document as DocxDocument
from fpdf import FPDF
from openpyxl import Workbook
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from app.catalogue.index import load_products
from app.config import PROJECT_ROOT
from app.ports import OutgoingEmail, build_eml
from app.pricing import find_customer
from app.schema import Attachment
from eval.cases import CASES, Case, L

OUT = PROJECT_ROOT / "eval" / "testset"
FONTS = {"sans": "arial.ttf", "bold": "arialbd.ttf", "hand": "Inkfree.ttf"}
TO = "quotes@northgate-supply.example"
START = datetime(2026, 10, 19, 8, 12, tzinfo=UTC)


def _font(kind: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(FONTS[kind], size)


def _company(case: Case) -> str:
    return find_customer(case.sender[1], case.sender[0])["company"]


def _qty(line: L) -> str:
    return "" if line.qty is None else f"{line.qty:g}"


def _latin(text: str) -> str:
    return text.encode("latin-1", errors="replace").decode("latin-1")


# --- documents --------------------------------------------------------------------------------
def pdf_table(case: Case, prices: bool = False) -> bytes:
    pdf = FPDF(format="A4")
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 15)
    pdf.cell(0, 9, _latin(_company(case)), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    pdf.cell(0, 5, _latin(f"{case.sender[0]}  |  {case.sender[1]}"), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(6)
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 8, _latin(case.doc_title), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    if case.customer_ref:
        pdf.cell(0, 5, f"Our reference: {case.customer_ref}", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 5, "Date: 2026-10-19", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)
    widths = [12, 110, 22, 30] + ([20] if prices else [])
    head = ["No.", "Description", "Qty", "Unit"] + (["Price"] if prices else [])
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_fill_color(225, 228, 232)
    for w, h in zip(widths, head, strict=True):
        pdf.cell(w, 7, h, border=1, fill=True)
    pdf.ln(7)
    pdf.set_font("Helvetica", "", 9)
    for i, line in enumerate(case.lines, start=1):
        cells = [str(i), line.text, _qty(line), line.unit or ""] + (["4.10" if i % 2 else "2.35"] if prices else [])
        for w, c in zip(widths, cells, strict=True):
            pdf.cell(w, 7, _latin(c), border=1)
        pdf.ln(7)
    pdf.ln(6)
    pdf.set_font("Helvetica", "", 8)
    notes = ("Delivery: to site, please state lead times. Prices to be valid for 30 days."
             if not prices else "Payment as agreed. Please confirm delivery date.")
    pdf.multi_cell(0, 4.5, notes, new_x="LMARGIN", new_y="NEXT")
    buf = io.BytesIO()
    pdf.output(buf)
    return buf.getvalue()


def pdf_list(case: Case) -> bytes:
    pdf = FPDF(format="A4")
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 8, _latin(_company(case)), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    pdf.ln(4)
    pdf.multi_cell(0, 5.5, "Materials list - please quote:", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    for i, line in enumerate(case.lines, start=1):
        unit = f" {line.unit}" if line.unit else ""
        pdf.multi_cell(0, 6, _latin(f"{i}.  {line.text}  -  {_qty(line)}{unit}"), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)
    pdf.multi_cell(0, 5.5, _latin(f"Thanks, {case.sender[0]}"), new_x="LMARGIN", new_y="NEXT")
    buf = io.BytesIO()
    pdf.output(buf)
    return buf.getvalue()


def xlsx(case: Case) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "RFQ"
    ws.append([_company(case)])
    ws.append([f"Ref: {case.customer_ref}" if case.customer_ref else "Enquiry"])
    ws.append([])
    ws.append(["Pos", "Description", "Qty", "Unit", "Notes"])
    for i, line in enumerate(case.lines, start=1):
        ws.append([i, line.text, line.qty, line.unit or "", ""])
    ws.append([])
    ws.append(["", "Please quote incl. lead times. Delivery to site."])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def docx(case: Case) -> bytes:
    doc = DocxDocument()
    doc.add_heading(_company(case), level=1)
    doc.add_paragraph(f"{case.doc_title} - {case.sender[0]}")
    table = doc.add_table(rows=1, cols=4)
    table.style = "Table Grid"
    for cell, title in zip(table.rows[0].cells, ["Item", "Description", "Quantity", "UoM"], strict=True):
        cell.text = title
    for i, line in enumerate(case.lines, start=1):
        row = table.add_row().cells
        row[0].text, row[1].text, row[2].text, row[3].text = str(i), line.text, _qty(line), line.unit or ""
    doc.add_paragraph("Please include lead times.")
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _page_image(case: Case, rng: random.Random) -> Image.Image:
    """A printed list on a white page, as a camera or scanner would see it."""
    img = Image.new("RGB", (1240, 1754), "white")
    d = ImageDraw.Draw(img)
    d.text((90, 90), _company(case), font=_font("bold", 40), fill="black")
    d.text((90, 150), case.doc_title + (f"   Ref: {case.customer_ref}" if case.customer_ref else ""),
           font=_font("sans", 28), fill="black")
    y = 240
    d.text((90, y), "No.", font=_font("bold", 26), fill="black")
    d.text((180, y), "Description", font=_font("bold", 26), fill="black")
    d.text((860, y), "Qty", font=_font("bold", 26), fill="black")
    d.text((990, y), "Unit", font=_font("bold", 26), fill="black")
    d.line((90, y + 40, 1150, y + 40), fill="black", width=2)
    y += 60
    for i, line in enumerate(case.lines, start=1):
        d.text((95, y), str(i), font=_font("sans", 26), fill="black")
        d.text((180, y), line.text, font=_font("sans", 26), fill="black")
        d.text((860, y), _qty(line), font=_font("sans", 26), fill="black")
        d.text((990, y), line.unit or "", font=_font("sans", 26), fill="black")
        y += 52
    return img


def _handwritten(case: Case, rng: random.Random) -> Image.Image:
    img = Image.new("RGB", (1100, 1400), (250, 248, 238))
    d = ImageDraw.Draw(img)
    for y in range(150, 1400, 64):
        d.line((40, y, 1060, y), fill=(170, 195, 225), width=2)
    d.line((120, 0, 120, 1400), fill=(225, 150, 150), width=2)
    d.text((140, 70), "for Northgate - pls price", font=_font("hand", 44), fill=(30, 40, 110))
    y = 166
    for line in case.lines:
        text = f"{line.text}  x {_qty(line)}" + (f" {line.unit}" if line.unit else "")
        d.text((140 + rng.randint(-6, 10), y - 50 + rng.randint(-4, 4)), text, font=_font("hand", 46),
               fill=(25, 35, 100))
        y += 128
    return img


def _camera(img: Image.Image, rng: random.Random, strength: float = 1.0) -> Image.Image:
    """Perspective, rotation, uneven light, blur and sensor noise."""
    w, h = img.size
    pad = int(60 * strength)
    canvas = Image.new("RGB", (w + 2 * pad, h + 2 * pad), (92, 84, 70))
    canvas.paste(img, (pad, pad))
    w2, h2 = canvas.size
    j = lambda: rng.randint(0, int(45 * strength))
    coeffs = _perspective_coeffs([(j(), j()), (w2 - j(), j()), (w2 - j(), h2 - j()), (j(), h2 - j())],
                                 [(0, 0), (w2, 0), (w2, h2), (0, h2)])
    canvas = canvas.transform((w2, h2), Image.Transform.PERSPECTIVE, coeffs, Image.Resampling.BICUBIC)
    canvas = canvas.rotate(rng.uniform(-3, 3) * strength, resample=Image.Resampling.BICUBIC, fillcolor=(92, 84, 70))
    arr = np.asarray(canvas).astype(np.float32)
    gradient = np.linspace(0.72, 1.05, arr.shape[1])[None, :, None] * np.linspace(1.0, 0.85, arr.shape[0])[:, None, None]
    arr = arr * gradient + np.random.default_rng(rng.randint(0, 10**6)).normal(0, 7, arr.shape)
    out = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
    return out.filter(ImageFilter.GaussianBlur(0.9 * strength))


def _perspective_coeffs(src: list, dst: list) -> list[float]:
    matrix = []
    for (x, y), (u, v) in zip(dst, src, strict=True):
        matrix.append([x, y, 1, 0, 0, 0, -u * x, -u * y])
        matrix.append([0, 0, 0, x, y, 1, -v * x, -v * y])
    a = np.array(matrix, dtype=np.float64)
    b = np.array([c for p in src for c in p], dtype=np.float64)
    return np.linalg.solve(a, b).tolist()


def _scan(img: Image.Image, rng: random.Random) -> bytes:
    """Grey, slightly rotated, speckled: an office scanner. Saved as an image-only PDF."""
    g = img.convert("L").rotate(rng.uniform(-1.5, 1.5), resample=Image.Resampling.BICUBIC, fillcolor=255)
    arr = np.asarray(g).astype(np.float32)
    arr = arr * 0.92 + 10 + np.random.default_rng(rng.randint(0, 10**6)).normal(0, 9, arr.shape)
    g = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(0.7))
    buf = io.BytesIO()
    g.save(buf, format="JPEG", quality=70)
    pdf = FPDF(format="A4")
    pdf.add_page()
    pdf.image(io.BytesIO(buf.getvalue()), x=0, y=0, w=210, h=297)
    out = io.BytesIO()
    pdf.output(out)
    return out.getvalue()


def _jpeg(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=82)
    return buf.getvalue()


def attachments_for(case: Case, rng: random.Random) -> list[Attachment]:
    slug = case.id
    match case.fmt:
        case "pdf_table":
            return [Attachment(f"RFQ_{slug}.pdf", "application/pdf", pdf_table(case))]
        case "po_pdf":
            return [Attachment(f"PO_{slug}.pdf", "application/pdf", pdf_table(case, prices=True))]
        case "pdf_list":
            return [Attachment(f"materials_{slug}.pdf", "application/pdf", pdf_list(case))]
        case "xlsx":
            return [Attachment(f"enquiry_{slug}.xlsx",
                               "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", xlsx(case))]
        case "docx":
            return [Attachment(f"request_{slug}.docx",
                               "application/vnd.openxmlformats-officedocument.wordprocessingml.document", docx(case))]
        case "scan":
            return [Attachment(f"scan_{slug}.pdf", "application/pdf", _scan(_page_image(case, rng), rng))]
        case "photo":
            return [Attachment(f"IMG_{4100 + int(slug[1:])}.jpg", "image/jpeg",
                               _jpeg(_camera(_page_image(case, rng), rng)))]
        case "handwritten":
            return [Attachment(f"IMG_{4200 + int(slug[1:])}.jpg", "image/jpeg",
                               _jpeg(_camera(_handwritten(case, rng), rng, 0.8)))]
    return []


def body_for(case: Case) -> str:
    if "{lines}" in case.body:
        return case.body.replace("{lines}", "\n".join(line.line() for line in case.lines))
    return case.body


def main() -> None:
    products = load_products()
    by_name = {p.name: p.sku for p in products}
    emails = OUT / "emails"
    emails.mkdir(parents=True, exist_ok=True)
    for old in emails.glob("*.eml"):
        old.unlink()
    truth = []
    for n, case in enumerate(CASES):
        rng = random.Random(1000 + n)
        message = OutgoingEmail(to=TO, sender=f"{case.sender[0]} <{case.sender[1]}>", subject=case.subject,
                                body=body_for(case), kind="request", attachments=attachments_for(case, rng))
        raw = build_eml(message)
        when = START + timedelta(hours=3 * n, minutes=7 * n)
        raw = _set_headers(raw, f"<{case.id}@testset.quotedesk>", format_datetime(when))
        path = emails / f"{case.id}.eml"
        path.write_bytes(raw)
        lines = []
        if case.email_class == "quote_request":
            for line in [*case.lines, *case.body_lines]:
                if line.sku is not None and line.sku not in by_name:
                    raise SystemExit(f"{case.id}: '{line.sku}' is not a catalogue name")
                lines.append({"text": line.text, "qty": line.qty, "unit": line.unit,
                              "sku": by_name.get(line.sku) if line.sku else None, "sku_name": line.sku,
                              "expect": line.expect})
        truth.append({"id": case.id, "file": f"emails/{case.id}.eml", "email_class": case.email_class,
                      "format": case.fmt, "tags": list(case.tags), "customer_ref": case.customer_ref,
                      "sender": case.sender[1], "lines": lines})
    (OUT / "ground_truth.json").write_text(json.dumps({"cases": truth}, indent=1, ensure_ascii=False),
                                           encoding="utf-8")
    n_lines = sum(len(c["lines"]) for c in truth)
    print(f"{len(truth)} emails, {n_lines} ground-truth lines -> {OUT}")


def _set_headers(raw: bytes, message_id: str, date: str) -> bytes:
    import email
    import email.policy

    msg = email.message_from_bytes(raw, policy=email.policy.default)
    del msg["Message-ID"]
    del msg["Date"]
    msg["Message-ID"] = message_id
    msg["Date"] = date
    return bytes(msg)


if __name__ == "__main__":
    main()
