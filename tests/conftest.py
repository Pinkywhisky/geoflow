"""Shared test isolation."""

from __future__ import annotations

from pathlib import Path

import pytest

import app.main as main
from app.profiles.compatibility import CompatibilityCache
from app.profiles.storage import ProfileRepository


@pytest.fixture(autouse=True)
def isolated_profiles(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Never read or write the drawing profiles of the running instance:
    their presence changes the upload flow (profile choice)."""

    root = tmp_path_factory.mktemp("profiles")
    monkeypatch.setattr(main, "profile_repository", ProfileRepository(root))
    monkeypatch.setattr(main, "compatibility_cache", CompatibilityCache())
    return root
