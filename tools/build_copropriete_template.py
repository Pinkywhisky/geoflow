"""Rebuild the versioned, generic GeoFlow copropriete DOCX template."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from docx import Document

from app.documents.generator import (
    DEFAULT_TEMPLATE_PATH,
    _configure_document,
)


FIXED_METADATA_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)
FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)


def _write_deterministic_package(package: BytesIO, output: Path) -> None:
    package.seek(0)
    with ZipFile(package, "r") as source, ZipFile(
        output, "w", compression=ZIP_DEFLATED
    ) as destination:
        for name in sorted(source.namelist()):
            info = ZipInfo(name, date_time=FIXED_ZIP_TIME)
            info.compress_type = ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o600 << 16
            destination.writestr(info, source.read(name))


def build_template(output: Path) -> None:
    document = Document()
    _configure_document(document)
    properties = document.core_properties
    properties.title = "GeoFlow — Template copropriété"
    properties.subject = "Copropriété"
    properties.author = "GeoFlow"
    properties.last_modified_by = "GeoFlow"
    properties.comments = ""
    properties.keywords = "GeoFlow, copropriété, template"
    properties.created = FIXED_METADATA_TIME
    properties.modified = FIXED_METADATA_TIME
    properties.revision = 1

    output.parent.mkdir(parents=True, exist_ok=True)
    package = BytesIO()
    document.save(package)
    _write_deterministic_package(package, output)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output", type=Path, default=DEFAULT_TEMPLATE_PATH
    )
    args = parser.parse_args()
    build_template(args.output)
    print(args.output)


if __name__ == "__main__":
    main()
