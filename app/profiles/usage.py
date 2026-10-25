"""Snapshot of the profile used by an analysis, and its traceability view.

The dossier keeps a frozen copy of the rules. The stored profile is only
looked up to tell the user whether it was renamed, revised or deleted since:
it is never applied to an analysed dossier.
"""

from __future__ import annotations

from collections import Counter

from app.domain import Dossier, ProfileUsage
from app.profiles.models import DrawingProfile

LEGACY_LABEL = "Analyse automatique / Legacy"


def usage_from_profile(profile: DrawingProfile) -> ProfileUsage:
    return ProfileUsage(
        mode="profil",
        profile_id=profile.id,
        profile_name=profile.name,
        revision=profile.revision,
        content_hash=profile.content_hash,
        rules=profile.rules(),
    )


def usage_summary(dossier: Dossier, current: DrawingProfile | None) -> dict[str, object]:
    """What the dossier was analysed with; ``current`` is the stored profile
    with the same id, if it still exists."""

    usage = dossier.profile_usage
    reconciliation = dossier.reconciliation
    origins = Counter(item.origine for item in (reconciliation.lot_proposals if reconciliation else []))
    if usage is None or usage.mode == "legacy":
        return {
            "mode": "legacy",
            "label": LEGACY_LABEL,
            "former": usage is None and reconciliation is not None,
            "analysed_at": usage.analysed_at if usage else None,
        }
    return {
        "mode": "profil",
        "label": f"{usage.profile_name} · v{usage.revision}",
        "profile_id": usage.profile_id,
        "name": usage.profile_name,
        "revision": usage.revision,
        "content_hash": usage.content_hash,
        "analysed_at": usage.analysed_at,
        "deleted": current is None,
        "current_name": current.name if current is not None and current.name != usage.profile_name else None,
        "newer_revision": (
            current.revision
            if current is not None and current.revision > usage.revision and current.content_hash != usage.content_hash
            else None
        ),
        "lots_profile": origins["profil"],
        "lots_legacy": origins["legacy"],
    }
