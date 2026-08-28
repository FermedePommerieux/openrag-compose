"""Thin Langflow tool that delegates document search to the OpenRAG backend.

The backend owns query construction, ACL-scoped OpenSearch access, consensus
fusion, diversity, reranking and provenance. This component deliberately
contains no OpenSearch query logic so chat cannot drift from ``SearchService``.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from langchain_core.tools import StructuredTool
from lfx.base.langchain_utilities.model import LCToolComponent
from lfx.io import IntInput, Output, SecretStrInput, StrInput
from lfx.schema.data import Data

UNTRUSTED_CHUNK_FENCE_START = "<<<UNTRUSTED_DOC_CHUNK>>>"
UNTRUSTED_CHUNK_FENCE_END = "<<<END_UNTRUSTED_DOC_CHUNK>>>"

# The artifact returned by LangChain is consumed by OpenRAG's source UI and
# deliberately retains the complete backend result.  Tool ``content`` is sent
# to the language model, so repeating ACLs, PROV-O JSON, source URLs and the
# ingestion profile on every chunk wastes context without adding evidence.
# Keep this projection explicit: adding a backend field must never silently
# increase model-token use.
MODEL_EVIDENCE_FIELDS = (
    "chunk_id",
    "document_id",
    "page",
    "chunk_index",
    "evidence_order",
    "score",
    "retrieval_plane",
    "retrieval_relation_depth",
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
    "source_relation_predicates",
    "source_relation_roles",
    "retrieval_relation_paths",
    "retrieval_plane",
    "retrieval_relation_depth",
    "retrieval_channels",
    "retrieval_relevance",
)
MODEL_COVERAGE_FIELDS = (
    "mode",
    "complete",
    "document_id",
    "covered_chunks",
    "total_chunks",
    "coverage_ratio",
    "next_cursor",
    "error",
)
MODEL_PROVENANCE_CONTEXT_EXCERPT_CHARACTERS = 800
FOCUSED_BACKEND_TIMEOUT_SECONDS = 2_400.0


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
    """Copy only present, non-empty fields into a model-facing projection."""
    return {
        field: value[field]
        for field in fields
        if field in value and (field in keep_null or value[field] not in (None, "", [], {}))
    }


def _model_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Build compact evidence content while preserving the source artifact.

    OpenSearch stores provenance and document profiling on every chunk so any
    independently retrieved hit is self-describing.  A model tool message has
    different economics: document metadata belongs once in a manifest and
    chunk rows need only stable citation identity, location and text.  This
    lossless split for answer evidence avoids the production failure where 237
    modest chunks expanded to more than 512k input tokens.
    """
    results = [item for item in payload.get("results", []) if isinstance(item, dict)]
    is_provenance_search = isinstance(payload.get("retrieval_planes"), dict)
    documents: list[dict[str, Any]] = []
    documents_by_id: dict[str, dict[str, Any]] = {}
    evidence: list[dict[str, Any]] = []

    for item in results:
        projected_evidence = _present_fields(item, MODEL_EVIDENCE_FIELDS)
        if (
            is_provenance_search
            and projected_evidence.get("retrieval_plane") == "context"
            and "text" in projected_evidence
        ):
            projected_evidence["text"] = str(projected_evidence["text"])[
                :MODEL_PROVENANCE_CONTEXT_EXCERPT_CHARACTERS
            ]
        evidence.append(projected_evidence)
        document_id = _as_text(item.get("document_id"))
        if not document_id:
            continue
        projected = documents_by_id.get(document_id)
        if projected is None:
            projected = _present_fields(item, MODEL_DOCUMENT_FIELDS)
            documents_by_id[document_id] = projected
            documents.append(projected)
            continue
        # A field can be absent from the highest-ranked chunk of a legacy
        # document but present later. Fill gaps without repeating metadata.
        for field, value in _present_fields(item, MODEL_DOCUMENT_FIELDS).items():
            projected.setdefault(field, value)

    compact: dict[str, Any] = {
        "results": evidence,
        "total": len(evidence),
        "documents": documents,
        "evidence_chunks_available": len(results),
    }
    for field in (
        "retrieval_fusion",
        "document_graph",
        "provenance_retrieval",
        "retrieval_planes",
        "noise_accounting",
    ):
        value = payload.get(field)
        if isinstance(value, dict):
            compact[field] = value
    coverage = payload.get("coverage")
    if isinstance(coverage, dict):
        compact_coverage = _present_fields(
            coverage, MODEL_COVERAGE_FIELDS, keep_null=("next_cursor",)
        )
        compact["coverage"] = compact_coverage
    for field in ("error", "warning", "retrieval_strategy", "retrieval_mode"):
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
        StrInput(
            name="filter_expression",
            display_name="Search Context (JSON)",
            value="OPENRAG_QUERY_FILTER",
            required=False,
            load_from_db=True,
            advanced=True,
            info=(
                "Backend filters, result limit and score threshold supplied by OpenRAG "
                "through a request-scoped Langflow global variable."
            ),
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

    def _request_context(self) -> tuple[dict[str, Any], int, float, str]:
        raw = _as_text(getattr(self, "filter_expression", ""))
        if not raw or raw == "OPENRAG_QUERY_FILTER":
            return {}, max(1, int(self.number_of_results or 10)), 0.0, ""
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
        progress_id = _as_text(parsed.get("progressId"))
        if len(progress_id) > 64 or (progress_id and not progress_id.replace("-", "").isalnum()):
            raise ValueError("OpenRAG retrieval progress id is invalid")
        return filters, limit, score_threshold, progress_id

    @staticmethod
    def _validated_payload(response: httpx.Response) -> dict[str, Any]:
        """Validate one backend response before it enters the agent context."""
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("OpenRAG retrieval response must be a JSON object")
        if not isinstance(payload.get("results", []), list):
            raise ValueError("OpenRAG retrieval response has an invalid results field")
        return payload

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
        if mode not in {"focused", "exhaustive"}:
            raise ValueError("evidence_mode must be focused or exhaustive")
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

        filters, limit, score_threshold, progress_id = self._request_context()
        # PROV-O expansion is part of ordinary focused retrieval. A chat-level
        # "exhaustive" intent no longer switches to a separate archive mode;
        # exhaustive remains only the explicit, single-document cursor API.
        backend_mode = mode
        with httpx.Client(
            timeout=httpx.Timeout(FOCUSED_BACKEND_TIMEOUT_SECONDS, connect=10.0)
        ) as client:
            response = client.post(
                url,
                headers=headers,
                json={
                    "query": query,
                    "filters": filters,
                    "limit": limit,
                    "scoreThreshold": score_threshold,
                    "evidenceMode": backend_mode,
                    "documentId": resolved_document_id or None,
                    "cursor": _as_text(cursor),
                    "batchSize": min(50, max(1, int(batch_size))),
                    "progressId": progress_id or None,
                },
            )
            payload = self._validated_payload(response)

        fenced_results: list[dict[str, Any]] = []
        for item in payload.get("results", []):
            if not isinstance(item, dict):
                continue
            item = dict(item)
            item["text"] = _fence_untrusted_text(str(item.get("text") or ""))
            fenced_results.append(item)
        return {**payload, "results": fenced_results}

    def search_documents(self, search_query: str) -> list[Data]:
        """Backward-compatible focused search used by component previews."""
        payload = self._retrieve_payload(search_query)
        return [Data(**item) for item in payload["results"]]

    def build_tool(self) -> StructuredTool:
        def search_documents(
            search_query: str,
            evidence_mode: str = "focused",
            document_id: str = "",
            cursor: str = "",
            batch_size: int = 20,
        ) -> tuple[str, list[dict[str, Any]]]:
            payload = self._retrieve_payload(
                search_query,
                evidence_mode=evidence_mode,
                document_id=document_id,
                cursor=cursor,
                batch_size=batch_size,
            )
            artifact = payload["results"]

            # LangChain stores the second tuple element on ToolMessage.artifact.
            # It retains full source/provenance data for OpenRAG's UI. Only the
            # compact projection enters the model context.
            return json.dumps(_model_payload(payload), ensure_ascii=False), artifact

        return StructuredTool.from_function(
            func=search_documents,
            name="search_documents",
            description=(
                "Search the indexed OpenRAG knowledge base. "
                "Build queries from stable identifiers and established context only; never "
                "add a candidate answer for the attribute being looked up. Use returned "
                "chunk_id values for inline citations. Use evidence_mode='focused' for "
                "ranked discovery: OpenSearch follows high-signal PROV-O links and supplies "
                "a deterministic document graph for human review. It never validates or "
                "excludes documents with an LLM. Use evidence_mode='exhaustive' only to read "
                "one explicitly selected document completely."
            ),
            response_format="content_and_artifact",
        )
