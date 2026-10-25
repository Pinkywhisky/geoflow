"""Synthetic DXF/DWG/DOCX smoke checks without development dependencies.

Run from the repository root, or from /app in the runtime image. All generated
files are kept in --output (use /tmp/qa inside a read-only container).
"""

from __future__ import annotations

import argparse
import importlib.util
from io import BytesIO
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from zipfile import ZipFile

# This tool is also mounted at /qa_runtime.py in the runtime image.
sys.path.insert(0, str(Path.cwd()))

from docx import Document

from app.documents import generate_copropriete_draft
from app.domain import Dossier
from app.dwg import dxf_source
from app.dwg.converter import _converter_command, _resolve_executable
from app.dxf import inspect_dxf
from app.storage import JsonDossierRepository


EXPECTED_AREAS = sorted([80.0, 72.0, 38.0, 25 * math.pi / 2])


def check_image() -> None:
    """Inspect the actual built image, not just Dockerfile COPY instructions."""
    assert Path.cwd() == Path("/app"), "Image check must run in the runtime image"
    for module in ("pytest", "httpx", "ruff", "mypy", "playwright", "coverage"):
        assert importlib.util.find_spec(module) is None, f"Development module present: {module}"
    assert {p.name for p in Path('/app').iterdir()} == {"app", "requirements.txt"}
    for name in (".env", "secrets", ".git", ".aws", ".codex", ".agents"):
        assert not Path("/app", name).exists(), f"Unexpected image artifact: {name}"
    assert not any(Path('/app').rglob('*.dxf'))
    assert not any(Path('/app').rglob('*.dwg'))
    assert not any(Path('/app').rglob('*.docx')) or list(Path('/app').rglob('*.docx')) == [
        Path('/app/app/documents/templates/copropriete_draft_v1.docx')
    ]
    assert os.getuid() != 0
    print("IMAGE: no development dependencies, fixtures, Git or credentials; non-root user OK")


def check_plan(path: Path, file_type: str) -> None:
    with dxf_source(path, file_type) as source:
        control, _ = inspect_dxf(source, path.name)
    areas = sorted(zone.surface_geometrique_unites for zone in control.zones_candidates)
    assert len(areas) == 4, f"Expected four 2D contours, got {len(areas)}"
    for observed, expected in zip(areas, EXPECTED_AREAS):
        assert math.isclose(observed, expected, rel_tol=1e-7), (observed, expected)
    assert control.unite_detectee == "metre"
    print(f"{file_type.upper()}: four contours and exact bulge areas OK")


def make_dwg(source: Path, output: Path) -> Path:
    with tempfile.TemporaryDirectory(prefix="qa-oda-", dir=output) as workspace:
        root = Path(workspace)
        incoming, outgoing = root / "input", root / "output"
        incoming.mkdir()
        outgoing.mkdir()
        shutil.copyfile(source, incoming / "source.dxf")
        command = _converter_command(_resolve_executable(), [
            str(incoming.resolve()), str(outgoing.resolve()), "ACAD2018", "DWG", "0", "1",
        ])
        environment = os.environ.copy()
        environment.setdefault("QT_QPA_PLATFORM", os.environ.get("ODA_QT_PLATFORM", "xcb"))
        completed = subprocess.run(command, env=environment, capture_output=True, timeout=180)
        if completed.returncode:
            log = output / "oda.log"
            log.write_bytes(completed.stdout + completed.stderr)
            raise RuntimeError(f"ODA DWG generation failed (exit {completed.returncode}); see {log}")
        candidates = [path for path in outgoing.rglob("*") if path.suffix.lower() == ".dwg"]
        if len(candidates) != 1:
            raise RuntimeError("ODA did not produce one DWG")
        target = output / "synthetic.dwg"
        shutil.copyfile(candidates[0], target)
        return target


def check_docx(fixtures: Path, output: Path) -> None:
    dossier = Dossier.model_validate_json((fixtures / "copropriete_complete.json").read_text())
    generated = generate_copropriete_draft(dossier)
    document = Document(BytesIO(generated.content))
    assert document.core_properties.author == "GeoFlow"
    text = "\n".join(paragraph.text for paragraph in document.paragraphs)
    assert "BROUILLON" in text
    with ZipFile(BytesIO(generated.content)) as archive:
        assert archive.testzip() is None
        for name in archive.namelist():
            if name.endswith(".rels"):
                assert b'TargetMode="External"' not in archive.read(name)
    repo = JsonDossierRepository(output / "data")
    repo.save(dossier)
    generation = generated.generation
    repo.save_generation_artifacts(
        dossier.id, generation.id, generation.nom_fichier, generated.content, generated.snapshot,
    )
    assert repo.generated_document_path(dossier.id, generation.id, generation.nom_fichier).is_file()
    print("DOCX: valid OOXML, draft warning, no external relationships, persistence OK")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", type=Path, default=Path("tests/fixtures"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--make-dwg", action="store_true", help="Requires a real ODA installation")
    parser.add_argument("--check-image", action="store_true", help="Audit the actual runtime image")
    parser.add_argument("--dwg", type=Path, help="Previously generated synthetic DWG")
    args = parser.parse_args()
    if args.check_image:
        check_image()
    args.output.mkdir(parents=True, exist_ok=True)
    source = args.fixtures / "geoflow_plan_test_source.dxf"
    check_plan(source, "dxf")
    check_docx(args.fixtures, args.output)
    dwg = make_dwg(source, args.output) if args.make_dwg else args.dwg
    if dwg:
        check_plan(dwg, "dwg")
    else:
        print("DWG: not run (supply --make-dwg or --dwg)")


if __name__ == "__main__":
    main()
