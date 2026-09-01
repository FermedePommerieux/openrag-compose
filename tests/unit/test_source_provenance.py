"""Contract tests for OpenRAG's bounded W3C PROV-O JSON profile."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from api.v1.chat import _extract_sources
from models.source_provenance import (
    PROV_NAMESPACE,
    SourceProvenance,
    source_provenance_mapping,
)
from services.document_index_writer import (
    DocumentIndexChunk,
    DocumentIndexContext,
    DocumentIndexWriter,
)
from services.langflow_ingest_token_service import LangflowIngestTokenService


def _provenance_payload() -> dict:
    return {
        "schema_version": "1.0",
        "entity": {
            "id": "urn:openrag:attachment:invoice-1",
            "type": f"{PROV_NAMESPACE}Entity",
            "source_system": "imap",
            "alternate_ids": ["cid:invoice@example.test"],
        },
        "relative_path": "administration/invoices/invoice.pdf",
        "relations": [
            {
                "role": "attachment_of",
                "target": {
                    "id": "urn:openrag:email:message-1",
                    "type": f"{PROV_NAMESPACE}Entity",
                },
            },
            {
                "role": "member_of",
                "target": {
                    "id": "urn:openrag:email-thread:thread-1",
                    "type": f"{PROV_NAMESPACE}Collection",
                },
            },
        ],
    }


def test_source_provenance_resolves_predicates_and_builds_query_fields():
    provenance = SourceProvenance.model_validate(_provenance_payload())

    assert provenance.relations[0].prov_predicate == f"{PROV_NAMESPACE}wasMemberOf"
    assert provenance.relations[1].prov_predicate == f"{PROV_NAMESPACE}wasMemberOf"
    assert provenance.index_fields()["source_relation_target_ids"] == [
        "urn:openrag:email:message-1",
        "urn:openrag:email-thread:thread-1",
    ]
    assert provenance.index_fields()["source_relation_roles"] == [
        "attachment_of",
        "member_of",
    ]
    assert provenance.index_fields()["source_relative_path"] == (
        "administration/invoices/invoice.pdf"
    )
    assert provenance.index_fields()["source_path_ancestors"] == [
        "administration",
        "administration/invoices",
    ]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload.update({"unknown": True}),
        lambda payload: payload.update({"schema_version": "2.0"}),
        lambda payload: payload["relations"][0].update({"role": "unbounded_relation"}),
        lambda payload: payload["relations"][0].update(
            {"prov_predicate": f"{PROV_NAMESPACE}wasDerivedFrom"}
        ),
    ],
)
def test_source_provenance_rejects_unbounded_or_inconsistent_json(mutation):
    payload = _provenance_payload()
    mutation(payload)

    with pytest.raises(ValidationError):
        SourceProvenance.model_validate(payload)


def test_source_provenance_rejects_duplicate_role_target_pairs():
    payload = _provenance_payload()
    payload["relations"].append(dict(payload["relations"][0]))

    with pytest.raises(ValidationError, match="duplicate relation"):
        SourceProvenance.model_validate(payload)


@pytest.mark.parametrize(
    "relative_path",
    [
        "/srv/openrag/secret.pdf",
        "C:\\Users\\person\\secret.pdf",
        "folder/../secret.pdf",
        "folder//secret.pdf",
        "folder/./secret.pdf",
        "folder/secret.pdf\n",
    ],
)
def test_source_provenance_rejects_non_portable_relative_paths(relative_path):
    payload = _provenance_payload()
    payload["relative_path"] = relative_path

    with pytest.raises(ValidationError, match="relative_path"):
        SourceProvenance.model_validate(payload)


def test_source_provenance_normalizes_windows_relative_separator():
    payload = _provenance_payload()
    payload["relative_path"] = "folder\\subfolder\\invoice.pdf"

    assert SourceProvenance.model_validate(payload).relative_path == (
        "folder/subfolder/invoice.pdf"
    )


def test_source_provenance_mapping_keeps_relation_target_pairing():
    mapping = source_provenance_mapping()

    relations = mapping["properties"]["relations"]
    assert relations["type"] == "nested"
    assert relations["properties"]["target"]["properties"]["id"] == {"type": "keyword"}
    assert mapping["properties"]["relative_path"]["type"] == "keyword"


def test_document_writer_repeats_provenance_on_each_verifiable_chunk():
    provenance = SourceProvenance.model_validate(_provenance_payload())
    context = DocumentIndexContext(
        document_id="doc-1",
        filename="invoice.pdf",
        mimetype="application/pdf",
        embedding_model="text-embedding-3-large",
        source_url="https://archive.example.test/invoice.pdf",
        source_provenance=provenance,
    )
    chunk = DocumentIndexChunk(
        chunk_id="doc-1_0",
        text="Total: 42 EUR",
        vector=[0.1, 0.2],
        page=1,
    )

    document = DocumentIndexWriter()._build_chunk_document(
        context=context,
        chunk=chunk,
        embedding_field="vector_field",
        indexed_time="2026-08-27T00:00:00+00:00",
    )

    assert document["source_url"] == "https://archive.example.test/invoice.pdf"
    assert document["source_entity_id"] == "urn:openrag:attachment:invoice-1"
    assert document["source_provenance"]["relations"][0]["role"] == "attachment_of"
    assert document["source_relative_path"] == "administration/invoices/invoice.pdf"
    assert document["source_path_ancestors"] == [
        "administration",
        "administration/invoices",
    ]


def test_langflow_signed_context_round_trips_provenance():
    provenance = SourceProvenance.model_validate(_provenance_payload())
    service = LangflowIngestTokenService(secret="test-secret" * 4, ttl_seconds=60)
    context = DocumentIndexContext(
        document_id="doc-1",
        filename="invoice.pdf",
        mimetype="application/pdf",
        embedding_model="text-embedding-3-large",
        source_provenance=provenance,
    )

    decoded, _ = service.validate_token(service.create_token(context))

    assert decoded.source_provenance == provenance


def test_chat_sources_add_provenance_only_when_present():
    payload = _provenance_payload()
    source = _extract_sources(
        {
            "results": [
                {
                    "filename": "invoice.pdf",
                    "text": "Total: 42 EUR",
                    "source_provenance": payload,
                    "source_entity_id": payload["entity"]["id"],
                    "source_relation_roles": ["attachment_of", "member_of"],
                }
            ]
        }
    )[0]

    assert source["source_entity_id"] == "urn:openrag:attachment:invoice-1"
    assert "source_provenance" not in source
    assert "source_relation_roles" not in source
    assert "urn:openrag:email:message-1" not in repr(source)


@pytest.mark.asyncio
async def test_existing_index_receives_only_missing_provenance_mappings():
    client = MagicMock()
    client.indices.get_mapping = AsyncMock(
        return_value={"documents": {"mappings": {"properties": {"filename": {"type": "text"}}}}}
    )
    client.indices.put_mapping = AsyncMock()

    await DocumentIndexWriter._ensure_source_provenance_mapping(client, "documents")

    body = client.indices.put_mapping.await_args.kwargs["body"]
    assert body["properties"]["source_provenance"]["properties"]["relations"]["type"] == ("nested")
    assert body["properties"]["source_entity_id"] == {"type": "keyword"}
    assert body["properties"]["source_relative_path"]["type"] == "keyword"


@pytest.mark.asyncio
async def test_incompatible_existing_provenance_mapping_fails_closed():
    client = MagicMock()
    client.indices.get_mapping = AsyncMock(
        return_value={
            "documents": {
                "mappings": {
                    "properties": {
                        "source_provenance": {"properties": {"relations": {"type": "object"}}}
                    }
                }
            }
        }
    )

    with pytest.raises(RuntimeError, match="must be nested"):
        await DocumentIndexWriter._ensure_source_provenance_mapping(client, "documents")
