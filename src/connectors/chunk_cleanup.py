"""Single deletion path for a connector file's indexed chunks."""

from collections.abc import Iterable
from typing import Any


def build_connector_file_chunks_query(
    file_ids: Iterable[str],
    *,
    connector_type: str | None = None,
    owner_user_id: str | None = None,
    shared: bool = False,
    keep_filenames: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Build an owner- and connector-scoped dual-id chunk query."""
    ids = [file_id for file_id in file_ids if file_id]
    filters: list[dict[str, Any]] = [
        {
            "bool": {
                "should": [
                    {"terms": {"document_id": ids}},
                    {"terms": {"connector_file_id": ids}},
                    {"terms": {"connector_file_id.keyword": ids}},
                ],
                "minimum_should_match": 1,
            }
        }
    ]
    if connector_type:
        filters.append({"term": {"connector_type": connector_type}})
    if shared:
        filters.append({"bool": {"must_not": {"exists": {"field": "owner"}}}})
    elif owner_user_id:
        filters.append({"term": {"owner": owner_user_id}})

    query: dict[str, Any] = {"bool": {"filter": filters}}
    keep = [name for name in (keep_filenames or []) if name]
    if keep:
        query["bool"]["must_not"] = [{"terms": {"filename": keep}}]
    return query


async def delete_connector_file_chunks(
    file_ids: Iterable[str],
    opensearch_client,
    *,
    connector_type: str | None = None,
    owner_user_id: str | None = None,
    shared: bool = False,
    keep_filenames: Iterable[str] | None = None,
    refresh: bool = False,
) -> int:
    """Delete visible concrete chunk ids through the trusted write client."""
    from config.settings import clients, get_index_name
    from utils.opensearch_delete import collect_visible_document_ids, delete_document_ids

    ids = [file_id for file_id in file_ids if file_id]
    if not ids:
        return 0

    write_client = clients.opensearch
    if write_client is None:
        raise RuntimeError("Backend OpenSearch write client is unavailable")

    chunk_ids = await collect_visible_document_ids(
        opensearch_client,
        index=get_index_name(),
        query=build_connector_file_chunks_query(
            ids,
            connector_type=connector_type,
            owner_user_id=owner_user_id,
            shared=shared,
            keep_filenames=keep_filenames,
        ),
    )
    return await delete_document_ids(
        write_client,
        index=get_index_name(),
        document_ids=chunk_ids,
        refresh=refresh,
    )
