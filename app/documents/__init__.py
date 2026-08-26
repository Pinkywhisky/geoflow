"""Versioned document generation boundary."""

from .context import GenerationReadiness, assess_generation_readiness
from .generator import (
    DEFAULT_TEMPLATE_PATH,
    TEMPLATE_ID,
    TEMPLATE_VERSION,
    GeneratedDocument,
    GenerationBlockedError,
    TemplateCorruptError,
    TemplateMissingError,
    generate_copropriete_draft,
)

__all__ = [
    "DEFAULT_TEMPLATE_PATH",
    "GeneratedDocument",
    "GenerationBlockedError",
    "GenerationReadiness",
    "TEMPLATE_ID",
    "TEMPLATE_VERSION",
    "TemplateCorruptError",
    "TemplateMissingError",
    "assess_generation_readiness",
    "generate_copropriete_draft",
]
