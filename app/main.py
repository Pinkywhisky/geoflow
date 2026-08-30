"""FastAPI entry point for GeoFlow."""

from __future__ import annotations

import logging
import math
import os
import tempfile
import time
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.domain import (
    Dossier,
    StatutValidationDonnees,
    StatutValidationMillieme,
    VerificationStatus,
)
from app.domain.models import StatutRevue
from app.documents import (
    GenerationBlockedError,
    TemplateCorruptError,
    TemplateMissingError,
    assess_generation_readiness,
    generate_copropriete_draft,
)
from app.documents.generator import DOCUMENT_MIME
from app.dwg import DwgConversionError, OdaNotAvailableError, dxf_source
from app.dxf import DxfAnalysisError, inspect_dxf
from app.quick_analysis import (
    QuickAnalysisNotFoundError,
    QuickAnalysisRepository,
    build_quick_diagnostic,
    prepare_quick_analysis_dossier,
    promote_quick_analysis,
)
from app.reconciliation import reconcile_dossier
from app.storage import DossierNotFoundError, JsonDossierRepository
from app.workflow import (
    add_parcel,
    associate_candidate,
    attach_import,
    confirm_lot_proposal,
    confirm_unit,
    create_dossier,
    has_current_data_validation,
    record_data_validation,
    mark_lot_proposal_for_review,
    remove_parcel,
    safe_filename,
    set_millieme_grid_complete,
    set_layer_status,
    set_planche_status,
    update_dossier_details,
    update_lot_metadata,
    update_parcel,
)


MAX_FILE_SIZE = 50 * 1024 * 1024
CHUNK_SIZE = 1024 * 1024
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("GEOFLOW_DATA_DIR", BASE_DIR.parent / "data"))
logger = logging.getLogger(__name__)

app = FastAPI(title="GeoFlow", version="0.4.3")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")
repository = JsonDossierRepository(DATA_DIR)
analysis_repository = QuickAnalysisRepository(DATA_DIR / "quick-analyses")

UNIT_OPTIONS = [
    ("metre", "Mètre"),
    ("millimetre", "Millimètre"),
    ("centimetre", "Centimètre"),
    ("pied", "Pied"),
    ("pouce", "Pouce"),
]
CATEGORIES = [
    ("principale", "Principale"),
    ("secondaire_annexe", "Secondaire / annexe"),
    ("exclue", "Exclue"),
    ("commune", "Commune"),
    ("autre", "Autre"),
]
USAGE_OPTIONS = (
    "Appartement",
    "Local commercial",
    "Cave",
    "Débarras",
    "Garage",
    "Bureau",
    "Autre",
)
MILLIEME_STATUS_OPTIONS = (
    (StatutValidationMillieme.A_CONFIRMER.value, "À confirmer"),
    (StatutValidationMillieme.VALIDE.value, "Validé"),
)
PAGE_SIZE = 25


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


def _workflow_steps(dossier: Dossier, active: str) -> list[dict[str, object]]:
    plan_ready = dossier.plan_importe is not None
    control_ready = plan_ready and bool(dossier.plan_importe.unite_confirmee)
    validated = has_current_data_validation(dossier)
    definitions = [
        ("dossier", "1. Dossier", f"/dossiers/{dossier.id}/dossier", True),
        ("plan", "2. Plan", f"/dossiers/{dossier.id}/plan", True),
        ("controle", "3. Contrôle", f"/dossiers/{dossier.id}/controle", plan_ready),
        ("lots", "4. Lots & surfaces", f"/dossiers/{dossier.id}/lots", control_ready),
        ("synthese", "5. Synthèse", f"/dossiers/{dossier.id}/synthese", control_ready),
        ("document", "6. Document", f"/dossiers/{dossier.id}/documents", validated),
    ]
    completed = {
        "dossier": True,
        "plan": plan_ready,
        "controle": control_ready,
        "lots": bool(dossier.zones),
        "synthese": validated,
        "document": bool(dossier.generations),
    }
    return [
        {
            "id": step_id,
            "label": label,
            "href": href if accessible else None,
            "active": step_id == active,
            "completed": completed[step_id],
            "locked": not accessible,
        }
        for step_id, label, href, accessible in definitions
    ]


def _workflow_page(
    request: Request,
    template_name: str,
    dossier: Dossier,
    active: str,
    message: str | None = None,
    status_code: int = 200,
    **context: object,
) -> HTMLResponse:
    labels = {
        StatutValidationDonnees.BROUILLON: "Brouillon",
        StatutValidationDonnees.A_VALIDER: "À valider",
        StatutValidationDonnees.VALIDE: "Validé",
    }
    return templates.TemplateResponse(
        request=request,
        name=template_name,
        context={
            "dossier": dossier,
            "message": message,
            "steps": _workflow_steps(dossier, active),
            "active_step": active,
            "workflow_status": labels[dossier.statut_validation_donnees],
            **context,
        },
        status_code=status_code,
    )


def _dossier_page(
    request: Request,
    dossier: Dossier,
    message: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    return _workflow_page(
        request, "dossier.html", dossier, "dossier", message, status_code
    )


def _plan_page(
    request: Request,
    dossier: Dossier,
    message: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    return _workflow_page(request, "plan.html", dossier, "plan", message, status_code)


def _control_page(
    request: Request,
    dossier: Dossier,
    message: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    return _workflow_page(
        request,
        "controle.html",
        dossier,
        "controle",
        message,
        status_code,
        unit_options=UNIT_OPTIONS,
    )


def _document_page(
    request: Request,
    dossier: Dossier,
    message: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    return _workflow_page(
        request,
        "document.html",
        dossier,
        "document",
        message,
        status_code,
        readiness=assess_generation_readiness(dossier),
        validation_current=has_current_data_validation(dossier),
    )


def _wants_json(request: Request) -> bool:
    return "application/json" in request.headers.get("accept", "")


def _saved_response(
    request: Request, dossier: Dossier, redirect_to: str
) -> Response:
    repository.save(dossier)
    if _wants_json(request):
        return JSONResponse(
            {
                "status": "ok",
                "message": "Enregistré",
                "validation_status": dossier.statut_validation_donnees.value,
            }
        )
    return RedirectResponse(redirect_to, status_code=303)


def _mutation_error(
    request: Request,
    dossier: Dossier,
    message: str,
    page: str,
) -> Response:
    if _wants_json(request):
        return JSONResponse(
            {"status": "error", "message": message},
            status_code=400,
        )
    renderers = {
        "controle": _control_page,
        "dossier": _dossier_page,
        "lots": _lots_page,
    }
    renderer = renderers[page]
    return renderer(request, dossier, message, 400)


def _get_dossier(dossier_id: str):
    try:
        return repository.get(dossier_id)
    except DossierNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Dossier introuvable.") from exc


def _lots_page(
    request: Request,
    dossier: Dossier,
    message: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    control = dossier.controle_technique
    if control is None:
        return _plan_page(
            request,
            dossier,
            "Importez un plan avant d'accéder aux lots et surfaces.",
            409,
        )

    if dossier.reconciliation is None:
        reconcile_dossier(dossier)
    reconciliation = dossier.reconciliation
    assert reconciliation is not None
    milliemes_by_lot = {item.lot_id: item for item in dossier.milliemes}

    query = request.query_params.get("q", "").strip()
    proposal_status = request.query_params.get(
        "proposal_status", "review"
    ).strip()
    technical_mode = (
        request.query_params.get("view") == "technical"
        or "assignment" in request.query_params
    )
    try:
        requested_page = max(1, int(request.query_params.get("page", "1")))
    except ValueError:
        requested_page = 1

    candidate_by_id = {item.id: item for item in control.zones_candidates}
    evidence_by_id = {item.id: item for item in reconciliation.preuves}
    zones_by_candidate = {
        zone.id.removeprefix("validee-"): zone for zone in dossier.zones
    }
    proposal_rows: list[dict[str, object]] = []
    technical_rows: list[dict[str, object]] = []

    if technical_mode:
        layer_filter = request.query_params.get("layer", "").strip()
        status_filter = request.query_params.get("status", "").strip()
        assignment_filter = request.query_params.get(
            "assignment", "unassigned"
        ).strip()
        filtered_candidates = []
        for candidate in control.zones_candidates:
            zone = zones_by_candidate.get(candidate.id)
            haystack = " ".join(
                [
                    candidate.id,
                    candidate.calque,
                    candidate.handle_dxf,
                    *candidate.textes_proches,
                ]
            ).casefold()
            if query and query.casefold() not in haystack:
                continue
            if layer_filter and candidate.calque != layer_filter:
                continue
            if status_filter and candidate.statut.value != status_filter:
                continue
            if assignment_filter == "assigned" and zone is None:
                continue
            if assignment_filter == "unassigned" and zone is not None:
                continue
            filtered_candidates.append(candidate)
        filtered_candidates.sort(
            key=lambda item: (
                item.statut != StatutRevue.CANDIDATE,
                item.calque.casefold(),
                item.id,
            )
        )
        filtered_count = len(filtered_candidates)
    else:
        filtered_proposals = []
        review_statuses = {
            VerificationStatus.A_CONFIRMER,
            VerificationStatus.NON_RESOLU,
            VerificationStatus.CONTRADICTOIRE,
            VerificationStatus.A_REVOIR,
        }
        for proposal in reconciliation.lot_proposals:
            haystack = " ".join(
                [
                    proposal.id,
                    proposal.numero_propose,
                    *proposal.candidate_zone_ids,
                ]
            ).casefold()
            if query and query.casefold() not in haystack:
                continue
            if proposal_status == "review" and proposal.statut not in review_statuses:
                continue
            if proposal_status not in {"", "all", "review"} and (
                proposal.statut.value != proposal_status
            ):
                continue
            filtered_proposals.append(proposal)
        filtered_proposals.sort(
            key=lambda item: (
                item.statut
                not in {
                    VerificationStatus.CONTRADICTOIRE,
                    VerificationStatus.A_REVOIR,
                    VerificationStatus.NON_RESOLU,
                    VerificationStatus.A_CONFIRMER,
                },
                int(item.numero_propose)
                if item.numero_propose.isdigit()
                else math.inf,
                item.numero_propose,
            )
        )
        filtered_count = len(filtered_proposals)

    page_count = max(1, math.ceil(filtered_count / PAGE_SIZE))
    current_page = min(requested_page, page_count)
    start = (current_page - 1) * PAGE_SIZE
    if technical_mode:
        page_candidates = filtered_candidates[start : start + PAGE_SIZE]
        technical_rows = [
            {
                "candidate": candidate,
                "zone": zones_by_candidate.get(candidate.id),
            }
            for candidate in page_candidates
        ]
        base_query = {
            "view": "technical",
            "q": query,
            "layer": layer_filter,
            "status": status_filter,
            "assignment": assignment_filter,
        }
    else:
        page_proposals = filtered_proposals[start : start + PAGE_SIZE]
        proposal_rows = [
            {
                "proposal": proposal,
                "evidence": [
                    evidence_by_id[evidence_id]
                    for evidence_id in proposal.evidence_ids
                    if evidence_id in evidence_by_id
                ],
                "candidates": [
                    candidate_by_id[candidate_id]
                    for candidate_id in proposal.candidate_zone_ids
                    if candidate_id in candidate_by_id
                ],
            }
            for proposal in page_proposals
        ]
        base_query = {
            "q": query,
            "proposal_status": proposal_status,
        }

    def page_url(page_number: int) -> str:
        params = {key: value for key, value in base_query.items() if value}
        params["page"] = str(page_number)
        return f"/dossiers/{dossier.id}/lots?{urlencode(params)}"

    counts = {
        "contours": reconciliation.contours_analyses,
        "business": len(reconciliation.business_zone_candidates),
        "proposed": len(reconciliation.lot_proposals),
        "auto": sum(
            item.statut == VerificationStatus.AUTO_VERIFIE
            for item in reconciliation.lot_proposals
        ),
        "review": sum(
            item.statut
            in {
                VerificationStatus.A_CONFIRMER,
                VerificationStatus.NON_RESOLU,
                VerificationStatus.A_REVOIR,
            }
            for item in reconciliation.lot_proposals
        ),
        "contradictions": sum(
            item.statut == VerificationStatus.CONTRADICTOIRE
            for item in reconciliation.lot_proposals
        ),
        "excluded": len(
            reconciliation.candidate_ids_exclus_techniquement
        ),
    }
    return _workflow_page(
        request,
        "lots.html",
        dossier,
        "lots",
        message,
        status_code,
        categories=CATEGORIES,
        proposal_rows=proposal_rows,
        technical_rows=technical_rows,
        technical_mode=technical_mode,
        counts=counts,
        filters=base_query,
        layer_options=sorted({item.calque for item in control.zones_candidates}),
        current_page=current_page,
        page_count=page_count,
        result_count=filtered_count,
        previous_url=page_url(current_page - 1) if current_page > 1 else None,
        next_url=page_url(current_page + 1) if current_page < page_count else None,
        reconciliation=reconciliation,
        usage_options=USAGE_OPTIONS,
        millieme_status_options=MILLIEME_STATUS_OPTIONS,
        milliemes_by_lot=milliemes_by_lot,
        millieme_total=sum(item.valeur for item in dossier.milliemes),
    )


def _summary_page(
    request: Request,
    dossier: Dossier,
    message: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    readiness = assess_generation_readiness(dossier)
    return _workflow_page(
        request,
        "synthese.html",
        dossier,
        "synthese",
        message,
        status_code,
        readiness=readiness,
        validation_current=has_current_data_validation(dossier),
    )


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
    if request.query_params.get("deleted") == "1":
        message = "Le dossier et ses documents générés ont été supprimés."
    elif request.query_params.get("analysis_deleted") == "1":
        message = "L’analyse rapide a été supprimée."
    else:
        message = None
    return _index(request, message)


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
    return RedirectResponse(f"/dossiers/{dossier.id}/plan", status_code=303)


@app.get("/dossiers/{dossier_id}", response_class=HTMLResponse)
async def legacy_dossier_page(dossier_id: str) -> RedirectResponse:
    _get_dossier(dossier_id)
    return RedirectResponse(f"/dossiers/{dossier_id}/plan", status_code=303)


@app.post("/dossiers/{dossier_id}/delete")
async def delete_dossier(dossier_id: str) -> RedirectResponse:
    try:
        repository.delete(dossier_id)
    except DossierNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Dossier introuvable.") from exc
    return RedirectResponse("/?deleted=1", status_code=303)


@app.get("/dossiers/{dossier_id}/dossier", response_class=HTMLResponse)
async def dossier_page(request: Request, dossier_id: str) -> HTMLResponse:
    return _dossier_page(request, _get_dossier(dossier_id))


@app.post("/dossiers/{dossier_id}/dossier/details")
async def save_dossier_details(
    request: Request,
    dossier_id: str,
    numero: str = Form(""),
    voie: str = Form(""),
    complement: str = Form(""),
    code_postal: str = Form(""),
    commune: str = Form(""),
    departement: str = Form(""),
    date_plan: str = Form(""),
) -> Response:
    dossier = _get_dossier(dossier_id)
    update_dossier_details(
        dossier,
        numero=numero,
        voie=voie,
        complement=complement,
        code_postal=code_postal,
        commune=commune,
        departement=departement,
        date_plan=date_plan,
    )
    return _saved_response(
        request, dossier, f"/dossiers/{dossier.id}/dossier"
    )


@app.post("/dossiers/{dossier_id}/cadastre")
async def create_parcel(
    request: Request,
    dossier_id: str,
    commune: str = Form(...),
    section: str = Form(...),
    numero: str = Form(...),
) -> Response:
    dossier = _get_dossier(dossier_id)
    try:
        add_parcel(
            dossier,
            commune=commune,
            section=section,
            numero=numero,
        )
    except ValueError as exc:
        return _mutation_error(request, dossier, str(exc), "dossier")
    return _saved_response(
        request, dossier, f"/dossiers/{dossier.id}/dossier#cadastre"
    )


@app.post("/dossiers/{dossier_id}/cadastre/{parcel_index}")
async def save_parcel(
    request: Request,
    dossier_id: str,
    parcel_index: int,
    commune: str = Form(...),
    section: str = Form(...),
    numero: str = Form(...),
) -> Response:
    dossier = _get_dossier(dossier_id)
    try:
        update_parcel(
            dossier,
            parcel_index,
            commune=commune,
            section=section,
            numero=numero,
        )
    except ValueError as exc:
        return _mutation_error(request, dossier, str(exc), "dossier")
    return _saved_response(
        request, dossier, f"/dossiers/{dossier.id}/dossier#cadastre"
    )


@app.post("/dossiers/{dossier_id}/cadastre/{parcel_index}/delete")
async def delete_parcel(
    request: Request,
    dossier_id: str,
    parcel_index: int,
) -> Response:
    dossier = _get_dossier(dossier_id)
    try:
        remove_parcel(dossier, parcel_index)
    except ValueError as exc:
        return _mutation_error(request, dossier, str(exc), "dossier")
    return _saved_response(
        request, dossier, f"/dossiers/{dossier.id}/dossier#cadastre"
    )


@app.get("/dossiers/{dossier_id}/plan", response_class=HTMLResponse)
async def plan_page(request: Request, dossier_id: str) -> HTMLResponse:
    return _plan_page(request, _get_dossier(dossier_id))


@app.get("/dossiers/{dossier_id}/controle", response_class=HTMLResponse)
async def control_page(request: Request, dossier_id: str) -> HTMLResponse:
    dossier = _get_dossier(dossier_id)
    if dossier.plan_importe is None:
        return _plan_page(
            request,
            dossier,
            "Importez un plan avant d'accéder au contrôle.",
            409,
        )
    return _control_page(request, dossier)


@app.get("/dossiers/{dossier_id}/lots", response_class=HTMLResponse)
async def lots_page(request: Request, dossier_id: str) -> HTMLResponse:
    dossier = _get_dossier(dossier_id)
    if dossier.plan_importe is None or not dossier.plan_importe.unite_confirmee:
        return _plan_page(
            request,
            dossier,
            "Le plan et son unité sont nécessaires avant les associations.",
            409,
        )
    return _lots_page(request, dossier)


@app.get("/dossiers/{dossier_id}/synthese", response_class=HTMLResponse)
async def summary_page(request: Request, dossier_id: str) -> HTMLResponse:
    dossier = _get_dossier(dossier_id)
    if dossier.plan_importe is None or not dossier.plan_importe.unite_confirmee:
        return _plan_page(
            request,
            dossier,
            "Le contrôle du plan doit précéder la synthèse.",
            409,
        )
    return _summary_page(request, dossier)


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
        return RedirectResponse(f"/dossiers/{dossier.id}/plan", status_code=303)
    except ValueError as exc:
        return _plan_page(request, dossier, str(exc), 400)
    except UploadTooLargeError:
        return _plan_page(
            request, dossier, "Le fichier dépasse la taille maximale de 50 Mo.", 413
        )
    except OdaNotAvailableError:
        logger.exception("DWG import requested without ODA File Converter")
        return _plan_page(
            request,
            dossier,
            "Conversion DWG indisponible : ODA File Converter n'est pas installé.",
            503,
        )
    except DwgConversionError:
        logger.exception("DWG conversion failed")
        return _plan_page(
            request, dossier, "Impossible de convertir ce fichier DWG.", 422
        )
    except DxfAnalysisError:
        logger.exception("Imported DXF analysis failed")
        return _plan_page(
            request, dossier, "Impossible d'analyser le plan converti.", 422
        )
    except OSError:
        logger.exception("Plan import handling failed")
        return _plan_page(
            request, dossier, "Impossible de traiter le fichier envoyé.", 500
        )
    except Exception:
        logger.exception("Unexpected plan import failure")
        return _plan_page(
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
    except ValueError as exc:
        return _mutation_error(request, dossier, str(exc), "controle")
    return _saved_response(
        request, dossier, f"/dossiers/{dossier.id}/controle"
    )


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
    except ValueError as exc:
        return _mutation_error(request, dossier, str(exc), "controle")
    return _saved_response(
        request, dossier, f"/dossiers/{dossier.id}/controle"
    )


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
    except ValueError as exc:
        return _mutation_error(request, dossier, str(exc), "controle")
    return _saved_response(
        request, dossier, f"/dossiers/{dossier.id}/controle"
    )


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
        reconcile_dossier(dossier)
    except ValueError as exc:
        return _mutation_error(request, dossier, str(exc), "lots")
    return _saved_response(request, dossier, f"/dossiers/{dossier.id}/lots")


@app.post("/dossiers/{dossier_id}/lots/{proposal_id}/review")
async def review_lot_proposal(
    request: Request,
    dossier_id: str,
    proposal_id: str,
    reason: str = Form(...),
) -> Response:
    dossier = _get_dossier(dossier_id)
    try:
        mark_lot_proposal_for_review(dossier, proposal_id, reason)
    except ValueError as exc:
        return _mutation_error(request, dossier, str(exc), "lots")
    return _saved_response(
        request,
        dossier,
        f"/dossiers/{dossier.id}/lots?proposal_status=review",
    )


@app.post("/dossiers/{dossier_id}/lots/{proposal_id}/confirm")
async def confirm_reconciled_lot(
    request: Request,
    dossier_id: str,
    proposal_id: str,
    candidate_ids: list[str] = Form(...),
    building_code: str = Form(...),
    level_code: str = Form(...),
    lot_number: str = Form(...),
    category: str = Form("principale"),
    reason: str = Form(...),
) -> Response:
    dossier = _get_dossier(dossier_id)
    try:
        confirm_lot_proposal(
            dossier=dossier,
            proposal_id=proposal_id,
            candidate_ids=candidate_ids,
            building_code=building_code,
            level_code=level_code,
            lot_number=lot_number,
            category=category,
            reason=reason,
        )
    except ValueError as exc:
        return _mutation_error(request, dossier, str(exc), "lots")
    return _saved_response(
        request,
        dossier,
        f"/dossiers/{dossier.id}/lots?proposal_status=review",
    )


@app.post("/dossiers/{dossier_id}/lots/{lot_id}/metadata")
async def save_lot_metadata(
    request: Request,
    dossier_id: str,
    lot_id: str,
    usage: str = Form(""),
    designation: str = Form(""),
    millieme_value: str = Form(""),
    millieme_base: str = Form("1000"),
    millieme_status: str = Form("a_confirmer"),
) -> Response:
    dossier = _get_dossier(dossier_id)
    try:
        update_lot_metadata(
            dossier,
            lot_id,
            usage=usage,
            designation=designation,
            millieme_value=millieme_value,
            millieme_base=millieme_base,
            millieme_status=millieme_status,
        )
    except ValueError as exc:
        return _mutation_error(request, dossier, str(exc), "lots")
    return _saved_response(
        request, dossier, f"/dossiers/{dossier.id}/lots#donnees-lots"
    )


@app.post("/dossiers/{dossier_id}/milliemes/status")
async def save_millieme_grid_status(
    request: Request,
    dossier_id: str,
    complete: bool = Form(False),
) -> Response:
    dossier = _get_dossier(dossier_id)
    set_millieme_grid_complete(dossier, complete)
    return _saved_response(
        request, dossier, f"/dossiers/{dossier.id}/lots#milliemes"
    )


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


@app.post("/dossiers/{dossier_id}/synthese/validate")
async def validate_dossier_data(request: Request, dossier_id: str) -> Response:
    dossier = _get_dossier(dossier_id)
    readiness = assess_generation_readiness(dossier)
    if readiness.blockers:
        return _summary_page(
            request,
            dossier,
            "La validation est impossible tant que les blocages ne sont pas levés.",
            409,
        )
    record_data_validation(dossier)
    dossier.statut = "valide"
    repository.save(dossier)
    return RedirectResponse(f"/dossiers/{dossier.id}/documents", status_code=303)


@app.get("/dossiers/{dossier_id}/documents", response_class=HTMLResponse)
async def document_page(request: Request, dossier_id: str) -> HTMLResponse:
    dossier = _get_dossier(dossier_id)
    if not has_current_data_validation(dossier):
        return _document_page(
            request,
            dossier,
            "Dossier à valider avant génération. Revenez à la synthèse.",
            409,
        )
    return _document_page(request, dossier)


@app.post("/dossiers/{dossier_id}/documents/generate")
async def generate_document(request: Request, dossier_id: str) -> Response:
    dossier = _get_dossier(dossier_id)
    if not has_current_data_validation(dossier):
        return _document_page(
            request,
            dossier,
            "Dossier à valider avant génération. Revenez à la synthèse.",
            409,
        )
    try:
        generated = generate_copropriete_draft(dossier)
        generation = generated.generation
        repository.save_generation_artifacts(
            dossier.id,
            generation.id,
            generation.nom_fichier,
            generated.content,
            generated.snapshot,
        )
        dossier.generations.append(generation)
        dossier.statut = "document_brouillon"
        repository.save(dossier)
    except GenerationBlockedError as exc:
        return _document_page(
            request,
            dossier,
            "La génération est bloquée : " + " ".join(exc.blockers),
            409,
        )
    except (TemplateMissingError, TemplateCorruptError):
        logger.exception("DOCX template unavailable or corrupt")
        return _document_page(
            request,
            dossier,
            "Le modèle de document est indisponible ou invalide.",
            500,
        )
    except (OSError, ValueError):
        logger.exception("DOCX generation persistence failed")
        return _document_page(
            request,
            dossier,
            "Impossible d'enregistrer le document généré.",
            500,
        )
    return RedirectResponse(f"/dossiers/{dossier.id}/documents", status_code=303)


@app.get("/dossiers/{dossier_id}/documents/{generation_id}/download")
async def download_document(dossier_id: str, generation_id: str) -> FileResponse:
    dossier = _get_dossier(dossier_id)
    generation = next(
        (item for item in dossier.generations if item.id == generation_id), None
    )
    if generation is None:
        raise HTTPException(status_code=404, detail="Document introuvable.")
    try:
        path = repository.generated_document_path(
            dossier.id, generation.id, generation.nom_fichier
        )
    except DossierNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Document introuvable.") from exc
    return FileResponse(
        path,
        media_type=DOCUMENT_MIME,
        filename=generation.nom_fichier,
    )


def _get_quick_analysis(analysis_id: str):
    try:
        return analysis_repository.get(analysis_id)
    except QuickAnalysisNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail="Analyse rapide introuvable ou expirée.",
        ) from exc


def _quick_analysis_page(request: Request, analysis_id: str) -> HTMLResponse:
    snapshot = _get_quick_analysis(analysis_id)
    return templates.TemplateResponse(
        request=request,
        name="results.html",
        context={
            "analysis": snapshot,
            "dossier": snapshot.dossier,
            "diagnostic": build_quick_diagnostic(snapshot),
        },
    )


@app.get("/analyses/{analysis_id}", response_class=HTMLResponse)
async def quick_analysis_result(request: Request, analysis_id: str) -> HTMLResponse:
    return _quick_analysis_page(request, analysis_id)


@app.post("/analyses/{analysis_id}/dossier")
async def create_dossier_from_analysis(analysis_id: str) -> RedirectResponse:
    snapshot = _get_quick_analysis(analysis_id)
    dossier = promote_quick_analysis(snapshot)
    repository.save(dossier)
    analysis_repository.delete(analysis_id)
    return RedirectResponse(f"/dossiers/{dossier.id}/dossier", status_code=303)


@app.post("/analyses/{analysis_id}/delete")
async def delete_quick_analysis(analysis_id: str) -> RedirectResponse:
    try:
        analysis_repository.delete(analysis_id)
    except QuickAnalysisNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail="Analyse rapide introuvable ou expirée.",
        ) from exc
    return RedirectResponse("/?analysis_deleted=1", status_code=303)


@app.post("/analyze", response_class=HTMLResponse)
async def analyze(request: Request, file: UploadFile | None = None) -> Response:
    """Create a persistent pre-diagnostic with the canonical DXF workflow."""

    temporary_path: Path | None = None
    started_at = time.perf_counter()
    try:
        filename, file_type = _upload_metadata(file)
        assert file is not None
        temporary_path = await _save_temporary_upload(file, f".{file_type}")
        with dxf_source(temporary_path, file_type) as dxf_path:
            control, planches = inspect_dxf(dxf_path, filename)
        dossier = prepare_quick_analysis_dossier(
            filename,
            file_type,
            control,
            planches,
        )
        snapshot = analysis_repository.create(
            dossier,
            duration_seconds=time.perf_counter() - started_at,
        )
        return RedirectResponse(f"/analyses/{snapshot.id}", status_code=303)
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
        return _index(
            request,
            "Impossible d’analyser ce fichier DXF. Réessayez avec un autre fichier.",
            422,
        )
    except OSError:
        logger.exception("Temporary upload handling failed")
        return _index(request, "Impossible de traiter le fichier envoyé.", 500)
    except Exception:
        logger.exception("Unexpected quick analysis failure")
        return _index(
            request,
            "Une erreur inattendue empêche cette analyse. Réessayez avec un autre fichier.",
            500,
        )
    finally:
        if file is not None:
            await file.close()
        _remove_temporary_file(temporary_path)
