"""Contract tests for OpenRAG's bounded W3C PROV-O JSON profile."""

import base64
import hashlib
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from api.v1.chat import _extract_sources
from models.source_provenance import (
    OPENARCHIVER_ATTACHMENT_CONTRACT,
    PROV_NAMESPACE,
    SourceProvenance,
    source_attachment_mapping,
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


def _attachment_payload(data: bytes = b"invoice") -> dict:
    sha256 = hashlib.sha256(data).hexdigest()
    document_id = base64.urlsafe_b64encode(bytes.fromhex(sha256)).rstrip(b"=").decode()[:24]
    return {
        "schema_version": "1.0",
        "entity": {
            "id": "urn:openrag:openarchiver:attachment:attachment-1",
            "type": "email_attachment",
            "source_system": "openarchiver",
            "label": "invoice.pdf",
        },
        "relations": [
            {
                "role": "attachment_of",
                "target": {
                    "id": "urn:openrag:openarchiver:email:source-1:email-1",
                    "type": "email_message",
                    "source_system": "openarchiver",
                },
            }
        ],
        "attachment_contract": {
            "contract": OPENARCHIVER_ATTACHMENT_CONTRACT,
            "version": 1,
            "source_kind": "openarchiver_attachment",
            "source_entity_id": "urn:openrag:openarchiver:attachment:attachment-1",
            "parent_source_entity_id": "urn:openrag:openarchiver:email:source-1:email-1",
            "attachment_id": "attachment-1",
            "parent_email_id": "email-1",
            "parent_archive_source_id": "source-1",
            "filename_original": "invoice.pdf",
            "mime_type_declared": "application/pdf",
            "mime_type_detected": "application/pdf",
            "size_bytes": len(data),
            "sha256": sha256,
            "document_id": document_id,
            "archive_locator": "openarchiver:attachment:attachment-1",
            "connector_version": "f" * 40,
        },
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


def test_valid_openarchiver_attachment_contract_is_internal_and_complete():
    provenance = SourceProvenance.model_validate(_attachment_payload())
    fields = provenance.index_fields(indexed_at="2026-09-02T00:00:00+00:00")

    assert "attachment_contract" not in fields["source_provenance"]
    assert fields["source_attachment"]["source_kind"] == "openarchiver_attachment"
    assert fields["source_attachment"]["ingested_at"] == "2026-09-02T00:00:00+00:00"
    assert fields["source_attachment"]["filename_original"] == "invoice.pdf"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("attachment_id", ""),
        ("parent_email_id", ""),
        ("sha256", "0" * 63),
        ("mime_type_declared", "malformed mime"),
    ],
)
def test_attachment_contract_rejects_missing_or_malformed_identity(field, value):
    payload = _attachment_payload()
    payload["attachment_contract"][field] = value

    with pytest.raises(ValidationError):
        SourceProvenance.model_validate(payload)


def test_unknown_but_valid_mime_type_is_preserved():
    payload = _attachment_payload()
    payload["attachment_contract"]["mime_type_declared"] = "application/x-future-format"

    provenance = SourceProvenance.model_validate(payload)

    assert provenance.attachment_contract.mime_type_declared == "application/x-future-format"


@pytest.mark.parametrize("mutation", ["filename_only", "parent_url_only", "mail_and_name"])
def test_heuristic_attachment_identity_is_never_a_contract(mutation):
    payload = _attachment_payload()
    contract = payload["attachment_contract"]
    if mutation == "filename_only":
        contract["attachment_id"] = ""
    elif mutation == "parent_url_only":
        contract["source_entity_id"] = "https://archive.example.test/email-1"
    else:
        contract["source_entity_id"] = "email-1/invoice.pdf"

    with pytest.raises(ValidationError):
        SourceProvenance.model_validate(payload)


def test_attachment_contract_rejects_hash_size_and_parent_mismatch():
    provenance = SourceProvenance.model_validate(_attachment_payload())

    with pytest.raises(ValueError, match="hash"):
        provenance.validate_attachment_binary(document_id="other", size_bytes=7)
    with pytest.raises(ValueError, match="size"):
        provenance.validate_attachment_binary(
            document_id=provenance.attachment_contract.document_id,
            size_bytes=8,
        )

    payload = _attachment_payload()
    payload["relations"][0]["target"]["id"] = "urn:openrag:openarchiver:email:other"
    with pytest.raises(ValidationError, match="not asserted"):
        SourceProvenance.model_validate(payload)


@pytest.mark.asyncio
async def test_same_attachment_id_and_hash_is_idempotent_but_other_hash_conflicts():
    provenance = SourceProvenance.model_validate(_attachment_payload())
    context = DocumentIndexContext(
        document_id=provenance.attachment_contract.document_id,
        mimetype="application/pdf",
        embedding_model="test-model",
        owner="owner-1",
        source_provenance=provenance,
    )
    client = MagicMock()
    client.search = AsyncMock(
        return_value={
            "hits": {
                "hits": [
                    {
                        "_source": {
                            "document_id": provenance.attachment_contract.document_id,
                            "source_attachment": {"sha256": provenance.attachment_contract.sha256},
                        }
                    }
                ]
            }
        }
    )
    writer = DocumentIndexWriter()

    await writer._validate_attachment_identity(client, index_name="documents", context=context)

    client.search.return_value["hits"]["hits"][0]["_source"]["document_id"] = "other"
    with pytest.raises(RuntimeError, match="identity conflict"):
        await writer._validate_attachment_identity(client, index_name="documents", context=context)


def test_attachment_contract_preserves_dls_owner_and_never_indexes_locator_publicly():
    provenance = SourceProvenance.model_validate(_attachment_payload())
    context = DocumentIndexContext(
        document_id=provenance.attachment_contract.document_id,
        filename="safe--attachment-1.pdf",
        mimetype="application/pdf",
        embedding_model="test-model",
        owner="owner-1",
        source_url="https://archive.example.test/email-1",
        source_provenance=provenance,
    )
    document = DocumentIndexWriter()._build_chunk_document(
        context=context,
        chunk=DocumentIndexChunk(chunk_id="chunk-0", text="invoice", vector=[0.1]),
        embedding_field="vector",
        indexed_time="2026-09-02T00:00:00+00:00",
    )

    assert document["owner"] == "owner-1"
    assert document["source_url"] == "https://archive.example.test/email-1"
    assert "archive_locator" not in repr(document["source_provenance"])
    assert source_attachment_mapping()["properties"]["archive_locator"]["type"] == "keyword"
