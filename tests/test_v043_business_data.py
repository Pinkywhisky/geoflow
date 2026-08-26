from __future__ import annotations

import json
from pathlib import Path

from app.documents import assess_generation_readiness
from app.domain import (
    AdressePostale,
    Dossier,
    Millieme,
    Parcelle,
    StatutValidationDonnees,
    StatutValidationMillieme,
)
from app.workflow import (
    add_parcel,
    record_data_validation,
    remove_parcel,
    set_millieme_grid_complete,
    update_dossier_details,
    update_lot_metadata,
)


FIXTURE = Path(__file__).parent / "fixtures" / "copropriete_complete.json"


def complete_dossier() -> Dossier:
    return Dossier.model_validate_json(FIXTURE.read_text(encoding="utf-8"))


def test_legacy_json_migrates_address_parcels_and_millieme_boolean() -> None:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload["milliemes"] = [
        {"lot_id": "lot-1", "valeur": 250, "base": 1000, "valide": True}
    ]
    dossier = Dossier.model_validate(payload)

    assert dossier.schema_version == "1.1"
    assert dossier.adresse == AdressePostale(voie="10 avenue des Tilleuls")
    assert dossier.references_cadastrales[0].section == "AB"
    assert dossier.references_cadastrales[0].numero == "101"
    assert dossier.milliemes[0].statut_validation == StatutValidationMillieme.VALIDE


def test_multiple_structured_parcels_round_trip() -> None:
    dossier = complete_dossier()
    dossier.references_cadastrales = []
    add_parcel(dossier, commune="Paris", section="AO", numero="163")
    add_parcel(dossier, commune="Paris", section="AO", numero="164")

    restored = Dossier.model_validate_json(dossier.model_dump_json())
    assert restored.references_cadastrales == [
        Parcelle(commune="Paris", section="AO", numero="163"),
        Parcelle(commune="Paris", section="AO", numero="164"),
    ]
    remove_parcel(restored, 0)
    assert restored.references_cadastrales[0].numero == "164"


def test_complete_address_and_plan_date_are_canonical() -> None:
    dossier = complete_dossier()
    update_dossier_details(
        dossier,
        numero="12",
        voie="rue des Écoles",
        complement="Bâtiment B",
        code_postal="75005",
        commune="Paris",
        departement="Paris",
        date_plan="2026-08-20",
    )

    assert dossier.adresse == AdressePostale(
        numero="12",
        voie="rue des Écoles",
        complement="Bâtiment B",
        code_postal="75005",
    )
    assert dossier.commune == "Paris"
    assert dossier.date_plan == "2026-08-20"


def test_lot_usage_designation_and_millieme_are_lot_centric() -> None:
    dossier = complete_dossier()
    update_lot_metadata(
        dossier,
        "lot-1",
        usage="Appartement",
        designation="Appartement comprenant entrée, séjour et cuisine.",
        millieme_value="400",
        millieme_base="1000",
        millieme_status="valide",
    )

    lot = next(item for item in dossier.lots if item.id == "lot-1")
    assert lot.usage == "Appartement"
    assert lot.designation.startswith("Appartement comprenant")
    assert len(lot.zone_ids) == 2
    assert dossier.milliemes == [
        Millieme(
            lot_id="lot-1",
            valeur=400,
            base=1000,
            statut_validation=StatutValidationMillieme.VALIDE,
        )
    ]


def test_complete_millieme_grid_requires_all_lots_and_total_1000() -> None:
    dossier = complete_dossier()
    dossier.milliemes = [
        Millieme(
            lot_id="lot-1",
            valeur=400,
            base=1000,
            statut_validation=StatutValidationMillieme.VALIDE,
        ),
        Millieme(
            lot_id="lot-2",
            valeur=600,
            base=1000,
            statut_validation=StatutValidationMillieme.VALIDE,
        ),
    ]
    set_millieme_grid_complete(dossier, True)
    readiness = assess_generation_readiness(dossier)
    assert not any("millièmes" in item.lower() for item in readiness.blockers)
    assert not any(
        item.code.startswith("milliemes") for item in readiness.user_actions
    )

    dossier.milliemes[1].valeur = 599
    readiness = assess_generation_readiness(dossier)
    assert any("999 au lieu de 1000" in item for item in readiness.blockers)


def test_business_change_invalidates_current_validation() -> None:
    dossier = complete_dossier()
    record_data_validation(dossier)
    assert dossier.statut_validation_donnees == StatutValidationDonnees.VALIDE

    update_lot_metadata(
        dossier,
        "lot-1",
        usage="Bureau",
        designation="Bureau au rez-de-chaussée.",
        millieme_value="",
        millieme_base="1000",
        millieme_status="a_confirmer",
    )
    assert dossier.statut_validation_donnees == StatutValidationDonnees.A_VALIDER
    assert dossier.sha256_validation_donnees is None
