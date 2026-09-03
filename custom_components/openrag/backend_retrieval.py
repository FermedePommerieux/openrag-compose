"""Thin Langflow tool that delegates document search to the OpenRAG backend.

The backend owns query construction, ACL-scoped OpenSearch access, RRF,
diversity, reranking and provenance.  This component deliberately contains no
OpenSearch query logic so the chat agent cannot drift from ``SearchService``.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

import httpx
from langchain_core.tools import StructuredTool
from lfx.base.langchain_utilities.model import LCToolComponent
from lfx.io import IntInput, MultilineInput, Output, SecretStrInput, StrInput
from lfx.schema.data import Data
from pydantic import BaseModel, ConfigDict, Field, model_validator

UNTRUSTED_CHUNK_FENCE_START = "<<<UNTRUSTED_DOC_CHUNK>>>"
UNTRUSTED_CHUNK_FENCE_END = "<<<END_UNTRUSTED_DOC_CHUNK>>>"
RETRIEVAL_GUARD_METADATA_KEY = "openrag_retrieval_guard"
METADATA_TOOL_SCHEMA_ID = "openrag.metadata-agent-search"
METADATA_TOOL_SCHEMA_VERSION = 1
METADATA_TOOL_NAME = "document_search_with_metadata"
MAX_METADATA_TOOL_FILTERS = 8
MAX_METADATA_TOOL_IN_VALUES = 16
MAX_METADATA_TOOL_FREE_TEXT = 512
MAX_METADATA_TOOL_RESULTS = 20

MetadataToolField = Literal[
    "production_day",
    "production_month",
    "production_year",
    "modification_day",
    "modification_month",
    "modification_year",
    "mime",
    "format_family",
    "extension",
    "source_document_type",
    "source_system",
    "source_entity_type",
    "source_entity_family",
    "parent_collection",
    "connector",
    "creator_observation",
    "last_modifier_observation",
    "producer_observation",
    "creator_application_observation",
    "binary_sha256",
    "has_temporal_conflict",
    "has_metadata_conflict",
]
MetadataToolOperator = Literal["EQUAL", "IN", "EXISTS", "NOT_EXISTS", "NOT_EQUAL"]


class MetadataToolFilter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: MetadataToolField
    operator: MetadataToolOperator
    value: str | list[str] | None = None
    calendar_basis: Literal["SOURCE_LOCAL", "UTC"] | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> MetadataToolFilter:
        temporal = self.field.startswith(("production_", "modification_"))
        if temporal != (self.calendar_basis is not None):
            raise ValueError("temporal filters require calendar_basis; other filters forbid it")
        if self.operator in {"EXISTS", "NOT_EXISTS"}:
            if self.value is not None:
                raise ValueError(f"{self.operator} does not accept a value")
        elif self.operator == "IN":
            if not isinstance(self.value, list) or not self.value:
                raise ValueError("IN requires a non-empty string array")
            if len(self.value) > MAX_METADATA_TOOL_IN_VALUES:
                raise ValueError(f"IN supports at most {MAX_METADATA_TOOL_IN_VALUES} values")
            if any(not isinstance(item, str) or not item.strip() for item in self.value):
                raise ValueError("IN values must be non-blank strings")
        elif not isinstance(self.value, str) or not self.value.strip():
            raise ValueError(f"{self.operator} requires one non-blank string value")
        values = self.value if isinstance(self.value, list) else [self.value]
        if any(isinstance(item, str) and len(item) > 256 for item in values):
            raise ValueError("metadata values must not exceed 256 characters")
        return self

    def canonical_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode="json", exclude_none=True)
        if self.operator == "IN":
            payload["value"] = sorted(set(payload["value"]))
        return payload


class MetadataToolQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    free_text: str = Field(min_length=1, max_length=MAX_METADATA_TOOL_FREE_TEXT)
    filters: list[MetadataToolFilter] = Field(
        min_length=1, max_length=MAX_METADATA_TOOL_FILTERS
    )
    limit: int = Field(default=10, ge=1, le=MAX_METADATA_TOOL_RESULTS)

    @model_validator(mode="after")
    def reject_blank_query(self) -> MetadataToolQuery:
        if not self.free_text.strip():
            raise ValueError("free_text must not be blank")
        return self


# Langflow evaluates custom-component source dynamically, so Pydantic cannot
# always recover local aliases or sibling model names from ``__module__``.
# Resolve them explicitly while the local symbols are authoritative.
MetadataToolFilter.model_rebuild(
    force=True,
    _types_namespace={
        "Literal": Literal,
        "MetadataToolField": MetadataToolField,
        "MetadataToolOperator": MetadataToolOperator,
    },
)
MetadataToolQuery.model_rebuild(
    force=True,
    _types_namespace={"MetadataToolFilter": MetadataToolFilter},
)

# Tool artifacts feed OpenRAG's source cards. For scope-exhaustive retrieval the
# backend transport profile guarantees this list is the bounded model projection,
# never the complete verified scope. Tool content repeats only evidence needed
# to write and cite the answer, with one manifest entry per document occurrence.
MODEL_EVIDENCE_FIELDS = (
    "chunk_id",
    "document_id",
    "page",
    "chunk_index",
    "evidence_order",
    "score",
    "text",
    "matched_queries",
    "matched_lanes",
    "best_rank_per_query",
    "query_contributions",
    "fusion_score",
)
MODEL_DOCUMENT_FIELDS = (
    "document_id",
    "filename",
    "mimetype",
    "connector_type",
    "source_entity_id",
    "source_entity_type",
    "source_entity_system",
    "source_entity_alternate_ids",
    "source_relative_path",
    "source_path_ancestors",
    "generated_at_time",
)
MODEL_COVERAGE_FIELDS = (
    "mode",
    "complete",
    "filename",
    "covered_chunks",
    "total_chunks",
    "coverage_ratio",
    "document_read_coverage_ratio",
    "next_cursor",
    "error",
    "query",
    "seed_discovery_complete",
    "seed_documents",
    "seed_entities",
    "valid_provenance_seed_documents",
    "invalid_provenance_seed_documents",
    "seed_provenance_complete",
    "requested_retrieval_profile",
    "effective_retrieval_profile",
    "retrieval_execution_complete",
    "retrieval_failure_codes",
    "scope_policy_id",
    "scope_policy_version",
    "graph_entities_visited",
    "graph_frontier_empty",
    "graph_limit_reached",
    "graph_stop_reason",
    "graph_failed",
    "graph_error",
    "graph_forward_hits",
    "graph_reverse_hits",
    "graph_forward_pages",
    "graph_reverse_pages",
    "graph_forward_verification_pages",
    "graph_reverse_verification_pages",
    "graph_distinct_results",
    "graph_stability_verified",
    "graph_stability_observations",
    "identity_shared_aliases_resolved",
    "relations_traversed",
    "relations_context_only",
    "relations_excluded_by_policy",
    "relations_unclassified",
    "documents_discovered",
    "documents_complete",
    "documents_incomplete",
    "stop_reason",
    "status_code",
    "status_message",
    "failure_codes",
    "model_evidence_chunks",
    "artifact_chunks",
    "discovery",
    "performance",
    "certification",
)


def _as_text(value: Any) -> str:
    """Read plain, secret and Langflow Message values without logging them."""
    if value is None:
        return ""
    if hasattr(value, "get_secret_value"):
        value = value.get_secret_value()
    if hasattr(value, "text"):
        value = value.text
    return str(value or "").strip()


def _fence_untrusted_text(text: str) -> str:
    escaped = text.replace(UNTRUSTED_CHUNK_FENCE_START, "\\" + UNTRUSTED_CHUNK_FENCE_START).replace(
        UNTRUSTED_CHUNK_FENCE_END, "\\" + UNTRUSTED_CHUNK_FENCE_END
    )
    return f"{UNTRUSTED_CHUNK_FENCE_START}\n{escaped}\n{UNTRUSTED_CHUNK_FENCE_END}"


def _retrieval_guard_metadata(
    filters: dict[str, Any], limit: int, score_threshold: float
) -> dict[str, Any]:
    """Expose only a stable effective-filter fingerprint to agent middleware."""
    canonical = json.dumps(
        {
            "filters": filters,
            "limit": limit,
            "scoreThreshold": score_threshold,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        RETRIEVAL_GUARD_METADATA_KEY: {
            "version": 1,
            "filter_fingerprint": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            "scope_policy_id": "documentary-prov-o",
            "scope_policy_version": 1,
        }
    }


def _present_fields(
    value: dict[str, Any],
    fields: tuple[str, ...],
    *,
    keep_null: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Copy present model-facing fields without manufacturing defaults."""
    return {
        field: value[field]
        for field in fields
        if field in value and (field in keep_null or value[field] not in (None, "", [], {}))
    }


def _without_opaque_relation_targets(value: dict[str, Any]) -> dict[str, Any]:
    """Omit relation targets whose DLS visibility is not independently known."""
    projected = {
        field: field_value
        for field, field_value in value.items()
        if field
        not in {
            "source_relation_target_ids",
            "source_relation_roles",
            "scope_context_relations",
        }
    }
    provenance = projected.get("source_provenance")
    if isinstance(provenance, dict):
        projected["source_provenance"] = {
            field: field_value for field, field_value in provenance.items() if field != "relations"
        }
    return projected


def _model_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Project full retrieval output into compact, citable answer evidence.

    The model receives only backend-selected leaf evidence and one readable
    record per document. Source URLs, ACLs, complete provenance and all other
    verified leaves remain in the UI artifact without spending model tokens.
    """
    model_results = payload.get("model_results", payload.get("results", []))
    results = [item for item in model_results if isinstance(item, dict)]
    documents: list[dict[str, Any]] = []
    documents_by_id: dict[str, dict[str, Any]] = {}
    evidence: list[dict[str, Any]] = []

    for item in results:
        evidence.append(_present_fields(item, MODEL_EVIDENCE_FIELDS))
        document_id = _as_text(item.get("document_id"))
        if not document_id:
            continue
        document = documents_by_id.get(document_id)
        if document is None:
            document = _present_fields(item, MODEL_DOCUMENT_FIELDS)
            documents_by_id[document_id] = document
            documents.append(document)
            continue
        for field, value in _present_fields(item, MODEL_DOCUMENT_FIELDS).items():
            document.setdefault(field, value)

    supplied_documents = payload.get("documents")
    if isinstance(supplied_documents, list):
        documents = []
        for item in supplied_documents:
            if not isinstance(item, dict):
                continue
            document = _present_fields(item, MODEL_DOCUMENT_FIELDS)
            for field in ("complete", "error"):
                if field in item and item[field] not in (None, ""):
                    document[field] = item[field]
            documents.append(document)

    compact: dict[str, Any] = {
        "results": evidence,
        "total": len(evidence),
        "documents": documents,
    }
    coverage = payload.get("coverage")
    if isinstance(coverage, dict):
        compact["coverage"] = _present_fields(
            coverage,
            MODEL_COVERAGE_FIELDS,
            keep_null=("next_cursor",),
        )
    discovery = payload.get("discovery")
    if isinstance(discovery, dict):
        compact["discovery"] = discovery
    metadata_agent = payload.get("metadata_agent")
    if isinstance(metadata_agent, dict):
        safe_fields = (
            "status",
            "schema_id",
            "schema_version",
            "interpreted_filters",
            "effective_filters",
            "unsupported_constraints",
            "ambiguous_constraints",
            "eligible_visible_occurrence_count",
            "filter_latency_seconds",
            "calendar_default",
            "truth_semantics",
            "error",
        )
        compact["metadata_agent"] = _present_fields(
            metadata_agent,
            safe_fields,
            keep_null=("error",),
        )
    for field in (
        "error",
        "warnings",
        "retrieval_strategy",
        "requested_retrieval_profile",
        "effective_retrieval_profile",
        "retrieval_execution_complete",
        "retrieval_failure_codes",
    ):
        if field in payload and payload[field] not in (None, ""):
            compact[field] = payload[field]
    return compact


class OpenRAGBackendRetrievalComponent(LCToolComponent):
    """Expose backend-owned Retrieval v2 as the agent's ``search_documents`` tool."""

    display_name = "OpenRAG Retrieval v2"
    description = (
        "Search the OpenRAG knowledge base through the backend-owned Retrieval v2 service. "
        "Use this tool for grounded document questions and cite returned chunk_id values."
    )
    icon = "Search"
    name = "OpenRAGBackendRetrieval"

    inputs = [
        StrInput(
            name="openrag_retrieval_url",
            display_name="OpenRAG Retrieval URL",
            value="OPENRAG_RETRIEVAL_URL",
            required=True,
            load_from_db=True,
            advanced=True,
            info="Internal backend /search endpoint supplied by OpenRAG at request time.",
        ),
        SecretStrInput(
            name="jwt_token",
            display_name="JWT Token",
            value="JWT",
            required=False,
            load_from_db=True,
            info="User JWT supplied by OpenRAG; never log this value.",
        ),
        MultilineInput(
            name="filter_expression",
            display_name="Search Context (JSON)",
            value="OPENRAG_QUERY_FILTER",
            required=False,
            advanced=True,
            info="Backend filters, result limit and score threshold supplied by OpenRAG.",
        ),
        MultilineInput(
            name="metadata_plan",
            display_name="Metadata Plan (JSON)",
            value="OPENRAG_METADATA_PLAN",
            required=False,
            load_from_db=True,
            advanced=True,
            info="Request-scoped deterministic plan supplied by the OpenRAG backend.",
        ),
        IntInput(
            name="number_of_results",
            display_name="Default Result Limit",
            value=10,
            advanced=True,
        ),
    ]

    outputs = [
        Output(
            display_name="Toolset",
            name="component_as_tool",
            method="build_tool",
            types=["Tool"],
            tool_mode=True,
        ),
        Output(
            display_name="Metadata Search Tool",
            name="metadata_search_tool",
            method="build_metadata_tool",
            types=["Tool"],
            tool_mode=True,
        ),
    ]

    def _metadata_plan(self) -> dict[str, Any] | None:
        raw = _as_text(getattr(self, "metadata_plan", ""))
        if not raw or raw == "OPENRAG_METADATA_PLAN":
            return None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("OpenRAG metadata plan is not valid JSON") from exc
        if not isinstance(parsed, dict):
            raise ValueError("OpenRAG metadata plan must be a JSON object")
        supplied_sha = str(parsed.get("plan_sha256") or "")
        canonical_plan = {key: value for key, value in parsed.items() if key != "plan_sha256"}
        canonical = json.dumps(
            canonical_plan,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        calculated_sha = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if supplied_sha != calculated_sha:
            raise ValueError("OpenRAG metadata plan fingerprint mismatch")
        if canonical_plan.get("schema_id") != "openrag.metadata-natural-language-plan":
            raise ValueError("unsupported OpenRAG metadata plan schema")
        if canonical_plan.get("schema_version") != 1:
            raise ValueError("unsupported OpenRAG metadata plan version")
        return canonical_plan

    @staticmethod
    def _planner_result(plan: dict[str, Any] | None, *, normal_tool: bool) -> dict[str, Any] | None:
        if plan is None or not plan.get("metadata_intent_detected"):
            return None
        status = str(plan.get("status") or "INVALID")
        if status == "VALID" and plan.get("requires_metadata_search"):
            if not normal_tool:
                return None
            return {
                "metadata_agent": {
                    "status": "VALID",
                    "error": "METADATA_TOOL_REQUIRED",
                    "ambiguous_constraints": [],
                    "unsupported_constraints": [],
                },
                "results": [],
            }
        return {
            "metadata_agent": {
                "status": status,
                "error": (
                    "Ask the user to clarify the metadata constraint."
                    if status == "AMBIGUOUS"
                    else "The requested metadata constraint is not safely representable."
                    if status == "UNSUPPORTED"
                    else "The metadata plan is invalid."
                ),
                "ambiguous_constraints": plan.get("ambiguities", []),
                "unsupported_constraints": plan.get("unsupported_constraints", []),
            },
            "results": [],
        }

    def _request_context(self) -> tuple[dict[str, Any], int, float]:
        raw = _as_text(getattr(self, "filter_expression", ""))
        if not raw or raw == "OPENRAG_QUERY_FILTER":
            return {}, max(1, int(self.number_of_results or 10)), 0.0
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("OpenRAG retrieval filter context is not valid JSON") from exc
        if not isinstance(parsed, dict):
            raise ValueError("OpenRAG retrieval filter context must be a JSON object")

        filters = parsed.get("filters", {})
        if not isinstance(filters, dict):
            raise ValueError("OpenRAG retrieval filters must be a JSON object")
        limit = parsed.get("limit", self.number_of_results)
        score_threshold = parsed.get("scoreThreshold", parsed.get("score_threshold", 0))
        try:
            limit = max(1, int(limit))
            score_threshold = float(score_threshold)
        except (TypeError, ValueError) as exc:
            raise ValueError("OpenRAG retrieval limit or score threshold is invalid") from exc
        return filters, limit, score_threshold

    def _retrieve_payload(
        self,
        search_query: str,
        *,
        evidence_mode: str = "focused",
        document_id: str = "",
        cursor: str = "",
        batch_size: int = 20,
        multi_query_discovery: bool = False,
        multi_query_max_queries: int = 4,
        multi_query_concurrency: int = 2,
    ) -> dict[str, Any]:
        """Call the backend and retain results plus its coverage certificate."""
        query = _as_text(search_query)
        mode = _as_text(evidence_mode) or "focused"
        if mode not in {"focused", "exhaustive", "scope_exhaustive"}:
            raise ValueError("evidence_mode must be focused, exhaustive, or scope_exhaustive")
        resolved_document_id = _as_text(document_id)
        if mode == "focused" and not query:
            return {"results": []}
        if mode == "exhaustive" and not resolved_document_id:
            raise ValueError("document_id is required in exhaustive mode")

        url = _as_text(getattr(self, "openrag_retrieval_url", ""))
        if not url or url == "OPENRAG_RETRIEVAL_URL":
            raise ValueError("OpenRAG Retrieval URL is not configured")
        jwt = _as_text(getattr(self, "jwt_token", ""))
        headers: dict[str, str] = {}
        if jwt:
            headers["Authorization"] = jwt if jwt.lower().startswith("bearer ") else f"Bearer {jwt}"

        filters, limit, score_threshold = self._request_context()
        request_body: dict[str, Any] = {
            "query": query,
            "filters": filters,
            "limit": limit,
            "scoreThreshold": score_threshold,
            "evidenceMode": mode,
            "documentId": resolved_document_id or None,
            "cursor": _as_text(cursor),
            "batchSize": min(50, max(1, int(batch_size))),
            "responseProfile": "langflow",
        }
        if multi_query_discovery:
            request_body.update(
                {
                    "multiQueryDiscovery": True,
                    "multiQueryMaxQueries": min(4, max(1, int(multi_query_max_queries))),
                    "multiQueryConcurrency": min(4, max(1, int(multi_query_concurrency))),
                }
            )
        with httpx.Client(timeout=300.0) as client:
            response = client.post(
                url,
                headers=headers,
                json=request_body,
            )
            response.raise_for_status()
            payload = response.json()

        if not isinstance(payload, dict):
            raise ValueError("OpenRAG retrieval response must be a JSON object")
        results = payload.get("results", [])
        if not isinstance(results, list):
            raise ValueError("OpenRAG retrieval response has an invalid results field")

        fenced_results: list[dict[str, Any]] = []
        for item in results:
            if not isinstance(item, dict):
                continue
            item = _without_opaque_relation_targets(item)
            item["text"] = _fence_untrusted_text(str(item.get("text") or ""))
            fenced_results.append(item)
        fenced_model_results: list[dict[str, Any]] = []
        for item in payload.get("model_results", []):
            if not isinstance(item, dict):
                continue
            item = _without_opaque_relation_targets(item)
            item["text"] = _fence_untrusted_text(str(item.get("text") or ""))
            fenced_model_results.append(item)
        documents = payload.get("documents")
        if isinstance(documents, list):
            documents = [
                _without_opaque_relation_targets(item)
                for item in documents
                if isinstance(item, dict)
            ]
        return {
            **payload,
            "results": fenced_results,
            **({"model_results": fenced_model_results} if "model_results" in payload else {}),
            **({"documents": documents} if isinstance(documents, list) else {}),
        }

    def search_documents(self, search_query: str) -> list[Data]:
        """Backward-compatible focused search used by component previews."""
        payload = self._retrieve_payload(search_query)
        return [Data(**item) for item in payload["results"]]

    def build_tool(self) -> StructuredTool:
        filters, limit, score_threshold = self._request_context()
        plan = self._metadata_plan()

        def search_documents(
            search_query: str,
            read_document_id: str = "",
            cursor: str = "",
            scope_exhaustive: bool = False,
            multi_query_discovery: bool = False,
        ) -> tuple[str, list[dict[str, Any]]]:
            """Search normally, investigate a dossier, or read one selected document."""
            planner_result = self._planner_result(plan, normal_tool=True)
            if planner_result is not None:
                return json.dumps(planner_result, ensure_ascii=False), []
            resolved_document_id = _as_text(read_document_id)
            if resolved_document_id and scope_exhaustive:
                raise ValueError("read_document_id and scope_exhaustive are mutually exclusive")
            payload = self._retrieve_payload(
                search_query,
                evidence_mode=(
                    "exhaustive"
                    if resolved_document_id
                    else "scope_exhaustive"
                    if scope_exhaustive
                    else "focused"
                ),
                document_id=resolved_document_id,
                cursor=cursor,
                batch_size=50 if resolved_document_id else 20,
                multi_query_discovery=multi_query_discovery,
            )
            artifact = payload["results"]

            # LangChain stores the second tuple element on ToolMessage.artifact.
            # It retains full source-card fields only for the bounded evidence
            # selected by the backend. The compact JSON avoids paying repeatedly
            # for URLs, ACLs and complete PROV-O JSON.
            return json.dumps(_model_payload(payload), ensure_ascii=False), artifact

        return StructuredTool.from_function(
            func=search_documents,
            name="search_documents",
            description=(
                "Search the indexed OpenRAG knowledge base. "
                "Build queries from stable identifiers and established context only; never "
                "add a candidate answer for the attribute being looked up. Use returned "
                "chunk_id values for inline citations. Set scope_exhaustive=true for explicit "
                "requests for all exchanges, all related documents, or a complete chronology; "
                "it performs ranked seed discovery, accessible PROV-O graph closure and verified "
                "full reads, and its coverage decides whether completeness may be claimed. "
                "Otherwise it performs normal ranked archive search. Set read_document_id only when "
                "the human explicitly selected one known document for complete reading; "
                "continue with coverage.next_cursor until complete=true. Never expose an "
                "internal document id as a human-facing scope label: use documents.filename. "
                "Set multi_query_discovery=true for one backend-controlled, bounded decomposition "
                "plan; it is still a single guarded discovery attempt under the same access scope."
            ),
            response_format="content_and_artifact",
            metadata=_retrieval_guard_metadata(filters, limit, score_threshold),
        )

    def build_metadata_tool(self) -> StructuredTool:
        plan = self._metadata_plan()
        filters, context_limit, score_threshold = self._request_context()

        def document_search_with_metadata(
            free_text: str,
            filters: list[MetadataToolFilter],
            limit: int = 10,
        ) -> tuple[str, list[dict[str, Any]]]:
            """Search with an exact, bounded metadata plan validated by OpenRAG."""
            blocked = self._planner_result(plan, normal_tool=False)
            if blocked is not None:
                return json.dumps(blocked, ensure_ascii=False), []
            if plan is None or not plan.get("requires_metadata_search"):
                payload = {
                    "metadata_agent": {
                        "status": "INVALID",
                        "error": "NO_METADATA_CONSTRAINT",
                        "ambiguous_constraints": [],
                        "unsupported_constraints": [],
                    },
                    "results": [],
                }
                return json.dumps(payload, ensure_ascii=False), []

            query = MetadataToolQuery(free_text=free_text, filters=filters, limit=limit)
            supplied_filters = sorted(
                (item.canonical_payload() for item in query.filters),
                key=lambda value: json.dumps(value, sort_keys=True, separators=(",", ":")),
            )
            expected_filters = sorted(
                plan.get("filters") or [],
                key=lambda value: json.dumps(value, sort_keys=True, separators=(",", ":")),
            )
            if query.free_text.strip() != str(plan.get("free_text") or "").strip() or supplied_filters != expected_filters:
                payload = {
                    "metadata_agent": {
                        "status": "INVALID",
                        "error": "AGENT_PLAN_MISMATCH",
                        "ambiguous_constraints": [],
                        "unsupported_constraints": [],
                    },
                    "results": [],
                }
                return json.dumps(payload, ensure_ascii=False), []

            url = _as_text(getattr(self, "openrag_retrieval_url", ""))
            if not url or url == "OPENRAG_RETRIEVAL_URL":
                raise ValueError("OpenRAG Retrieval URL is not configured")
            metadata_url = f"{url.rstrip('/')}/metadata-agent"
            jwt = _as_text(getattr(self, "jwt_token", ""))
            headers: dict[str, str] = {}
            if jwt:
                headers["Authorization"] = jwt if jwt.lower().startswith("bearer ") else f"Bearer {jwt}"
            request_body: dict[str, Any] = {
                "free_text": query.free_text.strip(),
                "filters": supplied_filters,
                "limit": query.limit,
                "schema_id": METADATA_TOOL_SCHEMA_ID,
                "schema_version": METADATA_TOOL_SCHEMA_VERSION,
            }
            with httpx.Client(timeout=300.0) as client:
                response = client.post(metadata_url, headers=headers, json=request_body)
                response.raise_for_status()
                payload = response.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("results", []), list):
                raise ValueError("OpenRAG metadata search response is invalid")
            results: list[dict[str, Any]] = []
            for item in payload.get("results", []):
                if not isinstance(item, dict):
                    continue
                item = _without_opaque_relation_targets(item)
                item["text"] = _fence_untrusted_text(str(item.get("text") or ""))
                results.append(item)
            normalized_payload: dict[str, Any] = {**payload, "results": results}
            return json.dumps(_model_payload(normalized_payload), ensure_ascii=False), results

        return StructuredTool.from_function(
            func=document_search_with_metadata,
            name=METADATA_TOOL_NAME,
            description=(
                "Search documents using explicit OpenRAG metadata constraints. Use it only when "
                "the request contains a technical format, production/modification calendar, "
                "source-system, creator, or other declared metadata constraint. Copy the semantic "
                "free text and strict field/operator/value filters exactly; temporal filters must "
                "state SOURCE_LOCAL (the default natural-language calendar) or UTC. Never invent "
                "a year, identity, source relation, document genre, raw OpenSearch/Lucene query, "
                "range, or unsupported operator. An AMBIGUOUS or UNSUPPORTED result means no search "
                "ran and must be explained or clarified. Metadata matches mean at least one valid "
                "metadata observation matched; they are not unconditional facts about a document."
            ),
            args_schema=MetadataToolQuery,
            response_format="content_and_artifact",
            metadata=_retrieval_guard_metadata(filters, min(context_limit, 20), score_threshold),
        )
