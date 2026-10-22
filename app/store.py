"""SQLite: received emails, quotes (as JSON) and the event log. One file, no server."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

from app.schema import Quote

SCHEMA = """
CREATE TABLE IF NOT EXISTS emails (
    id TEXT PRIMARY KEY, received_at TEXT, sender TEXT, subject TEXT, email_class TEXT, ref TEXT,
    error TEXT, raw BLOB
);
CREATE TABLE IF NOT EXISTS quotes (
    ref TEXT PRIMARY KEY, email_id TEXT, status TEXT, created_at TEXT, company TEXT, currency TEXT,
    total REAL, open_flags INTEGER, data TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, ref TEXT, email_id TEXT, kind TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS counters (name TEXT PRIMARY KEY, value INTEGER);
"""


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)
        self._lock = threading.Lock()

    # --- emails ---------------------------------------------------------------------------
    def has_email(self, email_id: str) -> bool:
        return self._db.execute("SELECT 1 FROM emails WHERE id = ?", (email_id,)).fetchone() is not None

    def save_email(self, email_id: str, sender: str, subject: str, raw: bytes) -> None:
        with self._lock, self._db:
            self._db.execute("INSERT OR IGNORE INTO emails (id, received_at, sender, subject, raw) VALUES (?,?,?,?,?)",
                             (email_id, _now(), sender, subject, raw))

    def update_email(self, email_id: str, email_class: str, ref: str | None, error: str | None) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE emails SET email_class = ?, ref = ?, error = ? WHERE id = ?",
                             (email_class, ref, error, email_id))

    def emails(self) -> list[dict]:
        rows = self._db.execute("SELECT id, received_at, sender, subject, email_class, ref, error FROM emails "
                                "ORDER BY received_at DESC").fetchall()
        return [dict(r) for r in rows]

    def raw_email(self, email_id: str) -> bytes | None:
        row = self._db.execute("SELECT raw FROM emails WHERE id = ?", (email_id,)).fetchone()
        return row["raw"] if row else None

    # --- quotes ---------------------------------------------------------------------------
    def next_ref(self, year: int) -> str:
        with self._lock, self._db:
            name = f"quote-{year}"
            self._db.execute("INSERT OR IGNORE INTO counters VALUES (?, 0)", (name,))
            self._db.execute("UPDATE counters SET value = value + 1 WHERE name = ?", (name,))
            n = self._db.execute("SELECT value FROM counters WHERE name = ?", (name,)).fetchone()[0]
        return f"Q-{year}-{n:05d}"

    def save_quote(self, quote: Quote) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO quotes VALUES (?,?,?,?,?,?,?,?,?)",
                (quote.ref, quote.email_id, quote.status, quote.created_at, quote.customer.get("company"),
                 quote.currency, quote.total, len(quote.open_flags), quote.model_dump_json()),
            )

    def get_quote(self, ref: str) -> Quote | None:
        row = self._db.execute("SELECT data FROM quotes WHERE ref = ?", (ref,)).fetchone()
        return Quote.model_validate_json(row["data"]) if row else None

    def list_quotes(self, status: str | None = None) -> list[dict]:
        sql = "SELECT ref, email_id, status, created_at, company, currency, total, open_flags FROM quotes"
        args: tuple = ()
        if status:
            sql += " WHERE status = ?"
            args = (status,)
        return [dict(r) for r in self._db.execute(sql + " ORDER BY created_at DESC, ref DESC", args).fetchall()]

    # --- log ----------------------------------------------------------------------------------
    def log(self, kind: str, detail: dict | str, ref: str | None = None, email_id: str | None = None) -> None:
        text = detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)
        with self._lock, self._db:
            self._db.execute("INSERT INTO events (ts, ref, email_id, kind, detail) VALUES (?,?,?,?,?)",
                             (_now(), ref, email_id, kind, text))

    def events(self, ref: str | None = None, limit: int = 200) -> list[dict]:
        if ref:
            rows = self._db.execute("SELECT * FROM events WHERE ref = ? ORDER BY id", (ref,)).fetchall()
        else:
            rows = self._db.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
