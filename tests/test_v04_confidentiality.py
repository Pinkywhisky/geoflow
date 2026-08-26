from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_candidate_files_do_not_contain_private_reference_markers() -> None:
    excluded_roots = {
        ".git",
        ".venv",
        ".pytest_cache",
        "data",
        "samples/private",
    }
    files = [
        path
        for path in ROOT.rglob("*")
        if path.is_file()
        and not any(
            path.relative_to(ROOT).as_posix() == excluded
            or path.relative_to(ROOT).as_posix().startswith(excluded + "/")
            for excluded in excluded_roots
        )
    ]
    forbidden = (
        "P" + "4074",
        "P" + "7388",
        "121 rue d'" + "Aboukir",
        "22-24-28 rue des " + "Bains",
    )
    leaks: list[str] = []
    for path in files:
        relative = path.relative_to(ROOT).as_posix()
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for marker in forbidden:
            if marker in relative or marker in content:
                leaks.append(f"{relative}: {marker}")
    assert leaks == []
