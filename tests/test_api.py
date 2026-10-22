import pytest
from fastapi.testclient import TestClient

from app import api as api_module
from app.config import settings
from app.ports import FolderInbox, FolderOutbox
from app.service import QuoteService
from app.store import Store
from tests.fakes import FakeRunner, line


@pytest.fixture
def client(tmp_data, search, catalogue):
    runner = FakeRunner(lines=[line(1, "SS hex bolt M10x50", 200, "pcs"), line(2, "unicorn polish", 2, category="other")],
                        picks={2: (None, "low")})
    service = QuoteService(Store(settings.db_path), FolderInbox(settings.inbox_dir), FolderOutbox(settings.outbox_dir),
                           runner, search, catalogue)
    api_module.app.dependency_overrides[api_module.get_service] = lambda: service
    yield TestClient(api_module.app)
    api_module.app.dependency_overrides.clear()


def test_request_review_approve_over_http(client):
    r = client.post("/requests", data={"sender": "dave.hirst@airevalleymech.example", "subject": "RFQ",
                                       "body": "please quote"})
    assert r.status_code == 200, r.text
    ref = r.json()["ref"]
    q = client.get(f"/quotes/{ref}").json()
    assert q["status"] == "needs_review" and q["blockers"]
    assert client.post(f"/quotes/{ref}/approve", json={"approver": "Alex"}).status_code == 409
    assert client.patch(f"/quotes/{ref}/lines/2", json={"excluded": True}).status_code == 200
    assert client.get(f"/quotes/{ref}/pdf").content[:4] == b"%PDF"
    assert client.get(f"/quotes/{ref}/preview.png").content[:4] == b"\x89PNG"
    r = client.post(f"/quotes/{ref}/approve", json={"approver": "Alex"})
    assert r.status_code == 200, r.text
    assert "dave.hirst@airevalleymech.example" in {m["to"] for m in client.get("/outbox").json()}
    assert client.post(f"/quotes/{ref}/outcome", json={"outcome": "lost"}).json()["status"] == "lost"
    assert [e["kind"] for e in client.get(f"/quotes/{ref}/events").json()][-1] == "marked_lost"


def test_errors_and_samples(client):
    assert client.get("/quotes/Q-NOPE").status_code == 404
    assert client.post("/requests", data={"sender": "nobody", "subject": "x"}).status_code == 422
    assert "r01" in client.get("/inbox/samples").json()
    assert client.post("/inbox/samples", params={"ids": ["r01"]}).json() == {"copied": ["r01"]}
    assert client.get("/catalogue/search", params={"q": "teflon tape"}).json()[0]["name"].startswith("PTFE")


def test_api_key_is_enforced_when_set(client, monkeypatch):
    monkeypatch.setattr(settings, "app_api_key", "s3cret")
    assert client.get("/quotes").status_code == 401
    assert client.get("/quotes", headers={"X-API-Key": "s3cret"}).status_code == 200
    assert client.get("/health").status_code == 200  # health stays open for the container check
