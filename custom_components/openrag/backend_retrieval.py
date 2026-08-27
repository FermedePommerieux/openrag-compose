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

    def _request_context(self) -> tuple[dict[str, Any], int, float, str]:
        raw = _as_text(getattr(self, "filter_expression", ""))
        if not raw or raw == "OPENRAG_QUERY_FILTER":
            return {}, max(1, int(self.number_of_results or 10)), 0.0, "focused"
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
        return filters, limit, score_threshold, retrieval_intent

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
        discovered: list[dict[str, str]] = []
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
                exhaustive_results.extend(
                    item for item in payload.get("results", []) if isinstance(item, dict)
                )
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
            document_coverages.append(
                {**final_coverage, "filename": document["filename"]}
            )

        documents_complete = sum(
            coverage.get("complete") is True for coverage in document_coverages
        )
        return {
            "results": exhaustive_results,
            "total": len(exhaustive_results),
            "discovery": {
                "mode": "focused",
                "document_ids": [document["document_id"] for document in discovered],
                "documents_found": len(discovered),
            },
            "coverage": {
                "mode": "exhaustive",
                "requested": True,
                # This certificate deliberately names the actual candidate
                # scope. It must never be presented as whole-corpus coverage.
                "scope": "focused_discovery_documents",
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

        filters, limit, score_threshold, retrieval_intent = self._request_context()
        with httpx.Client(timeout=30.0) as client:
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
            # JSON content remains useful to the model, while the native artifact
            # survives Langflow/OpenAI transport without relying on Data.__repr__.
            return json.dumps(payload, ensure_ascii=False), artifact

        return StructuredTool.from_function(
            func=search_documents,
            name="search_documents",
            description=(
                "Search the indexed OpenRAG knowledge base. "
                "Build queries from stable identifiers and established context only; never "
                "add a candidate answer for the attribute being looked up. Use returned "
                "chunk_id values for inline citations. Use evidence_mode='focused' for "
                "ranked discovery. Explicit exhaustive/list-all/compare/audit intent is "
                "marked by the backend: a focused call then automatically follows every "
                "authenticated cursor for every discovered document. Do not repeat those "
                "reads when coverage.complete=true. Never answer an "
                "explicit exhaustive request from focused results alone and never claim "
                "whole-corpus coverage for scope='focused_discovery_documents'."
            ),
            response_format="content_and_artifact",
        )
