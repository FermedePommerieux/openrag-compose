"""`_build_chunk_document` must carry `connector_file_id` from the context onto
the indexed document when present, so bucket-connector documents (COS/Azure/S3)
keep their raw, human-meaningful id available for dedupe/orphan lookups even
though `document_id` is now a stable hash (see
`tests/unit/test_resolve_document_id_connector_file_id.py`).
"""

from services.document_index_writer import (
    DocumentIndexChunk,
    DocumentIndexContext,
    DocumentIndexWriter,
)


def _context(**overrides):
    defaults = dict(
        document_id="hashed-id",
        filename="報告書.pdf",
        mimetype="application/pdf",
        embedding_model="test-model",
    )
    defaults.update(overrides)
    return DocumentIndexContext(**defaults)


def test_connector_file_id_from_context_is_set_on_document():
    writer = DocumentIndexWriter()
    context = _context(connector_file_id="my-bucket::報告書.pdf")
    chunk = DocumentIndexChunk(chunk_id="c1", text="hello", vector=[0.1, 0.2])

    doc = writer._build_chunk_document(
        context=context,
        chunk=chunk,
        embedding_field="embedding",
        indexed_time="2026-01-01T00:00:00Z",
    )

    assert doc["connector_file_id"] == "my-bucket::報告書.pdf"
    assert doc["document_id"] == "hashed-id"


def test_missing_connector_file_id_falls_back_to_chunk_metadata():
    writer = DocumentIndexWriter()
    context = _context(connector_file_id=None)
    chunk = DocumentIndexChunk(
        chunk_id="c1",
        text="hello",
        vector=[0.1, 0.2],
        metadata={"connector_file_id": "legacy-id"},
    )

    doc = writer._build_chunk_document(
        context=context,
        chunk=chunk,
        embedding_field="embedding",
        indexed_time="2026-01-01T00:00:00Z",
    )

    assert doc["connector_file_id"] == "legacy-id"


def test_no_connector_file_id_anywhere_omits_field():
    writer = DocumentIndexWriter()
    context = _context()
    chunk = DocumentIndexChunk(chunk_id="c1", text="hello", vector=[0.1, 0.2])

    doc = writer._build_chunk_document(
        context=context,
        chunk=chunk,
        embedding_field="embedding",
        indexed_time="2026-01-01T00:00:00Z",
    )

    assert "connector_file_id" not in doc


def test_chunk_position_and_strategy_are_indexed_for_retrieval_context():
    writer = DocumentIndexWriter()
    context = _context(chunking_strategy="hybrid")
    chunk = DocumentIndexChunk(
        chunk_id="c1",
        text="hello",
        vector=[0.1, 0.2],
        chunk_index=4,
    )

    doc = writer._build_chunk_document(
        context=context,
        chunk=chunk,
        embedding_field="embedding",
        indexed_time="2026-01-01T00:00:00Z",
    )

    assert doc["chunk_index"] == 4
    assert doc["chunking_strategy"] == "hybrid"
    assert doc["chunk_id"].endswith("_c1")


def test_logical_chunk_identity_stays_stable_while_generation_storage_id_changes():
    writer = DocumentIndexWriter()
    first = _context()
    replacement = _context(ingest_run_id="new-generation")

    assert writer._logical_chunk_id(first, "c1") == writer._logical_chunk_id(replacement, "c1")
    assert writer.storage_chunk_id(first, "c1") != writer.storage_chunk_id(replacement, "c1")
