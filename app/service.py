"""The back office: inbox -> pipeline -> review -> approval -> outbox, with a log of every step.

Nothing is sent without a person. `approve` is the only path to the outbox, and it
refuses while any of these is true:
  * a blocking flag is unresolved (not in catalogue, low confidence, size conflict, ...);
  * a margin is below the floor and nobody recorded a reason for the exception;
  * recomputed line totals or the grand total differ from the stored ones;
  * the outgoing text or the PDF contains a cost price or supplier name, or a supplier
    enquiry contains the customer's name or a selling price;
  * the recipient is not the address the request came from.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import UTC, datetime

import pdfplumber

from app.agents import AgentRunner
from app.catalogue.index import Catalogue
from app.catalogue_client import CatalogueSearch
from app.config import settings
from app.documents import fallback_cover, quote_email, quote_pdf, supplier_enquiries
from app.flow import PipelineResult, run_pipeline
from app.ports import InboxPort, OutboxPort, build_eml
from app.pricing import check_totals, load_rules, price_quote
from app.safety import customer_secrets, find_leaks, mentions_money, supplier_secrets
from app.schema import InboundEmail, Quote
from app.store import Store


class ReviewError(ValueError):
    """The requested action is not allowed in the quote's current state."""


@dataclass
class LineEdit:
    sku: str | None = None
    quantity: float | None = None
    unit: str | None = None
    margin_override: float | None = None
    clear_margin_override: bool = False
    excluded: bool | None = None
    resolve: list[str] | None = None  # flag codes the reviewer has checked


def pdf_text(data: bytes) -> str:
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        return "\n".join(page.extract_text() or "" for page in pdf.pages)


def outgoing_leaks(quote: Quote, catalogue: Catalogue) -> list[str]:
    """Run the confidentiality checks on exactly what would be sent."""
    suppliers = {p.supplier for p in catalogue.products}
    message = quote_email(quote)
    customer_side = "\n".join([message.subject, message.body, pdf_text(message.attachments[0].data)])
    leaks = [f"customer email/PDF: {x}" for x in find_leaks(customer_side, customer_secrets(quote, suppliers))]
    for enquiry in supplier_enquiries(quote, catalogue):
        leaks += [f"supplier enquiry to {enquiry.to}: {x}"
                  for x in find_leaks(f"{enquiry.subject}\n{enquiry.body}", supplier_secrets(quote))]
    return leaks


class QuoteService:
    def __init__(self, store: Store, inbox: InboxPort, outbox: OutboxPort, runner: AgentRunner,
                 search: CatalogueSearch, catalogue: Catalogue) -> None:
        self.store, self.inbox, self.outbox = store, inbox, outbox
        self.runner, self.search, self.catalogue = runner, search, catalogue

    # --- intake --------------------------------------------------------------------------------
    def poll_inbox(self) -> list[dict]:
        results = []
        for email in self.inbox.fetch_new():
            if self.store.has_email(email.id):
                self.inbox.mark_processed(email.id)
                continue
            results.append(self.process(email))
            self.inbox.mark_processed(email.id)
        return results

    def process(self, email: InboundEmail, raw: bytes | None = None) -> dict:
        self.store.save_email(email.id, email.sender, email.subject, raw or email.raw or _raw(email))
        self.store.log("received", {"from": email.sender, "subject": email.subject}, email_id=email.id)
        now = datetime.now(UTC)
        ref = self.store.next_ref(now.year)
        result: PipelineResult = run_pipeline(email, self.runner, self.search, self.catalogue, ref, now)
        if result.error:
            self.store.update_email(email.id, result.email_class, None, result.error)
            self.store.log("pipeline_error", result.error, email_id=email.id)
            return {"email_id": email.id, "email_class": result.email_class, "error": result.error}
        if result.quote is None:
            self.store.update_email(email.id, result.email_class, None, None)
            self.store.log("not_a_quote", {"class": result.email_class,
                                           "reason": result.intake.reason if result.intake else None},
                           email_id=email.id)
            return {"email_id": email.id, "email_class": result.email_class, "ref": None}
        quote = result.quote
        self._save(quote)
        self.store.update_email(email.id, "quote_request", quote.ref, None)
        self.store.log("drafted", {"lines": len(quote.lines), "open_flags": len(quote.open_flags),
                                   "model_calls": result.model_calls, "seconds": round(result.seconds, 1),
                                   "timings": result.timings}, ref=quote.ref, email_id=email.id)
        return {"email_id": email.id, "email_class": "quote_request", "ref": quote.ref,
                "open_flags": len(quote.open_flags)}

    def _save(self, quote: Quote) -> None:
        self.store.save_quote(quote)
        settings.quotes_dir.mkdir(parents=True, exist_ok=True)
        (settings.quotes_dir / f"{quote.ref}-draft.pdf").write_bytes(quote_pdf(quote, draft=True))

    # --- review ----------------------------------------------------------------------------------
    def get(self, ref: str) -> Quote:
        quote = self.store.get_quote(ref)
        if quote is None:
            raise KeyError(ref)
        return quote

    def edit_line(self, ref: str, line_no: int, edit: LineEdit, reviewer: str = "reviewer") -> Quote:
        quote = self._editable(ref)
        line = next((x for x in quote.lines if x.line_no == line_no), None)
        if line is None:
            raise KeyError(f"{ref} line {line_no}")
        changes = {}
        if edit.sku is not None:
            product = self.catalogue.get(edit.sku)
            if product is None:
                raise ReviewError(f"unknown SKU {edit.sku}")
            line.sku, line.product_name = product.sku, product.name
            # Choosing the product yourself settles the matching doubts.
            for f in line.flags:
                if f.code in ("not_in_catalogue", "low_confidence", "spec_mismatch", "no_decision", "invented_sku",
                              "unclear_line"):
                    f.resolved = True
            changes["sku"] = product.sku
        if edit.quantity is not None:
            line.requested_qty = edit.quantity
            changes["quantity"] = edit.quantity
        if edit.unit is not None:
            line.requested_unit = edit.unit
            changes["unit"] = edit.unit
        if edit.clear_margin_override:
            line.margin_override = None
            changes["margin_override"] = None
        elif edit.margin_override is not None:
            if not 0 <= edit.margin_override < 0.9:
                raise ReviewError("margin must be between 0 and 0.9")
            line.margin_override = edit.margin_override
            changes["margin_override"] = edit.margin_override
        if edit.excluded is not None:
            line.excluded = edit.excluded
            changes["excluded"] = edit.excluded
        if edit.resolve:
            for f in line.flags:
                if f.code in edit.resolve and f.code != "below_floor":
                    f.resolved = True
            changes["resolved"] = edit.resolve
        price_quote(quote, self.catalogue, today=datetime.fromisoformat(quote.created_at).date())
        self._save(quote)
        self.store.log("line_edited", {"line": line_no, "by": reviewer, **changes}, ref=ref)
        return quote

    def edit_cover(self, ref: str, subject: str, body: str, reviewer: str = "reviewer") -> Quote:
        quote = self._editable(ref)
        if mentions_money(body) or mentions_money(subject):
            raise ReviewError("prices belong in the PDF, not in the email text")
        if quote.ref not in subject:
            raise ReviewError(f"the subject must contain the quote number {quote.ref}")
        quote.cover_subject, quote.cover_body, quote.cover_edited_by = subject, body, reviewer
        quote.cover_not_quoted = quote.not_quoted
        self._save(quote)
        self.store.log("cover_edited", {"by": reviewer}, ref=ref)
        return quote

    def resolve_quote_flag(self, ref: str, code: str, reviewer: str = "reviewer") -> Quote:
        quote = self._editable(ref)
        for f in quote.flags:
            if f.code == code:
                f.resolved = True
        self._save(quote)
        self.store.log("flag_resolved", {"flag": code, "by": reviewer}, ref=ref)
        return quote

    def blockers(self, quote: Quote, exception_reason: str | None = None) -> list[str]:
        """Everything that stops this quote from being sent, in words a reviewer can act on."""
        problems = []
        for f in quote.flags:
            if f.blocking and not f.resolved:
                problems.append(f"quote: {f.code} - {f.message}")
        for line in quote.lines:
            if line.excluded:
                continue
            for f in line.open_flags:
                if f.code == "below_floor" and exception_reason:
                    continue
                problems.append(f"line {line.line_no}: {f.code} - {f.message}")
            if line.sku and line.line_total is None and not line.open_flags:
                problems.append(f"line {line.line_no}: not priced")
        if not any(not line.excluded and line.line_total is not None for line in quote.lines):
            problems.append("nothing to quote: every line is excluded or unpriced")
        problems += [f"totals: {p}" for p in check_totals(quote)]
        if quote.reply_to.lower() == settings.company_email.lower() or "@" not in quote.reply_to:
            problems.append(f"recipient {quote.reply_to!r} is not a customer address")
        return problems

    def approve(self, ref: str, approver: str, exception_reason: str | None = None) -> dict:
        quote = self._editable(ref)
        rules = load_rules()
        below = [line for line in quote.lines if not line.excluded and line.margin is not None
                 and line.margin < rules.minimum_margin]
        if below and not (exception_reason and exception_reason.strip()):
            raise ReviewError(f"{len(below)} line(s) below the {rules.minimum_margin:.0%} margin floor: "
                              "an exception reason is required")
        problems = self.blockers(quote, exception_reason)
        if problems:
            raise ReviewError("cannot approve: " + " | ".join(problems))
        email = self.store.raw_email(quote.email_id)
        original_sender = _sender_of(email) if email else quote.reply_to
        if original_sender and original_sender != quote.reply_to.lower():
            raise ReviewError(f"recipient {quote.reply_to} differs from the original sender {original_sender}")
        leaks = outgoing_leaks(quote, self.catalogue)
        if leaks:
            self.store.log("blocked_leak", leaks, ref=ref)
            raise ReviewError("confidential data in outgoing documents: " + "; ".join(leaks))

        for line in below:
            for f in line.flags:
                if f.code == "below_floor":
                    f.resolved = True
            quote.exceptions.append(f"line {line.line_no}: margin {line.margin:.1%} approved by {approver}: "
                                    f"{exception_reason}")
        if quote.cover_edited_by is None and quote.not_quoted != quote.cover_not_quoted:
            # The review changed what we can quote; the drafted note would now say the wrong thing.
            quote.cover_subject, quote.cover_body = fallback_cover(quote)
            quote.cover_not_quoted = quote.not_quoted
            self.store.log("cover_regenerated", "lines changed in review; template cover note used", ref=ref)
        quote.approved_by = approver
        quote.status = "sent"
        message = quote_email(quote)
        where = self.outbox.send(message)
        enquiries = [self.outbox.send(e) for e in supplier_enquiries(quote, self.catalogue)]
        settings.quotes_dir.mkdir(parents=True, exist_ok=True)
        (settings.quotes_dir / f"{quote.ref}.pdf").write_bytes(message.attachments[0].data)
        self.store.save_quote(quote)
        self.store.log("approved_and_sent", {"by": approver, "to": quote.reply_to, "total": quote.total,
                                             "currency": quote.currency, "outbox": where,
                                             "supplier_enquiries": len(enquiries), "exceptions": quote.exceptions},
                       ref=ref)
        return {"ref": ref, "status": quote.status, "outbox": where, "supplier_enquiries": enquiries}

    def reject(self, ref: str, reviewer: str, reason: str) -> Quote:
        quote = self._editable(ref)
        quote.status = "rejected"
        self.store.save_quote(quote)
        self.store.log("rejected", {"by": reviewer, "reason": reason}, ref=ref)
        return quote

    def set_outcome(self, ref: str, outcome: str, note: str = "") -> Quote:
        quote = self.get(ref)
        if outcome not in ("won", "lost"):
            raise ReviewError("outcome must be 'won' or 'lost'")
        if quote.status not in ("sent", "won", "lost"):
            raise ReviewError(f"only sent quotes can be won or lost (this one is {quote.status})")
        quote.status = outcome
        self.store.save_quote(quote)
        self.store.log(f"marked_{outcome}", {"note": note}, ref=ref)
        return quote

    def _editable(self, ref: str) -> Quote:
        quote = self.get(ref)
        if quote.status != "needs_review":
            raise ReviewError(f"{ref} is {quote.status}; only quotes waiting for review can change")
        return quote


def _raw(email: InboundEmail) -> bytes:
    from email.utils import formataddr

    from app.ports import OutgoingEmail

    return build_eml(OutgoingEmail(to=email.to or settings.company_email, subject=email.subject, body=email.body,
                                   kind="inbound", sender=formataddr((email.sender_name, email.sender)),
                                   attachments=email.attachments))


def _sender_of(raw: bytes) -> str:
    from app.ports import parse_eml

    return parse_eml(raw).sender

