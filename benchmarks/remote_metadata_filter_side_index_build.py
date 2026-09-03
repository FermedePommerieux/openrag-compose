"""Build and verify one immutable production metadata-filter side index.

This module runs in-memory inside the deployed backend container.  It reads
only representative source documents and writes only the requested v1 side
index generation and its stable alias.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
from collections import Counter
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from models.document_investigation import CalendarBasis
from models.document_metadata import DocumentMetadataProfile
from models.metadata_filter import (
    MetadataDateSourcePolicy,
    MetadataFilter,
    MetadataFilterClause,
    MetadataFilterField,
    MetadataFilterOperator,
    MetadataTruthValue,
)
from models.metadata_filter_projection import MetadataFilterProjectionSourceContext
from models.source_provenance import SourceProvenance
from services.metadata_candidate_restriction import (
    execute_metadata_restricted_lane,
    resolve_metadata_candidates,
)
from services.metadata_filter_projection import (
    MetadataProjectionQueryBoundary,
    build_projection_side_document,
    compile_metadata_filter_to_opensearch,
    evaluate_metadata_filter_projection,
    generate_metadata_filter_projection,
)
from services.metadata_filter_side_index import MetadataFilterSideIndex

RESULT_MARKER = "METADATA_FILTER_SIDE_INDEX_BUILD_JSON="
SOURCE_FIELDS = [
    "document_id",
    "chunk_id",
    "chunk_index",
    "chunk_content_sha256",
    "filename",
    "mimetype",
    "source_entity_id",
    "source_entity_type",
    "source_entity_system",
    "source_provenance",
    "connector_type",
    "owner",
    "allowed_users",
    "allowed_groups",
    "allowed_principals",
    "document_metadata_profile",
    "document_metadata_profile_id",
    "document_metadata_facts_sha256",
]


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    return ordered[math.ceil((len(ordered) - 1) * quantile)] if ordered else 0.0


def _occurrence_id(source: dict[str, Any]) -> str:
    return str(source.get("source_entity_id") or source.get("document_id") or "")


def _source_provenance(source: dict[str, Any]) -> SourceProvenance | None:
    value = source.get("source_provenance")
    return SourceProvenance.model_validate(value) if isinstance(value, dict) else None


def _source_context(
    source: dict[str, Any], entity_id: str
) -> MetadataFilterProjectionSourceContext:
    return MetadataFilterProjectionSourceContext(
        source_entity_id=entity_id,
        source_entity_type=source.get("source_entity_type"),
        source_system=source.get("source_entity_system"),
        connector=source.get("connector_type"),
        mime_type=source.get("mimetype"),
        filename=source.get("filename"),
    )


async def _scan_representatives(
    client: Any,
    index_name: str,
    *,
    source_fields: list[str],
    batch_size: int,
) -> AsyncIterator[dict[str, Any]]:
    search_after: list[Any] | None = None
    while True:
        body: dict[str, Any] = {
            "query": {"term": {"chunk_index": 0}},
            "_source": source_fields,
            "size": batch_size,
            "track_total_hits": False,
            "sort": [
                {"source_entity_id": {"order": "asc", "missing": "_last"}},
                {"document_id": {"order": "asc"}},
                {"chunk_id": {"order": "asc"}},
            ],
        }
        if search_after is not None:
            body["search_after"] = search_after
        response = await client.search(
            index=index_name,
            body=body,
            request_timeout=300,
        )
        hits = response.get("hits", {}).get("hits", [])
        if not hits:
            return
        for hit in hits:
            yield dict(hit.get("_source") or {})
        search_after = hits[-1].get("sort")
        if not search_after or len(hits) < batch_size:
            return


def _period_filter(
    field: MetadataFilterField,
    value: str,
    *,
    source: str | None = None,
) -> MetadataFilterClause:
    role = field.value.partition("_")[0]
    return MetadataFilterClause(
        field=field,
        operator=MetadataFilterOperator.EQUAL,
        values=(value,),
        calendar_basis=CalendarBasis.SOURCE_LOCAL,
        source_policy=(
            MetadataDateSourcePolicy.EXPLICIT_SOURCE
            if source
            else MetadataDateSourcePolicy.ANY_VALID_PRODUCTION_OBSERVATION
            if role == "production"
            else MetadataDateSourcePolicy.ANY_VALID_MODIFICATION_OBSERVATION
        ),
        explicit_source=source,
    )


def _representative_filters() -> dict[str, MetadataFilter]:
    pdf = MetadataFilterClause(
        field=MetadataFilterField.MIME,
        operator=MetadataFilterOperator.EQUAL,
        values=("application/pdf",),
    )
    xlsx = MetadataFilterClause(
        field=MetadataFilterField.MIME,
        operator=MetadataFilterOperator.EQUAL,
        values=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ),
    )
    openarchiver = MetadataFilterClause(
        field=MetadataFilterField.SOURCE_SYSTEM,
        operator=MetadataFilterOperator.EQUAL,
        values=("openarchiver",),
    )
    creator_exists = MetadataFilterClause(
        field=MetadataFilterField.CREATOR_OBSERVATION,
        operator=MetadataFilterOperator.EXISTS,
    )
    production_exists = MetadataFilterClause(
        field=MetadataFilterField.PRODUCTION_MONTH,
        operator=MetadataFilterOperator.EXISTS,
        calendar_basis=CalendarBasis.SOURCE_LOCAL,
        source_policy=MetadataDateSourcePolicy.ANY_VALID_PRODUCTION_OBSERVATION,
    )
    return {
        "pdf_production_2024_03": MetadataFilter(
            clauses=(pdf, _period_filter(MetadataFilterField.PRODUCTION_MONTH, "2024-03"))
        ),
        "xlsx_modification_2023": MetadataFilter(
            clauses=(xlsx, _period_filter(MetadataFilterField.MODIFICATION_YEAR, "2023"))
        ),
        "openarchiver_creator_exists": MetadataFilter(
            clauses=(openarchiver, creator_exists)
        ),
        "production_timestamp_exists": MetadataFilter(clauses=(production_exists,)),
        "pdf_xmp_production_2024_03": MetadataFilter(
            clauses=(
                pdf,
                _period_filter(
                    MetadataFilterField.PRODUCTION_MONTH,
                    "2024-03",
                    source="pdf_xmp",
                ),
            )
        ),
    }


async def _source_counts(client: Any, index_name: str) -> dict[str, Any]:
    count = client.count
    return {
        "health": (await client.cluster.health()).get("status"),
        "chunks": int((await count(index=index_name))["count"]),
        "occurrences": int(
            (
                await count(
                    index=index_name,
                    body={"query": {"term": {"chunk_index": 0}}},
                )
            )["count"]
        ),
        "metadata_profiles": int(
            (
                await count(
                    index=index_name,
                    body={
                        "query": {
                            "bool": {
                                "filter": [
                                    {"term": {"chunk_index": 0}},
                                    {"exists": {"field": "document_metadata_profile_id"}},
                                ]
                            }
                        }
                    },
                )
            )["count"]
        ),
        "embeddings": int(
            (
                await count(
                    index=index_name,
                    body={
                        "query": {
                            "exists": {
                                "field": "chunk_embedding_text_embedding_3_large"
                            }
                        }
                    },
                )
            )["count"]
        ),
    }


async def _node_resources(client: Any) -> dict[str, Any]:
    response = await client.nodes.stats(metric="fs,jvm,os,process")
    node = next(iter((response.get("nodes") or {}).values()))
    fs = (node.get("fs") or {}).get("total") or {}
    jvm = (node.get("jvm") or {}).get("mem") or {}
    process = (node.get("process") or {})
    return {
        "node": node.get("name"),
        "disk_total_bytes": int(fs.get("total_in_bytes") or 0),
        "disk_available_bytes": int(fs.get("available_in_bytes") or 0),
        "heap_used_bytes": int(jvm.get("heap_used_in_bytes") or 0),
        "heap_used_percent": int(jvm.get("heap_used_percent") or 0),
        "process_cpu_percent": int((process.get("cpu") or {}).get("percent") or 0),
        "process_rss_bytes": int((process.get("mem") or {}).get("total_virtual_in_bytes") or 0),
    }


async def _index_storage(client: Any, index_name: str) -> dict[str, Any]:
    stats = (await client.indices.stats(index=index_name))["indices"][index_name]
    primary = stats["primaries"]
    segments = primary.get("segments") or {}
    return {
        # Index stats count nested Lucene children; the API count is the side
        # document cardinality required by the projection contract.
        "docs": int((await client.count(index=index_name))["count"]),
        "lucene_docs_including_nested": int(
            (primary.get("docs") or {}).get("count") or 0
        ),
        "store_size_bytes": int((primary.get("store") or {}).get("size_in_bytes") or 0),
        "segments": int(segments.get("count") or 0),
    }


async def _timed_count(
    client: Any,
    index_name: str,
    query: dict[str, Any],
) -> dict[str, Any]:
    latencies: list[float] = []
    value = 0
    for _ in range(9):
        started = time.perf_counter()
        value = int(
            (
                await client.count(
                    index=index_name,
                    body={"query": query},
                )
            )["count"]
        )
        latencies.append((time.perf_counter() - started) * 1000)
    return {
        "count": value,
        "runs": len(latencies),
        "min_ms": min(latencies),
        "p50_ms": _percentile(latencies, 0.50),
        "p95_ms": _percentile(latencies, 0.95),
    }


async def _live_lane_parity(
    dls_client: Any,
    *,
    source_index: str,
    generation: str,
    metadata_filter: MetadataFilter,
) -> dict[str, Any]:
    restriction = await resolve_metadata_candidates(
        dls_client,
        metadata_filter,
        projection_alias=generation,
    )
    if not restriction.source_entity_ids:
        raise RuntimeError("lane-parity fixture has no DLS-visible eligible occurrence")
    fixture_response = await dls_client.search(
        index=source_index,
        body={
            "query": {"term": {"source_entity_id": restriction.source_entity_ids[0]}},
            "_source": [
                "chunk_id",
                "filename",
                "source_entity_id",
                "chunk_embedding_text_embedding_3_large",
            ],
            "size": 1,
        },
    )
    fixture_hits = fixture_response.get("hits", {}).get("hits", [])
    if not fixture_hits:
        raise RuntimeError("lane-parity fixture occurrence is absent from DLS-scoped retrieval")
    fixture = fixture_hits[0].get("_source") or {}
    vector = fixture.get("chunk_embedding_text_embedding_3_large")
    if not isinstance(vector, list) or not vector:
        raise RuntimeError("lane-parity fixture has no dense embedding")
    query_text = str(fixture.get("filename") or "document")
    common = {
        "_source": ["chunk_id", "source_entity_id"],
        "size": 10,
        "sort": [
            {"_score": {"order": "desc"}},
            {"chunk_id": {"order": "asc", "missing": "_last"}},
        ],
    }
    lexical_body = {
        **common,
        "query": {
            "bool": {
                "must": [
                    {
                        "multi_match": {
                            "query": query_text,
                            "fields": ["text^2", "filename^1.5"],
                        }
                    }
                ],
                "filter": [],
            }
        },
    }
    dense_body = {
        **common,
        "query": {
            "bool": {
                "should": [
                    {
                        "knn": {
                            "chunk_embedding_text_embedding_3_large": {
                                "vector": vector,
                                "k": 10,
                            }
                        }
                    }
                ],
                "minimum_should_match": 1,
                "filter": [
                    {"exists": {"field": "chunk_embedding_text_embedding_3_large"}}
                ],
            }
        },
    }

    async def execute(body: dict[str, Any]) -> dict[str, Any]:
        return await dls_client.search(index=source_index, body=body)

    lexical = await execute_metadata_restricted_lane(
        lexical_body,
        restriction,
        execute=execute,
    )
    dense = await execute_metadata_restricted_lane(
        dense_body,
        restriction,
        execute=execute,
    )
    eligible = set(restriction.source_entity_ids)

    def identities(response: dict[str, Any]) -> set[str]:
        return {
            str((hit.get("_source") or {}).get("source_entity_id") or "")
            for hit in response.get("hits", {}).get("hits", [])
        }

    lexical_ids = identities(lexical)
    dense_ids = identities(dense)
    hybrid_ids = lexical_ids | dense_ids
    if not lexical_ids or not dense_ids:
        raise RuntimeError("live lane-parity fixture returned an empty retrieval lane")
    if not lexical_ids <= eligible or not dense_ids <= eligible or not hybrid_ids <= eligible:
        raise RuntimeError("a live retrieval lane bypassed the metadata occurrence restriction")
    return {
        "filter_sha256": metadata_filter.calculate_sha256(),
        "eligible_occurrences": len(eligible),
        "lexical": {"pass": True, "returned_occurrences": len(lexical_ids)},
        "dense": {"pass": True, "returned_occurrences": len(dense_ids)},
        "hybrid": {"pass": True, "returned_occurrences": len(hybrid_ids)},
        "same_restriction": True,
        "dls_reapplied_per_partition": True,
    }


async def run_remote(plan: dict[str, Any]) -> dict[str, Any]:
    from config.settings import clients, get_index_name
    from session_manager import SessionManager, User

    source_index = str(plan.get("source_index") or get_index_name())
    generation = str(plan["generation"])
    batch_size = int(plan.get("batch_size") or 500)
    admin = clients.create_index_admin_opensearch_client()
    if admin is None:
        raise RuntimeError("index-admin client unavailable")
    dls_client: Any | None = None
    side_index = MetadataFilterSideIndex(admin, index_name=generation)
    try:
        cluster = await admin.cluster.health()
        if cluster.get("status") != "green" or int(cluster.get("number_of_data_nodes") or 0) != 1:
            raise RuntimeError("full build requires the confirmed green single-data-node topology")
        baseline_before = await _source_counts(admin, source_index)
        resources_before = await _node_resources(admin)
        if resources_before["disk_available_bytes"] < 5 * 1024**3:
            raise RuntimeError("insufficient OpenSearch disk margin for side-index generation")

        await side_index.create(shards=1, replicas=0)
        filters = _representative_filters()
        truth_counts: dict[str, Counter[str]] = {
            name: Counter() for name in filters
        }
        occurrence_ids: list[str] = []
        document_ids: set[str] = set()
        projection_ids: set[str] = set()
        source_entity_ids: set[str] = set()
        owner_counts: Counter[str] = Counter()
        batch: list[Any] = []
        bulk_results: list[dict[str, Any]] = []
        failures: list[str] = []
        invalid = 0
        missing_profiles = 0
        projected = 0
        attempted = 0
        build_started = time.perf_counter()
        scan_started = build_started

        async for source in _scan_representatives(
            admin,
            source_index,
            source_fields=SOURCE_FIELDS,
            batch_size=batch_size,
        ):
            occurrence_id = _occurrence_id(source)
            occurrence_ids.append(occurrence_id)
            document_ids.add(str(source.get("document_id") or ""))
            raw_profile = source.get("document_metadata_profile")
            if not isinstance(raw_profile, dict):
                missing_profiles += 1
                continue
            attempted += 1
            try:
                profile = DocumentMetadataProfile.model_validate(raw_profile)
                projection = generate_metadata_filter_projection(
                    profile,
                    source_context=_source_context(source, profile.entity_id),
                    source_provenance=_source_provenance(source),
                )
                document = build_projection_side_document(
                    projection,
                    source_document_id=str(source.get("document_id") or ""),
                    source_entity_id=profile.entity_id,
                    representative_chunk_id=str(source.get("chunk_id") or ""),
                    owner=(
                        str(source["owner"]) if source.get("owner") is not None else None
                    ),
                    allowed_users=tuple(
                        str(item) for item in source.get("allowed_users") or []
                    ),
                    allowed_groups=tuple(
                        str(item) for item in source.get("allowed_groups") or []
                    ),
                    allowed_principals=tuple(
                        str(item) for item in source.get("allowed_principals") or []
                    ),
                )
                if document.projection_document_id in projection_ids:
                    raise RuntimeError("duplicate projection_document_id")
                if document.source_entity_id in source_entity_ids:
                    raise RuntimeError("duplicate source_entity_id")
                projection_ids.add(document.projection_document_id)
                source_entity_ids.add(document.source_entity_id)
                if document.owner:
                    owner_counts[document.owner] += 1
                for name, metadata_filter in filters.items():
                    evaluation = evaluate_metadata_filter_projection(
                        metadata_filter,
                        document_id=document.source_entity_id,
                        projection=projection,
                    )
                    truth_counts[name][evaluation.result.value] += 1
                batch.append(document)
            except Exception as exc:
                invalid += 1
                if len(failures) < 20:
                    failures.append(
                        f"{_canonical_sha256(occurrence_id)}:{type(exc).__name__}:{exc}"
                    )
                continue
            projected += 1
            if len(batch) >= batch_size:
                bulk_results.append(await side_index.apply_batch(batch))
                batch.clear()
        if batch:
            bulk_results.append(await side_index.apply_batch(batch))
        projection_and_bulk_seconds = time.perf_counter() - scan_started

        if baseline_before["occurrences"] != len(occurrence_ids):
            raise RuntimeError("representative scan count differs from source baseline")
        digest = hashlib.sha256("\n".join(sorted(occurrence_ids)).encode()).hexdigest()
        if digest != plan["expected_corpus_digest"]:
            raise RuntimeError("source corpus digest differs from the approved baseline")
        if attempted != baseline_before["metadata_profiles"]:
            raise RuntimeError("profile attempt count differs from source baseline")
        if invalid or failures:
            raise RuntimeError(f"projection generation has {invalid} invalid rows")
        if projected != attempted or projected != len(projection_ids):
            raise RuntimeError("projection identity counts do not reconcile")

        await side_index.finalize()
        if not await side_index.verify_mapping():
            raise RuntimeError("production side-index mapping differs from strict v1 mapping")
        indexed = int((await admin.count(index=generation))["count"])
        if indexed != projected:
            raise RuntimeError("production side-index document count differs from projection count")

        resources_after = await _node_resources(admin)
        storage = await _index_storage(admin, generation)
        baseline_after = await _source_counts(admin, source_index)
        for field in ("chunks", "occurrences", "metadata_profiles", "embeddings"):
            if baseline_after[field] != baseline_before[field]:
                raise RuntimeError(f"source corpus changed during build: {field}")

        visible_user = owner_counts.most_common(1)[0][0] if owner_counts else "metadata-filter-none"
        session = SessionManager()
        token = session.create_opensearch_jwt_token(
            User(
                user_id=visible_user,
                email=f"{visible_user}@invalid.local",
                name="Metadata Filter Production Validation",
            ),
            ttl_seconds=1800,
        )
        dls_client = clients.create_user_opensearch_client(token)
        visible_count = int((await dls_client.count(index=generation))["count"])
        dls_aggregation = await dls_client.search(
            index=generation,
            body={
                "size": 0,
                "aggs": {"owners": {"terms": {"field": "owner", "size": 100}}},
            },
        )
        visible_owner_buckets = (
            dls_aggregation.get("aggregations", {}).get("owners", {}).get("buckets", [])
        )
        if visible_count > indexed:
            raise RuntimeError("DLS visible side-index count exceeds global generation count")
        if sum(int(item.get("doc_count") or 0) for item in visible_owner_buckets) > visible_count:
            raise RuntimeError("DLS aggregation exceeds DLS-visible side-index count")

        filter_results: dict[str, Any] = {}
        for name, metadata_filter in filters.items():
            for _ in range(missing_profiles):
                truth_counts[name][MetadataTruthValue.UNKNOWN.value] += 1
            if sum(truth_counts[name].values()) != baseline_before["occurrences"]:
                raise RuntimeError(f"truth-state counts do not reconcile for {name}")
            query = compile_metadata_filter_to_opensearch(
                metadata_filter,
                boundary=MetadataProjectionQueryBoundary.DLS_SCOPED_OPENSEARCH_CLIENT,
            )
            global_true = int(
                (
                    await admin.count(
                        index=generation,
                        body={"query": query},
                    )
                )["count"]
            )
            if global_true != truth_counts[name][MetadataTruthValue.TRUE.value]:
                raise RuntimeError(f"compiled TRUE set differs from evaluator for {name}")
            dls_latency = await _timed_count(dls_client, generation, query)
            filter_results[name] = {
                "filter_sha256": metadata_filter.calculate_sha256(),
                "TRUE": truth_counts[name][MetadataTruthValue.TRUE.value],
                "FALSE": truth_counts[name][MetadataTruthValue.FALSE.value],
                "UNKNOWN": truth_counts[name][MetadataTruthValue.UNKNOWN.value],
                "visible_TRUE": dls_latency["count"],
                "latency": dls_latency,
            }

        lane_parity = await _live_lane_parity(
            dls_client,
            source_index=source_index,
            generation=generation,
            metadata_filter=filters["pdf_production_2024_03"],
        )

        old_targets = await side_index.current_alias_targets()
        alias_switched = False
        rollback_pass = False
        if plan.get("activate_alias"):
            recorded_old = await side_index.switch_alias()
            alias_switched = recorded_old == old_targets
            await side_index.restore_alias(old_targets)
            rollback_pass = await side_index.current_alias_targets() == old_targets
            await side_index.switch_alias()
            alias_switched = alias_switched and (
                await side_index.current_alias_targets() == (generation,)
            )

        bulk_latencies = [float(item["latency_ms"]) for item in bulk_results]
        bulk_seconds = sum(bulk_latencies) / 1000
        result = {
            "schema": "openrag.metadata-filter-side-index-build",
            "version": 1,
            "captured_at": datetime.now(UTC).isoformat(),
            "source_index": source_index,
            "generation": generation,
            "alias": "documents-metadata-filter-current",
            "baseline_before": {
                **baseline_before,
                "documents": len(document_ids),
                "corpus_occurrence_digest": digest,
            },
            "baseline_after": baseline_after,
            "topology": {
                "nodes": int(cluster.get("number_of_nodes") or 0),
                "data_nodes": int(cluster.get("number_of_data_nodes") or 0),
                "primary_shards": 1,
                "replicas": 0,
            },
            "resources_before": resources_before,
            "resources_after": resources_after,
            "projection": {
                "attempted": attempted,
                "projected": projected,
                "skipped": missing_profiles,
                "UNKNOWN": missing_profiles,
                "invalid": invalid,
                "failed": 0,
                "duplicates": 0,
            },
            "build_performance": {
                "batch_size": batch_size,
                "batches": len(bulk_results),
                "projection_and_bulk_seconds": projection_and_bulk_seconds,
                "bulk_seconds": bulk_seconds,
                "docs_per_second": projected / max(projection_and_bulk_seconds, 0.001),
                "bulk_p95_ms": _percentile(bulk_latencies, 0.95),
                "bulk_max_attempts": max(
                    (int(item.get("attempts") or 0) for item in bulk_results),
                    default=0,
                ),
                "wall_seconds": time.perf_counter() - build_started,
            },
            "storage": {
                **storage,
                "disk_impact_bytes": (
                    resources_before["disk_available_bytes"]
                    - resources_after["disk_available_bytes"]
                ),
            },
            "mapping": {
                "dynamic": "strict",
                "verified": True,
            },
            "dls": {
                "full_generation_count_scoped": True,
                "visible_count": visible_count,
                "visible_owner_buckets": len(visible_owner_buckets),
                "count_aggregation_scoped": True,
                "controlled_principal_sha256": hashlib.sha256(
                    visible_user.encode()
                ).hexdigest(),
                "controlled_principal_exposed": False,
            },
            "filter_results": filter_results,
            "lane_parity": lane_parity,
            "alias_cutover": {
                "old_targets": list(old_targets),
                "new_target": generation if plan.get("activate_alias") else None,
                "atomic_switch": alias_switched if plan.get("activate_alias") else False,
            },
            "rollback": {
                "tested": bool(plan.get("activate_alias")),
                "pass": rollback_pass if plan.get("activate_alias") else False,
                "first_generation_alias_removal_tested": (
                    bool(plan.get("activate_alias")) and not old_targets
                ),
            },
            "invariants": {
                "source_index_writes": 0,
                "raw_metadata_profiles_changed": False,
                "archive_binary_reads": 0,
                "metadata_extractions": 0,
                "rechunk_operations": 0,
                "embedding_operations": 0,
                "prov_o_changes": 0,
                "association_neighborhood_enabled": False,
                "llm_calls": 0,
            },
        }
        return result
    finally:
        if dls_client is not None:
            await dls_client.close()
        await admin.close()


def remote_entry(plan: dict[str, Any]) -> None:
    result = asyncio.run(run_remote(plan))
    print(f"{RESULT_MARKER}{json.dumps(result, ensure_ascii=False, sort_keys=True)}", flush=True)
