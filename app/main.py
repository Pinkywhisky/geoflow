"""FastAPI entry point for GeoFlow."""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.domain.models import StatutRevue
from app.dwg import DwgConversionError, OdaNotAvailableError, dxf_source
from app.dxf import DxfAnalysisError, analyze_dxf, inspect_dxf
from app.storage import DossierNotFoundError, JsonDossierRepository
from app.workflow import (
    associate_candidate,
    attach_import,
    confirm_unit,
    create_dossier,
    safe_filename,
    set_layer_status,
    set_planche_status,
)


MAX_FILE_SIZE = 50 * 1024 * 1024
CHUNK_SIZE = 1024 * 1024
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("GEOFLOW_DATA_DIR", BASE_DIR.parent / "data"))
logger = logging.getLogger(__name__)

app = FastAPI(title="GeoFlow", version="0.3.0")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")
repository = JsonDossierRepository(DATA_DIR)


class UploadTooLargeError(RuntimeError):
    pass


def _index(
    request: Request, message: str | None = None, status_code: int = 200
) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"message": message, "dossiers": repository.list()},
        status_code=status_code,
    )


def _dossier_page(
    request: Request,
    dossier: object,
    message: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="dossier.html",
        context={
            "dossier": dossier,
            "message": message,
            "unit_options": [
                ("metre", "Mètre"),
                ("millimetre", "Millimètre"),
                ("centimetre", "Centimètre"),
                ("pied", "Pied"),
                ("pouce", "Pouce"),
            ],
            "categories": [
                ("principale", "Principale"),
                ("secondaire_annexe", "Secondaire / annexe"),
                ("exclue", "Exclue"),
                ("commune", "Commune"),
                ("autre", "Autre"),
            ],
        },
        status_code=status_code,
    )


def _get_dossier(dossier_id: str):
    try:
        return repository.get(dossier_id)
    except DossierNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Dossier introuvable.") from exc


def _remove_temporary_file(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.exception("Could not remove temporary upload: %s", path)


async def _save_temporary_upload(file: UploadFile, suffix: str) -> Path:
    path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as temporary:
            path = Path(temporary.name)
            size = 0
            # UploadFile is spooled by Starlette. Chunked reads bound application
            # memory and apply the explicit 50 MiB limit before analysis/conversion.
            while chunk := await file.read(CHUNK_SIZE):
                size += len(chunk)
                if size > MAX_FILE_SIZE:
                    raise UploadTooLargeError
                temporary.write(chunk)
        return path
    except Exception:
        _remove_temporary_file(path)
        raise


def _upload_metadata(file: UploadFile | None) -> tuple[str, str]:
    if file is None or not file.filename:
        raise ValueError("Veuillez choisir un fichier DXF ou DWG.")
    filename = safe_filename(file.filename)
    extension = Path(filename).suffix.lower()
    if extension not in {".dxf", ".dwg"}:
        raise ValueError(
            "Seuls les fichiers .dxf sont acceptés ici; "
            "les fichiers .dwg sont également acceptés."
        )
    return filename, extension.removeprefix(".")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return _index(request)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/dossiers", response_class=HTMLResponse)
async def new_dossier(
    request: Request,
    reference: str = Form(...),
    dossier_type: str = Form("copropriete"),
) -> Response:
    try:
        dossier = create_dossier(reference, dossier_type)
    except ValueError as exc:
        return _index(request, str(exc), 400)
    repository.save(dossier)
    return RedirectResponse(f"/dossiers/{dossier.id}", status_code=303)


@app.get("/dossiers/{dossier_id}", response_class=HTMLResponse)
async def dossier_page(request: Request, dossier_id: str) -> HTMLResponse:
    return _dossier_page(request, _get_dossier(dossier_id))


@app.post("/dossiers/{dossier_id}/import", response_class=HTMLResponse)
async def import_plan(
    request: Request, dossier_id: str, file: UploadFile | None = None
) -> Response:
    dossier = _get_dossier(dossier_id)
    temporary_path: Path | None = None
    try:
        filename, file_type = _upload_metadata(file)
        assert file is not None
        temporary_path = await _save_temporary_upload(file, f".{file_type}")
        with dxf_source(temporary_path, file_type) as dxf_path:
            control, planches = inspect_dxf(dxf_path, filename)
        attach_import(dossier, filename, file_type, control, planches)
        repository.save(dossier)
        return RedirectResponse(f"/dossiers/{dossier.id}", status_code=303)
    except ValueError as exc:
        return _dossier_page(request, dossier, str(exc), 400)
    except UploadTooLargeError:
        return _dossier_page(
            request, dossier, "Le fichier dépasse la taille maximale de 50 Mo.", 413
        )
    except OdaNotAvailableError:
        logger.exception("DWG import requested without ODA File Converter")
        return _dossier_page(
            request,
            dossier,
            "Conversion DWG indisponible : ODA File Converter n'est pas installé.",
            503,
        )
    except DwgConversionError:
        logger.exception("DWG conversion failed")
        return _dossier_page(
            request, dossier, "Impossible de convertir ce fichier DWG.", 422
        )
    except DxfAnalysisError:
        logger.exception("Imported DXF analysis failed")
        return _dossier_page(
            request, dossier, "Impossible d'analyser le plan converti.", 422
        )
    except OSError:
        logger.exception("Plan import handling failed")
        return _dossier_page(
            request, dossier, "Impossible de traiter le fichier envoyé.", 500
        )
    except Exception:
        logger.exception("Unexpected plan import failure")
        return _dossier_page(
            request, dossier, "Une erreur inattendue empêche cet import.", 500
        )
    finally:
        if file is not None:
            await file.close()
        _remove_temporary_file(temporary_path)


@app.post("/dossiers/{dossier_id}/unite", response_class=HTMLResponse)
async def validate_unit(
    request: Request,
    dossier_id: str,
    unit: str = Form(...),
    justification: str = Form(""),
) -> Response:
    dossier = _get_dossier(dossier_id)
    try:
        confirm_unit(dossier, unit, justification)
        repository.save(dossier)
    except ValueError as exc:
        return _dossier_page(request, dossier, str(exc), 400)
    return RedirectResponse(f"/dossiers/{dossier.id}", status_code=303)


@app.post("/dossiers/{dossier_id}/planches/{planche_id}")
async def review_planche(
    request: Request,
    dossier_id: str,
    planche_id: str,
    status: str = Form(...),
) -> Response:
    dossier = _get_dossier(dossier_id)
    try:
        set_planche_status(dossier, planche_id, StatutRevue(status))
        repository.save(dossier)
    except ValueError as exc:
        return _dossier_page(request, dossier, str(exc), 400)
    return RedirectResponse(f"/dossiers/{dossier.id}", status_code=303)


@app.post("/dossiers/{dossier_id}/calques")
async def review_layer(
    request: Request,
    dossier_id: str,
    layer_name: str = Form(...),
    status: str = Form(...),
) -> Response:
    dossier = _get_dossier(dossier_id)
    try:
        set_layer_status(dossier, layer_name, StatutRevue(status))
        repository.save(dossier)
    except ValueError as exc:
        return _dossier_page(request, dossier, str(exc), 400)
    return RedirectResponse(f"/dossiers/{dossier.id}", status_code=303)


@app.post("/dossiers/{dossier_id}/zones/{candidate_id}")
async def validate_zone(
    request: Request,
    dossier_id: str,
    candidate_id: str,
    building_code: str = Form(...),
    level_code: str = Form(...),
    lot_number: str = Form(""),
    category: str = Form(...),
    retained_surface: str = Form(""),
    justification: str = Form(""),
) -> Response:
    dossier = _get_dossier(dossier_id)
    try:
        selected_surface = (
            float(retained_surface.replace(",", ".")) if retained_surface.strip() else None
        )
        associate_candidate(
            dossier=dossier,
            candidate_id=candidate_id,
            building_code=building_code,
            level_code=level_code,
            lot_number=lot_number,
            category=category,
            retained_surface=selected_surface,
            justification=justification,
        )
        repository.save(dossier)
    except ValueError as exc:
        return _dossier_page(request, dossier, str(exc), 400)
    return RedirectResponse(f"/dossiers/{dossier.id}", status_code=303)


@app.get("/dossiers/{dossier_id}/export")
async def export_dossier(dossier_id: str) -> Response:
    dossier = _get_dossier(dossier_id)
    return Response(
        dossier.model_dump_json(indent=2),
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="geoflow-{dossier.id}.json"'
        },
    )


@app.post("/analyze", response_class=HTMLResponse)
async def analyze(request: Request, file: UploadFile | None = None) -> HTMLResponse:
    """Compatibility endpoint for the v0.2 one-shot surface analysis."""

    temporary_path: Path | None = None
    try:
        filename, file_type = _upload_metadata(file)
        assert file is not None
        temporary_path = await _save_temporary_upload(file, f".{file_type}")
        with dxf_source(temporary_path, file_type) as dxf_path:
            polylines = analyze_dxf(dxf_path)
        if not polylines:
            return _index(request, "Aucune polyligne fermée n'a été détectée.", 422)
        return templates.TemplateResponse(
            request=request,
            name="results.html",
            context={
                "filename": filename,
                "polylines": polylines,
                "total_area": sum(item["area"] for item in polylines),
            },
        )
    except ValueError as exc:
        return _index(request, str(exc), 400)
    except UploadTooLargeError:
        return _index(request, "Le fichier dépasse la taille maximale de 50 Mo.", 413)
    except OdaNotAvailableError:
        logger.exception("DWG analysis requested without ODA File Converter")
        return _index(
            request,
            "Conversion DWG indisponible : ODA File Converter n'est pas installé.",
            503,
        )
    except DwgConversionError:
        logger.exception("DWG conversion failed")
        return _index(request, "Impossible de convertir ce fichier DWG.", 422)
    except DxfAnalysisError:
        logger.exception("DXF analysis failed")
        return _index(request, "Impossible d’analyser ce fichier DXF.", 422)
    except OSError:
        logger.exception("Temporary upload handling failed")
        return _index(request, "Impossible de traiter le fichier envoyé.", 500)
    finally:
        if file is not None:
            await file.close()
        _remove_temporary_file(temporary_path)
