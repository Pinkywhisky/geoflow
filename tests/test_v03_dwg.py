import subprocess
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.dwg.converter as converter
import app.main as main
from app.dwg import DwgConversionError, OdaNotAvailableError, converted_dwg
from app.storage import JsonDossierRepository

from .test_v03_technical import build_technical_dxf


SAMPLE = Path(__file__).parents[1] / "samples" / "exemple_geometre.dxf"


def test_oda_missing_has_a_specific_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ODA_FILE_CONVERTER", raising=False)
    monkeypatch.setattr(converter.shutil, "which", lambda executable: None)
    with pytest.raises(OdaNotAvailableError, match="absent"):
        converter._resolve_executable()


def test_oda_can_be_wrapped_in_an_isolated_virtual_display(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ODA_USE_XVFB", "1")
    monkeypatch.setattr(
        converter.shutil,
        "which",
        lambda executable: "/usr/bin/xvfb-run"
        if executable == "xvfb-run"
        else None,
    )
    command = converter._converter_command(
        "/usr/bin/ODAFileConverter", ["/input", "/output"]
    )
    assert command == [
        "/usr/bin/xvfb-run",
        "-a",
        "--server-args=-screen 0 1024x768x24",
        "/usr/bin/ODAFileConverter",
        "/input",
        "/output",
    ]


def test_oda_conversion_error_is_wrapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.dwg"
    source.write_bytes(b"synthetic dwg")
    monkeypatch.setattr(converter, "_resolve_executable", lambda: "/fake/oda")
    monkeypatch.setattr(
        converter.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 7, "", "failure"),
    )
    with pytest.raises(DwgConversionError, match="retourne une erreur"):
        with converted_dwg(source):
            pass


def test_temporary_converted_dxf_is_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ODA_USE_XVFB", raising=False)
    source = tmp_path / "unsafe name.dwg"
    source.write_bytes(b"synthetic dwg")
    seen: dict[str, Path] = {}
    monkeypatch.setattr(converter, "_resolve_executable", lambda: "/fake/oda")

    def successful_conversion(command, **kwargs):
        input_dir = Path(command[1])
        output_dir = Path(command[2])
        seen["workspace"] = output_dir.parent
        seen["input"] = next(input_dir.iterdir())
        converted = output_dir / "source.dxf"
        converted.write_bytes(SAMPLE.read_bytes())
        return subprocess.CompletedProcess(command, 0, "ok", "")

    monkeypatch.setattr(converter.subprocess, "run", successful_conversion)
    with converted_dwg(source) as converted:
        seen["converted"] = converted
        assert converted.exists()
        assert seen["input"].name == "source.dwg"
    assert not seen["workspace"].exists()
    assert not seen["converted"].exists()


@pytest.fixture
def web_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> TestClient:
    monkeypatch.setattr(
        main, "repository", JsonDossierRepository(tmp_path / "web-json")
    )
    return TestClient(main.app)


def test_http_accepts_dwg_and_reuses_dxf_engine(
    web_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    @contextmanager
    def fake_source(source: Path, file_type: str):
        assert file_type == "dwg"
        yield SAMPLE

    monkeypatch.setattr(main, "dxf_source", fake_source)
    response = web_client.post(
        "/analyze", files={"file": ("plan.dwg", b"synthetic", "application/acad")}
    )
    assert response.status_code == 200
    assert "190.00 m²" in response.text


def test_http_reports_missing_oda(
    web_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    @contextmanager
    def missing_source(source: Path, file_type: str):
        raise OdaNotAvailableError("missing")
        yield source

    monkeypatch.setattr(main, "dxf_source", missing_source)
    response = web_client.post(
        "/analyze", files={"file": ("plan.dwg", b"synthetic")}
    )
    assert response.status_code == 503
    assert "ODA File Converter" in response.text
    assert "Traceback" not in response.text


def test_http_reports_oda_conversion_error(
    web_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    @contextmanager
    def failed_source(source: Path, file_type: str):
        raise DwgConversionError("failed")
        yield source

    monkeypatch.setattr(main, "dxf_source", failed_source)
    response = web_client.post(
        "/analyze", files={"file": ("plan.dwg", b"synthetic")}
    )
    assert response.status_code == 422
    assert "Impossible de convertir" in response.text
    assert "Traceback" not in response.text


def test_guided_dwg_import_sanitizes_name_and_escapes_text(
    tmp_path: Path,
    web_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    technical = build_technical_dxf(tmp_path / "converted.dxf")

    @contextmanager
    def fake_source(source: Path, file_type: str):
        assert file_type == "dwg"
        yield technical

    monkeypatch.setattr(main, "dxf_source", fake_source)
    created = web_client.post(
        "/dossiers",
        data={"reference": "SAFE", "dossier_type": "copropriete"},
        follow_redirects=False,
    )
    assert created.status_code == 303
    location = created.headers["location"]
    dossier_id = location.split("/")[-2]
    dossier_url = f"/dossiers/{dossier_id}"
    imported = web_client.post(
        f"{dossier_url}/import",
        files={"file": ("..\\..\\unsafe.dwg", b"synthetic")},
        follow_redirects=False,
    )
    assert imported.status_code == 303
    confirmed = web_client.post(
        f"{dossier_url}/unite",
        data={"unit": "metre", "justification": ""},
        follow_redirects=False,
    )
    assert confirmed.status_code == 303

    page = web_client.get(f"{dossier_url}/lots?assignment=all")
    assert page.status_code == 200
    assert "<script>alert(1)</script>" not in page.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page.text
    dossier = main.repository.get(dossier_id)
    assert dossier.plan_importe.nom_fichier_original == "unsafe.dwg"
    assert dossier.plan_importe.type_fichier == "dwg"
    exported = web_client.get(f"{dossier_url}/export")
    assert exported.status_code == 200
    assert ".." not in exported.json()["plan_importe"]["nom_fichier_original"]


def test_oversized_partial_upload_is_removed(
    tmp_path: Path,
    web_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_factory = main.tempfile.NamedTemporaryFile
    created: list[Path] = []

    def tracked(**kwargs):
        temporary = original_factory(dir=tmp_path, **kwargs)
        created.append(Path(temporary.name))
        return temporary

    monkeypatch.setattr(main.tempfile, "NamedTemporaryFile", tracked)
    monkeypatch.setattr(main, "MAX_FILE_SIZE", 5)
    response = web_client.post(
        "/analyze", files={"file": ("large.dwg", b"123456")}
    )
    assert response.status_code == 413
    assert len(created) == 1
    assert not created[0].exists()
