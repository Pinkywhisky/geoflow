from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.domain import ControleTechnique, Planche
from app.dxf import DxfAnalysisError
from app.quick_analysis import QuickAnalysisRepository

SAMPLE = Path(__file__).parents[1] / "samples" / "exemple_geometre.dxf"
client = TestClient(main.app)


@pytest.fixture(autouse=True)
def isolated_quick_analyses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        main,
        "analysis_repository",
        QuickAnalysisRepository(tmp_path / "quick-analyses"),
    )


def test_health() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_upload_valid_dxf() -> None:
    with SAMPLE.open("rb") as upload:
        response = client.post(
            "/analyze", files={"file": (SAMPLE.name, upload, "application/dxf")}
        )
    assert response.status_code == 200
    assert "Analyse terminée" in response.text
    assert "Contours analysés" in response.text
    assert "Créer un dossier à partir de cette analyse" in response.text


def test_upload_wrong_extension() -> None:
    response = client.post(
        "/analyze", files={"file": ("plan.txt", b"not a dxf", "text/plain")}
    )
    assert response.status_code == 400
    assert "Seuls les fichiers .dxf sont acceptés" in response.text


def test_upload_invalid_dxf_has_safe_error() -> None:
    response = client.post(
        "/analyze", files={"file": ("invalid.dxf", b"not a dxf", "application/dxf")}
    )
    assert response.status_code == 422
    assert "Impossible d’analyser ce fichier DXF" in response.text
    assert "Traceback" not in response.text


def test_upload_size_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main, "MAX_FILE_SIZE", 10)
    response = client.post(
        "/analyze", files={"file": ("large.dxf", b"x" * 11, "application/dxf")}
    )
    assert response.status_code == 413
    assert "taille maximale" in response.text


@pytest.mark.parametrize("outcome", ["success", "dxf_error", "unexpected"])
def test_temporary_file_removed(
    outcome: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_factory = main.tempfile.NamedTemporaryFile
    created_paths: list[Path] = []

    def tracked_temporary_file(**kwargs: object):
        requested_dir = kwargs.pop("dir", tmp_path)
        temporary = original_factory(dir=requested_dir, **kwargs)
        if kwargs.get("suffix") == ".dxf":
            created_paths.append(Path(temporary.name))
        return temporary

    monkeypatch.setattr(main.tempfile, "NamedTemporaryFile", tracked_temporary_file)
    if outcome == "success":
        monkeypatch.setattr(
            main,
            "inspect_dxf",
            lambda path, filename: (
                ControleTechnique(version_dxf="AC1024", unite_detectee="metre"),
                [
                    Planche(
                        id="model",
                        titre="Model",
                        methode_detection="layout_dxf",
                    )
                ],
            ),
        )
    elif outcome == "dxf_error":
        def raise_dxf_error(path: Path, filename: str) -> None:
            raise DxfAnalysisError("invalid")

        monkeypatch.setattr(main, "inspect_dxf", raise_dxf_error)
    else:
        def raise_unexpected(path: Path, filename: str) -> None:
            raise RuntimeError("unexpected")

        monkeypatch.setattr(main, "inspect_dxf", raise_unexpected)

    response = client.post("/analyze", files={"file": ("plan.dxf", b"data")})
    assert response.status_code == (500 if outcome == "unexpected" else 200 if outcome == "success" else 422)

    assert len(created_paths) == 1
    assert not created_paths[0].exists()
