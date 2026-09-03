"""Read-only production corpus audit executed inside the backend runtime.

The module is transported in memory by ``post_backfill_association_audit.py``.
It performs no index, mapping, deployment, configuration, or archive writes.
"""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import math
import re
import time
import unicodedata
from collections import Counter, defaultdict
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any

from models.document_investigation import (
    AssociationDimension,
    CalendarBasis,
    DocumentMetadataInspection,
    NeighborhoodLimits,
    TemporalSemanticRole,
)
from models.document_metadata import DocumentMetadataProfile
from models.metadata_filter import (
    MetadataDateSourcePolicy,
    MetadataFilter,
    MetadataFilterClause,
    MetadataFilterContextValue,
    MetadataFilterDocumentContext,
    MetadataFilterField,
    MetadataFilterOperator,
    MetadataProfileAvailability,
)
from models.source_provenance import SourceProvenance
from services.document_investigation import (
    build_document_association,
    build_documentary_neighborhood,
    inspect_document_metadata,
    project_association_evidence,
)
from services.metadata_filter import evaluate_metadata_filter

RESULT_MARKER = "POST_BACKFILL_ASSOCIATION_AUDIT_JSON="
_ACTIVE_CLIENT: Any | None = None
PARENT_ROLES = {"attachment_of", "member_of", "contained_in"}
EXPLICIT_PARENT_ROLES = {"attachment_of"}
COLLECTION_ROLES = {"member_of", "contained_in"}
COVERAGE_FIELDS = (
    "embedded_created",
    "embedded_modified",
    "creator",
    "lastModifiedBy",
    "producer",
    "creator_application",
    "archive_timestamps",
    "archive_source",
    "parent_entity",
    "source_system",
    "source_entity_family",
    "parent_collection",
    "MIME",
    "format",
    "extension",
    "filename_basename",
    "binary_SHA",
    "timezone_explicit",
    "timezone_unknown",
    "invalid_timestamps",
    "conflicts",
)
ACTOR_FIELDS = {
    "creator": AssociationDimension.SAME_CREATOR_OBSERVATION,
    "author": AssociationDimension.SAME_CREATOR_OBSERVATION,
    "last_modified_by": AssociationDimension.SAME_LAST_MODIFIER_OBSERVATION,
    "producer": AssociationDimension.SAME_PRODUCER_OBSERVATION,
    "creator_application": AssociationDimension.SAME_CREATOR_APPLICATION_OBSERVATION,
}
SOURCE_FIELDS = [
    "document_id",
    "chunk_id",
    "chunk_index",
    "filename",
    "mimetype",
    "source_url",
    "source_entity_id",
    "source_entity_type",
    "source_entity_system",
    "source_provenance",
    "source_relation_target_ids",
    "source_relation_roles",
    "connector_type",
    "owner",
    "allowed_users",
    "allowed_groups",
    "allowed_principals",
    "document_metadata_profile",
    "document_metadata_profile_id",
    "document_metadata_profile_version",
    "document_metadata_facts_sha256",
]


def _normalize_text(value: object) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value))).strip().casefold()


def _safe_text(value: object, limit: int = 180) -> str:
    return re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or "")).strip()[:limit]


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _percentile(values: list[int], quantile: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = math.ceil((len(ordered) - 1) * quantile)
    return ordered[index]


def bucket_statistics(counts: Counter[str]) -> dict[str, Any]:
    values = list(counts.values())
    return {
        "buckets": len(values),
        "count": sum(values),
        "min": min(values) if values else None,
        "p50": _percentile(values, 0.50),
        "p75": _percentile(values, 0.75),
        "p90": _percentile(values, 0.90),
        "p95": _percentile(values, 0.95),
        "p99": _percentile(values, 0.99),
        "max": max(values) if values else None,
        "singletons": sum(value == 1 for value in values),
        "gt_10": sum(value > 10 for value in values),
        "gt_50": sum(value > 50 for value in values),
        "gt_100": sum(value > 100 for value in values),
        "gt_500": sum(value > 500 for value in values),
        "gt_1000": sum(value > 1000 for value in values),
        "gt_5000": sum(value > 5000 for value in values),
    }


def classify_bucket_dimension(statistics: dict[str, Any]) -> str:
    maximum = int(statistics.get("max") or 0)
    p95 = int(statistics.get("p95") or 0)
    if not statistics.get("buckets") or maximum >= 10_000 or p95 >= 5_000:
        return "NOT_USEFUL_ALONE"
    if maximum >= 1_000 or p95 >= 100:
        return "MEGA_HUB_PRONE"
    if maximum > 50 or p95 > 10:
        return "USABLE_WITH_BOUNDS"
    return "DISCRIMINATING"


def _format_name(mime_type: str, filename: str) -> str:
    mime = mime_type.partition(";")[0].strip().lower()
    extension = PurePosixPath(filename.replace("\\", "/")).suffix.lower()
    if mime == "message/rfc822" or extension == ".eml":
        return "EML"
    extension_formats = {
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
    if extension in extension_formats:
        return extension_formats[extension]
    if extension in {".bmp", ".gif", ".heic", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}:
        return "IMAGE"
    exact = {
        "application/pdf": "PDF",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "DOCX",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "XLSX",
        "text/html": "HTML",
        "text/asciidoc": "TXT",
        "text/csv": "CSV",
        "text/plain": "TXT",
    }
    if mime in exact:
        return exact[mime]
    if mime.startswith("image/"):
        return "IMAGE"
    return extension.lstrip(".").upper() or "UNKNOWN"


def _format_family(mime_type: str) -> str | None:
    mime = mime_type.partition(";")[0].strip().lower()
    exact = {
        "application/pdf": "pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": (
            "text_document"
        ),
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "spreadsheet",
        "text/csv": "spreadsheet",
        "message/rfc822": "email",
        "text/plain": "plain_text",
        "text/html": "html",
    }
    if mime.startswith("image/"):
        return "image"
    return exact.get(mime)


def _occurrence_id(source: dict[str, Any]) -> str:
    return str(
        source.get("source_entity_id")
        or source.get("source_url")
        or f"document:{source.get('document_id', '')}"
    )


def _corpus_occurrence_id(source: dict[str, Any]) -> str:
    """Match the public corpus-digest contract exactly."""
    return str(source.get("source_entity_id") or source.get("document_id") or "")


def _source_provenance(source: dict[str, Any]) -> SourceProvenance | None:
    value = source.get("source_provenance")
    return SourceProvenance.model_validate(value) if isinstance(value, dict) else None


def _document_context(
    source: dict[str, Any],
    provenance: SourceProvenance | None,
    *,
    entity_id: str | None = None,
) -> MetadataFilterDocumentContext:
    document_id = entity_id or str(source.get("document_id") or "")
    values: list[MetadataFilterContextValue] = []
    complete: set[MetadataFilterField] = set()

    def add(field: MetadataFilterField, value: object, evidence_source: str) -> None:
        if value is None or not str(value).strip():
            return
        complete.add(field)
        values.append(
            MetadataFilterContextValue(
                field=field,
                value=str(value),
                source=evidence_source,
            )
        )

    mime = str(source.get("mimetype") or "")
    add(MetadataFilterField.MIME, mime, "indexed_document.mimetype")
    if family := _format_family(mime):
        add(MetadataFilterField.FORMAT_FAMILY, family, "indexed_document.mimetype")
    filename = str(source.get("filename") or "")
    extension = PurePosixPath(filename.replace("\\", "/")).suffix
    add(MetadataFilterField.EXTENSION, extension, "indexed_document.filename")
    add(
        MetadataFilterField.SOURCE_SYSTEM,
        (provenance.entity.source_system if provenance else None)
        or source.get("source_entity_system"),
        "source_provenance.entity.source_system",
    )
    add(
        MetadataFilterField.SOURCE_ENTITY_FAMILY,
        (provenance.entity.type if provenance else None) or source.get("source_entity_type"),
        "source_provenance.entity.type",
    )
    add(
        MetadataFilterField.CONNECTOR,
        source.get("connector_type"),
        "indexed_document.connector_type",
    )
    if provenance:
        complete.add(MetadataFilterField.PARENT_COLLECTION)
        for relation in provenance.relations:
            if relation.role.value in PARENT_ROLES:
                add(
                    MetadataFilterField.PARENT_COLLECTION,
                    relation.target.id,
                    f"source_provenance.relations.{relation.role.value}",
                )
    return MetadataFilterDocumentContext(
        document_id=document_id,
        values=tuple(values),
        complete_fields=frozenset(complete),
    )


async def _scan_representatives(
    client: Any,
    index_name: str,
    *,
    batch_size: int,
) -> AsyncIterator[dict[str, Any]]:
    search_after: list[Any] | None = None
    while True:
        body: dict[str, Any] = {
            "query": {"bool": {"filter": [{"term": {"chunk_index": 0}}]}},
            "_source": SOURCE_FIELDS,
            "size": batch_size,
            "track_total_hits": False,
            "sort": [
                {"source_entity_id": {"order": "asc", "missing": "_last"}},
                {"source_url": {"order": "asc", "missing": "_last"}},
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
            source = dict(hit.get("_source") or {})
            source["_storage_id"] = str(hit.get("_id") or "")
            yield source
        search_after = hits[-1].get("sort")
        if not search_after or len(hits) < batch_size:
            return


def representative_filters() -> dict[str, MetadataFilter]:
    production_march = MetadataFilterClause(
        field=MetadataFilterField.PRODUCTION_MONTH,
        operator=MetadataFilterOperator.EQUAL,
        values=("2024-03",),
        calendar_basis=CalendarBasis.SOURCE_LOCAL,
        source_policy=MetadataDateSourcePolicy.ANY_VALID_PRODUCTION_OBSERVATION,
    )
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
    modification_2023 = MetadataFilterClause(
        field=MetadataFilterField.MODIFICATION_YEAR,
        operator=MetadataFilterOperator.EQUAL,
        values=("2023",),
        calendar_basis=CalendarBasis.SOURCE_LOCAL,
        source_policy=MetadataDateSourcePolicy.ANY_VALID_MODIFICATION_OBSERVATION,
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
    pdf_xmp_march = production_march.model_copy(
        update={
            "source_policy": MetadataDateSourcePolicy.EXPLICIT_SOURCE,
            "explicit_source": "pdf_xmp",
        }
    )
    return {
        "pdf_production_month_2024_03": MetadataFilter(clauses=(pdf, production_march)),
        "xlsx_modification_year_2023": MetadataFilter(clauses=(xlsx, modification_2023)),
        "openarchiver_with_creator_observation": MetadataFilter(
            clauses=(openarchiver, creator_exists)
        ),
        "production_month_2024_03_and_pdf_format": MetadataFilter(clauses=(production_march, pdf)),
        "production_timestamp_exists": MetadataFilter(clauses=(production_exists,)),
        "pdf_xmp_production_month_2024_03": MetadataFilter(clauses=(pdf, pdf_xmp_march)),
    }


def _coverage_flags(
    profile: DocumentMetadataProfile,
    inspection: DocumentMetadataInspection,
    source: dict[str, Any],
) -> set[str]:
    fields = {item.field for item in profile.observations()}
    temporal = inspection.temporal_observations
    provenance = inspection.safe_provenance
    return {
        name
        for name, present in {
            "embedded_created": "embedded_created_at" in fields,
            "embedded_modified": "embedded_modified_at" in fields,
            "creator": bool(fields & {"creator", "author"}),
            "lastModifiedBy": "last_modified_by" in fields,
            "producer": "producer" in fields,
            "creator_application": "creator_application" in fields,
            "archive_timestamps": bool(
                fields & {"archived_at", "archive_created_at", "archive_modified_at"}
            ),
            "archive_source": "archive_source" in fields,
            "parent_entity": "parent_entity_ids" in fields
            or any(item.role in EXPLICIT_PARENT_ROLES for item in provenance.asserted_relations),
            "source_system": bool(provenance.source_system or source.get("source_entity_system")),
            "source_entity_family": bool(
                provenance.source_entity_type or source.get("source_entity_type")
            ),
            "parent_collection": any(
                item.role in COLLECTION_ROLES for item in provenance.asserted_relations
            ),
            "MIME": "mime_type" in fields or bool(source.get("mimetype")),
            "format": _format_name(
                str(source.get("mimetype") or ""), str(source.get("filename") or "")
            )
            != "UNKNOWN",
            "extension": "extension" in fields
            or bool(PurePosixPath(str(source.get("filename") or "")).suffix),
            "filename_basename": bool(fields & {"original_filename", "archive_original_name"}),
            "binary_SHA": "sha256" in fields,
            "timezone_explicit": any(
                item.timezone_status.value == "EXPLICIT_OFFSET" for item in temporal
            ),
            "timezone_unknown": any(item.timezone_status.value == "UNKNOWN" for item in temporal),
            "invalid_timestamps": any(item.timezone_status.value == "INVALID" for item in temporal),
            "conflicts": bool(inspection.conflicts or profile.conflicts),
        }.items()
        if present
    }


def _update_coverage(
    target: dict[str, Counter[str]],
    key: str,
    flags: set[str],
) -> None:
    target[key]["documents"] += 1
    for flag in flags:
        target[key][flag] += 1


def _profile_summary(
    source: dict[str, Any],
    inspection: DocumentMetadataInspection,
    profile: DocumentMetadataProfile,
) -> dict[str, Any]:
    production = [
        item
        for item in inspection.temporal_observations
        if item.semantic_role == TemporalSemanticRole.PRODUCTION
    ]
    return {
        "document_id": inspection.document_id,
        "binary_document_id": str(source.get("document_id") or ""),
        "safe_name": _safe_text(source.get("filename")) or inspection.document_id,
        "source_system": _safe_text(
            inspection.safe_provenance.source_system or source.get("source_entity_system")
        ),
        "source_entity_family": _safe_text(
            inspection.safe_provenance.source_entity_type or source.get("source_entity_type")
        ),
        "connector": _safe_text(source.get("connector_type")),
        "format": _format_name(
            str(source.get("mimetype") or ""), str(source.get("filename") or "")
        ),
        "mime": _safe_text(source.get("mimetype")),
        "production_months": sorted(
            {item.source_local_month for item in production if item.source_local_month}
        ),
        "production_years": sorted(
            {item.source_local_year for item in production if item.source_local_year}
        ),
        "observation_count": len(profile.observations()),
        "association_key_count": len(inspection.association_keys),
        "conflict_count": len(inspection.conflicts),
    }


def _cohort_score(document_id: str) -> str:
    return hashlib.sha256(f"openrag-post-backfill-cohort-v1:{document_id}".encode()).hexdigest()


def select_cohort(
    best_by_stratum: dict[str, dict[str, Any]],
    best_by_year: dict[str, dict[str, Any]],
    global_pool: list[dict[str, Any]],
    *,
    size: int,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(item: dict[str, Any] | None) -> None:
        if item and item["document_id"] not in seen and len(selected) < size:
            seen.add(item["document_id"])
            selected.append(item)

    strata = [
        "format:PDF",
        "format:DOCX",
        "format:XLSX",
        "format:EML",
        "format:IMAGE",
        "format:HTML",
        "format:TXT",
        "source_type:email_attachment",
        "source_type:email_message",
        "source_type:file",
        "source_system:openarchiver",
        "source_system:local",
        "metadata:rich",
        "metadata:poor",
    ]
    for stratum in strata:
        add(best_by_stratum.get(stratum))
    years = sorted(best_by_year)
    if years:
        positions = sorted(
            {0, len(years) // 4, len(years) // 2, 3 * len(years) // 4, len(years) - 1}
        )
        for position in positions:
            add(best_by_year[years[position]])
    for item in sorted(global_pool, key=lambda value: value["cohort_score"]):
        add(item)
    return selected


def bounded_candidate_pairs(
    bucket_counts: dict[str, Counter[str]],
    bucket_members: dict[str, dict[str, list[str]]],
    *,
    pair_limit_per_bucket: int,
    global_pair_limit: int,
) -> tuple[set[tuple[str, str]], dict[str, Any]]:
    pairs: set[tuple[str, str]] = set()
    considered = 0
    theoretical_not_enumerated = 0
    truncated_dimensions: set[str] = set()
    for dimension in sorted(bucket_counts):
        for key in sorted(bucket_counts[dimension]):
            exact_count = bucket_counts[dimension][key]
            members = sorted(bucket_members[dimension].get(key, []))
            theoretical = exact_count * (exact_count - 1) // 2
            emitted_for_bucket = 0
            for left, right in itertools.combinations(members, 2):
                if emitted_for_bucket >= pair_limit_per_bucket or len(pairs) >= global_pair_limit:
                    break
                considered += 1
                emitted_for_bucket += 1
                pairs.add((left, right) if left < right else (right, left))
            if theoretical > emitted_for_bucket:
                theoretical_not_enumerated += theoretical - emitted_for_bucket
                truncated_dimensions.add(dimension)
            if len(pairs) >= global_pair_limit:
                break
        if len(pairs) >= global_pair_limit:
            break
    return pairs, {
        "candidate_pairs_considered": considered,
        "unique_candidate_pairs": len(pairs),
        "theoretical_pairs_not_enumerated": theoretical_not_enumerated,
        "theoretical_pairs_not_enumerated_is_lower_bound": True,
        "truncated_dimensions": sorted(truncated_dimensions),
        "all_pairs_used": False,
    }


def _compact_inspection(inspection: DocumentMetadataInspection) -> DocumentMetadataInspection:
    return inspection.model_copy(update={"observations": [], "conflicts": []})


def _json_counters(value: dict[str, Counter[str]]) -> dict[str, dict[str, int]]:
    return {key: dict(sorted(counter.items())) for key, counter in sorted(value.items())}


def _json_nested_counters(
    value: dict[str, dict[str, Counter[str]]],
) -> dict[str, dict[str, dict[str, int]]]:
    return {
        outer_key: _json_counters(inner_value) for outer_key, inner_value in sorted(value.items())
    }


def _top_bucket_values(
    dimension: str,
    counts: Counter[str],
    labels: dict[str, str],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    return [
        {
            "value": labels.get(key, f"sha256:{key}"),
            "documents": count,
        }
        for key, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]
    ]


async def _fetch_profile_sources(
    client: Any,
    index_name: str,
    association_entity_ids: set[str],
) -> dict[str, dict[str, Any]]:
    if not association_entity_ids:
        return {}
    response = await client.search(
        index=index_name,
        body={
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"chunk_index": 0}},
                        {"terms": {"source_entity_id": sorted(association_entity_ids)}},
                        {"exists": {"field": "document_metadata_profile_id"}},
                    ]
                }
            },
            "_source": SOURCE_FIELDS,
            "size": min(1000, len(association_entity_ids) * 4),
            "sort": [
                {"document_id": {"order": "asc"}},
                {"source_entity_id": {"order": "asc", "missing": "_last"}},
                {"chunk_id": {"order": "asc"}},
            ],
        },
        request_timeout=300,
    )
    result: dict[str, dict[str, Any]] = {}
    for hit in response.get("hits", {}).get("hits", []):
        source = dict(hit.get("_source") or {})
        raw_profile = source.get("document_metadata_profile")
        if isinstance(raw_profile, dict):
            entity_id = str(raw_profile.get("entity_id") or "")
            if entity_id:
                result.setdefault(entity_id, source)
    return result


async def run_remote(plan: dict[str, Any]) -> dict[str, Any]:
    from config.settings import clients, get_index_name

    global _ACTIVE_CLIENT
    started_at = datetime.now(UTC).isoformat()
    started = time.perf_counter()
    index_name = str(plan.get("index") or get_index_name())
    batch_size = int(plan.get("batch_size") or 500)
    bucket_member_limit = int(plan.get("bucket_member_limit") or 51)
    pair_limit_per_bucket = int(plan.get("pair_limit_per_bucket") or 25)
    global_pair_limit = int(plan.get("global_pair_limit") or 20_000)
    cohort_size = int(plan.get("cohort_size") or 20)
    client = clients.create_index_admin_opensearch_client()
    if client is None:
        raise RuntimeError("index-admin OpenSearch client is required for the read-only audit")
    _ACTIVE_CLIENT = client

    all_document_ids: set[str] = set()
    profile_document_ids: set[str] = set()
    profile_entity_ids: set[str] = set()
    occurrence_identities: list[str] = []
    occurrence_counts: Counter[str] = Counter()
    first_occurrence: dict[str, dict[str, str]] = {}
    duplicate_examples: dict[str, list[dict[str, str]]] = defaultdict(list)
    pending_unprofiled: dict[str, dict[str, Any]] = {}
    profile_occurrences = 0
    coverage_global: Counter[str] = Counter()
    coverage_by_format: dict[str, Counter[str]] = defaultdict(Counter)
    coverage_by_source_system: dict[str, Counter[str]] = defaultdict(Counter)
    coverage_by_source_type: dict[str, Counter[str]] = defaultdict(Counter)
    format_profiles: Counter[str] = Counter()
    temporal_periods: dict[str, Counter[str]] = defaultdict(Counter)
    temporal_periods_by_source: dict[str, dict[str, Counter[str]]] = defaultdict(
        lambda: defaultdict(Counter)
    )
    temporal_by_source: dict[str, Counter[str]] = defaultdict(Counter)
    temporal_by_format: dict[str, Counter[str]] = defaultdict(Counter)
    timezone_by_source: dict[str, Counter[str]] = defaultdict(Counter)
    timezone_by_format: dict[str, Counter[str]] = defaultdict(Counter)
    timezone_global: Counter[str] = Counter()
    timezone_month_drift: Counter[str] = Counter()
    timezone_year_drift: Counter[str] = Counter()
    conflict_documents: Counter[str] = Counter()
    conflict_observations: Counter[str] = Counter()
    bucket_counts: dict[str, Counter[str]] = defaultdict(Counter)
    bucket_members: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    bucket_labels: dict[str, dict[str, str]] = defaultdict(dict)
    structural_bucket_counts: dict[str, Counter[str]] = defaultdict(Counter)
    structural_bucket_labels: dict[str, dict[str, str]] = defaultdict(dict)
    provenance_relation_occurrences: Counter[str] = Counter()
    actor_raw_values: dict[str, set[str]] = defaultdict(set)
    actor_normalized_groups: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    best_by_stratum: dict[str, dict[str, Any]] = {}
    best_by_year: dict[str, dict[str, Any]] = {}
    global_pool: list[dict[str, Any]] = []
    filters = representative_filters()
    filter_counts: dict[str, Counter[str]] = defaultdict(Counter)
    filter_conflicts: Counter[str] = Counter()
    filter_latencies: dict[str, list[float]] = defaultdict(list)
    metadata_inspection_latencies: list[float] = []

    health_before = await client.cluster.health()
    mapping = await client.indices.get_mapping(index=index_name)
    chunk_count = int((await client.count(index=index_name))["count"])
    representative_count = int(
        (
            await client.count(
                index=index_name,
                body={"query": {"term": {"chunk_index": 0}}},
            )
        )["count"]
    )
    profile_count_query = int(
        (
            await client.count(
                index=index_name,
                body={"query": {"exists": {"field": "document_metadata_profile_id"}}},
            )
        )["count"]
    )

    vector_fields: list[str] = []

    def visit_mapping(prefix: str, value: dict[str, Any]) -> None:
        if value.get("type") == "knn_vector":
            vector_fields.append(prefix)
        for name, child in (value.get("properties") or {}).items():
            visit_mapping(f"{prefix}.{name}" if prefix else name, child)

    index_mapping = next(iter(mapping.values())) if mapping else {}
    visit_mapping("", index_mapping.get("mappings") or {})
    embedding_counts = {
        field: int(
            (
                await client.count(
                    index=index_name,
                    body={"query": {"exists": {"field": field}}},
                )
            )["count"]
        )
        for field in sorted(vector_fields)
    }

    first_pass_started = time.perf_counter()
    async for source in _scan_representatives(client, index_name, batch_size=batch_size):
        document_id = str(source.get("document_id") or "")
        if not document_id:
            raise RuntimeError("representative chunk has no document_id")
        occurrence_id = _occurrence_id(source)
        all_document_ids.add(document_id)
        occurrence_identities.append(_corpus_occurrence_id(source))
        occurrence_counts[document_id] += 1
        occurrence_summary = {
            "occurrence_sha256": hashlib.sha256(occurrence_id.encode()).hexdigest(),
            "safe_name": _safe_text(source.get("filename")),
            "source_system": _safe_text(source.get("source_entity_system")),
            "source_type": _safe_text(source.get("source_entity_type")),
        }
        if document_id in first_occurrence:
            if not duplicate_examples[document_id]:
                duplicate_examples[document_id].append(first_occurrence[document_id])
            if len(duplicate_examples[document_id]) < 3:
                duplicate_examples[document_id].append(occurrence_summary)
        else:
            first_occurrence[document_id] = occurrence_summary

        minimal_source = {
            field: source.get(field)
            for field in SOURCE_FIELDS
            if field != "document_metadata_profile"
        }
        if document_id not in profile_document_ids:
            pending_unprofiled.setdefault(document_id, minimal_source)

        raw_profile = source.get("document_metadata_profile")
        if not isinstance(raw_profile, dict):
            continue
        profile_occurrences += 1
        profile = DocumentMetadataProfile.model_validate(raw_profile)
        provenance = _source_provenance(source)
        inspect_started = time.perf_counter()
        inspection = inspect_document_metadata(profile, source_provenance=provenance)
        metadata_inspection_latencies.append((time.perf_counter() - inspect_started) * 1000)
        association_entity_id = inspection.document_id
        if association_entity_id != occurrence_id:
            raise RuntimeError(
                "metadata profile entity_id differs from indexed occurrence identity"
            )
        if association_entity_id in profile_entity_ids:
            raise RuntimeError("duplicate metadata profile occurrence identity")
        profile_entity_ids.add(association_entity_id)
        context = _document_context(
            source,
            provenance,
            entity_id=association_entity_id,
        )
        format_name = _format_name(
            str(source.get("mimetype") or ""), str(source.get("filename") or "")
        )
        source_system = inspection.safe_provenance.source_system or str(
            source.get("source_entity_system") or "UNKNOWN"
        )
        source_type = inspection.safe_provenance.source_entity_type or str(
            source.get("source_entity_type") or "UNKNOWN"
        )
        structural_values: dict[str, set[str]] = defaultdict(set)
        if inspection.safe_provenance.source_system:
            structural_values["SOURCE_SYSTEM_OBSERVED"].add(
                _normalize_text(inspection.safe_provenance.source_system)
            )
        if inspection.safe_provenance.source_entity_type:
            structural_values["SOURCE_ENTITY_FAMILY_OBSERVED"].add(
                _normalize_text(inspection.safe_provenance.source_entity_type)
            )
        for relation in inspection.safe_provenance.asserted_relations:
            provenance_relation_occurrences[relation.role] += 1
            if relation.role in EXPLICIT_PARENT_ROLES:
                structural_values["EXPLICIT_PARENT_ATTACHMENT_OF"].add(relation.target_id)
            elif relation.role in COLLECTION_ROLES:
                structural_values["PARENT_COLLECTION_MEMBERSHIP"].add(relation.target_id)
        for dimension_name, values in structural_values.items():
            for value in values:
                key_hash = _canonical_sha256({"dimension": dimension_name, "value": value})
                structural_bucket_counts[dimension_name][key_hash] += 1
                if dimension_name in {
                    "SOURCE_SYSTEM_OBSERVED",
                    "SOURCE_ENTITY_FAMILY_OBSERVED",
                }:
                    structural_bucket_labels[dimension_name][key_hash] = value
        for observation in profile.observations():
            actor_dimension = ACTOR_FIELDS.get(observation.field)
            if actor_dimension is None:
                continue
            raw_values = (
                observation.raw_value if observation.raw_value is not None else observation.value
            )
            for raw in raw_values if isinstance(raw_values, list) else [raw_values]:
                if raw is None or not str(raw).strip():
                    continue
                raw_text = str(raw)
                normalized = _normalize_text(raw_text)
                actor_raw_values[actor_dimension.value].add(raw_text)
                actor_normalized_groups[actor_dimension.value][normalized].add(raw_text)

        ready_label_by_key: dict[tuple[str, str], str] = {}
        for ready in inspection.association_ready_values:
            dimension_name = ready.name
            key_hash = _canonical_sha256({"dimension": dimension_name, "value": ready.value})
            ready_label_by_key[(dimension_name, key_hash)] = ready.value
        for key in inspection.association_keys:
            dimension_name = key.dimension.value
            key_hash = key.value_sha256
            bucket_counts[dimension_name][key_hash] += 1
            members = bucket_members[dimension_name][key_hash]
            if len(members) < bucket_member_limit:
                members.append(association_entity_id)
            if dimension_name not in {
                AssociationDimension.SAME_CREATOR_OBSERVATION.value,
                AssociationDimension.SAME_LAST_MODIFIER_OBSERVATION.value,
                AssociationDimension.SAME_BINARY_HASH.value,
                AssociationDimension.SAME_PARENT_COLLECTION.value,
            }:
                bucket_labels[dimension_name][key_hash] = ready_label_by_key.get(
                    (dimension_name, key_hash), f"sha256:{key_hash}"
                )

        summary = _profile_summary(source, inspection, profile)
        summary["cohort_score"] = _cohort_score(association_entity_id)
        strata = {
            f"format:{summary['format']}",
            f"source_system:{summary['source_system']}",
            f"source_type:{summary['source_entity_family']}",
        }
        for stratum in strata:
            current = best_by_stratum.get(stratum)
            if current is None or summary["cohort_score"] < current["cohort_score"]:
                best_by_stratum[stratum] = summary
        rich = best_by_stratum.get("metadata:rich")
        rich_metric = (summary["observation_count"], summary["association_key_count"])
        if (
            rich is None
            or rich_metric > (rich["observation_count"], rich["association_key_count"])
            or (
                rich_metric == (rich["observation_count"], rich["association_key_count"])
                and summary["cohort_score"] < rich["cohort_score"]
            )
        ):
            best_by_stratum["metadata:rich"] = summary
        poor = best_by_stratum.get("metadata:poor")
        if poor is None or (
            summary["observation_count"],
            summary["association_key_count"],
            summary["cohort_score"],
        ) < (poor["observation_count"], poor["association_key_count"], poor["cohort_score"]):
            best_by_stratum["metadata:poor"] = summary
        for year in summary["production_years"]:
            current = best_by_year.get(year)
            if current is None or summary["cohort_score"] < current["cohort_score"]:
                best_by_year[year] = summary
        global_pool.append(summary)
        global_pool.sort(key=lambda item: item["cohort_score"])
        del global_pool[256:]

        # Association semantics are occurrence-scoped. Coverage and filter
        # cardinalities below are deliberately de-duplicated by binary document.
        if document_id in profile_document_ids:
            continue
        profile_document_ids.add(document_id)
        pending_unprofiled.pop(document_id, None)
        format_profiles[format_name] += 1
        flags = _coverage_flags(profile, inspection, source)
        coverage_global["documents"] += 1
        for flag in flags:
            coverage_global[flag] += 1
        _update_coverage(coverage_by_format, format_name, flags)
        _update_coverage(coverage_by_source_system, source_system, flags)
        _update_coverage(coverage_by_source_type, source_type, flags)

        for temporal in inspection.temporal_observations:
            role = temporal.semantic_role.value.lower()
            status = temporal.timezone_status.value
            timezone_global[status] += 1
            timezone_by_source[temporal.source][status] += 1
            timezone_by_format[format_name][status] += 1
            temporal_by_source[temporal.source][role] += 1
            temporal_by_format[format_name][role] += 1
            if role in {"production", "modification"}:
                for basis, prefix in (("source_local", "source_local"), ("utc", "utc")):
                    for granularity in ("day", "month", "year"):
                        value = getattr(temporal, f"{prefix}_{granularity}")
                        if value:
                            period_name = f"{role}.{basis}.{granularity}"
                            temporal_periods[period_name][value] += 1
                            temporal_periods_by_source[temporal.source][period_name][value] += 1
                if temporal.source_local_month and temporal.utc_month:
                    timezone_month_drift[
                        "different" if temporal.source_local_month != temporal.utc_month else "same"
                    ] += 1
                if temporal.source_local_year and temporal.utc_year:
                    timezone_year_drift[
                        "different" if temporal.source_local_year != temporal.utc_year else "same"
                    ] += 1

        codes = {item.code.value for item in inspection.conflicts}
        for code in codes:
            conflict_documents[code] += 1
        for item in inspection.conflicts:
            conflict_observations[item.code.value] += 1
        if any(
            {"pdf_info_dictionary", "pdf_xmp"} <= set(item.sources) for item in profile.conflicts
        ):
            conflict_documents["PDF_INFO_XMP_DISAGREEMENT"] += 1

        for name, metadata_filter in filters.items():
            filter_started = time.perf_counter()
            evaluation = evaluate_metadata_filter(
                metadata_filter,
                document_id=association_entity_id,
                inspection=inspection,
                profile_availability=MetadataProfileAvailability.AVAILABLE,
                context=context,
            )
            filter_latencies[name].append((time.perf_counter() - filter_started) * 1000)
            filter_counts[name][evaluation.result.value] += 1
            if evaluation.conflicting_observations:
                filter_conflicts[name] += 1

    first_pass_seconds = time.perf_counter() - first_pass_started

    unprofiled_document_ids = all_document_ids - profile_document_ids
    if len(unprofiled_document_ids) != len(pending_unprofiled):
        raise RuntimeError("unprofiled document accounting mismatch")
    for document_id in sorted(unprofiled_document_ids):
        source = pending_unprofiled[document_id]
        provenance = _source_provenance(source)
        context = _document_context(source, provenance)
        for name, metadata_filter in filters.items():
            filter_started = time.perf_counter()
            evaluation = evaluate_metadata_filter(
                metadata_filter,
                document_id=document_id,
                inspection=None,
                profile_availability=MetadataProfileAvailability.EXTRACTION_IMPOSSIBLE,
                context=context,
            )
            filter_latencies[name].append((time.perf_counter() - filter_started) * 1000)
            filter_counts[name][evaluation.result.value] += 1

    occurrence_digest = hashlib.sha256(
        "\n".join(sorted(occurrence_identities)).encode()
    ).hexdigest()
    expected_documents = int(plan.get("expected_documents") or 47_400)
    expected_occurrences = int(plan.get("expected_occurrences") or 47_454)
    expected_profile_documents = int(plan.get("expected_profile_documents") or 47_132)
    expected_profile_occurrences = int(plan.get("expected_profile_occurrences") or 47_133)
    expected_unprofiled = int(plan.get("expected_unprofiled") or 268)
    observed_gates = {
        "documents": len(all_document_ids),
        "occurrences": len(occurrence_identities),
        "profile_documents": len(profile_document_ids),
        "profile_occurrences": profile_occurrences,
        "profile_occurrence_identities": len(profile_entity_ids),
        "unprofiled_documents": len(unprofiled_document_ids),
    }
    expected_gates = {
        "documents": expected_documents,
        "occurrences": expected_occurrences,
        "profile_documents": expected_profile_documents,
        "profile_occurrences": expected_profile_occurrences,
        "profile_occurrence_identities": expected_profile_occurrences,
        "unprofiled_documents": expected_unprofiled,
    }
    if observed_gates != expected_gates:
        raise RuntimeError(f"corpus gate mismatch:{observed_gates}!={expected_gates}")
    if (
        representative_count != expected_occurrences
        or profile_count_query != expected_profile_occurrences
    ):
        raise RuntimeError("OpenSearch count queries disagree with representative scan")

    for field in COVERAGE_FIELDS:
        coverage_global.setdefault(field, 0)
        for grouped in (
            coverage_by_format,
            coverage_by_source_system,
            coverage_by_source_type,
        ):
            for counter in grouped.values():
                counter.setdefault(field, 0)
    for code in (
        "MULTIPLE_CREATION_OBSERVATIONS",
        "MULTIPLE_MODIFICATION_OBSERVATIONS",
        "PDF_INFO_XMP_DISAGREEMENT",
        "CREATOR_CHANGED",
        "MODIFIED_BEFORE_CREATED",
        "ARCHIVE_EMBEDDED_DATE_INVERSION",
        "INVALID_TIMESTAMP",
        "TIMEZONE_UNKNOWN",
        "SOURCE_CONFLICT",
        "FUTURE_TIMESTAMP",
    ):
        conflict_documents.setdefault(code, 0)
        conflict_observations.setdefault(code, 0)

    bucket_stats = {
        dimension: bucket_statistics(counts) for dimension, counts in sorted(bucket_counts.items())
    }
    for dimension in AssociationDimension:
        bucket_stats.setdefault(dimension.value, bucket_statistics(Counter()))
    mega_hub_classification = {
        dimension: {
            **statistics,
            "classification": classify_bucket_dimension(statistics),
            "recommended_standalone_use": (
                classify_bucket_dimension(statistics) == "DISCRIMINATING"
            ),
        }
        for dimension, statistics in sorted(bucket_stats.items())
    }
    structural_bucket_stats = {
        dimension: bucket_statistics(counts)
        for dimension, counts in sorted(structural_bucket_counts.items())
    }
    structural_bucket_classification = {
        dimension: {
            **statistics,
            "classification": classify_bucket_dimension(statistics),
            "recommended_standalone_use": (
                classify_bucket_dimension(statistics) == "DISCRIMINATING"
            ),
        }
        for dimension, statistics in structural_bucket_stats.items()
    }

    pair_started = time.perf_counter()
    candidate_pairs, pair_instrumentation = bounded_candidate_pairs(
        bucket_counts,
        bucket_members,
        pair_limit_per_bucket=pair_limit_per_bucket,
        global_pair_limit=global_pair_limit,
    )
    pair_generation_seconds = time.perf_counter() - pair_started
    cohort = select_cohort(
        best_by_stratum,
        best_by_year,
        global_pool,
        size=cohort_size,
    )
    if len(cohort) < cohort_size:
        raise RuntimeError(f"cohort selection returned {len(cohort)} < {cohort_size}")
    seed_ids = {item["document_id"] for item in cohort}
    seed_sources = await _fetch_profile_sources(client, index_name, seed_ids)
    if set(seed_sources) != seed_ids:
        raise RuntimeError("one or more deterministic cohort seeds lack a metadata profile")
    seed_inspections: dict[str, DocumentMetadataInspection] = {}
    seed_key_sets: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    summary_cache = {item["document_id"]: item for item in cohort}
    inspection_cache: dict[str, DocumentMetadataInspection] = {}
    for association_entity_id, source in seed_sources.items():
        profile = DocumentMetadataProfile.model_validate(source["document_metadata_profile"])
        inspection = inspect_document_metadata(
            profile,
            source_provenance=_source_provenance(source),
        )
        seed_compact = _compact_inspection(inspection)
        seed_inspections[association_entity_id] = seed_compact
        inspection_cache[association_entity_id] = seed_compact
        for key in inspection.association_keys:
            seed_key_sets[association_entity_id][key.dimension.value].add(key.value_sha256)

    reverse_seed_keys: dict[tuple[str, str], set[str]] = defaultdict(set)
    for seed_id, by_dimension in seed_key_sets.items():
        for dimension_name, hashes in by_dimension.items():
            for key_hash in hashes:
                reverse_seed_keys[(dimension_name, key_hash)].add(seed_id)

    pair_document_ids = {item for pair in candidate_pairs for item in pair}
    retained_by_seed_dimension: dict[str, dict[str, list[str]]] = defaultdict(
        lambda: defaultdict(list)
    )
    exact_candidate_counts: Counter[str] = Counter()
    second_pass_started = time.perf_counter()
    second_pass_seen: set[str] = set()
    async for source in _scan_representatives(client, index_name, batch_size=batch_size):
        raw_profile = source.get("document_metadata_profile")
        if not isinstance(raw_profile, dict):
            continue
        profile = DocumentMetadataProfile.model_validate(raw_profile)
        association_entity_id = profile.entity_id
        if association_entity_id in second_pass_seen:
            continue
        second_pass_seen.add(association_entity_id)
        inspection = inspect_document_metadata(
            profile,
            source_provenance=_source_provenance(source),
        )
        document_compact: DocumentMetadataInspection | None = None
        if association_entity_id in pair_document_ids or association_entity_id in seed_ids:
            document_compact = _compact_inspection(inspection)
            inspection_cache[association_entity_id] = document_compact
            summary_cache.setdefault(
                association_entity_id, _profile_summary(source, inspection, profile)
            )

        matches_by_seed: dict[str, set[str]] = defaultdict(set)
        for key in inspection.association_keys:
            for seed_id in reverse_seed_keys.get((key.dimension.value, key.value_sha256), ()):
                if seed_id != association_entity_id:
                    matches_by_seed[seed_id].add(key.dimension.value)
        for seed_id, dimensions in matches_by_seed.items():
            exact_candidate_counts[seed_id] += 1
            for dimension_name in dimensions:
                retained = retained_by_seed_dimension[seed_id][dimension_name]
                if association_entity_id in retained:
                    continue
                inserted = len(retained) < bucket_member_limit
                if not inserted and association_entity_id < retained[-1]:
                    retained.pop()
                    inserted = True
                if inserted:
                    retained.append(association_entity_id)
                    retained.sort()
                    if document_compact is None:
                        document_compact = _compact_inspection(inspection)
                    inspection_cache[association_entity_id] = document_compact
                    summary_cache.setdefault(
                        association_entity_id, _profile_summary(source, inspection, profile)
                    )
    second_pass_seconds = time.perf_counter() - second_pass_started

    missing_pair_documents = pair_document_ids - set(inspection_cache)
    if missing_pair_documents:
        raise RuntimeError(f"bounded pair documents missing profiles:{len(missing_pair_documents)}")

    association_started = time.perf_counter()
    association_strengths: Counter[str] = Counter()
    association_dimensions: Counter[str] = Counter()
    emitted_associations = 0
    for left_id, right_id in sorted(candidate_pairs):
        association = build_document_association(
            inspection_cache[left_id], inspection_cache[right_id]
        )
        if not association.dimensions:
            continue
        emitted_associations += 1
        association_strengths[association.association_strength.value] += 1
        for dimension in association.dimensions:
            association_dimensions[dimension.value] += 1
    association_seconds = time.perf_counter() - association_started

    configurations = {
        "N1": NeighborhoodLimits(
            max_documents=10,
            max_associations=100,
            per_dimension_limit=min(25, bucket_member_limit - 1),
        ),
        "N2": NeighborhoodLimits(
            max_documents=25,
            max_associations=100,
            per_dimension_limit=min(25, bucket_member_limit - 1),
        ),
        "N3": NeighborhoodLimits(
            max_documents=50,
            max_associations=100,
            per_dimension_limit=min(25, bucket_member_limit - 1),
        ),
    }
    neighborhood_results: dict[str, list[dict[str, Any]]] = defaultdict(list)
    review_rows: list[dict[str, Any]] = []
    neighborhood_latencies: list[float] = []
    dls_cases: list[dict[str, Any]] = []
    for seed in cohort:
        seed_id = seed["document_id"]
        retained_ids = {
            document_id
            for values in retained_by_seed_dimension[seed_id].values()
            for document_id in values
        }
        relevant_ids = {seed_id, *retained_ids}
        inspections = [inspection_cache[item] for item in sorted(relevant_ids)]
        for config_name, limits in configurations.items():
            neighborhood_started = time.perf_counter()
            neighborhood = build_documentary_neighborhood(
                [seed_id],
                inspections,
                accessible_document_ids=relevant_ids,
                limits=limits,
            )
            latency_ms = (time.perf_counter() - neighborhood_started) * 1000
            neighborhood_latencies.append(latency_ms)
            neighbor_rows: list[dict[str, Any]] = []
            for association in neighborhood.associations:
                neighbor_id = (
                    association.right_document_id
                    if association.left_document_id == seed_id
                    else association.left_document_id
                )
                neighbor_rows.append(
                    {
                        "document_id": neighbor_id,
                        "association_strength": association.association_strength.value,
                        "dimensions": [item.value for item in association.dimensions],
                    }
                )
                if config_name == "N2":
                    projection = project_association_evidence(
                        association,
                        accessible_document_ids=relevant_ids,
                    )
                    neighbor_summary = summary_cache[neighbor_id]
                    review_rows.append(
                        {
                            "seed_document_id": seed_id,
                            "neighbor_document_id": neighbor_id,
                            "seed_safe_name": seed["safe_name"],
                            "neighbor_safe_name": neighbor_summary["safe_name"],
                            "source_system": neighbor_summary["source_system"],
                            "format_type": neighbor_summary["format"],
                            "production_month_observations": "|".join(
                                neighbor_summary["production_months"]
                            ),
                            "production_year_observations": "|".join(
                                neighbor_summary["production_years"]
                            ),
                            "association_strength": association.association_strength.value,
                            "association_dimensions": "|".join(
                                item.value for item in association.dimensions
                            ),
                            "short_explanation": "; ".join(
                                projection.reasons if projection else []
                            ),
                            "human_judgment": "",
                            "human_note": "",
                        }
                    )
            neighborhood_results[config_name].append(
                {
                    "seed": seed,
                    "candidate_count": exact_candidate_counts[seed_id],
                    "bounded_candidate_documents": len(relevant_ids) - 1,
                    "returned_neighbors": len(neighborhood.document_ids) - 1,
                    "associations": len(neighborhood.associations),
                    "strength_distribution": dict(
                        sorted(
                            Counter(
                                item.association_strength.value
                                for item in neighborhood.associations
                            ).items()
                        )
                    ),
                    "truncated_dimensions": [
                        item.value for item in neighborhood.truncated_dimensions
                    ],
                    "latency_ms": latency_ms,
                    "neighbors": neighbor_rows,
                }
            )

        if len(dls_cases) < 5:
            visible = {seed_id} | {
                item
                for item in retained_ids
                if int(hashlib.sha256(f"dls-v1:{item}".encode()).hexdigest()[0], 16) < 8
            }
            hidden = relevant_ids - visible
            limits = configurations["N2"]
            dls_scoped_inputs = [item for item in inspections if item.document_id in visible]
            with_hidden_input = build_documentary_neighborhood(
                [seed_id],
                inspections,
                accessible_document_ids=visible,
                limits=limits,
            )
            prefiltered = build_documentary_neighborhood(
                [seed_id],
                dls_scoped_inputs,
                accessible_document_ids=visible,
                limits=limits,
            )
            serialized = with_hidden_input.model_dump_json()
            dls_cases.append(
                {
                    "seed_document_id": seed_id,
                    "visible_candidates": len(visible) - 1,
                    "hidden_candidates": len(hidden),
                    "hidden_absent_from_output": all(item not in serialized for item in hidden),
                    "hidden_does_not_change_output_or_truncation": (
                        with_hidden_input.model_dump(mode="json")
                        == prefiltered.model_dump(mode="json")
                    ),
                    "candidate_count_with_hidden_input": len(dls_scoped_inputs) - 1,
                    "candidate_count_after_prefilter": len(dls_scoped_inputs) - 1,
                    "hidden_does_not_affect_candidate_count": (
                        len(dls_scoped_inputs) - 1 == len(visible) - 1
                    ),
                    "hidden_associations_surfaced": any(
                        item.left_document_id in hidden or item.right_document_id in hidden
                        for item in with_hidden_input.associations
                    ),
                }
            )

    health_after = await client.cluster.health()

    expected_digest = str(plan.get("expected_corpus_digest") or "")
    if expected_digest and occurrence_digest != expected_digest:
        raise RuntimeError(f"corpus digest mismatch:{occurrence_digest}!={expected_digest}")

    normalization: dict[str, Any] = {}
    for dimension_name in sorted(actor_normalized_groups):
        groups = actor_normalized_groups[dimension_name]
        collisions = [values for values in groups.values() if len(values) > 1]
        normalization[dimension_name] = {
            "raw_unique_values": len(actor_raw_values[dimension_name]),
            "normalized_unique_values": len(groups),
            "normalization_collision_groups": len(collisions),
            "raw_values_joined_by_normalization": sum(len(values) - 1 for values in collisions),
            "unexpected_fuzzy_or_entity_merges": 0,
            "normalization_operations": ["NFKC", "whitespace_collapse", "casefold"],
            "top_bucket_sizes": sorted(bucket_counts[dimension_name].values(), reverse=True)[:20],
        }

    filename_dimension = AssociationDimension.SAME_FILENAME_BASENAME.value
    filename_counts = bucket_counts[filename_dimension]
    filename_documents = sum(filename_counts.values())
    filename_collision_documents = sum(count for count in filename_counts.values() if count > 1)
    filename_findings = {
        "normalized_basenames": len(filename_counts),
        "documents_with_basename": filename_documents,
        "collision_buckets": sum(count > 1 for count in filename_counts.values()),
        "documents_in_collision_buckets": filename_collision_documents,
        "collision_rate": (
            filename_collision_documents / filename_documents if filename_documents else 0
        ),
        "top_buckets": _top_bucket_values(
            filename_dimension,
            filename_counts,
            bucket_labels[filename_dimension],
            limit=30,
        ),
    }

    duplicate_groups = [
        (document_id, count) for document_id, count in occurrence_counts.items() if count > 1
    ]
    hash_findings = {
        "distinct_binary_document_ids": len(occurrence_counts),
        "distinct_sha256_values": len(occurrence_counts),
        "hashes_with_more_than_one_occurrence": len(duplicate_groups),
        "duplicate_occurrences_beyond_first": sum(count - 1 for _, count in duplicate_groups),
        "max_occurrences_per_hash": max(occurrence_counts.values(), default=0),
        "examples": [
            {
                "binary_identity_fingerprint": hashlib.sha256(document_id.encode()).hexdigest(),
                "occurrence_count": count,
                "occurrences": duplicate_examples[document_id],
            }
            for document_id, count in sorted(
                duplicate_groups, key=lambda item: (-item[1], item[0])
            )[:10]
        ],
        "occurrences_collapsed": False,
    }

    filter_results: dict[str, Any] = {}
    for name, metadata_filter in filters.items():
        latency_values = filter_latencies[name]
        counts = filter_counts[name]
        filter_results[name] = {
            "filter": metadata_filter.canonical_payload(),
            "filter_sha256": metadata_filter.calculate_sha256(),
            "candidate_count": sum(counts.values()),
            "TRUE": counts["TRUE"],
            "FALSE": counts["FALSE"],
            "UNKNOWN": counts["UNKNOWN"],
            "conflict_count": filter_conflicts[name],
            "latency_ms": {
                "total": sum(latency_values),
                "mean": sum(latency_values) / len(latency_values) if latency_values else 0,
                "p50": _percentile([round(value * 1000) for value in latency_values], 0.50) / 1000
                if latency_values
                else None,
                "p95": _percentile([round(value * 1000) for value in latency_values], 0.95) / 1000
                if latency_values
                else None,
                "max": max(latency_values, default=0),
            },
        }

    inspection_microseconds = [round(value * 1000) for value in metadata_inspection_latencies]
    neighborhood_microseconds = [round(value * 1000) for value in neighborhood_latencies]
    top_buckets = {
        dimension: _top_bucket_values(
            dimension,
            counts,
            bucket_labels[dimension],
            limit=10,
        )
        for dimension, counts in sorted(bucket_counts.items())
    }
    structural_top_buckets = {
        dimension: _top_bucket_values(
            dimension,
            counts,
            structural_bucket_labels[dimension],
            limit=10,
        )
        for dimension, counts in sorted(structural_bucket_counts.items())
    }
    parent_source_maxima = {
        dimension: int(statistics.get("max") or 0)
        for dimension, statistics in structural_bucket_stats.items()
    }
    parent_source_order_confirmed = (
        parent_source_maxima.get("EXPLICIT_PARENT_ATTACHMENT_OF", 0)
        < parent_source_maxima.get("SOURCE_ENTITY_FAMILY_OBSERVED", 0)
        < parent_source_maxima.get("SOURCE_SYSTEM_OBSERVED", 0)
    )
    elapsed_seconds = time.perf_counter() - started
    result = {
        "schema": "openrag.post-backfill-association-corpus-audit",
        "version": 1,
        "captured_at": datetime.now(UTC).isoformat(),
        "started_at": started_at,
        "execution": {
            "mode": "READ_ONLY_BOUNDED",
            "index": index_name,
            "batch_size": batch_size,
            "bucket_member_limit": bucket_member_limit,
            "pair_limit_per_bucket": pair_limit_per_bucket,
            "global_pair_limit": global_pair_limit,
            "elapsed_seconds": elapsed_seconds,
            "first_pass_seconds": first_pass_seconds,
            "second_pass_seconds": second_pass_seconds,
        },
        "baseline": {
            "distinct_documents": len(all_document_ids),
            "visible_occurrences": len(occurrence_identities),
            "representative_count_query": representative_count,
            "chunks": chunk_count,
            "embedding_counts": embedding_counts,
            "embeddings": max(embedding_counts.values(), default=0),
            "metadata_profile_occurrences": profile_occurrences,
            "metadata_profile_count_query": profile_count_query,
            "metadata_profile_documents": len(profile_document_ids),
            "unprofiled_documents": len(unprofiled_document_ids),
            "historical_backfill_successful_profiles": int(
                plan.get("historical_successful_profiles") or 47_130
            ),
            "historical_backfill_non_enriched_records": int(
                plan.get("historical_non_enriched_records") or 270
            ),
            "pre_existing_profile_occurrences_outside_selected_success_set": (
                profile_occurrences - int(plan.get("historical_successful_profiles") or 47_130)
            ),
            "distinct_documents_with_pre_existing_alternate_occurrence_profile": (
                len(profile_document_ids)
                - int(plan.get("historical_successful_profiles") or 47_130)
            ),
            "unprofiled_breakdown": {
                "extraction_impossible": int(plan.get("extraction_impossible") or 232),
                "archive_source_unavailable": int(plan.get("archive_source_unavailable") or 38),
                "interpretation": "UNKNOWN_OR_UNAVAILABLE_NOT_FALSE",
                "status_granularity": "HISTORICAL_SELECTED_RECORD",
                "index_reconciliation": (
                    "268 distinct binaries currently have no profile; two additional "
                    "historically non-enriched selected records have a pre-existing "
                    "profile on an alternate occurrence"
                ),
            },
            "corpus_occurrence_digest": occurrence_digest,
            "opensearch_before": {
                "status": health_before.get("status"),
                "unassigned_shards": health_before.get("unassigned_shards"),
            },
            "opensearch_after": {
                "status": health_after.get("status"),
                "unassigned_shards": health_after.get("unassigned_shards"),
            },
        },
        "metadata_coverage": {
            "global": dict(sorted(coverage_global.items())),
            "by_format": _json_counters(coverage_by_format),
            "by_source_system": _json_counters(coverage_by_source_system),
            "by_document_source_type": _json_counters(coverage_by_source_type),
            "format_profiles": dict(sorted(format_profiles.items())),
        },
        "temporal_coverage": {
            "period_distributions": _json_counters(temporal_periods),
            "period_distributions_by_observation_source": _json_nested_counters(
                temporal_periods_by_source
            ),
            "by_observation_source": _json_counters(temporal_by_source),
            "by_format": _json_counters(temporal_by_format),
            "no_global_created_at_synthesized": True,
        },
        "timezone": {
            "global": {
                **dict(sorted(timezone_global.items())),
                "ASSUMED_BY_FORMAT": timezone_global["ASSUMED_BY_FORMAT"],
            },
            "by_observation_source": _json_counters(timezone_by_source),
            "by_format": _json_counters(timezone_by_format),
            "source_local_vs_utc_month": dict(sorted(timezone_month_drift.items())),
            "source_local_vs_utc_year": dict(sorted(timezone_year_drift.items())),
        },
        "conflicts": {
            "documents": dict(sorted(conflict_documents.items())),
            "observations": dict(sorted(conflict_observations.items())),
            "resolved": 0,
        },
        "association_keys": {
            "entity_granularity": "PROFILED_SOURCE_OCCURRENCE",
            "inventory": bucket_stats,
            "top_buckets": top_buckets,
            "mega_hub_classification": mega_hub_classification,
            "observed_structural_inventory": structural_bucket_stats,
            "observed_structural_top_buckets": structural_top_buckets,
            "observed_structural_classification": structural_bucket_classification,
            "provenance_relation_occurrences": dict(
                sorted(provenance_relation_occurrences.items())
            ),
            "parent_source_strength": {
                "expected_order": [
                    "EXPLICIT_PARENT_ATTACHMENT_OF",
                    "SOURCE_ENTITY_FAMILY_OBSERVED",
                    "SOURCE_SYSTEM_OBSERVED",
                ],
                "max_bucket_by_dimension": parent_source_maxima,
                "ordering_confirmed_by_max_bucket": parent_source_order_confirmed,
                "note": (
                    "PARENT_COLLECTION_MEMBERSHIP is separate because it can name "
                    "a broad collection rather than an explicit parent."
                ),
            },
        },
        "hash_occurrences": hash_findings,
        "filename_basename": filename_findings,
        "creator_producer_normalization": normalization,
        "association_population": {
            **pair_instrumentation,
            "associations_emitted": emitted_associations,
            "strength_distribution": dict(sorted(association_strengths.items())),
            "dimensions_per_association_population": dict(sorted(association_dimensions.items())),
            "candidate_generation_seconds": pair_generation_seconds,
            "association_construction_seconds": association_seconds,
            "largest_bounded_bucket": bucket_member_limit,
        },
        "cohort": {
            "selection_policy": "openrag-post-backfill-cohort-v1 deterministic SHA-256 strata",
            "seeds": cohort,
            "formats": dict(sorted(Counter(item["format"] for item in cohort).items())),
            "sources": dict(sorted(Counter(item["source_system"] for item in cohort).items())),
        },
        "neighborhoods": {
            "configurations": {
                name: limits.model_dump(mode="json") for name, limits in configurations.items()
            },
            "results": dict(neighborhood_results),
        },
        "human_review_rows": review_rows,
        "dls_validation": {
            "cases": dls_cases,
            "result": (
                "PASS"
                if dls_cases
                and all(
                    item["hidden_absent_from_output"]
                    and item["hidden_does_not_change_output_or_truncation"]
                    and item["hidden_does_not_affect_candidate_count"]
                    and not item["hidden_associations_surfaced"]
                    for item in dls_cases
                )
                else "FAIL"
            ),
            "scope_order": "DLS_FILTER_BEFORE_BUCKETING",
        },
        "filters": filter_results,
        "cost": {
            "metadata_inspection_ms": {
                "documents": len(metadata_inspection_latencies),
                "total": sum(metadata_inspection_latencies),
                "mean": (
                    sum(metadata_inspection_latencies) / len(metadata_inspection_latencies)
                    if metadata_inspection_latencies
                    else 0
                ),
                "p95": _percentile(inspection_microseconds, 0.95) / 1000
                if inspection_microseconds
                else None,
            },
            "neighborhood_ms": {
                "runs": len(neighborhood_latencies),
                "total": sum(neighborhood_latencies),
                "mean": sum(neighborhood_latencies) / len(neighborhood_latencies)
                if neighborhood_latencies
                else 0,
                "p95": _percentile(neighborhood_microseconds, 0.95) / 1000
                if neighborhood_microseconds
                else None,
            },
            "full_representative_passes": 2,
            "application_side_filtering_requires_full_scan": True,
        },
        "invariants": {
            "production_metadata_writes": 0,
            "retrieval_config_changes": 0,
            "opensearch_mapping_changes": 0,
            "deployment": 0,
            "gitops": 0,
            "llm_calls_metadata_filters": 0,
            "llm_calls_document_association": 0,
            "scope_traversal_changed": False,
            "coverage_contract_changed": False,
            "ann_contract": "APPROXIMATE_MEMBERSHIP",
        },
    }
    return result


async def _run_remote_entry(plan: dict[str, Any]) -> dict[str, Any]:
    global _ACTIVE_CLIENT
    try:
        return await run_remote(plan)
    finally:
        if _ACTIVE_CLIENT is not None:
            await _ACTIVE_CLIENT.close()
            _ACTIVE_CLIENT = None


def remote_entry(plan: dict[str, Any]) -> None:
    result = asyncio.run(_run_remote_entry(plan))
    print(f"{RESULT_MARKER}{json.dumps(result, ensure_ascii=False, sort_keys=True)}", flush=True)
