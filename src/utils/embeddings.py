from utils.embedding_fields import build_knn_vector_field, get_embedding_field_name
from utils.logging_config import get_logger

logger = get_logger(__name__)


async def create_index_body(
    embedding_model: str | None = None,
    embedding_dimensions: int | None = None,
) -> dict:
    """Create a static index body configuration.

    Returns:
        OpenSearch index body configuration
    """
    from config.embedding_constants import OPENAI_DEFAULT_EMBEDDING_MODEL
    from config.settings import (
        ACL_PRINCIPAL_LABELS_MAPPING,
        OPENSEARCH_NUMBER_OF_REPLICAS,
        OPENSEARCH_NUMBER_OF_SHARDS,
        VECTOR_DIM,
        get_openrag_config,
    )

    resolved_embedding_model = (
        embedding_model
        or get_openrag_config().knowledge.embedding_model
        or OPENAI_DEFAULT_EMBEDDING_MODEL
    )

    from models.source_provenance import source_provenance_mapping

    properties = {
        # Sortable logical chunk identity for Retrieval v2. `_id` cannot be
        # sorted by OpenSearch and temporary generations must not change this.
        "chunk_id": {"type": "keyword"},
        "document_id": {"type": "keyword"},
        "filename": {"type": "keyword"},
        "mimetype": {"type": "keyword"},
        "page": {"type": "integer"},
        "chunk_index": {"type": "integer"},
        "chunking_strategy": {"type": "keyword"},
        "chunking_config_fingerprint": {"type": "keyword"},
        "text": {"type": "text"},
        # Legacy field - kept for backward compatibility and for clusters where
        # Langflow cannot perform mapping updates with a DLS-filtered JWT.
        "chunk_embedding": build_knn_vector_field(VECTOR_DIM),
        # Track which embedding model was used for this chunk
        "embedding_model": {"type": "keyword"},
        "embedding_dimensions": {"type": "integer"},
        "source_url": {"type": "keyword"},
        "connector_file_id": {"type": "keyword"},
        # W3C PROV-O source identity and relations. ``source_url`` remains a
        # mutable access locator and is intentionally not used as identity.
        "source_provenance": source_provenance_mapping(),
        "source_entity_id": {"type": "keyword"},
        "source_entity_type": {"type": "keyword"},
        "source_entity_system": {"type": "keyword"},
        "source_entity_alternate_ids": {"type": "keyword"},
        "source_relation_target_ids": {"type": "keyword"},
        "source_relation_roles": {"type": "keyword"},
        "source_relative_path": {"type": "keyword", "ignore_above": 4096},
        "source_path_ancestors": {"type": "keyword", "ignore_above": 4096},
        "connector_type": {"type": "keyword"},
        "parser": {"type": "keyword"},
        "chunk_size": {"type": "integer"},
        "chunk_overlap": {"type": "integer"},
        "ingest_run_id": {"type": "keyword"},
        "owner": {"type": "keyword"},
        "owner_email": {"type": "keyword"},
        "allowed_users": {"type": "keyword"},
        "allowed_groups": {"type": "keyword"},
        "allowed_principals": {"type": "keyword"},
        "allowed_principal_labels": ACL_PRINCIPAL_LABELS_MAPPING,
        "created_time": {"type": "date"},
        "modified_time": {"type": "date"},
        "indexed_time": {"type": "date"},
        "metadata": {"type": "object"},
    }

    if embedding_dimensions:
        properties[get_embedding_field_name(resolved_embedding_model)] = build_knn_vector_field(
            embedding_dimensions
        )

    return {
        "settings": {
            "index": {"knn": True},
            "number_of_shards": OPENSEARCH_NUMBER_OF_SHARDS,
            "number_of_replicas": OPENSEARCH_NUMBER_OF_REPLICAS,
        },
        "mappings": {"properties": properties},
    }
