"""Share-link management API (report/router.py) — design_report.md §9.3, §11.

Same setup as tests/test_report_api.py. The public routes (design_report.md §9.4) are tested at the end."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from document_analyzer.app import create_app
from document_analyzer.auth import AuthContext, get_auth_context
from document_analyzer.report import public as report_public_module
from document_analyzer.report import router as report_router_module
from tests.fakes import FakeClient

ORG, OTHER = "org-123", "org-999"
BASE = "/api/reports/r-1/share-links"


@pytest.fixture
def fake_client():
    return FakeClient(org_id=ORG)


def _app(fake_client, org_id):
    app = create_app()

    async def fake_auth():
        return AuthContext(user_id="user-1", org_id=org_id, access_token="tok", client=fake_client)

    app.dependency_overrides[get_auth_context] = fake_auth
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def client(fake_client, monkeypatch):
    monkeypatch.setattr(report_router_module, "get_service_role_client", lambda: fake_client)
    fake_client.table("assessment_reports").insert({"id": "r-1", "org_id": ORG}).execute()
    return _app(fake_client, ORG)


@pytest.fixture
def other_client(fake_client, client):
    return _app(fake_client, OTHER)


def _links(fc):
    return fc.table("report_share_links").rows


def _days(iso):
    return (datetime.fromisoformat(iso) - datetime.now(UTC)) / timedelta(days=1)


def test_create_returns_the_token_once_and_stores_only_its_hash(client, fake_client):
    response = client.post(BASE, json={})

    assert response.status_code == 201
    body = response.json()
    [row] = _links(fake_client)
    assert row["token_hash"] == report_router_module.hash_share_token(body["token"])
    assert body["token"] not in repr(row)
    assert len(body["token"]) >= 43  # 32 random bytes, urlsafe base64
    assert body["share_path"] == f"/?share={body['token']}"
    assert (row["org_id"], row["report_id"], row["created_by"]) == (ORG, "r-1", "user-1")
    assert set(body) == {"id", "token", "share_path", "created_at", "expires_at"}


def test_default_and_capped_expiry(client):
    default = client.post(BASE, json={}).json()
    assert 13.9 < _days(default["expires_at"]) < 14.1
    capped = client.post(BASE, json={"expires_in_days": 10_000}).json()
    assert 89.9 < _days(capped["expires_at"]) < 90.1
    short = client.post(BASE, json={"expires_in_days": 3}).json()
    assert 2.9 < _days(short["expires_at"]) < 3.1


@pytest.mark.parametrize("days", [0, -5])
def test_expiry_below_one_day_is_422(client, fake_client, days):
    assert client.post(BASE, json={"expires_in_days": days}).status_code == 422
    assert _links(fake_client) == []


def test_every_token_is_different(client):
    assert client.post(BASE, json={}).json()["token"] != client.post(BASE, json={}).json()["token"]


def test_list_never_exposes_the_token_or_its_hash(client, fake_client):
    token = client.post(BASE, json={}).json()["token"]

    response = client.get(BASE)

    assert response.status_code == 200
    [link] = response.json()["share_links"]
    assert set(link) == {"id", "created_at", "expires_at", "revoked_at", "access_count", "last_accessed_at", "status"}
    assert link["status"] == "active" and link["access_count"] == 0
    assert token not in response.text and _links(fake_client)[0]["token_hash"] not in response.text


def test_list_status_is_active_expired_or_revoked(client, fake_client):
    now = datetime.now(UTC)
    for link_id, expires, revoked in [
        ("active", now + timedelta(days=1), None),
        ("expired", now - timedelta(days=1), None),
        ("revoked", now + timedelta(days=1), now.isoformat()),
        ("revoked-and-expired", now - timedelta(days=1), now.isoformat()),
    ]:
        fake_client.table("report_share_links").insert(
            {"id": link_id, "org_id": ORG, "report_id": "r-1", "token_hash": link_id, "created_by": "user-1",
             "created_at": now.isoformat(), "expires_at": expires.isoformat(), "revoked_at": revoked}
        ).execute()

    statuses = {link["id"]: link["status"] for link in client.get(BASE).json()["share_links"]}

    assert statuses == {"active": "active", "expired": "expired", "revoked": "revoked", "revoked-and-expired": "revoked"}


def test_revoke_sets_the_revoker_and_is_idempotent(client, fake_client):
    link_id = client.post(BASE, json={}).json()["id"]

    first = client.post(f"/api/share-links/{link_id}/revoke")
    second = client.post(f"/api/share-links/{link_id}/revoke")

    assert first.status_code == second.status_code == 200
    assert first.json() == {"id": link_id, "revoked_at": first.json()["revoked_at"], "status": "revoked"}
    assert second.json()["revoked_at"] == first.json()["revoked_at"]
    [row] = _links(fake_client)
    assert row["revoked_by"] == "user-1" and row["revoked_at"] == first.json()["revoked_at"]
    assert client.get(BASE).json()["share_links"][0]["status"] == "revoked"


def test_missing_report_or_link_is_404(client):
    assert client.post("/api/reports/nope/share-links", json={}).status_code == 404
    assert client.get("/api/reports/nope/share-links").status_code == 404
    assert client.post("/api/share-links/nope/revoke").status_code == 404


def test_another_org_cannot_create_list_or_revoke(client, other_client, fake_client):
    link_id = client.post(BASE, json={}).json()["id"]

    assert other_client.post(BASE, json={}).status_code == 404
    assert other_client.get(BASE).status_code == 404
    assert other_client.post(f"/api/share-links/{link_id}/revoke").status_code == 404
    assert len(_links(fake_client)) == 1 and not _links(fake_client)[0].get("revoked_at")


def test_missing_auth_is_401():
    for method, path in [("post", BASE), ("get", BASE), ("post", "/api/share-links/x/revoke")]:
        assert getattr(TestClient(create_app()), method)(path).status_code == 401


# --- public routes (design_report.md §9.4) -----------------------------------------------------------------

HTML = b"<html><body>snapshot</body></html>"
PDF = b"%PDF-1.7 fake"
GENERATED = "2026-10-01T12:00:00+00:00"
NOT_FOUND = {"detail": "not found"}


@pytest.fixture
def public(fake_client, monkeypatch):
    monkeypatch.setattr(report_router_module, "get_service_role_client", lambda: fake_client)
    monkeypatch.setattr(report_public_module, "get_service_role_client", lambda: fake_client)
    monkeypatch.delenv("REPORT_PUBLIC_RATE_LIMIT", raising=False)
    report_public_module.reset_rate_limits()
    fake_client.cascades["assessment_reports"] = [("report_share_links", "report_id")]
    reports = fake_client.table("assessment_reports")
    reports.insert(
        {"id": "r-1", "org_id": ORG, "created_at": GENERATED, "html_path": f"{ORG}/r-1/report.html", "pdf_path": f"{ORG}/r-1/report.pdf"}
    ).execute()
    bucket = fake_client.storage.from_("reports")
    bucket.upload(f"{ORG}/r-1/report.html", HTML)
    bucket.upload(f"{ORG}/r-1/report.pdf", PDF)
    return TestClient(create_app(), raise_server_exceptions=False)  # no auth override: these routes take none


def _token(fake_client, client, **link):
    """A real token created through the API, then optionally edited to put the link in some state."""
    token = client.post(BASE, json={}).json()["token"]
    _links(fake_client)[-1].update(link)
    return token


def _assert_hardened(response):
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-robots-tag"] == "noindex"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_a_valid_token_serves_the_stored_html_with_the_hardened_headers(public, fake_client, client):
    token = _token(fake_client, client)

    response = public.get(f"/api/public/reports/{token}")

    assert response.status_code == 200 and response.content == HTML
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert response.headers["content-security-policy"] == report_router_module.ARTIFACT_CSP
    assert response.headers["x-report-generated-at"] == GENERATED
    assert response.headers["x-report-pdf"] == "available"
    _assert_hardened(response)


def test_a_valid_token_serves_the_stored_pdf(public, fake_client, client):
    token = _token(fake_client, client)

    response = public.get(f"/api/public/reports/{token}/pdf")

    assert response.status_code == 200 and response.content == PDF
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"] == 'attachment; filename="report-r-1.pdf"'
    _assert_hardened(response)


def test_no_pdf_is_reported_and_its_route_is_the_same_404(public, fake_client, client):
    token = _token(fake_client, client)
    fake_client.table("assessment_reports").rows[0]["pdf_path"] = None

    assert public.get(f"/api/public/reports/{token}").headers["x-report-pdf"] == "unavailable"
    response = public.get(f"/api/public/reports/{token}/pdf")
    assert (response.status_code, response.json()) == (404, NOT_FOUND)


@pytest.mark.parametrize("path", ["", "/pdf"])
def test_every_failure_is_the_same_404(public, fake_client, client, path):
    past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    valid = _token(fake_client, client)
    expired = _token(fake_client, client, expires_at=past)
    revoked = _token(fake_client, client, revoked_at=past, revoked_by="user-1")
    deleted_report = _token(fake_client, client, report_id="gone")
    unknown = "A" * 43
    bad = [expired, revoked, deleted_report, unknown, "short", "x" * 44, "a%20b", "." * 43, valid[:-1] + "!", valid + "A"]

    responses = [public.get(f"/api/public/reports/{token}{path}") for token in bad]

    assert {(r.status_code, r.text) for r in responses} == {(404, '{"detail":"not found"}')}
    for response in responses:
        _assert_hardened(response)
    assert public.get(f"/api/public/reports/{valid}{path}").status_code == 200


def test_a_deleted_report_or_project_takes_its_links_with_it(public, fake_client, client):
    token = _token(fake_client, client)
    assert public.get(f"/api/public/reports/{token}").status_code == 200

    fake_client.table("assessment_reports").delete().eq("id", "r-1").execute()  # a project delete cascades to this

    assert _links(fake_client) == []
    for path in ("", "/pdf"):
        assert public.get(f"/api/public/reports/{token}{path}").json() == NOT_FOUND


def test_a_report_row_without_its_file_is_the_same_404(public, fake_client, client):
    token = _token(fake_client, client)
    fake_client.storage.from_("reports")._objects.clear()

    assert public.get(f"/api/public/reports/{token}").json() == NOT_FOUND


def test_a_row_pointing_at_another_path_is_never_read(public, fake_client, client):
    token = _token(fake_client, client)
    fake_client.table("assessment_reports").rows[0]["html_path"] = f"{OTHER}/r-9/report.html"
    fake_client.storage.from_("reports").upload(f"{OTHER}/r-9/report.html", b"secret")

    assert public.get(f"/api/public/reports/{token}").status_code == 404


def test_access_is_counted_for_hits_but_not_for_misses(public, fake_client, client):
    token = _token(fake_client, client)

    public.get(f"/api/public/reports/{token}")
    public.get(f"/api/public/reports/{token}/pdf")
    public.get("/api/public/reports/" + "B" * 43)

    [link] = _links(fake_client)
    assert link["access_count"] == 2 and link["last_accessed_at"]


def test_revoking_takes_effect_on_the_next_request(public, fake_client, client):
    created = client.post(BASE, json={}).json()
    assert public.get(f"/api/public/reports/{created['token']}").status_code == 200

    client.post(f"/api/share-links/{created['id']}/revoke")

    assert public.get(f"/api/public/reports/{created['token']}").status_code == 404


def test_the_token_is_never_stored_in_plain_text(public, fake_client, client):
    token = _token(fake_client, client)
    public.get(f"/api/public/reports/{token}")

    assert token not in repr(_links(fake_client)) and token not in repr(fake_client.table("assessment_reports").rows)


def test_the_public_routes_read_no_plan_or_project_state(public, fake_client, client, monkeypatch):
    token = _token(fake_client, client)
    touched = []
    original = fake_client.table
    monkeypatch.setattr(fake_client, "table", lambda name: (touched.append(name), original(name))[1])

    public.get(f"/api/public/reports/{token}")
    public.get(f"/api/public/reports/{token}/pdf")

    assert set(touched) == {"report_share_links", "assessment_reports"}


def test_extra_parameters_cannot_change_what_is_read(public, fake_client, client):
    token = _token(fake_client, client)

    response = public.get(f"/api/public/reports/{token}?report_id=other&org_id={OTHER}")

    assert response.status_code == 200 and response.content == HTML


def test_the_rate_limit_applies_to_misses_and_zero_turns_it_off(public, fake_client, client, monkeypatch):
    monkeypatch.setenv("REPORT_PUBLIC_RATE_LIMIT", "3")
    report_public_module.reset_rate_limits()
    token = _token(fake_client, client)

    statuses = [public.get("/api/public/reports/" + "C" * 43).status_code for _ in range(3)]
    blocked = public.get(f"/api/public/reports/{token}")

    assert statuses == [404, 404, 404]
    assert blocked.status_code == 429 and blocked.json() == {"detail": "too many requests"}
    assert int(blocked.headers["retry-after"]) >= 1
    assert not _links(fake_client)[0].get("access_count")
    monkeypatch.setenv("REPORT_PUBLIC_RATE_LIMIT", "0")
    assert public.get(f"/api/public/reports/{token}").status_code == 200


def test_another_org_cannot_read_the_report_behind_a_link_or_use_its_token(public, fake_client, client, other_client):
    token = _token(fake_client, client)

    for path in ("", "/html", "/pdf", "/share-links"):
        assert other_client.get(f"/api/reports/r-1{path}").status_code == 404, path
    assert other_client.post("/api/reports/r-1/pdf/retry").status_code == 404
    # The token is the credential, so it works for anyone; it is never tied to, or widened by, an org.
    assert public.get(f"/api/public/reports/{token}").status_code == 200


def test_the_token_appears_only_in_the_creation_response(public, fake_client, client):
    created = client.post(BASE, json={})
    token = created.json()["token"]
    assert token in created.text

    later = [
        client.get(BASE),
        client.get("/api/reports/r-1"),
        client.post(f"/api/share-links/{created.json()['id']}/revoke"),
        client.get(BASE),
    ]

    for response in later:
        assert token not in response.text and report_router_module.hash_share_token(token) not in response.text
