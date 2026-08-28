"""Small regression tests for the deterministic PDF text quality guard."""

from services.text_quality import (
    docling_document_has_mojibake,
    docling_document_has_usable_text,
    docling_document_text,
    docling_pdf_ocr_retry_reason,
    has_pdf_character_map_mojibake,
)

CORRUPT_PDF_TEXT = (
    "Tous les pins et les chênes ĚĞ ƋƵĂůŝƚĠ ďŽŝƐ Ě͛ƈƵǀƌĞ͕ "
    "ƐƵƉĠƌŝĞƵƌƐ ă ϯϱ Đŵ ĚĞ ĚŝĂŵğƚƌĞ ă ŚĂƵƚĞƵƌ Ě͛ŚŽŵŵĞ."
)


def test_detects_observed_broken_pdf_character_map() -> None:
    assert has_pdf_character_map_mojibake(CORRUPT_PDF_TEXT)
    assert docling_document_has_mojibake(
        {"texts": [{"text": "Titre normal"}, {"text": CORRUPT_PDF_TEXT}]}
    )


def test_accepts_normal_french_and_sparse_extended_characters() -> None:
    text = (
        "Tous les pins et les chênes de qualité bois d’œuvre, supérieurs à "
        "35 cm de diamètre à hauteur d’homme seront maintenus. Đorđe et Ţiriac."
    )
    assert not has_pdf_character_map_mojibake(text)
    assert not docling_document_has_mojibake({"texts": [{"text": text}]})


def test_docling_text_collection_and_empty_detection() -> None:
    document = {
        "texts": [{"text": "  "}, {"text": "Texte lisible"}],
        "tables": [{"data": {"table_cells": [{"text": "Cellule"}]}}],
    }

    assert docling_document_text(document) == "  \nTexte lisible\nCellule"
    assert docling_document_has_usable_text(document)
    assert not docling_document_has_usable_text({"texts": [{"text": " \n "}]})
    assert docling_pdf_ocr_retry_reason({"texts": []}) == "no usable extracted text"
    assert docling_pdf_ocr_retry_reason(document) is None
