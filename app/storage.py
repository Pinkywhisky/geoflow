"""Small JSON persistence boundary, replaceable by a database later."""

from __future__ import annotations

import os
import re
import shutil
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

    def delete(self, dossier_id: str) -> None:
        """Delete one validated dossier path and its generated artifacts."""

        dossier_path = self._path(dossier_id)
        if not dossier_path.is_file():
            raise DossierNotFoundError(dossier_id)
        generated_root = (self.root / "dossiers" / dossier_id).resolve()
        data_root = self.root.resolve()
        if not generated_root.is_relative_to(data_root) or generated_root == data_root:
            raise DossierNotFoundError(dossier_id)
        pending_delete = dossier_path.with_name(f".{dossier_id}-deleting.tmp")
        os.replace(dossier_path, pending_delete)
        try:
            if generated_root.is_dir():
                shutil.rmtree(generated_root)
            pending_delete.unlink()
        except Exception:
            if pending_delete.exists():
                os.replace(pending_delete, dossier_path)
            raise

    def save_generation_artifacts(
        self,
        dossier_id: str,
        generation_id: str,
        filename: str,
        document: bytes,
        snapshot: bytes,
    ) -> None:
        """Persist a DOCX and its canonical snapshot below the data root."""

        if not _SAFE_ID.fullmatch(dossier_id) or not _SAFE_ID.fullmatch(generation_id):
            raise ValueError("Identifiant de génération non sûr.")
        if (
            Path(filename).name != filename
            or not filename.lower().endswith(".docx")
            or len(filename) > 180
        ):
            raise ValueError("Nom de document non sûr.")
        target_dir = self.root / "dossiers" / dossier_id / "generated" / generation_id
        target_dir.mkdir(parents=True, exist_ok=False)
        try:
            self._write_bytes_atomic(target_dir / "dossier_snapshot.json", snapshot)
            self._write_bytes_atomic(target_dir / filename, document)
        except Exception:
            for child in target_dir.iterdir():
                child.unlink(missing_ok=True)
            target_dir.rmdir()
            raise

    def generated_document_path(
        self, dossier_id: str, generation_id: str, filename: str
    ) -> Path:
        if not _SAFE_ID.fullmatch(dossier_id) or not _SAFE_ID.fullmatch(generation_id):
            raise DossierNotFoundError(dossier_id)
        if Path(filename).name != filename or not filename.lower().endswith(".docx"):
            raise DossierNotFoundError(generation_id)
        generated_root = (
            self.root / "dossiers" / dossier_id / "generated" / generation_id
        ).resolve()
        target = (generated_root / filename).resolve()
        if not target.is_relative_to(generated_root) or not target.is_file():
            raise DossierNotFoundError(generation_id)
        return target

    @staticmethod
    def _write_bytes_atomic(target: Path, payload: bytes) -> None:
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=target.parent,
                prefix=f".{target.name}-",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary.write(payload)
                temporary_path = Path(temporary.name)
            os.replace(temporary_path, target)
            temporary_path = None
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
