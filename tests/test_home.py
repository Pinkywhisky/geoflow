from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.storage import JsonDossierRepository
from app.workflow import create_dossier


@pytest.fixture
def home_client(tmp_path, monkeypatch):
    repository = JsonDossierRepository(tmp_path)
    monkeypatch.setattr(main, "repository", repository)
    return TestClient(main.app), repository


def test_empty_home_explains_the_three_steps(home_client) -> None:
    client, _ = home_client
    page = client.get("/")
    assert page.status_code == 200
    assert 'class="onboarding"' in page.text
    assert "data-drop-form" in page.text
    assert 'class="count"' not in page.text


def test_home_lists_most_recent_dossier_first_with_progress(home_client, tmp_path) -> None:
    client, repository = home_client
    older = create_dossier("PLUS-ANCIEN", "copropriete")
    newer = create_dossier("PLUS-RECENT", "copropriete")
    repository.save(older)
    repository.save(newer)
    os.utime(tmp_path / f"{older.id}.json", (1_000_000_000, 1_000_000_000))

    assert [item.reference for item in repository.list()] == ["PLUS-RECENT", "PLUS-ANCIEN"]
    page = client.get("/").text
    assert page.index("PLUS-RECENT") < page.index("PLUS-ANCIEN")
    assert f'href="/dossiers/{newer.id}/plan"' in page
    assert "2/6 · Plan" in page
    assert "modifié le 09/09/2001" in page


def test_blank_dossier_form_reopens_on_creation_error(home_client) -> None:
    client, _ = home_client
    response = client.post("/dossiers", data={"reference": "   "})
    assert response.status_code == 400
    assert '<details class="blank-dossier" open>' in response.text


def test_relative_time_labels() -> None:
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    assert main._relative_time(now) == "à l’instant"
    assert main._relative_time(now - timedelta(minutes=5)) == "il y a 5 min"
    assert main._relative_time(now - timedelta(hours=3)) == "il y a 3 h"
    assert main._relative_time(now - timedelta(hours=30)) == "hier"
    assert main._relative_time(None) == ""


def test_static_assets_are_versioned_by_content(home_client) -> None:
    client, _ = home_client
    page = client.get("/").text
    version = main.templates.env.globals["static_version"]
    assert len(version) == 10
    assert f'/static/style.css?v={version}"' in page
    assert f'/static/app.js?v={version}"' in page
    assert client.get(f"/static/style.css?v={version}").status_code == 200
