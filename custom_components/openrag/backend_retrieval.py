"""Thin Langflow tool that delegates document search to the OpenRAG backend.

The backend owns query construction, ACL-scoped OpenSearch access, RRF,
diversity, reranking and provenance.  This component deliberately contains no
OpenSearch query logic so the chat agent cannot drift from ``SearchService``.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from langchain_core.tools import StructuredTool
from lfx.base.langchain_utilities.model import LCToolComponent
from lfx.io import IntInput, MultilineInput, Output, SecretStrInput, StrInput
from lfx.schema.data import Data

UNTRUSTED_CHUNK_FENCE_START = "<<<UNTRUSTED_DOC_CHUNK>>>"
UNTRUSTED_CHUNK_FENCE_END = "<<<END_UNTRUSTED_DOC_CHUNK>>>"

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
    "source_relation_target_ids",
    "source_relation_roles",
    "source_relative_path",
    "source_path_ancestors",
    "generated_at_time",
    "scope_context_relations",
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
    "scope_policy_id",
    "scope_policy_version",
    "graph_entities_visited",
    "graph_frontier_empty",
    "graph_limit_reached",
    "graph_stop_reason",
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
    for field in ("error", "warning", "retrieval_strategy"):
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
        )
    ]

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
        with httpx.Client(timeout=300.0) as client:
            response = client.post(
                url,
                headers=headers,
                json={
                    "query": query,
                    "filters": filters,
                    "limit": limit,
                    "scoreThreshold": score_threshold,
                    "evidenceMode": mode,
                    "documentId": resolved_document_id or None,
                    "cursor": _as_text(cursor),
                    "batchSize": min(50, max(1, int(batch_size))),
                    "responseProfile": "langflow",
                },
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
            item = dict(item)
            item["text"] = _fence_untrusted_text(str(item.get("text") or ""))
            fenced_results.append(item)
        fenced_model_results: list[dict[str, Any]] = []
        for item in payload.get("model_results", []):
            if not isinstance(item, dict):
                continue
            item = dict(item)
            item["text"] = _fence_untrusted_text(str(item.get("text") or ""))
            fenced_model_results.append(item)
        return {
            **payload,
            "results": fenced_results,
            **({"model_results": fenced_model_results} if "model_results" in payload else {}),
        }

    def search_documents(self, search_query: str) -> list[Data]:
        """Backward-compatible focused search used by component previews."""
        payload = self._retrieve_payload(search_query)
        return [Data(**item) for item in payload["results"]]

    def build_tool(self) -> StructuredTool:
        def search_documents(
            search_query: str,
            read_document_id: str = "",
            cursor: str = "",
            scope_exhaustive: bool = False,
        ) -> tuple[str, list[dict[str, Any]]]:
            """Search normally, investigate a dossier, or read one selected document."""
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
                "internal document id as a human-facing scope label: use documents.filename."
            ),
            response_format="content_and_artifact",
        )
