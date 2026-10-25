"""Browser writes, autosave, upload provenance and CSP compatibility."""

from pathlib import Path
import re

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

import app.main as main
from app.domain import ControleTechnique, Dossier, Planche
from app.profiles.models import DrawingProfile
from app.quick_analysis import QuickAnalysisRepository
from app.security import SecurityMiddleware
from app.storage import JsonDossierRepository
from app.version import __version__
from app.workflow import record_data_validation


POST_PATHS = [
    re.sub(r"\{[^}]+\}", "missing", route.path)
    for route in main.app.routes
    if "POST" in getattr(route, "methods", set())
]


@pytest.mark.parametrize("path", POST_PATHS)
@pytest.mark.parametrize("headers", [
    {"Origin": "https://attacker.example"},
    {"Sec-Fetch-Site": "same-site", "Origin": "http://testserver"},
])
def test_all_post_routes_reject_foreign_browser_writes(path, headers):
    response = TestClient(main.app).post(path, headers=headers)
    assert response.status_code == 403
    assert response.headers["x-content-type-options"] == "nosniff"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "repository", JsonDossierRepository(tmp_path / "data"))
    monkeypatch.setattr(main, "analysis_repository", QuickAnalysisRepository(tmp_path / "analyses"))
    return TestClient(main.app)


@pytest.mark.parametrize("headers", [
    {"Origin": "null"},
    {"Origin": "http://testserver.evil"},
    {"Origin": "https://testserver"},
    {"Origin": "http://testserver:8080"},
    {"Origin": "http://testserver:0"},
    {"Origin": "http://testserver/path"},
    {"Origin": "http://user@testserver"},
    {"Origin": "http://testserver:bad"},
    {"Origin": "http://testserver", "Sec-Fetch-Site": "cross-site"},
    {"Sec-Fetch-Site": "none"},
    {"Referer": "https://attacker.example/form"},
    {"Origin": "null", "Referer": "http://testserver/form"},
    {"Cookie": "synthetic-session=value"},
    [("Origin", "http://testserver"), ("Origin", "https://attacker.example")],
])
def test_untrusted_provenance_never_creates_a_dossier(client, headers):
    response = client.post("/dossiers", data={"reference": "REFUSED"}, headers=headers)
    assert response.status_code == 403
    assert main.repository.list() == []


@pytest.mark.parametrize("headers", [
    {"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"},
    {"Origin": "http://testserver:80", "Cookie": "synthetic-session=value"},
    {"Referer": "http://testserver/dossiers?tab=plan"},
    {},  # Direct CLI clients without browser cookies remain supported.
])
def test_same_origin_form_autosave_and_upload_work(client, headers):
    response = client.post(
        "/dossiers", data={"reference": "SYNTHETIC"}, headers=headers, follow_redirects=False,
    )
    assert response.status_code == 303
    dossier = main.repository.list()[0]
    saved = client.post(
        f"/dossiers/{dossier.id}/reference", data={"reference": "RENAMED"},
        headers={**headers, "Accept": "application/json"},
    )
    assert saved.status_code == 200
    assert main.repository.get(dossier.id).reference == "RENAMED"
    source = Path(__file__).parent / "fixtures" / "geoflow_plan_test_source.dxf"
    uploaded = client.post(
        f"/dossiers/{dossier.id}/import",
        files={"file": (source.name, source.read_bytes(), "application/dxf")},
        headers=headers, follow_redirects=False,
    )
    assert uploaded.status_code == 303
    assert main.repository.get(dossier.id).controle_technique is not None


def tunnel_app(public_origin="https://geoflow.example.com"):
    app = FastAPI()
    app.add_middleware(SecurityMiddleware, public_origin=public_origin)

    @app.post("/write")
    async def write():
        return {"ok": True}

    return TestClient(app, base_url="http://geoflow.example.com")


def test_tls_termination_uses_explicit_public_origin_only():
    client = tunnel_app()
    assert client.post("/write", headers={"Origin": "https://geoflow.example.com"}).status_code == 200
    assert client.post("/write", headers={"Origin": "https://geoflow.example.com:443"}).status_code == 200
    assert client.post("/write", headers={"Origin": "https://geoflow.example.com:8443"}).status_code == 403
    assert client.post("/write", headers={"Origin": "https://another.example.com"}).status_code == 403
    unconfigured = tunnel_app("")
    assert unconfigured.post("/write", headers={
        "Origin": "https://geoflow.example.com", "X-Forwarded-Proto": "https",
    }).status_code == 403


@pytest.mark.parametrize("origin", ["null", "https://example.com/path", "https://user@example.com"])
def test_invalid_public_origin_fails_closed(origin):
    with pytest.raises(ValueError, match="GEOFLOW_PUBLIC_ORIGIN"):
        tunnel_app(origin).post("/write")


@pytest.mark.parametrize("path", ["/", "/health", "/static/app.js", "/does-not-exist"])
def test_security_headers_cover_pages_static_health_and_errors(client, path):
    response = client.get(path)
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "same-origin"
    assert "camera=()" in response.headers["permissions-policy"]
    policy = response.headers["content-security-policy"]
    assert "script-src 'self'" in policy
    assert "frame-ancestors 'none'" in policy
    assert "unsafe-inline" not in policy


def test_theme_is_external_and_version_is_shared(client):
    page = client.get("/").text
    assert not re.search(r"<script\b(?![^>]*\bsrc=)", page)
    assert "/static/theme.js?v=" in page
    assert client.get("/static/theme.js").status_code == 200
    assert f"v{__version__}" in page
    assert main.app.version == __version__


@pytest.mark.parametrize("extra_byte", [False, True])
def test_real_fifty_mib_upload_boundary(client, monkeypatch, extra_byte):
    inspections = []

    def inspect(path, filename):
        inspections.append((path, path.stat().st_size))
        return ControleTechnique(version_dxf="AC1032", unite_detectee="metre"), [
            Planche(id="model", titre="Model", methode_detection="layout_dxf")
        ]

    monkeypatch.setattr(main, "inspect_dxf", inspect)
    size = 50 * 1024 * 1024 + int(extra_byte)
    response = client.post(
        "/analyze", files={"file": ("synthetic.dxf", b"x" * size)},
        headers={"Origin": "http://testserver"}, follow_redirects=False,
    )
    assert response.status_code == (413 if extra_byte else 303)
    if extra_byte:
        assert inspections == []
    else:
        assert inspections[0][1] == size
        assert not inspections[0][0].exists()


def test_get_screens_do_not_modify_business_files(client):
    fixture = Path(__file__).parent / "fixtures" / "copropriete_complete.json"
    dossier = Dossier.model_validate_json(fixture.read_text())
    record_data_validation(dossier)
    main.repository.save(dossier)
    profile = main.profile_repository.save(DrawingProfile(name="Synthetic GET check"))
    source = fixture.with_name("geoflow_plan_test_source.dxf")
    uploaded = client.post(
        "/analyze", files={"file": (source.name, source.read_bytes())}, follow_redirects=False,
    )
    analysis_path = uploaded.headers["location"].removesuffix("/profil")
    client.post(analysis_path + "/profil", data={"profile": "legacy"})

    def business_files():
        roots = (main.repository.root, main.analysis_repository.root, main.profile_repository.root)
        return {str(path): path.read_bytes() for root in roots for path in root.rglob("*.json")}

    before = business_files()
    pages = ["/", "/health", "/profils", f"/profils/{profile.id}", analysis_path, analysis_path + "/profil"]
    pages += [f"/dossiers/{dossier.id}/{step}" for step in (
        "dossier", "plan", "controle", "lots", "synthese", "documents",
    )]
    for path in pages:
        assert client.get(path).status_code == 200
    assert business_files() == before


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/docs/oauth2-redirect"])
def test_generated_api_docs_keep_compatibility_without_weakening_app_csp(client, path):
    response = client.get(path)
    assert response.status_code == 200
    assert "content-security-policy" not in response.headers
    assert response.headers["x-frame-options"] == "DENY"
    assert "content-security-policy" in client.get("/").headers
