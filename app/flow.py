"""The quoting pipeline as a CrewAI Flow: models read and write, code decides.

    intake ──(router)── quote ──> read ─> extract ─> retrieve ─> match ─> decide ─> price ─> cover ─> done
                    └── skip ──> not_a_quote (logged, nothing drafted)

  intake    CrewAI agent classifies the email                          (model)
  read      attachments -> text / page images                          (code)
  extract   line items with a schema                                   (model)
  retrieve  top 5 catalogue candidates per line, over MCP              (code)
  match     CrewAI agent picks a SKU per line, may search again (MCP)  (model)
  decide    flags: not found, low confidence, size conflict, unclear   (code)
  price     margins, floor, units, currency, VAT                       (code)
  cover     CrewAI agent writes the email prose, checked for money     (model + code)

`run_pipeline` returns a Quote (or None for a non-quote email) and never sends anything:
a person approves every quote (app/service.py).

The baseline in the evaluation (`pipeline="single"`) swaps the three agents for one
structured call that classifies and extracts, takes the top search hit per line, and
uses the template cover note. Everything in code stays the same.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from crewai.flow.flow import Flow, listen, router, start
from pydantic import BaseModel, PrivateAttr

from app.agents import EXTRACT_SYSTEM, AgentRunner, Step, extract_with_model
from app.catalogue.index import Catalogue
from app.catalogue.normalise import normalise, spec_conflicts
from app.catalogue_client import CatalogueSearch
from app.documents import fallback_cover
from app.pricing import find_customer, price_quote
from app.reader import Document, UnsupportedFile, read_document
from app.safety import injection_signals, mentions_money
from app.schema import (
    Candidate,
    EmailClass,
    ExtractionResult,
    Flag,
    InboundEmail,
    IntakeResult,
    MatchDecision,
    Quote,
    QuoteLine,
)

TOP_K = 5


@dataclass
class PipelineResult:
    email_class: str
    quote: Quote | None
    intake: IntakeResult | None
    model_calls: int
    seconds: float
    timings: dict[str, float] = field(default_factory=dict)
    error: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0


class FlowState(BaseModel):
    email_class: str = ""
    model_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    timings: dict[str, float] = {}


class QuoteFlow(Flow[FlowState]):
    _email: InboundEmail = PrivateAttr()
    _runner: AgentRunner = PrivateAttr()
    _search: CatalogueSearch = PrivateAttr()
    _catalogue: Catalogue = PrivateAttr()
    _ref: str = PrivateAttr()
    _now: datetime = PrivateAttr()
    _intake: IntakeResult | None = PrivateAttr(default=None)
    _docs: list[Document] = PrivateAttr(default_factory=list)
    _read_flags: list[Flag] = PrivateAttr(default_factory=list)
    _extraction: ExtractionResult | None = PrivateAttr(default=None)
    _candidates: dict[int, list[Candidate]] = PrivateAttr(default_factory=dict)
    _decisions: dict[int, MatchDecision] = PrivateAttr(default_factory=dict)
    _quote: Quote | None = PrivateAttr(default=None)

    def __init__(self, email: InboundEmail, runner: AgentRunner, search: CatalogueSearch, catalogue: Catalogue,
                 ref: str, now: datetime | None = None) -> None:
        super().__init__(suppress_flow_events=True)
        self._email, self._runner, self._search, self._catalogue = email, runner, search, catalogue
        self._ref = ref
        self._now = now or datetime.now(UTC)

    def _timed(self, name: str, started: float, step: Step | None = None) -> None:
        # For model steps the step's own time counts: a cached step reports what the real call took.
        self.state.timings[name] = round(step.seconds if step else time.perf_counter() - started, 2)
        if step:
            self.state.model_calls += step.calls
            self.state.tokens_in += step.tokens_in
            self.state.tokens_out += step.tokens_out

    @start()
    def intake(self) -> None:
        t = time.perf_counter()
        step = self._runner.classify(self._email)
        self._intake = step.value
        self.state.email_class = step.value.email_class
        self._timed("intake", t, step)

    @router(intake)
    def route(self) -> str:
        return "quote" if self.state.email_class == "quote_request" else "skip"

    @listen("skip")
    def not_a_quote(self) -> None:
        self._quote = None

    @listen("quote")
    def read(self) -> None:
        t = time.perf_counter()
        for att in self._email.attachments:
            try:
                self._docs.append(read_document(att.data, att.filename))
            except UnsupportedFile as exc:
                self._read_flags.append(Flag(code="unreadable_attachment", message=str(exc)))
        self._timed("read", t)

    @listen(read)
    def extract(self) -> None:
        t = time.perf_counter()
        step = self._runner.extract(self._email, self._docs)
        self._extraction = step.value
        self._timed("extract", t, step)

    @listen(extract)
    def retrieve(self) -> None:
        t = time.perf_counter()
        for line in self._extraction.lines:
            query = " ".join(x for x in (line.description, line.specification) if x)
            self._candidates[line.line_no] = self._search.search(query, k=TOP_K)
        self._timed("retrieve", t)

    @listen(retrieve)
    def match(self) -> None:
        t = time.perf_counter()
        lines = [
            {"line_no": line.line_no, "customer_wrote": line.description, "specification": line.specification,
             "quantity": line.quantity, "unit": line.unit, "category_guess": line.category,
             "candidates": [{"sku": c.sku, "name": c.name, "unit": c.unit, "stock": c.stock, "why": c.why}
                            for c in self._candidates[line.line_no]]}
            for line in self._extraction.lines
        ]
        if lines:
            step = self._runner.match(lines, self._search.agent_tools())
            self._decisions = {d.line_no: d for d in step.value.decisions}
            self._timed("match", t, step)

    @listen(match)
    def decide(self) -> None:
        self._quote = build_quote(self._email, self._intake, self._extraction, self._candidates, self._decisions,
                                  self._catalogue, self._ref, self._now, self._read_flags)

    @listen(decide)
    def price(self) -> None:
        price_quote(self._quote, self._catalogue)

    @listen(price)
    def cover(self) -> None:
        t = time.perf_counter()
        step = self._runner.cover(cover_facts(self._quote))
        note = step.value
        if mentions_money(note.body) or mentions_money(note.subject) or self._quote.ref not in note.subject:
            self._quote.cover_subject, self._quote.cover_body = fallback_cover(self._quote)
            self._quote.flags.append(Flag(code="cover_replaced", blocking=False, message=(
                "The drafted cover note mentioned money or lost the quote number; the template was used.")))
        else:
            self._quote.cover_subject, self._quote.cover_body = note.subject, note.body
        self._quote.cover_not_quoted = self._quote.not_quoted
        self._timed("cover", t, step)


# --- code decisions -----------------------------------------------------------------------------------
def build_quote(email: InboundEmail, intake: IntakeResult | None, extraction: ExtractionResult,
                candidates: dict[int, list[Candidate]], decisions: dict[int, MatchDecision], catalogue: Catalogue,
                ref: str, now: datetime, extra_flags: list[Flag] | None = None) -> Quote:
    customer = find_customer(email.sender, email.sender_name)
    quote = Quote(ref=ref, email_id=email.id, created_at=now.isoformat(timespec="seconds"), customer=customer,
                  customer_reference=extraction.customer_reference, currency=customer["currency"],
                  reply_to=email.sender, flags=list(extra_flags or []))
    if not customer["known"]:
        quote.flags.append(Flag(code="new_customer", blocking=False, message=(
            f"{email.sender} is not a known customer: quoted as retail, prepaid. Check before sending.")))
    signals = injection_signals(f"{email.subject}\n{email.body}")
    if signals or (intake is not None and intake.suspicious_instructions):
        detail = "; ".join(f'"{s}"' for s in signals) or "the intake agent saw instructions aimed at the system"
        quote.flags.append(Flag(code="possible_injection", message=(
            f"The email tries to instruct the system ({detail}). Prices and the recipient were NOT changed; "
            "confirm the request is genuine.")))
    if not extraction.lines:
        quote.flags.append(Flag(code="no_lines", message="No line items could be read from this request."))

    for line in extraction.lines:
        q = QuoteLine(line_no=line.line_no, description=line.description, specification=line.specification,
                      requested_qty=line.quantity, requested_unit=line.unit, category=line.category,
                      candidates=candidates.get(line.line_no, []))
        if line.uncertain:
            q.flags.append(Flag(code="unclear_line", message=line.note or "The extraction marked this line unclear."))
        decision = decisions.get(line.line_no)
        apply_decision(q, decision, catalogue)
        quote.lines.append(q)
    return quote


def apply_decision(q: QuoteLine, decision: MatchDecision | None, catalogue: Catalogue) -> None:
    """Accept a model's pick only if the SKU exists and its sizes agree with the request."""
    if decision is None:
        q.flags.append(Flag(code="no_decision", message="The matcher returned no decision for this line."))
        return
    q.match_confidence, q.match_reason = decision.confidence, decision.reason
    product = catalogue.get(decision.sku.strip().upper()) if decision.sku else None
    if decision.sku and product is None:
        q.flags.append(Flag(code="invented_sku", message=f"The matcher chose '{decision.sku}', which does not exist."))
        return
    if product is None:
        q.flags.append(Flag(code="not_in_catalogue", message=(
            f"No catalogue product fits: {decision.reason}" if decision.reason else "No catalogue product fits.")))
        return
    q.sku, q.product_name = product.sku, product.name
    request = normalise(" ".join(x for x in (q.description, q.specification) if x))
    conflicts = spec_conflicts(request, catalogue.normalised_name(product.sku))
    if conflicts:
        q.flags.append(Flag(code="spec_mismatch", message="Size differs from the request: " + "; ".join(conflicts)))
    if decision.confidence != "high":
        q.flags.append(Flag(code="low_confidence", message=f"Matcher confidence {decision.confidence}: {decision.reason}"))
    if q.category not in ("other", product.category):
        q.flags.append(Flag(code="category_differs", blocking=False, message=(
            f"Extracted as {q.category}, product is {product.category}.")))


def cover_facts(quote: Quote) -> dict:
    """What the cover-note agent may know. No prices, costs or suppliers."""
    return {
        "quote_ref": quote.ref,
        "customer_company": quote.customer.get("company"),
        "contact_name": quote.customer.get("contact"),
        "their_reference": quote.customer_reference,
        "valid_until": quote.valid_until,
        "lines_quoted": sum(1 for line in quote.lines if line.line_total is not None and not line.excluded),
        "not_quoted": [line.description for line in quote.lines if line.line_total is None or line.excluded],
        "lead_times": [f"{line.product_name}: {f.message}" for line in quote.lines for f in line.flags
                       if f.code == "out_of_stock"],
    }


# --- entry points --------------------------------------------------------------------------------------------
def run_pipeline(email: InboundEmail, runner: AgentRunner, search: CatalogueSearch, catalogue: Catalogue,
                 ref: str, now: datetime | None = None) -> PipelineResult:
    started = time.perf_counter()
    flow = QuoteFlow(email, runner, search, catalogue, ref, now)
    try:
        flow.kickoff()
    except Exception as exc:  # noqa: BLE001 -- a provider failure is reported, never scored as a decision
        return PipelineResult(flow.state.email_class or "error", None, flow._intake, flow.state.model_calls,
                              time.perf_counter() - started, dict(flow.state.timings), f"{type(exc).__name__}: {exc}")
    quote = flow._quote
    seconds = sum(flow.state.timings.values())
    if quote is not None:
        quote.model_calls, quote.seconds, quote.pipeline = flow.state.model_calls, round(seconds, 2), runner.name
    return PipelineResult(flow.state.email_class, quote, flow._intake, flow.state.model_calls, seconds,
                          dict(flow.state.timings), tokens_in=flow.state.tokens_in, tokens_out=flow.state.tokens_out)


class SingleCallExtraction(ExtractionResult):
    email_class: EmailClass


SINGLE_SYSTEM = EXTRACT_SYSTEM + (
    "\nAlso set email_class: quote_request (asks for prices/availability), purchase_order, reminder (chases a "
    "quote) or other. For anything that is not a quote_request return no lines."
)


def single_call_extract(email: InboundEmail, docs: list[Document]) -> Step[SingleCallExtraction]:
    return extract_with_model(email, docs, SINGLE_SYSTEM, SingleCallExtraction)


def run_single_call(
    email: InboundEmail, search: CatalogueSearch, catalogue: Catalogue, ref: str, now: datetime | None = None,
    extract: Callable[[InboundEmail, list[Document]], Step[SingleCallExtraction]] = single_call_extract,
) -> PipelineResult:
    """Baseline: one structured call (classify + extract), top search hit per line, template cover."""
    started = time.perf_counter()
    now = now or datetime.now(UTC)
    docs, flags = [], []
    for att in email.attachments:
        try:
            docs.append(read_document(att.data, att.filename))
        except UnsupportedFile as exc:
            flags.append(Flag(code="unreadable_attachment", message=str(exc)))
    try:
        step = extract(email, docs)
        result, calls = step.value, step.calls
        tokens = {"tokens_in": step.tokens_in, "tokens_out": step.tokens_out}
    except Exception as exc:  # noqa: BLE001 -- a provider failure is reported, never scored as a decision
        return PipelineResult("error", None, None, 0, time.perf_counter() - started, error=str(exc))
    if result.email_class != "quote_request":
        return PipelineResult(result.email_class, None, None, calls, step.seconds, **tokens)
    code_started = time.perf_counter()
    candidates, decisions = {}, {}
    for line in result.lines:
        query = " ".join(x for x in (line.description, line.specification) if x)
        hits = search.search(query, k=TOP_K)
        candidates[line.line_no] = hits
        decisions[line.line_no] = top_hit_decision(query, hits, catalogue)
    quote = build_quote(email, None, result, candidates, decisions, catalogue, ref, now, flags)
    price_quote(quote, catalogue)
    quote.cover_subject, quote.cover_body = fallback_cover(quote)
    quote.cover_not_quoted = quote.not_quoted
    seconds = step.seconds + time.perf_counter() - code_started
    quote.model_calls, quote.seconds, quote.pipeline = calls, round(seconds, 2), "single"
    return PipelineResult("quote_request", quote, None, calls, seconds, **tokens)


def top_hit_decision(query: str, hits: list[Candidate], catalogue: Catalogue) -> MatchDecision:
    """No model: take the best hit if it shares most of the request's words, else nothing."""
    q_tokens = set(normalise(query).split())
    if not hits or not q_tokens:
        return MatchDecision(line_no=0, sku=None, confidence="low", reason="no search results")
    best = hits[0]
    overlap = len(q_tokens & set(catalogue.normalised_name(best.sku).split())) / len(q_tokens)
    if overlap < 0.4:
        return MatchDecision(line_no=0, sku=None, confidence="low", reason=f"top hit shares {overlap:.0%} of terms")
    return MatchDecision(line_no=0, sku=best.sku, confidence="high" if overlap >= 0.6 else "medium",
                         reason=f"top search hit, {overlap:.0%} term overlap")
