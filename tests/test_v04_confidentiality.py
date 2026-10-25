from __future__ import annotations

from pathlib import Path
import subprocess


ROOT = Path(__file__).parents[1]


def test_candidate_files_do_not_contain_private_reference_markers() -> None:
    # Inspect candidates for publication, never ignored credentials/runtime data.
    candidates = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
    ).split(b"\0")
    files = [
        ROOT / name.decode()
        for name in candidates
        if name and (ROOT / name.decode()).is_file()
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
