"""HTTP API: the review screen (ui/) and any other client talk only to this.

    uv run uvicorn app.api:app --port 8000      docs at http://localhost:8000/docs

Endpoints that run the pipeline are plain `def` (FastAPI runs them in a thread pool):
an `async def` with blocking model calls would freeze the server on one request.
"""

from __future__ import annotations

import shutil
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from email.utils import make_msgid
from functools import cache
from typing import Annotated

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel

from app.agents import CrewRunner
from app.catalogue.index import get_catalogue
from app.catalogue_client import LocalCatalogue, McpCatalogue
from app.config import PROJECT_ROOT, settings
from app.documents import quote_pdf
from app.ports import FolderInbox, FolderOutbox, parse_eml
from app.schema import Attachment, InboundEmail
from app.service import LineEdit, QuoteService, ReviewError
from app.store import Store

SAMPLES = PROJECT_ROOT / "eval" / "testset" / "emails"


@cache
def get_service() -> QuoteService:
    return QuoteService(Store(settings.db_path), FolderInbox(settings.inbox_dir), FolderOutbox(settings.outbox_dir),
                        CrewRunner(), McpCatalogue(), get_catalogue())


@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    if get_service.cache_info().currsize:
        get_service().search.stop()  # the MCP server subprocess


app = FastAPI(title="QuoteDesk", version="0.1.0", lifespan=lifespan,
              description="Email quote requests -> drafted quotes -> human approval. Fictional distributor.")


def require_key(x_api_key: Annotated[str | None, Header()] = None) -> None:
    if settings.app_api_key and x_api_key != settings.app_api_key:
        raise HTTPException(401, "missing or wrong X-API-Key")


Auth = Annotated[None, Depends(require_key)]
Service = Annotated[QuoteService, Depends(get_service)]


def _quote_or_404(service: QuoteService, ref: str):
    try:
        return service.get(ref)
    except KeyError as exc:
        raise HTTPException(404, f"no quote {ref}") from exc


class EditBody(BaseModel):
    sku: str | None = None
    quantity: float | None = None
    unit: str | None = None
    margin_override: float | None = None
    clear_margin_override: bool = False
    excluded: bool | None = None
    resolve: list[str] | None = None
    reviewer: str = "reviewer"


class CoverBody(BaseModel):
    subject: str
    body: str
    reviewer: str = "reviewer"


class ApproveBody(BaseModel):
    approver: str
    exception_reason: str | None = None


class RejectBody(BaseModel):
    reviewer: str
    reason: str


class OutcomeBody(BaseModel):
    outcome: str
    note: str = ""


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "provider": settings.llm_provider, "model": settings.llm_model_resolved,
            "llm_key_set": bool(settings.api_key(settings.llm_provider))}


# --- inbox ------------------------------------------------------------------------------------
@app.post("/inbox/poll")
def poll_inbox(_: Auth, service: Service) -> list[dict]:
    """Process every new .eml in the inbox folder (or Gmail/IMAP, behind the same port)."""
    return service.poll_inbox()


@app.get("/inbox/samples")
def list_samples(_: Auth) -> list[str]:
    return sorted(p.stem for p in SAMPLES.glob("*.eml"))


@app.post("/inbox/samples")
def load_samples(_: Auth, ids: Annotated[list[str], Query()]) -> dict:
    """Copy sample emails from the test set into the demo inbox."""
    settings.inbox_dir.mkdir(parents=True, exist_ok=True)
    copied = []
    for sample_id in ids:
        src = SAMPLES / f"{sample_id}.eml"
        if not src.exists():
            raise HTTPException(404, f"no sample {sample_id}")
        shutil.copy(src, settings.inbox_dir / src.name)
        copied.append(sample_id)
    return {"copied": copied}


@app.post("/requests")
def submit_request(
    _: Auth, service: Service, sender: Annotated[str, Form()], subject: Annotated[str, Form()],
    body: Annotated[str, Form()] = "", sender_name: Annotated[str, Form()] = "",
    files: Annotated[list[UploadFile] | None, File()] = None,
) -> dict:
    """Run one request without an inbox: the same pipeline, for testing and integrations."""
    if "@" not in sender:
        raise HTTPException(422, "sender must be an email address")
    email = InboundEmail(
        id=make_msgid(domain="api.quotedesk"), sender=sender.lower(), sender_name=sender_name or sender,
        subject=subject, date=datetime.now(UTC).isoformat(), body=body, to=settings.company_email,
        attachments=[Attachment(f.filename or "attachment", f.content_type or "application/octet-stream",
                                f.file.read()) for f in files or []],
    )
    return service.process(email)


@app.get("/emails")
def list_emails(_: Auth, service: Service) -> list[dict]:
    return service.store.emails()


# --- quotes -------------------------------------------------------------------------------------
@app.get("/quotes")
def list_quotes(_: Auth, service: Service, status: str | None = None) -> list[dict]:
    return service.store.list_quotes(status)


@app.get("/quotes/{ref}")
def get_quote(_: Auth, service: Service, ref: str) -> dict:
    quote = _quote_or_404(service, ref)
    return {**quote.model_dump(), "open_flags": [f.model_dump() for f in quote.open_flags],
            "blockers": service.blockers(quote) if quote.status == "needs_review" else []}


@app.get("/quotes/{ref}/pdf")
def get_pdf(_: Auth, service: Service, ref: str, draft: bool = True) -> Response:
    quote = _quote_or_404(service, ref)
    data = quote_pdf(quote, draft=draft and quote.status == "needs_review")
    return Response(data, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{ref}.pdf"'})


@app.get("/quotes/{ref}/email")
def get_original_email(_: Auth, service: Service, ref: str) -> dict:
    quote = _quote_or_404(service, ref)
    raw = service.store.raw_email(quote.email_id)
    if raw is None:
        raise HTTPException(404, "original email not stored")
    msg = parse_eml(raw)
    return {"from": f"{msg.sender_name} ({msg.sender})" if msg.sender_name != msg.sender else msg.sender, "subject": msg.subject, "date": msg.date, "body": msg.body,
            "attachments": [{"index": i, "filename": a.filename, "content_type": a.content_type, "size": len(a.data)}
                            for i, a in enumerate(msg.attachments)]}


@app.get("/quotes/{ref}/attachments/{index}")
def get_attachment(_: Auth, service: Service, ref: str, index: int) -> Response:
    quote = _quote_or_404(service, ref)
    msg = parse_eml(service.store.raw_email(quote.email_id) or b"")
    if not 0 <= index < len(msg.attachments):
        raise HTTPException(404, "no such attachment")
    att = msg.attachments[index]
    return Response(att.data, media_type=att.content_type,
                    headers={"Content-Disposition": f'inline; filename="{att.filename}"'})


def _png(data: bytes, content_type: str) -> bytes:
    """First page of a PDF (or the image itself) as PNG, so the thin UI needs no PDF library."""
    import io

    import pypdfium2 as pdfium
    from PIL import Image

    if content_type == "application/pdf" or data[:4] == b"%PDF":
        img = pdfium.PdfDocument(data)[0].render(scale=1.6).to_pil()
    elif content_type.startswith("image/"):
        img = Image.open(io.BytesIO(data))
    else:
        raise HTTPException(415, "no preview for this file type")
    img.thumbnail((1400, 1800))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


@app.get("/quotes/{ref}/preview.png")
def quote_preview(_: Auth, service: Service, ref: str) -> Response:
    quote = _quote_or_404(service, ref)
    return Response(_png(quote_pdf(quote, draft=quote.status == "needs_review"), "application/pdf"),
                    media_type="image/png")


@app.get("/quotes/{ref}/attachments/{index}/preview.png")
def attachment_preview(_: Auth, service: Service, ref: str, index: int) -> Response:
    quote = _quote_or_404(service, ref)
    msg = parse_eml(service.store.raw_email(quote.email_id) or b"")
    if not 0 <= index < len(msg.attachments):
        raise HTTPException(404, "no such attachment")
    att = msg.attachments[index]
    return Response(_png(att.data, att.content_type), media_type="image/png")


@app.patch("/quotes/{ref}/lines/{line_no}")
def edit_line(_: Auth, service: Service, ref: str, line_no: int, body: EditBody) -> dict:
    _quote_or_404(service, ref)
    edit = LineEdit(**body.model_dump(exclude={"reviewer"}))
    try:
        return service.edit_line(ref, line_no, edit, body.reviewer).model_dump()
    except ReviewError as exc:
        raise HTTPException(409, str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.put("/quotes/{ref}/cover")
def edit_cover(_: Auth, service: Service, ref: str, body: CoverBody) -> dict:
    _quote_or_404(service, ref)
    try:
        return service.edit_cover(ref, body.subject, body.body, body.reviewer).model_dump()
    except ReviewError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/quotes/{ref}/flags/{code}/resolve")
def resolve_flag(_: Auth, service: Service, ref: str, code: str, reviewer: str = "reviewer") -> dict:
    _quote_or_404(service, ref)
    try:
        return service.resolve_quote_flag(ref, code, reviewer).model_dump()
    except ReviewError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/quotes/{ref}/approve")
def approve(_: Auth, service: Service, ref: str, body: ApproveBody) -> dict:
    _quote_or_404(service, ref)
    try:
        return service.approve(ref, body.approver, body.exception_reason)
    except ReviewError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/quotes/{ref}/reject")
def reject(_: Auth, service: Service, ref: str, body: RejectBody) -> dict:
    _quote_or_404(service, ref)
    try:
        return service.reject(ref, body.reviewer, body.reason).model_dump()
    except ReviewError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/quotes/{ref}/outcome")
def outcome(_: Auth, service: Service, ref: str, body: OutcomeBody) -> dict:
    _quote_or_404(service, ref)
    try:
        return service.set_outcome(ref, body.outcome, body.note).model_dump()
    except ReviewError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/quotes/{ref}/events")
def quote_events(_: Auth, service: Service, ref: str) -> list[dict]:
    return service.store.events(ref)


# --- catalogue, outbox, log ---------------------------------------------------------------------------
@app.get("/catalogue/search")
def catalogue_search(_: Auth, q: str, k: int = 8) -> list[dict]:
    return [c.model_dump() for c in LocalCatalogue().search(q, k=max(1, min(k, 20)))]


@app.get("/outbox")
def outbox(_: Auth, service: Service) -> list[dict]:
    out = []
    for path in reversed(FolderOutbox(settings.outbox_dir).list()):
        msg = parse_eml(path.read_bytes())
        out.append({"file": path.name, "to": msg.to, "subject": msg.subject, "body": msg.body,
                    "attachments": [a.filename for a in msg.attachments]})
    return out


@app.get("/events")
def events(_: Auth, service: Service, limit: int = 100) -> list[dict]:
    return service.store.events(limit=limit)
