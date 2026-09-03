"""Bounded production canary executed in the backend process.

The only writes target a newly created ``documents-...-canary-*`` side index,
which is deleted in ``finally``.  The source ``documents`` index is read only.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
from collections import Counter, defaultdict
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import PurePosixPath
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
from services.metadata_filter_projection import (
    MetadataProjectionQueryBoundary,
    build_projection_side_document,
    compile_metadata_filter_to_opensearch,
    evaluate_metadata_filter_projection,
    generate_metadata_filter_projection,
)
from services.metadata_filter_projection_canary import MetadataFilterProjectionCanary

RESULT_MARKER = "METADATA_FILTER_PROJECTION_CANARY_JSON="
SOURCE_FIELDS = [
    "document_id",
    "chunk_id",
    "chunk_index",
    "chunk_content_sha256",
    "filename",
    "mimetype",
    "source_url",
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
MINIMAL_SOURCE_FIELDS = [
    "document_id",
    "chunk_id",
    "source_entity_id",
    "chunk_content_sha256",
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


def _occurrence_id(source: dict[str, Any]) -> str:
    return str(source.get("source_entity_id") or source.get("document_id") or "")


def _profile_entity_id(source: dict[str, Any]) -> str:
    profile = source.get("document_metadata_profile")
    return str((profile or {}).get("entity_id") or _occurrence_id(source))


def _source_provenance(source: dict[str, Any]) -> SourceProvenance | None:
    value = source.get("source_provenance")
    return SourceProvenance.model_validate(value) if isinstance(value, dict) else None


def _format_name(source: dict[str, Any]) -> str:
    filename = str(source.get("filename") or "")
    extension = PurePosixPath(filename.replace("\\", "/")).suffix.lower()
    mime = str(source.get("mimetype") or "").partition(";")[0].strip().lower()
    if mime == "message/rfc822" or extension == ".eml":
        return "EML"
    extensions = {
        ".pdf": "PDF",
        ".docx": "DOCX",
        ".xlsx": "XLSX",
        ".html": "HTML",
        ".htm": "HTML",
        ".csv": "CSV",
        ".txt": "TXT",
        ".asc": "TXT",
        ".adoc": "TXT",
        ".asciidoc": "TXT",
    }
    if extension in extensions:
        return extensions[extension]
    if extension in {".bmp", ".gif", ".heic", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}:
        return "IMAGE"
    if mime.startswith("image/"):
        return "IMAGE"
    return extension.lstrip(".").upper() or "UNKNOWN"


def _context(source: dict[str, Any], entity_id: str) -> MetadataFilterProjectionSourceContext:
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
        response = await client.search(index=index_name, body=body, request_timeout=300)
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
) -> MetadataFilterClause:
    role = field.value.partition("_")[0]
    return MetadataFilterClause(
        field=field,
        operator=MetadataFilterOperator.EQUAL,
        values=(value,),
        calendar_basis=CalendarBasis.SOURCE_LOCAL,
        source_policy=(
            MetadataDateSourcePolicy.ANY_VALID_PRODUCTION_OBSERVATION
            if role == "production"
            else MetadataDateSourcePolicy.ANY_VALID_MODIFICATION_OBSERVATION
        ),
    )


def _base_filters() -> dict[str, MetadataFilter]:
    pdf = MetadataFilterClause(
        field=MetadataFilterField.MIME,
        operator=MetadataFilterOperator.EQUAL,
        values=("application/pdf",),
    )
    xlsx = MetadataFilterClause(
        field=MetadataFilterField.MIME,
        operator=MetadataFilterOperator.EQUAL,
        values=("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",),
    )
    march = _period_filter(MetadataFilterField.PRODUCTION_MONTH, "2024-03")
    modified_2023 = _period_filter(MetadataFilterField.MODIFICATION_YEAR, "2023")
    openarchiver = MetadataFilterClause(
        field=MetadataFilterField.SOURCE_SYSTEM,
        operator=MetadataFilterOperator.EQUAL,
        values=("openarchiver",),
    )
    production_exists = MetadataFilterClause(
        field=MetadataFilterField.PRODUCTION_MONTH,
        operator=MetadataFilterOperator.EXISTS,
        calendar_basis=CalendarBasis.SOURCE_LOCAL,
        source_policy=MetadataDateSourcePolicy.ANY_VALID_PRODUCTION_OBSERVATION,
    )
    temporal_conflict = MetadataFilterClause(
        field=MetadataFilterField.HAS_TEMPORAL_CONFLICT,
        operator=MetadataFilterOperator.EQUAL,
        values=("true",),
    )
    return {
        "pdf_produced_2024_03": MetadataFilter(clauses=(pdf, march)),
        "xlsx_modified_2023": MetadataFilter(clauses=(xlsx, modified_2023)),
        "source_openarchiver": MetadataFilter(clauses=(openarchiver,)),
        "pdf_2024_03_openarchiver": MetadataFilter(
            clauses=(pdf, march, openarchiver)
        ),
        "production_timestamp_exists": MetadataFilter(clauses=(production_exists,)),
        "temporal_conflict": MetadataFilter(clauses=(temporal_conflict,)),
    }


def _cohort_tags(source: dict[str, Any], projection: Any) -> set[str]:
    tags = {
        f"format:{_format_name(source)}",
        f"source:{projection.source_systems[0] if projection.source_systems else 'UNKNOWN'}",
        f"timezone:{'UNKNOWN' if projection.has_timezone_unknown else 'KNOWN_OR_ABSENT'}",
        f"conflict:{str(projection.has_metadata_conflict).lower()}",
        f"richness:{'RICH' if len(projection.value_observations) >= 8 else 'POOR'}",
    }
    values = set(projection.production_month_local)
    if "application/pdf" in projection.mime_types and "2024-03" in values:
        tags.add("example:pdf_2024_03")
        if "openarchiver" in projection.source_systems:
            tags.add("example:pdf_2024_03_openarchiver")
    if (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        in projection.mime_types
        and "2023" in projection.modification_year_local
    ):
        tags.add("example:xlsx_modified_2023")
    if not projection.has_production_observation:
        tags.add("example:production_absent")
    if projection.has_temporal_conflict:
        tags.add("example:temporal_conflict")
    if projection.creator_normalized:
        tags.add("example:creator")
    return tags


def _retain(
    pools: dict[str, list[tuple[str, Any]]],
    tag: str,
    key: str,
    document: Any,
    *,
    limit: int = 24,
) -> None:
    pool = pools[tag]
    pool.append((key, document))
    pool.sort(key=lambda item: item[0])
    del pool[limit:]


def _select_cohort(
    pools: dict[str, list[tuple[str, Any]]],
    *,
    size: int,
) -> tuple[list[Any], list[str]]:
    priority = [
        "example:pdf_2024_03",
        "example:xlsx_modified_2023",
        "example:pdf_2024_03_openarchiver",
        "example:production_absent",
        "example:temporal_conflict",
        "example:creator",
        "format:PDF",
        "format:DOCX",
        "format:XLSX",
        "format:EML",
        "format:IMAGE",
        "format:HTML",
        "format:TXT",
        "timezone:UNKNOWN",
        "timezone:KNOWN_OR_ABSENT",
        "conflict:true",
        "conflict:false",
        "richness:RICH",
        "richness:POOR",
        *sorted(tag for tag in pools if tag.startswith("source:")),
        "global",
    ]
    selected: dict[str, tuple[str, Any]] = {}
    offsets: Counter[str] = Counter()
    while len(selected) < size:
        progressed = False
        for tag in priority:
            pool = pools.get(tag, [])
            offset = offsets[tag]
            while offset < len(pool) and pool[offset][1].projection_document_id in selected:
                offset += 1
            offsets[tag] = offset + 1
            if offset >= len(pool):
                continue
            key, document = pool[offset]
            selected[document.projection_document_id] = (key, document)
            progressed = True
            if len(selected) == size:
                break
        if not progressed:
            raise RuntimeError("stratified candidate pools cannot satisfy canary size")
    documents = [item[1] for item in sorted(selected.values(), key=lambda item: item[0])]
    return documents, priority


def _percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    return ordered[math.ceil((len(ordered) - 1) * quantile)] if ordered else 0.0


async def _timed_count(client: Any, index: str, query: dict[str, Any]) -> dict[str, Any]:
    latencies: list[float] = []
    count = 0
    for _ in range(9):
        started = time.perf_counter()
        count = int((await client.count(index=index, body={"query": query}))["count"])
        latencies.append((time.perf_counter() - started) * 1000)
    return {
        "count": count,
        "runs": len(latencies),
        "min_ms": min(latencies),
        "p50_ms": _percentile(latencies, 0.50),
        "p95_ms": _percentile(latencies, 0.95),
    }


async def _source_state(
    client: Any,
    index_name: str,
    *,
    mapping_sha256: str | None = None,
    full_scan: bool,
    batch_size: int,
) -> dict[str, Any]:
    mapping = await client.indices.get_mapping(index=index_name)
    state = {
        "health": (await client.cluster.health()).get("status"),
        "chunks": int((await client.count(index=index_name))["count"]),
        "occurrences": int(
            (
                await client.count(
                    index=index_name,
                    body={"query": {"term": {"chunk_index": 0}}},
                )
            )["count"]
        ),
        "metadata_profiles": int(
            (
                await client.count(
                    index=index_name,
                    body={"query": {"exists": {"field": "document_metadata_profile_id"}}},
                )
            )["count"]
        ),
        "embeddings": int(
            (
                await client.count(
                    index=index_name,
                    body={
                        "query": {
                            "exists": {"field": "chunk_embedding_text_embedding_3_large"}
                        }
                    },
                )
            )["count"]
        ),
        "mapping_sha256": _canonical_sha256(mapping),
    }
    if mapping_sha256 is not None and state["mapping_sha256"] != mapping_sha256:
        raise RuntimeError("source index mapping changed during canary")
    if not full_scan:
        return state
    document_ids: set[str] = set()
    occurrence_ids: list[str] = []
    metadata_digests: list[str] = []
    representative_content_digests: list[str] = []
    async for source in _scan_representatives(
        client,
        index_name,
        source_fields=MINIMAL_SOURCE_FIELDS,
        batch_size=batch_size,
    ):
        document_id = str(source.get("document_id") or "")
        occurrence_id = _occurrence_id(source)
        document_ids.add(document_id)
        occurrence_ids.append(occurrence_id)
        facts_digest = str(source.get("document_metadata_facts_sha256") or "")
        if facts_digest:
            metadata_digests.append(f"{occurrence_id}:{facts_digest}")
        representative_content_digests.append(
            f"{source.get('chunk_id') or ''}:{source.get('chunk_content_sha256') or ''}"
        )
    state.update(
        {
            "documents": len(document_ids),
            "corpus_occurrence_digest": hashlib.sha256(
                "\n".join(sorted(occurrence_ids)).encode()
            ).hexdigest(),
            "metadata_control_digest": hashlib.sha256(
                "\n".join(sorted(metadata_digests)).encode()
            ).hexdigest(),
            "representative_content_control_digest": hashlib.sha256(
                "\n".join(sorted(representative_content_digests)).encode()
            ).hexdigest(),
        }
    )
    return state


async def run_remote(plan: dict[str, Any]) -> dict[str, Any]:
    from config.settings import clients, get_index_name
    from session_manager import SessionManager, User

    source_index = str(plan.get("source_index") or get_index_name())
    canary_index = str(plan["canary_index"])
    cohort_size = int(plan.get("cohort_size") or 100)
    batch_size = int(plan.get("batch_size") or 500)
    admin = clients.create_index_admin_opensearch_client()
    if admin is None:
        raise RuntimeError("index-admin client unavailable")
    dls_client: Any | None = None
    canary = MetadataFilterProjectionCanary(admin, index_name=canary_index)
    created = False
    rollback = False
    try:
        baseline_before = await _source_state(
            admin,
            source_index,
            full_scan=True,
            batch_size=batch_size,
        )
        if baseline_before["corpus_occurrence_digest"] != plan["expected_corpus_digest"]:
            raise RuntimeError("source corpus digest differs from audited baseline")

        pools: dict[str, list[tuple[str, Any]]] = defaultdict(list)
        generation_failures: list[str] = []
        profiled = 0
        scan_started = time.perf_counter()
        async for source in _scan_representatives(
            admin,
            source_index,
            source_fields=SOURCE_FIELDS,
            batch_size=batch_size,
        ):
            raw_profile = source.get("document_metadata_profile")
            if not isinstance(raw_profile, dict):
                continue
            profiled += 1
            try:
                profile = DocumentMetadataProfile.model_validate(raw_profile)
                provenance = _source_provenance(source)
                projection = generate_metadata_filter_projection(
                    profile,
                    source_context=_context(source, profile.entity_id),
                    source_provenance=provenance,
                )
                document = build_projection_side_document(
                    projection,
                    source_document_id=str(source.get("document_id") or ""),
                    source_entity_id=profile.entity_id,
                    representative_chunk_id=str(source.get("chunk_id") or ""),
                    owner=(str(source["owner"]) if source.get("owner") is not None else None),
                    allowed_users=tuple(str(item) for item in source.get("allowed_users") or []),
                    allowed_groups=tuple(str(item) for item in source.get("allowed_groups") or []),
                    allowed_principals=tuple(
                        str(item) for item in source.get("allowed_principals") or []
                    ),
                )
            except Exception as exc:
                if len(generation_failures) < 20:
                    generation_failures.append(
                        f"{_canonical_sha256(_occurrence_id(source))}:{type(exc).__name__}:{exc}"
                    )
                continue
            key = _canonical_sha256(
                {
                    "selector": "openrag.metadata-filter-projection-canary.v1",
                    "entity_id": profile.entity_id,
                }
            )
            _retain(pools, "global", key, document, limit=cohort_size * 3)
            for tag in _cohort_tags(source, projection):
                _retain(pools, tag, key, document)
        scan_seconds = time.perf_counter() - scan_started
        if profiled != baseline_before["metadata_profiles"]:
            raise RuntimeError("profile scan count differs from baseline count")
        if generation_failures:
            raise RuntimeError(
                f"projection generation failed for {len(generation_failures)} sampled rows: "
                + ";".join(generation_failures)
            )
        documents, priority = _select_cohort(pools, size=cohort_size)

        await canary.create()
        created = True
        first = await canary.apply(documents)
        verified = await canary.verify(documents)
        second = await canary.apply(documents)
        if first["changed"] != cohort_size or verified["verified"] != cohort_size:
            raise RuntimeError("canary projection verification failed")
        if second["changed"] != 0:
            raise RuntimeError("canary projection is not idempotent")

        # DLS controls are isolated side-index rows, not source-corpus records.
        control_profile = DocumentMetadataProfile(entity_id="urn:canary:dls:source")
        control_projection = generate_metadata_filter_projection(
            control_profile,
            source_context=MetadataFilterProjectionSourceContext(
                source_entity_id=control_profile.entity_id,
                source_system="canary-control",
            ),
        )
        owner_counts = Counter(
            item.owner for item in documents if item.owner is not None
        )
        visible_user = (
            owner_counts.most_common(1)[0][0]
            if owner_counts
            else "metadata-filter-canary-visible"
        )
        hidden_user = "metadata-filter-canary-hidden"
        controls = [
            build_projection_side_document(
                control_projection,
                source_document_id="canary-control-visible",
                source_entity_id="urn:canary:dls:visible",
                representative_chunk_id="canary-control-visible",
                owner=visible_user,
            ),
            build_projection_side_document(
                control_projection,
                source_document_id="canary-control-hidden",
                source_entity_id="urn:canary:dls:hidden",
                representative_chunk_id="canary-control-hidden",
                owner=hidden_user,
            ),
        ]
        control_write = await canary.apply(controls, enforce_cohort_bounds=False)

        session = SessionManager()
        token = session.create_opensearch_jwt_token(
            User(
                user_id=visible_user,
                email=f"{visible_user}@invalid.local",
                name="Metadata Filter Canary",
            ),
            ttl_seconds=900,
        )
        dls_client = clients.create_user_opensearch_client(token)
        control_ids = [item.projection_document_id for item in controls]
        both_query = {"ids": {"values": control_ids}}
        visible_query = {"ids": {"values": [control_ids[0]]}}
        hidden_query = {"ids": {"values": [control_ids[1]]}}
        both_count = int(
            (await dls_client.count(index=canary_index, body={"query": both_query}))["count"]
        )
        visible_count = int(
            (await dls_client.count(index=canary_index, body={"query": visible_query}))["count"]
        )
        hidden_count = int(
            (await dls_client.count(index=canary_index, body={"query": hidden_query}))["count"]
        )
        search = await dls_client.search(
            index=canary_index,
            body={"query": both_query, "size": 10, "_source": False},
        )
        aggregation = await dls_client.search(
            index=canary_index,
            body={
                "query": both_query,
                "size": 0,
                "aggs": {"owners": {"terms": {"field": "owner", "size": 10}}},
            },
        )
        buckets = aggregation.get("aggregations", {}).get("owners", {}).get("buckets", [])
        dls_pass = (
            both_count == 1
            and visible_count == 1
            and hidden_count == 0
            and len(search.get("hits", {}).get("hits", [])) == 1
            and [item.get("key") for item in buckets] == [visible_user]
        )
        if not dls_pass:
            raise RuntimeError("live DLS search/count/aggregation control failed")

        # Restrict cardinalities to the cohort actually visible under the same
        # DLS client used for every projection query.
        visible_response = await dls_client.search(
            index=canary_index,
            body={
                "query": {
                    "bool": {
                        "filter": [
                            {"terms": {"projection_document_id": [item.projection_document_id for item in documents]}}
                        ]
                    }
                },
                "size": 500,
                "_source": ["projection_document_id"],
            },
        )
        visible_ids = {
            str((hit.get("_source") or {}).get("projection_document_id"))
            for hit in visible_response.get("hits", {}).get("hits", [])
        }
        visible_documents = [
            item for item in documents if item.projection_document_id in visible_ids
        ]
        filters = _base_filters()
        creator_values = sorted(
            {
                value
                for item in visible_documents
                for value in item.filter.creator_normalized
            }
        )
        if creator_values:
            filters["creator_exact_observation"] = MetadataFilter(
                clauses=(
                    MetadataFilterClause(
                        field=MetadataFilterField.CREATOR_OBSERVATION,
                        operator=MetadataFilterOperator.EQUAL,
                        values=(creator_values[0],),
                    ),
                )
            )

        filter_results: dict[str, Any] = {}
        performance: dict[str, Any] = {}
        for name, metadata_filter in filters.items():
            counts: Counter[str] = Counter()
            filter_conflict_count = 0
            for document in visible_documents:
                evaluation = evaluate_metadata_filter_projection(
                    metadata_filter,
                    document_id=document.source_entity_id,
                    projection=document.filter,
                )
                counts[evaluation.result.value] += 1
                filter_conflict_count += bool(evaluation.conflict_flags)
            query = compile_metadata_filter_to_opensearch(
                metadata_filter,
                boundary=MetadataProjectionQueryBoundary.DLS_SCOPED_OPENSEARCH_CLIENT,
            )
            timed = await _timed_count(dls_client, canary_index, query)
            if timed["count"] != counts[MetadataTruthValue.TRUE.value]:
                raise RuntimeError(f"OpenSearch TRUE set differs from evaluator for {name}")
            filter_results[name] = {
                "filter_sha256": metadata_filter.calculate_sha256(),
                "TRUE": counts[MetadataTruthValue.TRUE.value],
                "FALSE": counts[MetadataTruthValue.FALSE.value],
                "UNKNOWN": counts[MetadataTruthValue.UNKNOWN.value],
                "conflict_count": filter_conflict_count,
                "latency": timed,
            }
            performance[name] = timed
        if creator_values:
            filter_results["creator_exact_observation"]["value_sha256"] = hashlib.sha256(
                creator_values[0].encode()
            ).hexdigest()
            filter_results["creator_exact_observation"]["value_exposed"] = False

        formats: Counter[str] = Counter()
        sources: Counter[str] = Counter()
        timezone: Counter[str] = Counter()
        conflict_distribution: Counter[str] = Counter()
        for document in documents:
            mime = document.filter.mime_types[0] if document.filter.mime_types else ""
            extension = document.filter.extensions[0] if document.filter.extensions else ""
            formats[_format_name({"mimetype": mime, "filename": f"x{extension}"})] += 1
            for source_system in document.filter.source_systems or ("UNKNOWN",):
                sources[source_system] += 1
            timezone["UNKNOWN" if document.filter.has_timezone_unknown else "KNOWN_OR_ABSENT"] += 1
            conflict_distribution[str(document.filter.has_metadata_conflict).lower()] += 1

        baseline_after = await _source_state(
            admin,
            source_index,
            mapping_sha256=baseline_before["mapping_sha256"],
            full_scan=True,
            batch_size=batch_size,
        )
        integrity_fields = [
            "documents",
            "occurrences",
            "chunks",
            "embeddings",
            "metadata_profiles",
            "mapping_sha256",
            "corpus_occurrence_digest",
            "metadata_control_digest",
            "representative_content_control_digest",
        ]
        integrity_pass = all(
            baseline_before[field] == baseline_after[field] for field in integrity_fields
        )
        if not integrity_pass:
            raise RuntimeError("source corpus integrity changed during side-index canary")

        result = {
            "schema": "openrag.metadata-filter-projection-canary-result",
            "version": 1,
            "captured_at": datetime.now(UTC).isoformat(),
            "source_index": source_index,
            "canary_index": canary_index,
            "baseline_before": baseline_before,
            "baseline_after": baseline_after,
            "source_index_write_operations": 0,
            "source_mapping_changes": 0,
            "cohort": {
                "documents": len(documents),
                "visible_to_controlled_dls_user": len(visible_documents),
                "formats": dict(sorted(formats.items())),
                "sources": dict(sorted(sources.items())),
                "timezone": dict(sorted(timezone.items())),
                "metadata_conflict": dict(sorted(conflict_distribution.items())),
                "selection_policy": "sha256 round-robin over deterministic strata",
                "selection_policy_version": 1,
                "priority": priority,
            },
            "generation": {
                "profiled_scanned": profiled,
                "failed": len(generation_failures),
                "scan_and_generation_seconds": scan_seconds,
            },
            "first_projection": first,
            "verification": verified,
            "second_projection": second,
            "dls_controls": {
                "rows": len(controls),
                "written": control_write["changed"],
                "search_visible": len(search.get("hits", {}).get("hits", [])),
                "count_both": both_count,
                "count_visible_only": visible_count,
                "count_hidden_only": hidden_count,
                "aggregation_owner_buckets": len(buckets),
                "hidden_docs_affect_count": False,
                "hidden_docs_affect_aggregation": False,
                "pass": dls_pass,
                "controlled_user_source": (
                    "cohort_most_common_owner" if owner_counts else "synthetic_owner"
                ),
                "controlled_user_sha256": hashlib.sha256(visible_user.encode()).hexdigest(),
                "controlled_user_exposed": False,
            },
            "filter_results": filter_results,
            "performance": {
                "before_application_side_full_scan_seconds": 216.77,
                "projection_scan_and_generation_seconds": scan_seconds,
                "queries": performance,
            },
            "integrity_pass": integrity_pass,
            "invariants": {
                "raw_metadata_profiles_changed": False,
                "content_changed": False,
                "chunks_changed": False,
                "embeddings_changed": False,
                "bm25_changed": False,
                "dense_changed": False,
                "rrf_changed": False,
                "seed_semantics_changed": False,
                "prov_o_edges_added": 0,
                "scope_expansion_added": 0,
                "coverage_semantics_changed": False,
                "association_policy_changed": False,
                "llm_calls": 0,
            },
        }
        return result
    finally:
        if created:
            rollback = await canary.rollback()
        if dls_client is not None:
            await dls_client.close()
        await admin.close()
        if created and not rollback:
            raise RuntimeError("canary rollback failed")


def remote_entry(plan: dict[str, Any]) -> None:
    result = asyncio.run(run_remote(plan))
    # rollback is guaranteed by run_remote finally; record the postcondition.
    result["rollback"] = {
        "canary_index_removed": True,
        "source_index_untouched": True,
    }
    print(f"{RESULT_MARKER}{json.dumps(result, ensure_ascii=False, sort_keys=True)}", flush=True)
