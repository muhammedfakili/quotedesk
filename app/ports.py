"""Interfaces to the outside world: where emails come from and where replies go.

The pipeline only knows these Protocols. The demo uses folders of .eml files (real
RFC 5322 messages, so any mail client can open them); Gmail, IMAP or Outlook would be
one new class each, with no change to the agents or the flow.
"""

from __future__ import annotations

import email
import email.policy
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr
from pathlib import Path
from typing import Protocol

from app.schema import Attachment, InboundEmail


@dataclass
class OutgoingEmail:
    to: str
    subject: str
    body: str
    kind: str  # "quote" | "supplier_enquiry"
    sender: str = ""
    in_reply_to: str | None = None
    quote_ref: str | None = None
    attachments: list[Attachment] = field(default_factory=list)


class InboxPort(Protocol):
    def fetch_new(self) -> list[InboundEmail]: ...

    def mark_processed(self, message_id: str) -> None: ...


class OutboxPort(Protocol):
    def send(self, message: OutgoingEmail) -> str:
        """Deliver (or draft) the message; returns where it went (path, draft id...)."""
        ...


# --- .eml parsing / writing ---------------------------------------------------------------------
def parse_eml(raw: bytes) -> InboundEmail:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    name, addr = parseaddr(str(msg.get("From", "")))
    body_part = msg.get_body(preferencelist=("plain", "html"))
    body = body_part.get_content() if body_part is not None else ""
    if body_part is not None and body_part.get_content_type() == "text/html":
        body = re.sub(r"<[^>]+>", " ", body)
    attachments = [
        Attachment(filename=part.get_filename() or "attachment", content_type=part.get_content_type(),
                   data=part.get_payload(decode=True) or b"")
        for part in msg.iter_attachments()
    ]
    return InboundEmail(
        id=str(msg.get("Message-ID", "")).strip() or make_msgid(domain="local"),
        sender=addr.lower(), sender_name=name or addr, subject=str(msg.get("Subject", "")),
        date=str(msg.get("Date", "")), body=body.strip(), to=str(msg.get("To", "")), attachments=attachments,
        raw=raw,
    )


def build_eml(message: OutgoingEmail) -> bytes:
    msg = EmailMessage()
    msg["From"] = message.sender
    msg["To"] = message.to
    msg["Subject"] = message.subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=message.sender.rsplit("@", 1)[-1] or "local")
    if message.in_reply_to:
        msg["In-Reply-To"] = message.in_reply_to
        msg["References"] = message.in_reply_to
    msg.set_content(message.body)
    for att in message.attachments:
        maintype, _, subtype = att.content_type.partition("/")
        msg.add_attachment(att.data, maintype=maintype, subtype=subtype, filename=att.filename)
    return bytes(msg)


# --- folder-based mocks ----------------------------------------------------------------------------
class FolderInbox:
    """Every .eml in the folder is a new message until it is marked processed."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.folder.mkdir(parents=True, exist_ok=True)
        self._seen_file = folder / ".processed"

    def _seen(self) -> set[str]:
        return set(self._seen_file.read_text(encoding="utf-8").split("\n")) if self._seen_file.exists() else set()

    def fetch_new(self) -> list[InboundEmail]:
        seen = self._seen()
        messages = [parse_eml(p.read_bytes()) for p in sorted(self.folder.glob("*.eml"))]
        return [m for m in messages if m.id not in seen]

    def mark_processed(self, message_id: str) -> None:
        with self._seen_file.open("a", encoding="utf-8") as fh:
            fh.write(message_id + "\n")


class FolderOutbox:
    """Writes each outgoing message as an .eml file. Nothing leaves the machine."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.folder.mkdir(parents=True, exist_ok=True)

    def send(self, message: OutgoingEmail) -> str:
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
        slug = re.sub(r"[^A-Za-z0-9-]+", "-", f"{message.kind}-{message.quote_ref or ''}").strip("-")
        path = self.folder / f"{stamp}-{slug}.eml"
        path.write_bytes(build_eml(message))
        return str(path)

    def list(self) -> list[Path]:
        return sorted(self.folder.glob("*.eml"))


class GmailDraftOutbox:
    """Creates a Gmail *draft* (users.drafts.create) instead of sending: a person presses Send in Gmail.

    Needs an OAuth access token with the gmail.compose scope. Written against the
    documented REST API; not exercised against a live Gmail account in this repo.
    """

    URL = "https://gmail.googleapis.com/gmail/v1/users/me/drafts"

    def __init__(self, access_token: str) -> None:
        import httpx

        self._http = httpx.Client(headers={"Authorization": f"Bearer {access_token}"}, timeout=30)

    def send(self, message: OutgoingEmail) -> str:
        import base64

        raw = base64.urlsafe_b64encode(build_eml(message)).decode()
        response = self._http.post(self.URL, json={"message": {"raw": raw}})
        response.raise_for_status()
        return f"gmail-draft:{response.json()['id']}"
