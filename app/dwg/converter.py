"""Isolated and defensive invocation of ODA File Converter."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Literal


logger = logging.getLogger(__name__)


class OdaNotAvailableError(RuntimeError):
    """ODA File Converter cannot be found on this runtime."""


class DwgConversionError(RuntimeError):
    """ODA failed to produce a readable DXF."""


def _resolve_executable() -> str:
    configured = os.environ.get("ODA_FILE_CONVERTER", "ODAFileConverter")
    if Path(configured).is_file():
        return str(Path(configured))
    resolved = shutil.which(configured)
    if resolved is None:
        raise OdaNotAvailableError(
            "ODA File Converter est absent ou ODA_FILE_CONVERTER est incorrect."
        )
    return resolved


@contextmanager
def converted_dwg(source: str | Path) -> Iterator[Path]:
    """Convert a DWG to a temporary DXF and always remove the workspace."""

    executable = _resolve_executable()
    with tempfile.TemporaryDirectory(prefix="geoflow-oda-") as workspace_name:
        workspace = Path(workspace_name)
        input_dir = workspace / "input"
        output_dir = workspace / "output"
        input_dir.mkdir()
        output_dir.mkdir()

        # The upload name is never reused: this fixed name prevents traversal and
        # avoids converter surprises with control characters or platform separators.
        converter_input = input_dir / "source.dwg"
        shutil.copyfile(Path(source), converter_input)
        command = [
            executable,
            str(input_dir),
            str(output_dir),
            "ACAD2018",
            "DXF",
            "0",
            "1",
        ]
        environment = os.environ.copy()
        environment.setdefault(
            "QT_QPA_PLATFORM", os.environ.get("ODA_QT_PLATFORM", "xcb")
        )
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=180,
                env=environment,
            )
        except FileNotFoundError as exc:
            raise OdaNotAvailableError("ODA File Converter est introuvable.") from exc
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.exception("ODA File Converter could not complete")
            raise DwgConversionError("La conversion DWG a echoue.") from exc

        if completed.returncode != 0:
            logger.error(
                "ODA conversion failed (code=%s): %s",
                completed.returncode,
                (completed.stderr or completed.stdout or "")[-2000:],
            )
            raise DwgConversionError("ODA File Converter a retourne une erreur.")

        candidates = [
            path
            for path in output_dir.rglob("*")
            if path.is_file() and path.suffix.lower() == ".dxf"
        ]
        if not candidates:
            logger.error("ODA conversion completed without a DXF output")
            raise DwgConversionError("ODA File Converter n'a produit aucun DXF.")

        converted = candidates[0].resolve()
        if not converted.is_relative_to(output_dir.resolve()):
            raise DwgConversionError("Sortie ODA non sure.")
        yield converted


@contextmanager
def dxf_source(
    source: str | Path, file_type: Literal["dxf", "dwg"]
) -> Iterator[Path]:
    if file_type == "dxf":
        yield Path(source)
        return
    with converted_dwg(source) as converted:
        yield converted
