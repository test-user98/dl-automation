"""API tests for admin + lookup routers.

These boot a fresh FastAPI TestClient against an isolated SQLite file so we
can assert seed data, auth, filters, edge cases, and the customer-side
lookup, without touching the real `data/customers.db`.
"""

import importlib
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    # Isolate DB + minimal env so the app boots without secrets exploding.
    monkeypatch.setenv("API_SECRET_KEY", "test-secret")
    monkeypatch.setenv("ADMIN_SECRET",   "admin-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake")
    monkeypatch.setenv("STATE_BACKEND",  "sqlite")
    monkeypatch.setenv("LEARNING_DB_PATH", str(tmp_path / "learning.db"))
    monkeypatch.setenv("STATE_DB_PATH",    str(tmp_path / "state.db"))
    monkeypatch.chdir(tmp_path)

    # Force re-import of the customer store module so DB_PATH picks up the cwd
    import agent.customer_store as cs
    importlib.reload(cs)
    import agent.customer_seed
    importlib.reload(agent.customer_seed)
    import api.deps; importlib.reload(api.deps)
    import api.admin; importlib.reload(api.admin)
    import api.lookup; importlib.reload(api.lookup)
    import api.onboard; importlib.reload(api.onboard)
    import api.server; importlib.reload(api.server)

    with TestClient(api.server.app) as c:
        yield c


def _hdr(c): return {"X-Admin-Secret": "admin-secret"}


def test_admin_requires_secret(client):
    r = client.get("/admin/summary")
    assert r.status_code == 401

    r = client.get("/admin/summary", headers={"X-Admin-Secret": "wrong"})
    assert r.status_code == 401


def test_seed_populated(client):
    r = client.get("/admin/summary", headers=_hdr(client))
    assert r.status_code == 200
    counts = r.json()["counts"]
    assert counts["customers"]    >= 5
    assert counts["applications"] >= 5


def test_list_customers(client):
    r = client.get("/admin/customers", headers=_hdr(client))
    assert r.status_code == 200
    rows = r.json()["items"]
    assert all("phone" in row for row in rows)
    assert all(row["customer_id"].startswith("CUST-") for row in rows)


def test_customer_search(client):
    r = client.get("/admin/customers?search=Aarav", headers=_hdr(client))
    assert r.status_code == 200
    rows = r.json()["items"]
    assert any("Aarav" in (row["name"] or "") for row in rows)


def test_customer_detail_by_phone_and_id(client):
    by_phone = client.get("/admin/customers/9876512345", headers=_hdr(client))
    assert by_phone.status_code == 200
    body = by_phone.json()
    cid = body["customer"]["customer_id"]
    assert body["applications"]
    by_id = client.get(f"/admin/customers/{cid}", headers=_hdr(client))
    assert by_id.status_code == 200
    assert by_id.json()["customer"]["customer_id"] == cid


def test_customer_not_found(client):
    assert client.get("/admin/customers/0000000000", headers=_hdr(client)).status_code == 404
    assert client.get("/admin/customers/CUST-FAKEFAKE", headers=_hdr(client)).status_code == 404


def test_list_applications_filters(client):
    r = client.get("/admin/applications?status=SUBMITTED", headers=_hdr(client))
    assert r.status_code == 200
    rows = r.json()["items"]
    assert all(a["status"] == "SUBMITTED" for a in rows)

    r2 = client.get("/admin/applications?service=DL_RENEWAL", headers=_hdr(client))
    assert all(a["service_type"] == "DL_RENEWAL" for a in r2.json()["items"])


def test_application_detail_and_note(client):
    rows = client.get("/admin/applications", headers=_hdr(client)).json()["items"]
    app_id = rows[0]["app_id"]
    d = client.get(f"/admin/applications/{app_id}", headers=_hdr(client))
    assert d.status_code == 200
    body = d.json()
    assert body["application"]["app_id"] == app_id
    # Add a note
    n = client.post(
        f"/admin/applications/{app_id}/notes",
        headers={**_hdr(client), "Content-Type": "application/json"},
        json={"text": "Looked at the docs"},
    )
    assert n.status_code == 200
    # And retrieve
    d2 = client.get(f"/admin/applications/{app_id}", headers=_hdr(client))
    notes = d2.json()["notes"]
    assert any("Looked at the docs" == n["text"] for n in notes)


def test_application_note_validation(client):
    rows = client.get("/admin/applications", headers=_hdr(client)).json()["items"]
    app_id = rows[0]["app_id"]
    r = client.post(
        f"/admin/applications/{app_id}/notes",
        headers={**_hdr(client), "Content-Type": "application/json"},
        json={"text": ""},
    )
    assert r.status_code == 422  # pydantic min_length


def test_application_not_found(client):
    r = client.get("/admin/applications/APP-NOPE", headers=_hdr(client))
    assert r.status_code == 404


def test_document_preview(client):
    rows = client.get("/admin/applications", headers=_hdr(client)).json()["items"]
    # Find an app with docs
    for r in rows:
        d = client.get(f"/admin/applications/{r['app_id']}", headers=_hdr(client))
        docs = d.json()["documents"]
        if docs:
            doc_id = docs[0]["doc_id"]
            preview = client.get(f"/admin/documents/{doc_id}/preview", headers=_hdr(client))
            assert preview.status_code in (200, 410)  # 410 if seed file missing
            return
    pytest.skip("no seeded documents found")


# ─── Lookup endpoint ──────────────────────────────────────────────────────────

def test_lookup_missing_args(client):
    assert client.get("/lookup").status_code == 400


def test_lookup_unknown_returns_empty(client):
    r = client.get("/lookup?phone=0000000000")
    assert r.status_code == 200
    assert r.json()["found"] is False


def test_lookup_known(client):
    r = client.get("/lookup?phone=9876512345")
    assert r.status_code == 200
    body = r.json()
    assert body["found"] is True
    assert body["customer"]["phone_mask"].startswith("+91 ")
    assert len(body["applications"]) >= 1


def test_lookup_rate_limit(client):
    for _ in range(10):
        r = client.get("/lookup?phone=9876512345")
        assert r.status_code in (200,)
    assert client.get("/lookup?phone=9876512345").status_code == 429


# ─── New endpoints (UI/API polish round) ──────────────────────────────────────

def test_application_search_filter(client):
    """The /admin/applications search param should match phone/name/app/number."""
    r = client.get("/admin/applications?search=Aarav", headers=_hdr(client))
    assert r.status_code == 200
    rows = r.json()["items"]
    assert any("Aarav" in (a.get("customer_name") or "") for a in rows)

    r2 = client.get("/admin/applications?search=9876512345", headers=_hdr(client))
    assert any(a.get("customer_phone") == "9876512345" for a in r2.json()["items"])


def test_status_update_endpoint(client):
    rows = client.get("/admin/applications", headers=_hdr(client)).json()["items"]
    app_id = rows[0]["app_id"]
    # Bad status rejected
    r = client.post(
        f"/admin/applications/{app_id}/status",
        headers={**_hdr(client), "Content-Type": "application/json"},
        json={"status": "MOON"},
    )
    assert r.status_code == 400
    # Valid status accepted, note auto-added
    r = client.post(
        f"/admin/applications/{app_id}/status",
        headers={**_hdr(client), "Content-Type": "application/json"},
        json={"status": "CANCELLED", "note": "operator cancelled per customer call"},
    )
    assert r.status_code == 200
    assert r.json()["application"]["status"] == "CANCELLED"
    assert any(e["status"] == "CANCELLED" for e in r.json()["events"])
    # Note is visible in detail
    d = client.get(f"/admin/applications/{app_id}", headers=_hdr(client))
    assert any("operator cancelled" in n["text"] for n in d.json()["notes"])
    assert any(e["status"] == "CANCELLED" for e in d.json()["events"])


def test_doc_preview_query_secret(client):
    """The img-tag preview path accepts ?secret= without the header."""
    rows = client.get("/admin/applications", headers=_hdr(client)).json()["items"]
    for r in rows:
        d = client.get(f"/admin/applications/{r['app_id']}", headers=_hdr(client))
        docs = d.json()["documents"]
        if not docs:
            continue
        doc_id = docs[0]["doc_id"]
        # No header at all -> 401
        bad = client.get(f"/admin/documents/{doc_id}/preview")
        assert bad.status_code == 401
        # With query string secret -> 200 or 410
        ok = client.get(f"/admin/documents/{doc_id}/preview?secret=admin-secret")
        assert ok.status_code in (200, 410)
        return
    pytest.skip("no seeded documents")


def test_upload_rejects_oversize(client):
    """/onboard/extract-dl-image should reject files over 8 MB."""
    huge = b"x" * (9 * 1024 * 1024)  # 9 MB
    r = client.post(
        "/onboard/extract-dl-image",
        files={"file": ("big.jpg", huge, "image/jpeg")},
    )
    assert r.status_code == 413


def test_upload_rejects_bad_mime(client):
    r = client.post(
        "/onboard/extract-dl-image",
        files={"file": ("x.exe", b"binary", "application/octet-stream")},
    )
    assert r.status_code == 415


def test_lookup_by_application_number(client):
    """Lookup by application number finds the customer record."""
    # Seeded app numbers include 'RJ-DL-2026-04219'
    r = client.get("/lookup?application_number=RJ-DL-2026-04219")
    assert r.status_code == 200
    body = r.json()
    assert body["found"] is True
    assert any(a["application_number"] == "RJ-DL-2026-04219"
               for a in body["applications"])
    assert all("timeline" in a for a in body["applications"])


def test_favicon_served(client):
    r = client.get("/favicon.ico")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/")
