"""Talks to the QuoteDesk API. The UI never imports app/ -- it could run on another machine."""

from __future__ import annotations

import os

import httpx

API_URL = os.environ.get("QUOTEDESK_API_URL", "http://localhost:8000")
API_KEY = os.environ.get("APP_API_KEY")


class ApiError(RuntimeError):
    pass


class ApiClient:
    def __init__(self, base_url: str = API_URL, api_key: str | None = API_KEY) -> None:
        headers = {"X-API-Key": api_key} if api_key else {}
        # Processing an inbox of several emails on the free tier takes minutes.
        self._http = httpx.Client(base_url=base_url, headers=headers, timeout=900)

    def _call(self, method: str, path: str, **kwargs: object) -> httpx.Response:
        try:
            r = self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise ApiError(f"API not reachable at {self._http.base_url}: {exc}") from exc
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text)
            except ValueError:
                detail = r.text
            raise ApiError(f"{r.status_code}: {detail}")
        return r

    def health(self) -> dict:
        try:
            return self._call("GET", "/health").json()
        except ApiError as exc:
            return {"status": "down", "detail": str(exc)}

    def samples(self) -> list[str]:
        return self._call("GET", "/inbox/samples").json()

    def load_samples(self, ids: list[str]) -> dict:
        return self._call("POST", "/inbox/samples", params={"ids": ids}).json()

    def poll(self) -> list[dict]:
        return self._call("POST", "/inbox/poll").json()

    def quotes(self, status: str | None = None) -> list[dict]:
        return self._call("GET", "/quotes", params={"status": status} if status else None).json()

    def quote(self, ref: str) -> dict:
        return self._call("GET", f"/quotes/{ref}").json()

    def original_email(self, ref: str) -> dict:
        return self._call("GET", f"/quotes/{ref}/email").json()

    def attachment_preview(self, ref: str, index: int) -> bytes:
        return self._call("GET", f"/quotes/{ref}/attachments/{index}/preview.png").content

    def quote_preview(self, ref: str) -> bytes:
        return self._call("GET", f"/quotes/{ref}/preview.png").content

    def quote_pdf(self, ref: str) -> bytes:
        return self._call("GET", f"/quotes/{ref}/pdf").content

    def edit_line(self, ref: str, line_no: int, **changes: object) -> dict:
        return self._call("PATCH", f"/quotes/{ref}/lines/{line_no}", json=changes).json()

    def edit_cover(self, ref: str, subject: str, body: str, reviewer: str) -> dict:
        return self._call("PUT", f"/quotes/{ref}/cover",
                          json={"subject": subject, "body": body, "reviewer": reviewer}).json()

    def resolve_flag(self, ref: str, code: str, reviewer: str) -> dict:
        return self._call("POST", f"/quotes/{ref}/flags/{code}/resolve", params={"reviewer": reviewer}).json()

    def approve(self, ref: str, approver: str, exception_reason: str | None) -> dict:
        return self._call("POST", f"/quotes/{ref}/approve",
                          json={"approver": approver, "exception_reason": exception_reason}).json()

    def reject(self, ref: str, reviewer: str, reason: str) -> dict:
        return self._call("POST", f"/quotes/{ref}/reject", json={"reviewer": reviewer, "reason": reason}).json()

    def outcome(self, ref: str, outcome: str, note: str = "") -> dict:
        return self._call("POST", f"/quotes/{ref}/outcome", json={"outcome": outcome, "note": note}).json()

    def events(self, ref: str | None = None) -> list[dict]:
        return self._call("GET", f"/quotes/{ref}/events" if ref else "/events").json()

    def emails(self) -> list[dict]:
        return self._call("GET", "/emails").json()

    def outbox(self) -> list[dict]:
        return self._call("GET", "/outbox").json()

    def search(self, q: str, k: int = 8) -> list[dict]:
        return self._call("GET", "/catalogue/search", params={"q": q, "k": k}).json()
