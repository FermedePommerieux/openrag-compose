"""Live two-principal DLS audit over lexical, vector, graph and exact-read paths."""

import hashlib
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from opensearchpy import AsyncOpenSearch
from opensearchpy._async.http_aiohttp import AIOHttpConnection

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.openrag_skip_app_onboard,
]

CANARY_A = "AUDIT-CANARY-A-7B1683D7"
CANARY_B = "AUDIT-CANARY-B-9E42C1F5"


def _admin_client():
    from config.settings import (
        IBM_AUTH_ENABLED,
        OPENSEARCH_HOST,
        OPENSEARCH_PASSWORD,
        OPENSEARCH_PORT,
        OPENSEARCH_USERNAME,
    )

    if IBM_AUTH_ENABLED:
        pytest.skip("OSS JWT DLS is not used in IBM auth mode")
    if not OPENSEARCH_PASSWORD:
        pytest.skip("OPENSEARCH_PASSWORD is required for live DLS integration")
    return AsyncOpenSearch(
        hosts=[{"host": OPENSEARCH_HOST, "port": OPENSEARCH_PORT}],
        connection_class=AIOHttpConnection,
        scheme="https",
        use_ssl=True,
        verify_certs=False,
        ssl_assert_fingerprint=None,
        http_auth=(OPENSEARCH_USERNAME, OPENSEARCH_PASSWORD),
        http_compress=True,
    )


def _index_body() -> dict:
    from models.source_provenance import source_provenance_mapping

    keyword_fields = (
        "chunk_id",
        "document_id",
        "document_content_sha256",
        "chunk_content_sha256",
        "filename",
        "mimetype",
        "embedding_model",
        "owner",
        "allowed_users",
        "allowed_groups",
        "allowed_principals",
        "source_entity_id",
        "source_entity_type",
        "source_entity_system",
        "source_entity_alternate_ids",
        "source_relation_target_ids",
        "source_relation_roles",
        "ingest_run_id",
    )
    properties: dict[str, Any] = {
        field: {"type": "keyword"} for field in keyword_fields
    }
    properties.update(
        {
            "text": {"type": "text"},
            "chunk_index": {"type": "integer"},
            "page": {"type": "integer"},
            "document_profile_version": {"type": "integer"},
            "document_order_verified": {"type": "boolean"},
            "document_chunk_count": {"type": "integer"},
            "document_page_count": {"type": "integer"},
            "document_max_page": {"type": "integer"},
            "document_character_count": {"type": "integer"},
            "audit_vector": {
                "type": "knn_vector",
                "dimension": 2,
                "method": {
                    "name": "hnsw",
                    "space_type": "cosinesimil",
                    "engine": "lucene",
                },
            },
            "source_provenance": source_provenance_mapping(),
        }
    )
    return {
        "settings": {
            "index": {"knn": True},
            "number_of_shards": 1,
            "number_of_replicas": 0,
        },
        "mappings": {"properties": properties},
    }


def _document(side: str, user_id: str, relation_target: str) -> dict:
    canary = CANARY_A if side == "A" else CANARY_B
    vector = [1.0, 0.0] if side == "A" else [0.0, 1.0]
    document_id = f"audit-{side.lower()}-only"
    entity_id = f"urn:openrag:audit:{side.lower()}-only"
    text = f"Controlled cross-user DLS evidence {canary}"
    return {
        "chunk_id": f"{document_id}:0",
        "document_id": document_id,
        "document_content_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "chunk_content_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "document_profile_version": 1,
        "document_order_verified": True,
        "document_chunk_count": 1,
        "document_page_count": 1,
        "document_max_page": 1,
        "document_character_count": len(text),
        "filename": f"{side.lower()}-only-audit.txt",
        "mimetype": "text/plain",
        "text": text,
        "chunk_index": 0,
        "page": 1,
        "embedding_model": "audit-model",
        "audit_vector": vector,
        "owner": user_id,
        "allowed_users": [user_id],
        "allowed_groups": [],
        "allowed_principals": [],
        "source_entity_id": entity_id,
        "source_entity_type": "document",
        "source_entity_system": "openrag-audit",
        "source_entity_alternate_ids": [],
        "source_relation_target_ids": [relation_target],
        "source_relation_roles": ["reply_to"],
        "ingest_run_id": "audit-closeout",
        "source_provenance": {
            "schema_version": "1.0",
            "entity": {
                "id": entity_id,
                "type": "document",
                "source_system": "openrag-audit",
            },
            "relations": [
                {
                    "role": "reply_to",
                    "target": {
                        "id": relation_target,
                        "type": "document",
                        "source_system": "openrag-audit",
                    },
                    "prov_predicate": "http://www.w3.org/ns/prov#wasInfluencedBy",
                }
            ],
        },
    }


async def _ids(client, index_name: str, query: dict) -> set[str]:
    response = await client.search(
        index=index_name,
        body={"query": query, "_source": ["document_id"], "size": 10},
    )
    return {
        hit["_source"]["document_id"]
        for hit in response.get("hits", {}).get("hits", [])
    }


@pytest.mark.parametrize("direction", [("A", "B"), ("B", "A")])
async def test_live_cross_user_dls_all_retrieval_surfaces(direction):
    import services.search_service as search_service_module
    from config.settings import DLS_PRINCIPAL_INDEX_BODY, DLS_PRINCIPAL_INDEX_NAME, clients
    from services.retrieval_service import expand_provenance_graph, reciprocal_rank_fusion
    from services.search_service import SearchService
    from session_manager import SessionManager, User
    from utils.opensearch_utils import setup_opensearch_security

    admin = _admin_client()
    if not await admin.ping():
        await admin.close()
        pytest.skip("OpenSearch is not reachable")

    suffix = uuid4().hex
    index_name = f"documents_cross_user_dls_{suffix}"
    user_ids = {side: f"audit-dls-{side.lower()}-{suffix}" for side in ("A", "B")}
    entity_ids = {side: f"urn:openrag:audit:{side.lower()}-only" for side in ("A", "B")}
    documents = {
        side: _document(side, user_ids[side], entity_ids["B" if side == "A" else "A"])
        for side in ("A", "B")
    }
    user_clients = {}
    original_get_index_name = search_service_module.get_index_name

    try:
        await setup_opensearch_security(admin)
        await admin.indices.create(index=index_name, body=_index_body())
        if not await admin.indices.exists(index=DLS_PRINCIPAL_INDEX_NAME):
            await admin.indices.create(
                index=DLS_PRINCIPAL_INDEX_NAME,
                body=DLS_PRINCIPAL_INDEX_BODY,
            )
        for side in ("A", "B"):
            await admin.index(
                index=DLS_PRINCIPAL_INDEX_NAME,
                id=user_ids[side],
                body={
                    "user_name": user_ids[side],
                    "auth_user_id": user_ids[side],
                    "auth_email": f"{user_ids[side]}@example.invalid",
                    "provider": "audit",
                    "principals": [],
                    "updated_at": "2026-09-01T00:00:00+00:00",
                },
                refresh=True,
            )
        await admin.bulk(
            body=[
                item
                for side in ("A", "B")
                for item in (
                    {"index": {"_index": index_name, "_id": documents[side]["chunk_id"]}},
                    documents[side],
                )
            ],
            refresh=True,
        )

        token_manager = SessionManager("audit-cross-user-dls")
        for side in ("A", "B"):
            token = token_manager.create_opensearch_jwt_token(
                User(
                    user_id=user_ids[side],
                    email=f"{user_ids[side]}@example.invalid",
                    name=f"Audit user {side}",
                ),
                ttl_seconds=300,
            )
            user_clients[side] = clients.create_user_opensearch_client(token)

        visible_side, hidden_side = direction
        visible_client = user_clients[visible_side]
        visible_document = documents[visible_side]
        hidden_document = documents[hidden_side]
        hidden_canary = CANARY_A if hidden_side == "A" else CANARY_B

        lexical_ids = await _ids(
            visible_client,
            index_name,
            {"match_phrase": {"text": hidden_canary}},
        )
        vector_ids = await _ids(
            visible_client,
            index_name,
            {
                "knn": {
                    "audit_vector": {
                        "vector": hidden_document["audit_vector"],
                        "k": 10,
                    }
                }
            },
        )
        lexical_response = await visible_client.search(
            index=index_name,
            body={"query": {"match": {"text": hidden_canary}}, "size": 10},
        )
        vector_response = await visible_client.search(
            index=index_name,
            body={
                "query": {
                    "knn": {
                        "audit_vector": {
                            "vector": hidden_document["audit_vector"],
                            "k": 10,
                        }
                    }
                },
                "size": 10,
            },
        )
        hybrid_hits = reciprocal_rank_fusion(
            [
                lexical_response.get("hits", {}).get("hits", []),
                vector_response.get("hits", {}).get("hits", []),
            ],
            k=60,
        )

        assert hidden_document["document_id"] not in lexical_ids
        assert hidden_document["document_id"] not in vector_ids
        assert hidden_document["document_id"] not in {
            hit.get("_source", {}).get("document_id") for hit in hybrid_hits
        }

        graph = await expand_provenance_graph(
            visible_client,
            index_name=index_name,
            seed_entity_ids=[visible_document["source_entity_id"]],
            seed_documents=[visible_document],
        )
        assert {item["document_id"] for item in graph["documents"]} == {
            visible_document["document_id"]
        }
        assert hidden_document["source_entity_id"] not in graph["entities"]
        assert all(
            hidden_document["source_entity_id"]
            not in (edge["source_entity_id"], edge["target_entity_id"])
            for edge in graph["edges"]
        )
        assert graph["coverage"]["frontier_empty"] is True
        assert graph["coverage"]["limit_reached"] is False

        routing_manager = SimpleNamespace(
            get_user_opensearch_client=lambda user_id, jwt_token: visible_client
        )
        service = SearchService(session_manager=routing_manager)
        search_service_module.get_index_name = lambda: index_name
        hidden_read = await service.read_document_chunks(
            hidden_document["document_id"],
            user_id=user_ids[visible_side],
            jwt_token="redacted-test-token-placeholder",
        )
        missing_read = await service.read_document_chunks(
            "audit-does-not-exist",
            user_id=user_ids[visible_side],
            jwt_token="redacted-test-token-placeholder",
        )
        citations = await service.resolve_cited_chunks(
            [hidden_document["chunk_id"]],
            user_id=user_ids[visible_side],
            jwt_token="redacted-test-token-placeholder",
        )
        assert hidden_read["results"] == []
        assert hidden_read["coverage"]["complete"] is False
        assert hidden_read["error"] == missing_read["error"]
        assert citations == []
    finally:
        search_service_module.get_index_name = original_get_index_name
        for client in user_clients.values():
            await client.close()
        await admin.indices.delete(index=index_name, ignore_unavailable=True)
        for user_id in user_ids.values():
            await admin.delete(
                index=DLS_PRINCIPAL_INDEX_NAME,
                id=user_id,
                ignore=[404],
                refresh=True,
            )
        await admin.close()
