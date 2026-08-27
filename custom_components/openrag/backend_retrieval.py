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
    # Keep the audit decision and its exact PROV-O explanation once per
    # document.  The complete source artifact still remains available to the
    # citation UI; this compact manifest is what lets the answer model explain
    # why an otherwise implicit document belongs to the candidate set.
    "retrieval_relation_paths",
    "retrieval_relevance_decision",
    "retrieval_relevance_reason",
    "retrieval_supporting_document_ids",
)
MODEL_COVERAGE_FIELDS = (
    "mode",
    "requested",
    "scope",
    "complete",
    "document_id",
    "covered_chunks",
    "total_chunks",
    "coverage_ratio",
    "documents_complete",
    "documents_total",
    "next_cursor",
    "error",
    "filename",
)
# These fields are computed during audit discovery rather than persisted in
# OpenSearch. Carry them onto the subsequent full-document reads so the final
# model manifest and source artifact do not lose the proof that selected an
# implicit document.
AUDIT_DOCUMENT_CONTEXT_FIELDS = (
    "retrieval_relation_paths",
    "retrieval_relevance_decision",
    "retrieval_relevance_reason",
    "retrieval_supporting_document_ids",
)
# Compatibility guard for an older backend that has not produced a certified
# hierarchical synthesis. At roughly three characters per token this leaves a
# conservative margin below a 272k context once prompts and answer tokens are
# included. New archive audits never rely on this fallback.
MODEL_RAW_EVIDENCE_CHARACTER_BUDGET = 600_000


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
    audit_synthesis = payload.get("audit_synthesis")
    synthesis_certified = (
        isinstance(audit_synthesis, dict)
        and audit_synthesis.get("complete") is True
        and audit_synthesis.get("verified") is True
    )
    synthesis_failed = (
        isinstance(audit_synthesis, dict) and not synthesis_certified
    )
    raw_characters = sum(len(str(item.get("text") or "")) for item in results)
    include_raw_evidence = (
        not synthesis_certified
        and not synthesis_failed
        and raw_characters <= MODEL_RAW_EVIDENCE_CHARACTER_BUDGET
    )
    documents: list[dict[str, Any]] = []
    documents_by_id: dict[str, dict[str, Any]] = {}
    evidence: list[dict[str, Any]] = []

    for item in results:
        if include_raw_evidence:
            evidence.append(_present_fields(item, MODEL_EVIDENCE_FIELDS))
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
    if isinstance(audit_synthesis, dict):
        compact["audit_synthesis"] = audit_synthesis
    if not include_raw_evidence and not synthesis_certified:
        compact["error"] = (
            "Hierarchical audit synthesis is incomplete; raw evidence was withheld to prevent "
            "an uncertified or over-context answer."
        )
        compact["raw_evidence_omitted"] = True
        compact["raw_evidence_characters"] = raw_characters
    coverage = payload.get("coverage")
    if isinstance(coverage, dict):
        compact_coverage = _present_fields(
            coverage, MODEL_COVERAGE_FIELDS, keep_null=("next_cursor",)
        )
        nested = coverage.get("documents")
        if isinstance(nested, list):
            compact_coverage["documents"] = [
                _present_fields(item, MODEL_COVERAGE_FIELDS, keep_null=("next_cursor",))
                for item in nested
                if isinstance(item, dict)
            ]
        compact["coverage"] = compact_coverage
    discovery = payload.get("discovery")
    if isinstance(discovery, dict):
        compact["discovery"] = {
            key: discovery[key]
            for key in (
                "mode",
                "documents_found",
                "chunks_returned",
                "lanes",
                "truncated",
                "lexical_completeness_certified",
                "semantic_completeness_certified",
                "provenance_completeness_certified",
                "contextual_review_complete",
                "query_expansion",
                "contextual_review",
                "hierarchical_synthesis",
            )
            if key in discovery
        }
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
                "Backend filters, result limit, score threshold and trusted retrieval intent "
                "supplied by OpenRAG through a request-scoped Langflow global variable."
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

    def _request_context(self) -> tuple[dict[str, Any], int, float, str, str]:
        raw = _as_text(getattr(self, "filter_expression", ""))
        if not raw or raw == "OPENRAG_QUERY_FILTER":
            return {}, max(1, int(self.number_of_results or 10)), 0.0, "focused", ""
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
        retrieval_intent = _as_text(parsed.get("retrievalIntent", "focused")).lower()
        if retrieval_intent not in {"focused", "exhaustive"}:
            raise ValueError("OpenRAG retrieval intent must be focused or exhaustive")
        audit_progress_id = _as_text(parsed.get("auditProgressId", ""))
        if len(audit_progress_id) > 64 or (
            audit_progress_id and not audit_progress_id.replace("-", "").isalnum()
        ):
            raise ValueError("OpenRAG audit progress id is invalid")
        return filters, limit, score_threshold, retrieval_intent, audit_progress_id

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

    def _start_required_exhaustive_reads(
        self,
        client: httpx.Client,
        *,
        url: str,
        headers: dict[str, str],
        query: str,
        filters: dict[str, Any],
        limit: int,
        score_threshold: float,
        focused_payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Turn explicit exhaustive intent into real document evidence reads.

        Focused discovery is allowed to identify candidate documents, but its
        chunks are never returned as if they were exhaustive evidence.  The
        tool follows every authenticated cursor for every discovered document
        itself. Completeness is therefore an execution invariant rather than a
        discretionary sequence of extra tool calls left to the language model.
        """
        if isinstance(focused_payload.get("audit_synthesis"), dict):
            # Retrieval v15 performs full reads and hierarchical synthesis in
            # the authenticated backend. Re-reading here would duplicate every
            # chunk and discard the backend's exact coverage snapshot.
            return focused_payload
        discovered: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in focused_payload.get("results", []):
            if not isinstance(item, dict):
                continue
            document_id = _as_text(item.get("document_id"))
            if not document_id or document_id in seen:
                continue
            seen.add(document_id)
            discovered.append(
                {
                    "document_id": document_id,
                    "filename": _as_text(item.get("filename")),
                    **_present_fields(item, AUDIT_DOCUMENT_CONTEXT_FIELDS),
                }
            )

        exhaustive_results: list[dict[str, Any]] = []
        document_coverages: list[dict[str, Any]] = []
        for document in discovered:
            cursor = ""
            seen_cursors: set[str] = set()
            final_coverage: dict[str, Any] = {}
            while True:
                response = client.post(
                    url,
                    headers=headers,
                    json={
                        "query": query,
                        "filters": filters,
                        "limit": limit,
                        "scoreThreshold": score_threshold,
                        "evidenceMode": "exhaustive",
                        "documentId": document["document_id"],
                        "cursor": cursor,
                        "batchSize": 50,
                    },
                )
                payload = self._validated_payload(response)
                for item in payload.get("results", []):
                    if not isinstance(item, dict):
                        continue
                    enriched = dict(item)
                    for field in AUDIT_DOCUMENT_CONTEXT_FIELDS:
                        if field in document:
                            enriched[field] = document[field]
                    exhaustive_results.append(enriched)
                coverage = payload.get("coverage")
                if not isinstance(coverage, dict):
                    final_coverage = {
                        "mode": "exhaustive",
                        "document_id": document["document_id"],
                        "complete": False,
                        "error": payload.get("error") or "missing coverage certificate",
                    }
                    break
                final_coverage = dict(coverage)
                if coverage.get("complete") is True:
                    break
                next_cursor = _as_text(coverage.get("next_cursor"))
                if not next_cursor or next_cursor in seen_cursors:
                    final_coverage["complete"] = False
                    final_coverage["error"] = (
                        "incomplete coverage returned no fresh continuation cursor"
                    )
                    break
                seen_cursors.add(next_cursor)
                cursor = next_cursor
            document_coverages.append({**final_coverage, "filename": document["filename"]})

        documents_complete = sum(
            coverage.get("complete") is True for coverage in document_coverages
        )
        backend_discovery = focused_payload.get("discovery")
        if not isinstance(backend_discovery, dict):
            backend_discovery = {"mode": "focused"}
        discovery_mode = _as_text(backend_discovery.get("mode")) or "focused"
        return {
            "results": exhaustive_results,
            "total": len(exhaustive_results),
            "discovery": {
                **backend_discovery,
                "mode": discovery_mode,
                "document_ids": [document["document_id"] for document in discovered],
                "documents_found": len(discovered),
            },
            "coverage": {
                "mode": "exhaustive",
                "requested": True,
                # This certificate deliberately names the actual candidate
                # scope. It must never be presented as whole-corpus coverage.
                "scope": (
                    "archive_audit_candidates"
                    if discovery_mode == "archive_audit"
                    else "focused_discovery_documents"
                ),
                "complete": bool(document_coverages)
                and documents_complete == len(document_coverages),
                "documents_complete": documents_complete,
                "documents_total": len(document_coverages),
                "documents": document_coverages,
            },
        }

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

        filters, limit, score_threshold, retrieval_intent, audit_progress_id = (
            self._request_context()
        )
        # Only trusted request context can enable the deeper archive-audit
        # discovery path. The public tool argument remains focused/exhaustive,
        # so a model cannot independently widen its authenticated search scope.
        backend_mode = "audit" if mode == "focused" and retrieval_intent == "exhaustive" else mode
        # Archive audits may page an entire lexical result set and then read
        # every candidate document. Keep a short connection timeout while
        # allowing slow, evidence-complete responses from Raspberry Pi nodes.
        with httpx.Client(timeout=httpx.Timeout(2_400.0, connect=10.0)) as client:
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
                    "progressId": audit_progress_id or None,
                },
            )
            payload = self._validated_payload(response)
            if mode == "focused" and retrieval_intent == "exhaustive":
                payload = self._start_required_exhaustive_reads(
                    client,
                    url=url,
                    headers=headers,
                    query=query,
                    filters=filters,
                    limit=limit,
                    score_threshold=score_threshold,
                    focused_payload=payload,
                )

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
                "ranked discovery. Explicit exhaustive/list-all/compare/audit intent is "
                "marked by the backend: a deep document-diverse OpenSearch audit discovery "
                "then automatically follows every authenticated cursor for every discovered "
                "document. Do not repeat those "
                "reads when coverage.complete=true. Never answer an "
                "explicit exhaustive request from focused results alone. Neither "
                "scope='focused_discovery_documents' nor scope='archive_audit_candidates' "
                "proves semantic completeness for the whole corpus."
            ),
            response_format="content_and_artifact",
        )
