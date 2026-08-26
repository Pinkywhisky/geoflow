"""Small JSON persistence boundary, replaceable by a database later."""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

from app.domain import Dossier


_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class DossierNotFoundError(KeyError):
    pass


class JsonDossierRepository:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def _path(self, dossier_id: str) -> Path:
        if not _SAFE_ID.fullmatch(dossier_id):
            raise DossierNotFoundError(dossier_id)
        return self.root / f"{dossier_id}.json"

    def save(self, dossier: Dossier) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        target = self._path(dossier.id)
        payload = dossier.model_dump_json(indent=2)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.root,
                prefix=f".{dossier.id}-",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary.write(payload)
                temporary.write(chr(10))
                temporary_path = Path(temporary.name)
            os.replace(temporary_path, target)
            temporary_path = None
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def get(self, dossier_id: str) -> Dossier:
        path = self._path(dossier_id)
        try:
            return Dossier.model_validate_json(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise DossierNotFoundError(dossier_id) from exc

    def list(self) -> list[Dossier]:
        if not self.root.exists():
            return []
        dossiers: list[Dossier] = []
        for path in sorted(self.root.glob("*.json"), reverse=True):
            try:
                dossiers.append(Dossier.model_validate_json(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return dossiers
