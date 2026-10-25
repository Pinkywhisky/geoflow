"""Controlled mini-format for profile patterns: no user regex, no eval.

Two kinds of patterns exist:

* layer patterns, matched against layer names: literal characters plus the
  wildcards ``*`` (any sequence) and ``?`` (one character), case-insensitive;
* text templates, matched against annotation texts: literal characters plus
  exactly one placeholder among ``{numero}``, ``{surface}`` and ``{niveau}``.

Both compile to anchored, bounded regular expressions built only from
escaped literals and fixed sub-patterns, so a profile can never inject a
regular expression, and matching stays linear on bounded inputs.
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

LAYER_PATTERN_MAX = 128
TEMPLATE_MAX = 60
TEXT_MAX = 300
PLACEHOLDERS = {
    "numero": r"([0-9]{1,6}[A-Z]?)",
    "surface": r"([0-9]{1,6}(?:[.,][0-9]{1,3})?)",
    "niveau": r"(.{1,40}?)",
}
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_PLACEHOLDER = re.compile(r"\{([a-z]+)\}")


def normalize_text(value: str) -> str:
    """Case- and accent-insensitive form used for every comparison."""

    decomposed = unicodedata.normalize("NFKD", value[:TEXT_MAX])
    ascii_value = decomposed.encode("ascii", "ignore").decode("ascii")
    return " ".join(ascii_value.upper().split())


def clean_label(value: str, limit: int) -> str:
    return " ".join(_CONTROL.sub(" ", value).split())[:limit]


def validate_layer_pattern(value: str) -> str:
    cleaned = clean_label(value, LAYER_PATTERN_MAX + 1)
    if not cleaned:
        raise ValueError("Un motif de calque ne peut pas être vide.")
    if len(cleaned) > LAYER_PATTERN_MAX:
        raise ValueError(f"Un motif de calque dépasse {LAYER_PATTERN_MAX} caractères.")
    if cleaned.strip("*?") == "":
        raise ValueError("Un motif de calque doit contenir au moins un caractère littéral.")
    return cleaned


@lru_cache(maxsize=512)
def _layer_regex(pattern: str) -> re.Pattern[str]:
    parts = []
    for char in normalize_text(pattern):
        if char == "*":
            if not parts or parts[-1] != ".*":
                parts.append(".*")
        elif char == "?":
            parts.append(".")
        else:
            parts.append(re.escape(char))
    return re.compile("^" + "".join(parts) + "$")


def layer_matches(pattern: str, layer_name: str) -> bool:
    return bool(_layer_regex(pattern).match(normalize_text(layer_name)))


def validate_template(value: str, required: str) -> str:
    """A template with exactly one placeholder, which must be ``required``."""

    cleaned = clean_label(value, TEMPLATE_MAX + 1)
    if len(cleaned) > TEMPLATE_MAX:
        raise ValueError(f"Un motif de texte dépasse {TEMPLATE_MAX} caractères.")
    names = _PLACEHOLDER.findall(cleaned)
    if names != [required]:
        raise ValueError(f"Le motif doit contenir exactement une fois {{{required}}}.")
    if any(name not in PLACEHOLDERS for name in names):
        raise ValueError("Motif inconnu.")
    if "{" in _PLACEHOLDER.sub("", cleaned) or "}" in _PLACEHOLDER.sub("", cleaned):
        raise ValueError("Accolade isolée dans le motif.")
    return cleaned


def _compact(template: str, text: str) -> str:
    # Numbers and surfaces ignore spacing ("S = 12 m2" is "S=12m2"); level
    # names keep it, since "SOUS SOL" is a value.
    return text if "{NIVEAU}" in template else text.replace(" ", "")


@lru_cache(maxsize=256)
def _template_regex(template: str) -> re.Pattern[str]:
    normalized = normalize_text(template)
    normalized = _compact(normalized, normalized)
    pieces = []
    position = 0
    for match in re.finditer(r"\{([A-Z]+)\}", normalized):
        pieces.append(re.escape(normalized[position : match.start()]))
        pieces.append(PLACEHOLDERS[match.group(1).lower()])
        position = match.end()
    pieces.append(re.escape(normalized[position:]))
    return re.compile("^" + "".join(pieces) + "$")


def template_value(template: str, text: str) -> str | None:
    """Captured placeholder value, or None when the text does not match."""

    normalized_template = normalize_text(template)
    candidate = _compact(normalized_template, normalize_text(text))
    match = _template_regex(template).match(candidate)
    return match.group(1).strip() if match else None


def surface_value(template: str, text: str) -> float | None:
    value = template_value(template, text)
    if value is None:
        return None
    try:
        return float(value.replace(",", "."))
    except ValueError:
        return None


def infer_template(texts: list[str], placeholder: str) -> tuple[str, int] | None:
    """Most frequent shape once the value is replaced by its placeholder.

    Returns the template and how many texts it matches; only shapes built
    from the text itself are considered, never a guess.
    """

    # A value never starts right after a letter or digit: "M2" is a unit.
    value_pattern = re.compile(PLACEHOLDERS[placeholder].replace("([", "(?<![0-9A-Z])([", 1))
    counts: dict[str, int] = {}
    for text in texts:
        normalized = normalize_text(text)
        found = list(value_pattern.finditer(normalized))
        if len(found) != 1:
            continue
        match = found[0]
        shape = normalized[: match.start()] + "{" + placeholder + "}" + normalized[match.end() :]
        if placeholder != "niveau":
            shape = shape.replace(" ", "")
        if len(shape) <= TEMPLATE_MAX:
            counts[shape] = counts.get(shape, 0) + 1
    if not counts:
        return None
    shape = max(counts, key=lambda item: (counts[item], -len(item), item))
    return _readable(shape), counts[shape]


def _readable(shape: str) -> str:
    """Display form of an inferred template; matching ignores these changes."""

    shape = re.sub(r"(?<=[A-Z])\{", " {", shape)
    shape = re.sub(r"\}(?=[A-Z])", "} ", shape)
    return re.sub(r"M2$", "m²", shape)
