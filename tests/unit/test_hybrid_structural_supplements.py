"""Exhaustive structural evidence preserved alongside HybridChunker output."""

import tiktoken

from utils.document_processing import _structural_supplement_chunks


def _item(ref, text, label, *, left, top, page=1):
    return {
        "self_ref": ref,
        "text": text,
        "label": label,
        "prov": [
            {
                "page_no": page,
                "bbox": {"l": left, "t": top, "r": left + 10, "b": top - 10},
            }
        ],
    }


def test_supplements_only_uncovered_items_and_preserves_spatial_structure():
    document = {
        "texts": [
            _item("#/texts/0", "Covered body", "text", left=20, top=700),
            _item("#/texts/1", "Right identity block", "page_footer", left=300, top=100),
            _item("#/texts/2", "Left identity block", "page_footer", left=20, top=100),
            _item("#/texts/3", "Document title", "section_header", left=20, top=800),
        ]
    }

    chunks = _structural_supplement_chunks(
        document,
        covered_refs={"#/texts/0"},
        covered_text_lines=set(),
        max_tokens=128,
    )

    combined = "\n".join(chunk["text"] for chunk in chunks)
    assert "Covered body" not in combined
    assert "Region: page_footer" in combined
    assert "Region: section_header" in combined
    assert combined.index("Left identity block") < combined.index("Right identity block")
    assert all(chunk["type"] == "docling_structural_supplement" for chunk in chunks)


def test_supplement_chunks_respect_hybrid_token_limit():
    document = {
        "texts": [
            _item(
                f"#/texts/{index}",
                "structural evidence " * 20,
                "page_footer",
                left=index * 10,
                top=100,
            )
            for index in range(5)
        ]
    }

    chunks = _structural_supplement_chunks(
        document,
        covered_refs=set(),
        covered_text_lines=set(),
        max_tokens=64,
    )
    tokenizer = tiktoken.get_encoding("cl100k_base")

    assert len(chunks) > 1
    assert all(len(tokenizer.encode(chunk["text"])) <= 64 for chunk in chunks)


def test_exact_line_coverage_avoids_duplicates_without_hiding_short_headers():
    document = {
        "texts": [
            _item("#/texts/0", "Document", "section_header", left=20, top=800),
            _item("#/texts/1", "Document number: 42", "text", left=20, top=700),
        ]
    }

    chunks = _structural_supplement_chunks(
        document,
        covered_refs=set(),
        covered_text_lines={"document number: 42"},
        max_tokens=128,
    )

    combined = "\n".join(chunk["text"] for chunk in chunks)
    assert "Text: Document" in combined
    assert "Document number: 42" not in combined
