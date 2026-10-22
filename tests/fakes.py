"""A scripted stand-in for the model steps: tests run offline, with no API key."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.agents import Step
from app.reader import Document
from app.schema import (
    CoverNote,
    ExtractedLine,
    ExtractionResult,
    InboundEmail,
    IntakeResult,
    MatchDecision,
    MatchResult,
)


def line(n: int, text: str, qty: float | None, unit: str | None = None, category: str = "fasteners",
         uncertain: bool = False) -> ExtractedLine:
    return ExtractedLine(line_no=n, description=text, quantity=qty, unit=unit, category=category,
                         uncertain=uncertain)


@dataclass
class FakeRunner:
    """Answers like a model would, from a script.

    email_class: what intake says. lines: what extraction returns.
    picks: line_no -> (sku or None, confidence); lines without a pick get the first candidate, "high".
    cover_body: what the cover agent writes (lets tests try a cover note that mentions money).
    """

    email_class: str = "quote_request"
    lines: list[ExtractedLine] = field(default_factory=list)
    picks: dict[int, tuple[str | None, str]] = field(default_factory=dict)
    cover_body: str = "Dear customer,\n\nPlease find our quotation attached.\n\nSales Desk"
    suspicious: bool = False
    name: str = "fake"
    calls: list[str] = field(default_factory=list)

    def classify(self, email: InboundEmail) -> Step[IntakeResult]:
        self.calls.append("classify")
        return Step(IntakeResult(email_class=self.email_class, confidence="high", reason="scripted",
                                 suspicious_instructions=self.suspicious), 1, 0.0)

    def extract(self, email: InboundEmail, docs: list[Document]) -> Step[ExtractionResult]:
        self.calls.append("extract")
        return Step(ExtractionResult(customer_reference="REF-1", lines=self.lines), 1, 0.0)

    def match(self, lines: list[dict], tools: list) -> Step[MatchResult]:
        self.calls.append("match")
        decisions = []
        for item in lines:
            n = item["line_no"]
            if n in self.picks:
                sku, conf = self.picks[n]
            else:
                sku, conf = (item["candidates"][0]["sku"] if item["candidates"] else None), "high"
            decisions.append(MatchDecision(line_no=n, sku=sku, confidence=conf, reason="scripted"))
        return Step(MatchResult(decisions=decisions), 1, 0.0)

    def cover(self, context: dict) -> Step[CoverNote]:
        self.calls.append("cover")
        return Step(CoverNote(subject=f"Quotation {context['quote_ref']}", body=self.cover_body), 1, 0.0)


def email(sender: str = "dave.hirst@airevalleymech.example", body: str = "Please quote, thanks",
          subject: str = "RFQ", attachments: list | None = None, msg_id: str = "<t1@test>") -> InboundEmail:
    return InboundEmail(id=msg_id, sender=sender, sender_name="Dave Hirst", subject=subject, date="", body=body,
                        attachments=attachments or [])
