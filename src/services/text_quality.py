"""Deterministic quality checks for text extracted from documents.

These checks do not try to repair text. They recognise a narrow,
high-confidence signature produced by broken PDF character maps so ingestion
can retry the original document with full-page OCR.
"""

from collections.abc import Mapping, Sequence
from typing import Any

# These rare characters occur together in the PDF corruption observed in
# production. Requiring them plus a dense run of Latin-extended characters
# avoids classifying ordinary accents, names, or another language as corrupt.
_RARE_PDF_MOJIBAKE_MARKERS = frozenset("ƋƵƈǀϬϯ")
_MIN_RARE_MARKERS = 3
_MIN_EXTENDED_CHARACTERS = 8
_MIN_EXTENDED_RATIO = 0.08


def has_pdf_character_map_mojibake(text: str) -> bool:
    """Return whether ``text`` has a high-confidence broken PDF cmap signature."""
    visible = sum(not character.isspace() for character in text)
    if visible == 0:
        return False

    rare_markers = sum(character in _RARE_PDF_MOJIBAKE_MARKERS for character in text)
    if rare_markers < _MIN_RARE_MARKERS:
        return False

    extended = sum(0x0100 <= ord(character) <= 0x024F for character in text)
    return extended >= _MIN_EXTENDED_CHARACTERS and extended / visible >= _MIN_EXTENDED_RATIO


def docling_document_has_mojibake(document: Mapping[str, Any]) -> bool:
    """Inspect the textual fields of a Docling JSON document for mojibake."""
    text_parts: list[str] = []

    def collect(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, nested in value.items():
                if key == "text" and isinstance(nested, str):
                    text_parts.append(nested)
                else:
                    collect(nested)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            for nested in value:
                collect(nested)

    collect(document)
    return has_pdf_character_map_mojibake("\n".join(text_parts))
