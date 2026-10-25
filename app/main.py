"""FastAPI entry point for GeoFlow."""

from __future__ import annotations

import hashlib
import logging
import math
import os
import tempfile
import threading
import time
from collections import Counter
from datetime import datetime, timezone
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

from app.concurrency import AnalysisAdmissionMiddleware, run_analysis
from app.security import SecurityMiddleware
from app.version import __version__
from app.domain import (
    Dossier,
    GlobalCheckStatus,
    ProfileUsage,
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
from app.dxf.technical import meters_per_unit
from app.quick_analysis import (
    QuickAnalysisSnapshot,
    QuickAnalysisNotFoundError,
    QuickAnalysisRepository,
    build_quick_diagnostic,
    is_pending,
    prepare_quick_analysis_dossier,
    promote_quick_analysis,
)
from app.annex_view import build_annex_rows
from app.profiles.compatibility import LEVELS as COMPATIBILITY_LEVELS
from app.profiles.compatibility import CompatibilityCache, recommend
from app.profiles.evaluation import STATUS_LABELS, evaluate_profile
from app.profiles.forms import ProfileFormError, profile_from_form
from app.profiles.inference import infer_profile, legacy_view, unmapped_layer_suggestions
from app.profiles.models import LAYER_ROLES, TEXT_SOURCES, DrawingProfile, ProfileCheck
from app.profiles.storage import ProfileNotFoundError, ProfileRepository
from app.profiles.usage import usage_from_profile, usage_summary
from app.labels import UNIT_OPTIONS, humanize, unit_label, verification_label
from app.plan_preview import build_class_preview, build_plan_overview, build_proposal_preview
from app.reconciliation import reconcile_dossier
from app.storage import DossierNotFoundError, JsonDossierRepository
from app.workflow import (
    add_parcel,
    apply_profile_choice,
    associate_candidate,
    attach_annex,
    attach_import,
    attach_proposed_annexes,
    clean_reference,
    confirm_lot_proposal,
    confirm_unit,
    create_dossier,
    has_current_data_validation,
    rename_dossier,
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

app = FastAPI(title="GeoFlow", version=__version__)
app.add_middleware(AnalysisAdmissionMiddleware)
app.add_middleware(
    SecurityMiddleware, public_origin=os.environ.get("GEOFLOW_PUBLIC_ORIGIN", ""),
    # FastAPI's generated developer docs use CDN assets and inline scripts.
    # Keep their existing behavior without weakening the application's CSP.
    csp_exempt_paths=frozenset({"/docs", "/redoc", "/docs/oauth2-redirect"}),
)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


def _static_version() -> str:
    """Content hash appended to asset URLs so browsers and proxies refetch them."""
    digest = hashlib.sha256()
    for path in sorted((BASE_DIR / "static").glob("*.*")):
        digest.update(path.read_bytes())
    return digest.hexdigest()[:10]


templates.env.globals["static_version"] = _static_version()
templates.env.globals["app_version"] = __version__
repository = JsonDossierRepository(DATA_DIR)
analysis_repository = QuickAnalysisRepository(DATA_DIR / "quick-analyses")

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
CHECK_LABELS = {
    "numeros_lots_uniques": "Numéros de lots uniques",
    "zones_non_reutilisees": "Contours non réutilisés",
    "propositions_completes": "Propositions complètes",
    "reconciliation_resolue": "Rapprochements résolus",
    "lots_constitues": "Lots constitués",
    "zones_retenues_affectees": "Zones retenues affectées",
}


templates.env.filters["unit_label"] = unit_label
templates.env.filters["verification_label"] = verification_label
templates.env.filters["check_label"] = lambda code: CHECK_LABELS.get(
    code, humanize(code)
)


class UploadTooLargeError(RuntimeError):
    pass


def _index(
    request: Request,
    message: str | None = None,
    status_code: int = 200,
    blank_form_error: bool = False,
) -> HTMLResponse:
    dossiers = repository.list()
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "message": message,
            "dossiers": dossiers,
            "progress": {item.id: _dossier_progress(item) for item in dossiers},
            "blank_form_error": blank_form_error,
        },
        status_code=status_code,
    )


def _relative_time(moment: datetime | None) -> str:
    if moment is None:
        return ""
    seconds = (datetime.now(timezone.utc) - moment).total_seconds()
    if seconds < 60:
        return "à l’instant"
    if seconds < 3600:
        return f"il y a {int(seconds // 60)} min"
    if seconds < 86400:
        return f"il y a {int(seconds // 3600)} h"
    if seconds < 2 * 86400:
        return "hier"
    return f"le {moment.astimezone().strftime('%d/%m/%Y')}"


def _dossier_progress(dossier: Dossier) -> dict[str, object]:
    """Where a dossier stands, and the step to resume it from."""
    steps = _workflow_steps(dossier, "")
    accessible = [step for step in steps if step["href"]]
    remaining = [step for step in accessible if not step["completed"]]
    target = remaining[0] if remaining else accessible[-1]
    return {
        "href": target["href"],
        "step_id": target["id"],
        "step_label": str(target["label"]).split(". ", 1)[-1],
        "step_number": steps.index(target) + 1,
        "step_count": len(steps),
        "steps": [
            {
                "state": "done"
                if step["completed"]
                else "current"
                if step is target
                else "todo",
                "label": str(step["label"]).split(". ", 1)[-1],
            }
            for step in steps
        ],
        "pending": sum(int(step["pending"]) for step in steps),
        "modified": _relative_time(repository.modified_at(dossier.id)),
    }


def _blocker_link(dossier: Dossier, message: str) -> tuple[str, str] | None:
    """Point a readiness blocker to the step where the user can resolve it."""
    lowered = message.casefold()
    if "unité" in lowered or "planche" in lowered:
        return f"/dossiers/{dossier.id}/controle", "Corriger dans Contrôle"
    if any(word in lowered for word in ("surface", "zone", "lot", "réconciliation")):
        return f"/dossiers/{dossier.id}/lots", "Corriger dans Lots & surfaces"
    return None


PENDING_PROPOSAL_STATUSES = {
    VerificationStatus.A_CONFIRMER,
    VerificationStatus.NON_RESOLU,
    VerificationStatus.CONTRADICTOIRE,
    VerificationStatus.A_REVOIR,
}


def _lots_pending_count(dossier: Dossier) -> int:
    """Proposals and blocking global checks still requiring a human decision."""
    reconciliation = dossier.reconciliation
    if reconciliation is None:
        return 0
    pending = sum(
        item.statut in PENDING_PROPOSAL_STATUSES
        for item in reconciliation.lot_proposals
    )
    if pending:
        return pending
    return sum(
        check.statut == GlobalCheckStatus.BLOQUANT
        for check in reconciliation.controles_globaux
    )


def _workflow_steps(dossier: Dossier, active: str) -> list[dict[str, object]]:
    plan_ready = dossier.plan_importe is not None
    control_ready = plan_ready and bool(dossier.plan_importe.unite_confirmee)
    validated = has_current_data_validation(dossier)
    lots_pending = _lots_pending_count(dossier) if control_ready else 0
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
        "lots": bool(dossier.zones) and not lots_pending,
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
            "pending": lots_pending if step_id == "lots" else 0,
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
        request,
        "dossier.html",
        dossier,
        "dossier",
        message,
        status_code,
        profile_usage=_usage_summary(dossier),
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
                "reference": dossier.reference,
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


def _ensure_reference_available(reference: str, exclude_id: str | None = None) -> None:
    existing = repository.find_by_reference(reference, exclude_id)
    if existing is not None:
        raise ValueError(
            f"La référence « {existing.reference} » est déjà utilisée par un autre dossier."
        )


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
            if proposal_status == "review" and proposal.statut not in PENDING_PROPOSAL_STATUSES:
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
                "set_aside": [
                    candidate_by_id[candidate_id]
                    for candidate_id in proposal.candidate_zone_ids_ecartes
                    if candidate_id in candidate_by_id
                ],
                "preview": build_proposal_preview(
                    control.zones_candidates, reconciliation, proposal
                ),
                # Level read by the drawing profile, offered as the default.
                "level": next(
                    (
                        str(evidence_by_id[evidence_id].valeur)
                        for evidence_id in proposal.evidence_ids
                        if evidence_id in evidence_by_id
                        and evidence_by_id[evidence_id].source == "niveau_profil"
                    ),
                    "",
                ),
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
        annexes=None if technical_mode else build_annex_rows(dossier),
        plan_overview=(
            []
            if technical_mode
            else build_plan_overview(
                control.zones_candidates,
                reconciliation,
                lambda proposal: (
                    f"/dossiers/{dossier.id}/lots?"
                    + urlencode({"proposal_status": "all", "q": proposal.numero_propose})
                    + f"#proposal-{proposal.id}"
                ),
            )
        ),
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
        blocker_rows=[
            (item, _blocker_link(dossier, item)) for item in readiness.blockers
        ],
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
    except BaseException:
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
        _ensure_reference_available(dossier.reference)
    except ValueError as exc:
        return _index(request, str(exc), 400, blank_form_error=True)
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


@app.post("/dossiers/{dossier_id}/reference")
async def save_dossier_reference(
    request: Request,
    dossier_id: str,
    reference: str = Form(""),
) -> Response:
    dossier = _get_dossier(dossier_id)
    try:
        cleaned = clean_reference(reference)
        _ensure_reference_available(cleaned, exclude_id=dossier.id)
        rename_dossier(dossier, cleaned)
    except ValueError as exc:
        return _mutation_error(request, dossier, str(exc), "dossier")
    return _saved_response(request, dossier, f"/dossiers/{dossier.id}/dossier")


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
    original = dossier.model_dump_json()
    temporary_path: Path | None = None
    try:
        filename, file_type = _upload_metadata(file)
        assert file is not None
        temporary_path = await _save_temporary_upload(file, f".{file_type}")
        await run_analysis(_import_plan, temporary_path, file_type, filename, dossier)
        # Analysis yields to autosave/deletion. Commit only if the source dossier
        # still exists and is unchanged; this check/save never crosses an await.
        current = _get_dossier(dossier_id)
        if current.model_dump_json() != original:
            return _plan_page(
                request, current,
                "Le dossier a été modifié pendant l’analyse. Réessayez l’import.", 409,
            )
        repository.save(dossier)
        return RedirectResponse(f"/dossiers/{dossier.id}/plan", status_code=303)
    except HTTPException:
        raise
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
        try:
            if file is not None:
                await file.close()
        finally:
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


@app.post("/dossiers/{dossier_id}/annexes/confirm-proposals")
async def confirm_annex_proposals(request: Request, dossier_id: str) -> Response:
    with repository.lock(dossier_id):
        dossier = _get_dossier(dossier_id)
        try:
            attach_proposed_annexes(dossier)
        except ValueError as exc:
            return _mutation_error(request, dossier, str(exc), "lots")
        return _saved_response(request, dossier, f"/dossiers/{dossier.id}/lots#annexes")


@app.post("/dossiers/{dossier_id}/annexes/{candidate_id}")
async def save_annex_attachment(
    request: Request,
    dossier_id: str,
    candidate_id: str,
    lot_id: str = Form(""),
    level_code: str = Form(""),
) -> Response:
    # Many annexes are saved in quick succession: keep each cycle atomic.
    with repository.lock(dossier_id):
        dossier = _get_dossier(dossier_id)
        try:
            attach_annex(dossier, candidate_id, lot_id.strip() or None, level_code)
        except ValueError as exc:
            return _mutation_error(request, dossier, str(exc), "lots")
        return _saved_response(request, dossier, f"/dossiers/{dossier.id}/lots#annexes")


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


def _quick_analysis_page(
    request: Request,
    analysis_id: str,
    reference_error: str | None = None,
    reference_value: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    snapshot = _get_quick_analysis(analysis_id)
    return templates.TemplateResponse(
        request=request,
        name="results.html",
        context={
            "analysis": snapshot,
            "dossier": snapshot.dossier,
            "diagnostic": build_quick_diagnostic(snapshot),
            "profile_usage": _usage_summary(snapshot.dossier),
            "has_profiles": bool(profile_repository.list()[0]),
            "reference_error": reference_error,
            "reference_value": (
                snapshot.dossier.reference if reference_value is None else reference_value
            ),
        },
        status_code=status_code,
    )


@app.get("/analyses/{analysis_id}", response_class=HTMLResponse)
async def quick_analysis_result(request: Request, analysis_id: str) -> Response:
    if is_pending(_get_quick_analysis(analysis_id)):
        return RedirectResponse(f"/analyses/{analysis_id}/profil", status_code=303)
    return _quick_analysis_page(request, analysis_id)


@app.post("/analyses/{analysis_id}/dossier")
async def create_dossier_from_analysis(
    request: Request,
    analysis_id: str,
    reference: str | None = Form(None),
) -> Response:
    snapshot = _get_quick_analysis(analysis_id)
    if is_pending(snapshot):
        return RedirectResponse(f"/analyses/{analysis_id}/profil", status_code=303)
    try:
        dossier = promote_quick_analysis(snapshot, reference)
        _ensure_reference_available(dossier.reference)
    except ValueError as exc:
        return _quick_analysis_page(
            request,
            analysis_id,
            reference_error=str(exc),
            reference_value=reference or "",
            status_code=409 if "déjà utilisée" in str(exc) else 400,
        )
    repository.save(dossier)
    analysis_repository.delete(analysis_id)
    usage = dossier.profile_usage
    if usage is not None and usage.mode == "profil":
        profile_repository.mark_used(usage.profile_id)
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


def _import_plan(path: Path, file_type: str, filename: str, dossier: Dossier) -> None:
    with dxf_source(path, file_type) as dxf_path:
        control, planches = inspect_dxf(dxf_path, filename)
    attach_import(dossier, filename, file_type, control, planches)


def _inspect_analysis(
    path: Path, file_type: str, filename: str, started_at: float
) -> QuickAnalysisSnapshot:
    with dxf_source(path, file_type) as dxf_path:
        control, planches = inspect_dxf(dxf_path, filename)
    dossier = prepare_quick_analysis_dossier(filename, file_type, control, planches)
    return analysis_repository.create(
        dossier, duration_seconds=time.perf_counter() - started_at
    )


def _apply_analysis_profile(
    snapshot: QuickAnalysisSnapshot, usage: ProfileUsage | None
) -> None:
    apply_profile_choice(snapshot.dossier, usage)


async def _create_analysis(file: UploadFile | None) -> QuickAnalysisSnapshot:
    """Inspect an uploaded plan once and keep it as a temporary snapshot."""

    temporary_path: Path | None = None
    started_at = time.perf_counter()
    try:
        filename, file_type = _upload_metadata(file)
        assert file is not None
        temporary_path = await _save_temporary_upload(file, f".{file_type}")
        return await run_analysis(
            _inspect_analysis, temporary_path, file_type, filename, started_at
        )
    finally:
        try:
            if file is not None:
                await file.close()
        finally:
            _remove_temporary_file(temporary_path)


def _analysis_failure(exc: Exception) -> tuple[str, int]:
    """User message and status for a failed upload; call inside ``except``."""

    if isinstance(exc, ValueError):
        return str(exc), 400
    if isinstance(exc, UploadTooLargeError):
        return "Le fichier dépasse la taille maximale de 50 Mo.", 413
    if isinstance(exc, OdaNotAvailableError):
        logger.exception("DWG analysis requested without ODA File Converter")
        return "Conversion DWG indisponible : ODA File Converter n'est pas installé.", 503
    if isinstance(exc, DwgConversionError):
        logger.exception("DWG conversion failed")
        return "Impossible de convertir ce fichier DWG.", 422
    if isinstance(exc, DxfAnalysisError):
        logger.exception("DXF analysis failed")
        return "Impossible d’analyser ce fichier DXF. Réessayez avec un autre fichier.", 422
    if isinstance(exc, OSError):
        logger.exception("Temporary upload handling failed")
        return "Impossible de traiter le fichier envoyé.", 500
    logger.exception("Unexpected quick analysis failure")
    return "Une erreur inattendue empêche cette analyse. Réessayez avec un autre fichier.", 500


@app.post("/analyze", response_class=HTMLResponse)
async def analyze(request: Request, file: UploadFile | None = None) -> Response:
    """Create a persistent pre-diagnostic with the canonical DXF workflow."""

    try:
        snapshot = await _create_analysis(file)
    except Exception as exc:
        message, status = _analysis_failure(exc)
        return _index(request, message, status)
    if profile_repository.list()[0]:
        return RedirectResponse(f"/analyses/{snapshot.id}/profil", status_code=303)
    # Without any profile the choice would be empty: analyse automatically.
    await run_analysis(_apply_analysis_profile, snapshot, None)
    analysis_repository.update(snapshot)
    return RedirectResponse(f"/analyses/{snapshot.id}", status_code=303)


# Drawing profiles ---------------------------------------------------------

profile_repository = ProfileRepository(DATA_DIR / "profiles")
profile_repository_lock = threading.Lock()
ROLE_PREVIEW_CLASSES = {
    "surface_principale": "is-ok",
    "annexe": "is-review",
    "partie_commune": "is-context",
    "hors_copropriete": "is-conflict",
    "exclusion": "is-excluded",
    "archive": "is-excluded",
}


compatibility_cache = CompatibilityCache()


def _file_declares_unit(dossier: Dossier) -> bool:
    """Whether the file itself declares a usable unit (a profile may have
    imposed the retained one since)."""

    plan = dossier.plan_importe
    return plan is not None and meters_per_unit(plan.unite_detectee) is not None


def _usage_summary(dossier: Dossier) -> dict[str, object]:
    usage = dossier.profile_usage
    current = None
    if usage is not None and usage.mode == "profil":
        try:
            current = profile_repository.get(usage.profile_id)
        except ProfileNotFoundError:
            current = None
    return usage_summary(dossier, current)


def _profile_report(profile: DrawingProfile, snapshot: QuickAnalysisSnapshot) -> dict[str, object]:
    dossier = snapshot.dossier
    report = evaluate_profile(
        profile,
        dossier.controle_technique,
        dossier.planches,
        unit_declared=_file_declares_unit(dossier),
    )
    classes = {
        candidate_id: ROLE_PREVIEW_CLASSES[role]
        for role, ids in report["role_ids"].items()
        for candidate_id in ids
    }
    report["preview"] = build_class_preview(dossier.controle_technique.zones_candidates, classes)
    return report


def _report_fragments(report: dict[str, object]) -> dict[str, str]:
    macros = templates.env.get_template("_profile_report.html").module
    fragments = {
        "drawing": macros.facts(report["drawing"]),
        "lots": macros.facts(report["lots"]),
        "lot_examples": macros.examples(report["lot_examples"]),
        "surfaces": macros.facts(report["surfaces"]),
        "surface_examples": macros.examples(report["surface_examples"]),
        "levels": macros.facts(report["levels"]),
        "annexes": macros.facts(report["annexes"]),
        "versions": macros.facts(report["versions"]),
        "report": macros.report(report),
        "preview": macros.preview(report["preview"]),
    }
    fragments.update({f"role_{role}": macros.facts(lines) for role, lines in report["roles"].items()})
    return {key: str(value) for key, value in fragments.items()}


def _optional_analysis(analysis_id: str | None) -> QuickAnalysisSnapshot | None:
    if not analysis_id:
        return None
    try:
        return analysis_repository.get(analysis_id)
    except QuickAnalysisNotFoundError:
        return None


def _profiles_page(
    request: Request, message: str | None = None, status_code: int = 200, notice: bool = False
) -> HTMLResponse:
    profiles, unreadable = profile_repository.list()
    return templates.TemplateResponse(
        request=request,
        name="profiles.html",
        context={
            "profiles": profiles,
            "default_id": profile_repository.default_id(),
            "unreadable": unreadable,
            "message": message,
            "notice": notice,
            "status_labels": STATUS_LABELS,
            "modified": {item.id: _relative_time(item.updated_at) for item in profiles},
        },
        status_code=status_code,
    )


def _profile_wizard(
    request: Request,
    profile: DrawingProfile,
    snapshot: QuickAnalysisSnapshot | None,
    *,
    is_new: bool,
    errors: list[str] | None = None,
    status_code: int = 200,
    analysis_missing: bool = False,
) -> HTMLResponse:
    context: dict[str, object] = {
        "profile": profile,
        "snapshot": snapshot,
        "is_new": is_new,
        "errors": errors or [],
        "analysis_missing": analysis_missing,
        "layer_roles": LAYER_ROLES,
        "text_sources": TEXT_SOURCES,
        "unit_options": UNIT_OPTIONS,
        "saved": request.query_params.get("enregistre") == "1",
        "from_flow": request.query_params.get("parcours") == "1",
    }
    if snapshot is not None:
        dossier = legacy_view(snapshot.dossier)
        control = dossier.controle_technique
        report = _profile_report(profile, snapshot)
        _, explanations = infer_profile(dossier)
        contour_counts = Counter(item.calque for item in control.zones_candidates)
        text_counts = Counter(item.calque for item in control.textes)
        context.update(
            report=report,
            fragments=_report_fragments(report),
            explanations=explanations,
            contour_layers=sorted(contour_counts.items()),
            text_layers=sorted(text_counts.items()),
            layout_names=[item.nom for item in control.layouts if item.nom.casefold() != "model"],
            suggestions=[] if is_new else unmapped_layer_suggestions(profile, dossier),
            plan_name=dossier.plan_importe.nom_fichier_original if dossier.plan_importe else "",
        )
    return templates.TemplateResponse(
        request=request, name="profile_wizard.html", context=context, status_code=status_code
    )


def _with_check(profile: DrawingProfile, snapshot: QuickAnalysisSnapshot | None) -> DrawingProfile:
    if snapshot is None:
        return profile
    report = _profile_report(profile, snapshot)
    return profile.model_copy(update={"last_check": ProfileCheck(status=report["status"])})


@app.get("/profils", response_class=HTMLResponse)
async def profiles_list(request: Request) -> HTMLResponse:
    notices = {
        "supprime": {"1": "Le profil a été supprimé."},
        "defaut": {"1": "Profil par défaut enregistré : il sera présélectionné à chaque dépôt de plan compatible.", "0": "Aucun profil par défaut."},
    }
    message = next(
        (texts[value] for key, texts in notices.items() if (value := request.query_params.get(key)) in texts), None
    )
    return _profiles_page(request, message, notice=message is not None)


@app.post("/profils/analyse", response_class=HTMLResponse)
async def profile_from_plan(request: Request, file: UploadFile | None = None) -> Response:
    try:
        snapshot = await _create_analysis(file)
    except Exception as exc:
        message, status = _analysis_failure(exc)
        return _profiles_page(request, message, status)
    return RedirectResponse(f"/profils/nouveau?analyse={snapshot.id}", status_code=303)


@app.get("/profils/nouveau", response_class=HTMLResponse)
async def new_profile(request: Request, analyse: str = "") -> HTMLResponse:
    snapshot = _optional_analysis(analyse)
    if snapshot is None:
        return _profiles_page(request, "Analyse introuvable ou expirée : déposez à nouveau le plan.", 404)
    profile, _ = infer_profile(legacy_view(snapshot.dossier))
    return _profile_wizard(request, profile, snapshot, is_new=True)


@app.post("/profils/apercu")
async def profile_preview(request: Request, analyse: str = "") -> JSONResponse:
    """Live check of the profile being edited; nothing is saved."""

    snapshot = _optional_analysis(analyse)
    if snapshot is None:
        return JSONResponse({"ok": False, "messages": ["Analyse introuvable ou expirée."]}, status_code=404)
    try:
        profile = profile_from_form(await request.form())
    except ProfileFormError as exc:
        return JSONResponse({"ok": False, "messages": exc.messages}, status_code=422)
    report = _profile_report(profile, snapshot)
    return JSONResponse(
        {
            "ok": True,
            "status": report["status"],
            "status_label": report["status_label"],
            "conclusion": report["conclusion"],
            "fragments": _report_fragments(report),
        }
    )


@app.post("/profils/enregistrer", response_class=HTMLResponse)
async def create_profile(request: Request, analyse: str = "") -> Response:
    snapshot = _optional_analysis(analyse)
    form = await request.form()
    try:
        profile = profile_from_form(form)
    except ProfileFormError as exc:
        draft, _ = (
            infer_profile(legacy_view(snapshot.dossier))
            if snapshot
            else (DrawingProfile(name="Nouveau profil de dessin"), None)
        )
        return _profile_wizard(request, draft, snapshot, is_new=True, errors=exc.messages, status_code=422)
    saved = profile_repository.save(_with_check(profile, snapshot))
    suffix = f"&analyse={snapshot.id}" if snapshot else ""
    if snapshot and request.query_params.get("parcours") == "1":
        suffix += "&parcours=1"
    return RedirectResponse(f"/profils/{saved.id}?enregistre=1{suffix}", status_code=303)


def _get_profile(profile_id: str) -> DrawingProfile:
    try:
        return profile_repository.get(profile_id)
    except ProfileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Profil introuvable.") from exc


@app.get("/profils/{profile_id}", response_class=HTMLResponse)
async def edit_profile(request: Request, profile_id: str, analyse: str = "") -> HTMLResponse:
    profile = _get_profile(profile_id)
    snapshot = _optional_analysis(analyse)
    return _profile_wizard(
        request, profile, snapshot, is_new=False, analysis_missing=bool(analyse) and snapshot is None
    )


@app.post("/profils/{profile_id}", response_class=HTMLResponse)
async def update_profile(request: Request, profile_id: str, analyse: str = "") -> Response:
    form = await request.form()  # never await while holding the lock
    with profile_repository_lock:
        base = _get_profile(profile_id)
        snapshot = _optional_analysis(analyse)
        try:
            profile = profile_from_form(form, base)
        except ProfileFormError as exc:
            return _profile_wizard(request, base, snapshot, is_new=False, errors=exc.messages, status_code=422)
        saved = profile_repository.save(_with_check(profile, snapshot))
    suffix = f"&analyse={snapshot.id}" if snapshot else ""
    return RedirectResponse(f"/profils/{saved.id}?enregistre=1{suffix}", status_code=303)


@app.post("/profils/{profile_id}/tester", response_class=HTMLResponse)
async def test_profile(request: Request, profile_id: str, file: UploadFile | None = None) -> Response:
    profile = _get_profile(profile_id)
    try:
        snapshot = await _create_analysis(file)
    except Exception as exc:
        message, status = _analysis_failure(exc)
        return _profile_wizard(request, profile, None, is_new=False, errors=[message], status_code=status)
    return RedirectResponse(f"/profils/{profile.id}?analyse={snapshot.id}#etape-5", status_code=303)


@app.post("/profils/{profile_id}/dupliquer")
async def duplicate_profile(profile_id: str) -> RedirectResponse:
    _get_profile(profile_id)
    copy = profile_repository.duplicate(profile_id)
    return RedirectResponse(f"/profils/{copy.id}", status_code=303)


@app.post("/profils/{profile_id}/supprimer")
async def delete_profile(profile_id: str) -> RedirectResponse:
    try:
        profile_repository.delete(profile_id)
    except ProfileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Profil introuvable.") from exc
    return RedirectResponse("/profils?supprime=1", status_code=303)


@app.post("/profils/{profile_id}/defaut")
async def set_default_profile(profile_id: str) -> RedirectResponse:
    try:
        profile_repository.set_default(profile_id)
    except ProfileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Profil introuvable.") from exc
    return RedirectResponse("/profils?defaut=1", status_code=303)


@app.post("/profils/defaut/retirer")
async def clear_default_profile() -> RedirectResponse:
    profile_repository.set_default(None)
    return RedirectResponse("/profils?defaut=0", status_code=303)


# Daily flow: choose the profile of an inspected plan -----------------------


def _profile_options(snapshot: QuickAnalysisSnapshot) -> dict[str, object]:
    """Saved profiles assessed on the plan, plus the automatic engine.

    A profile is preselected when it was already chosen for this plan, or is
    the default one and not « peu compatible », or is clearly recommended.
    Otherwise nothing is preselected.
    """

    profiles, unreadable = profile_repository.list()
    dossier = snapshot.dossier
    control = dossier.controle_technique
    unit_declared = _file_declares_unit(dossier)
    assessments = {
        profile.id: compatibility_cache.get(snapshot.id, profile, control, unit_declared) for profile in profiles
    }
    recommended = recommend(assessments)
    default_id = profile_repository.default_id()
    last_used = profile_repository.last_used()
    recent = max((key for key in assessments if key in last_used), key=last_used.__getitem__, default=None)
    usage = dossier.profile_usage
    if not is_pending(snapshot):
        if usage is not None and usage.mode == "profil":
            checked = usage.profile_id if usage.profile_id in assessments else None
        else:
            checked = "legacy"
    elif default_id in assessments and assessments[default_id].level != "peu":
        checked = default_id
    else:
        checked = recommended
    ordered = sorted(
        profiles,
        key=lambda item: (
            item.id != checked,
            item.id != recommended,
            item.id != default_id,
            -(last_used[item.id].timestamp() if item.id in last_used else 0),
            item.name.casefold(),
        ),
    )
    options = [
        {
            "id": item.id,
            "name": item.name,
            "revision": item.revision,
            "compatibility": assessments[item.id],
            "recommended": item.id == recommended,
            "default": item.id == default_id,
            "recent": item.id == recent,
        }
        for item in ordered
    ]
    return {"options": options, "checked": checked, "unreadable": unreadable}


def _profile_choice_page(
    request: Request,
    snapshot: QuickAnalysisSnapshot,
    *,
    selected: str | None = None,
    warning: object | None = None,
    error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    choice = _profile_options(snapshot)
    if selected is not None:
        choice["checked"] = selected
    plan = snapshot.dossier.plan_importe
    return templates.TemplateResponse(
        request=request,
        name="profile_choice.html",
        context={
            **choice,
            "analysis": snapshot,
            "plan_name": plan.nom_fichier_original if plan else "",
            "pending": is_pending(snapshot),
            "warning": warning,
            "error": error,
            "levels": COMPATIBILITY_LEVELS,
        },
        status_code=status_code,
    )


@app.get("/analyses/{analysis_id}/profil", response_class=HTMLResponse)
async def choose_profile(request: Request, analysis_id: str) -> HTMLResponse:
    return _profile_choice_page(request, _get_quick_analysis(analysis_id))


@app.post("/analyses/{analysis_id}/profil", response_class=HTMLResponse)
async def analyse_with_profile(
    request: Request,
    analysis_id: str,
    profile: str = Form(""),
    confirm: str = Form(""),
) -> Response:
    """Business analysis of the inspected plan with the chosen profile.

    The stored snapshot is reused: the plan is never read again. A profile
    that fits the plan poorly is applied only after an explicit confirmation.
    """

    snapshot = _get_quick_analysis(analysis_id)
    if profile == "legacy":
        usage = None
    else:
        try:
            chosen = profile_repository.get(profile)
        except ProfileNotFoundError:
            return _profile_choice_page(
                request, snapshot, error="Choisissez un profil de dessin ou l’analyse automatique.", status_code=422
            )
        dossier = snapshot.dossier
        compatibility = compatibility_cache.get(
            snapshot.id, chosen, dossier.controle_technique, _file_declares_unit(dossier)
        )
        if compatibility.level == "peu" and confirm != "1":
            return _profile_choice_page(
                request, snapshot, selected=chosen.id, warning={"name": chosen.name, "compatibility": compatibility}
            )
        usage = usage_from_profile(chosen)
    await run_analysis(_apply_analysis_profile, snapshot, usage)
    try:
        analysis_repository.update(snapshot)
    except QuickAnalysisNotFoundError as exc:
        # A user may delete/promote the snapshot while this thread is running.
        raise HTTPException(
            status_code=404, detail="Analyse rapide introuvable ou expirée."
        ) from exc
    return RedirectResponse(f"/analyses/{analysis_id}", status_code=303)
