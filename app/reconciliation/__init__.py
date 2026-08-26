"""Deterministic, explainable reconciliation between DXF contours and lots."""

from .engine import (
    DEFAULT_RULES,
    RECONCILIATION_RULES_VERSION,
    ReconciliationRules,
    SurfaceTolerance,
    parse_principal_surface,
    reconcile_dossier,
)

__all__ = [
    "DEFAULT_RULES",
    "RECONCILIATION_RULES_VERSION",
    "ReconciliationRules",
    "SurfaceTolerance",
    "parse_principal_surface",
    "reconcile_dossier",
]
