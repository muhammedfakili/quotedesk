"""Turn an email attachment into what the model can read: text, page images, or both.

From InvoiceIQ, extended for the formats a quote request arrives in:
  digital PDF  -> text layer + page images      scanned PDF -> page images only
  photo        -> one image (EXIF rotation fixed, downsized)
  Excel        -> the sheet as tab-separated rows   Word -> paragraphs and tables
  .txt / .csv  -> text
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from pathlib import Path

import pdfplumber
import pypdfium2 as pdfium
from PIL import Image as PILImage
from PIL import ImageOps

from app.llm import Image

RENDER_DPI = 150
MAX_SIDE = 2000  # phone photos are 4000+ px; the model doesn't need that many
MAX_PAGES = 10
MAX_ROWS = 400
MIN_TEXT_PER_PAGE = 40  # less than this per page = no real text layer (a scan)

PDF_TYPES = {".pdf"}
IMAGE_TYPES = {".jpg", ".jpeg", ".png", ".webp"}
SHEET_TYPES = {".xlsx", ".xlsm"}
WORD_TYPES = {".docx"}
TEXT_TYPES = {".txt", ".csv", ".tsv"}
SUPPORTED = PDF_TYPES | IMAGE_TYPES | SHEET_TYPES | WORD_TYPES | TEXT_TYPES


class UnsupportedFile(ValueError):
    """Not a format we can read (or a broken file)."""


@dataclass
class Document:
    name: str
    kind: str  # pdf_text | pdf_scan | image | sheet | word | text
    text: str | None = None
    pages: list[Image] = field(default_factory=list)

    @property
    def needs_vision(self) -> bool:
        return self.text is None


def read_document(data: bytes, name: str) -> Document:
    suffix = Path(name).suffix.lower()
    if suffix in PDF_TYPES:
        return _read_pdf(data, name)
    if suffix in IMAGE_TYPES:
        return Document(name=name, kind="image", pages=[_normalise_image(data)])
    if suffix in SHEET_TYPES:
        return Document(name=name, kind="sheet", text=_read_sheet(data, name))
    if suffix in WORD_TYPES:
        return Document(name=name, kind="word", text=_read_word(data, name))
    if suffix in TEXT_TYPES:
        return Document(name=name, kind="text", text=data.decode("utf-8", errors="replace"))
    raise UnsupportedFile(f"Unsupported attachment type '{suffix}' ({name}).")


def _read_pdf(data: bytes, name: str) -> Document:
    try:
        pdf = pdfium.PdfDocument(data)
    except pdfium.PdfiumError as exc:
        raise UnsupportedFile(f"Could not open '{name}' as a PDF: {exc}") from exc
    n = min(len(pdf), MAX_PAGES)
    with pdfplumber.open(io.BytesIO(data)) as plumber:
        texts = [(page.extract_text() or "").strip() for page in plumber.pages[:n]]
    if sum(len(t) for t in texts) >= MIN_TEXT_PER_PAGE * n:
        text = "\n\n".join(f"--- page {i + 1} ---\n{t}" for i, t in enumerate(texts)) if n > 1 else texts[0]
        return Document(name=name, kind="pdf_text", text=text)
    pages = [_png(pdf[i].render(scale=RENDER_DPI / 72).to_pil()) for i in range(n)]
    return Document(name=name, kind="pdf_scan", pages=pages)


def _read_sheet(data: bytes, name: str) -> str:
    from openpyxl import load_workbook

    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:
        raise UnsupportedFile(f"Could not open '{name}' as a spreadsheet: {exc}") from exc
    out = []
    for ws in wb.worksheets:
        rows = []
        for row in ws.iter_rows(values_only=True, max_row=MAX_ROWS):
            cells = ["" if v is None else str(v) for v in row]
            if any(c.strip() for c in cells):
                rows.append("\t".join(cells).rstrip())
        if rows:
            out.append(f"--- sheet {ws.title} ---\n" + "\n".join(rows))
    return "\n\n".join(out)


def _read_word(data: bytes, name: str) -> str:
    import docx

    try:
        document = docx.Document(io.BytesIO(data))
    except Exception as exc:
        raise UnsupportedFile(f"Could not open '{name}' as a Word document: {exc}") from exc
    parts = [p.text for p in document.paragraphs if p.text.strip()]
    for table in document.tables:
        parts.extend("\t".join(cell.text.strip() for cell in row.cells) for row in table.rows)
    return "\n".join(parts)


def _normalise_image(data: bytes) -> Image:
    try:
        img = PILImage.open(io.BytesIO(data))
        img.load()
    except Exception as exc:
        raise UnsupportedFile(f"Could not read the image: {exc}") from exc
    img = ImageOps.exif_transpose(img).convert("RGB")  # phone photos store rotation in EXIF
    img.thumbnail((MAX_SIDE, MAX_SIDE))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)
    return Image(buf.getvalue(), "image/jpeg")


def _png(img: PILImage.Image) -> Image:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG", optimize=True)
    return Image(buf.getvalue(), "image/png")
