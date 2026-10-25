"""Human-readable labels shared by the workflow pages and the quick diagnostic."""

from __future__ import annotations

from app.domain import VerificationStatus

UNIT_OPTIONS = [
    ("metre", "Mètre"),
    ("millimetre", "Millimètre"),
    ("centimetre", "Centimètre"),
    ("pied", "Pied"),
    ("pouce", "Pouce"),
]

VERIFICATION_LABELS = {
    VerificationStatus.AUTO_VERIFIE.value: "Auto-vérifié",
    VerificationStatus.A_CONFIRMER.value: "À confirmer",
    VerificationStatus.NON_RESOLU.value: "Non résolu",
    VerificationStatus.CONTRADICTOIRE.value: "Contradictoire",
    VerificationStatus.EXCLU_TECHNIQUE.value: "Exclu techniquement",
    VerificationStatus.CONFIRME_MANUEL.value: "Confirmé",
    VerificationStatus.A_REVOIR.value: "À revoir",
}


def humanize(value: str | None) -> str:
    text = (value or "").replace("_", " ").strip()
    return text[:1].upper() + text[1:]


def unit_label(value: str | None) -> str:
    if not value:
        return "Non déterminée"
    return dict(UNIT_OPTIONS).get(value, value.replace("_", " "))


def verification_label(value: str | VerificationStatus | None) -> str:
    key = value.value if isinstance(value, VerificationStatus) else value
    return VERIFICATION_LABELS.get(key or "", humanize(key))
