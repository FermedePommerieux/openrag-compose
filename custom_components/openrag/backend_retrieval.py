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
    escaped = text.replace(
        UNTRUSTED_CHUNK_FENCE_START, "\\" + UNTRUSTED_CHUNK_FENCE_START
    ).replace(UNTRUSTED_CHUNK_FENCE_END, "\\" + UNTRUSTED_CHUNK_FENCE_END)
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

    def search_documents(self, search_query: str) -> list[Data]:
        """Call the authenticated backend endpoint and preserve every provenance field."""
        query = _as_text(search_query)
        if not query:
            return []

        url = _as_text(getattr(self, "openrag_retrieval_url", ""))
        if not url or url == "OPENRAG_RETRIEVAL_URL":
            raise ValueError("OpenRAG Retrieval URL is not configured")
        jwt = _as_text(getattr(self, "jwt_token", ""))
        headers: dict[str, str] = {}
        if jwt:
            headers["Authorization"] = jwt if jwt.lower().startswith("bearer ") else f"Bearer {jwt}"

        filters, limit, score_threshold = self._request_context()
        with httpx.Client(timeout=30.0) as client:
            response = client.post(
                url,
                headers=headers,
                json={
                    "query": query,
                    "filters": filters,
                    "limit": limit,
                    "scoreThreshold": score_threshold,
                },
            )
            response.raise_for_status()
            payload = response.json()

        results = payload.get("results", []) if isinstance(payload, dict) else []
        if not isinstance(results, list):
            raise ValueError("OpenRAG retrieval response has an invalid results field")

        data: list[Data] = []
        for item in results:
            if not isinstance(item, dict):
                continue
            item = dict(item)
            item["text"] = _fence_untrusted_text(str(item.get("text") or ""))
            data.append(Data(**item))
        return data

    def build_tool(self) -> StructuredTool:
        def search_documents(search_query: str) -> list[Data]:
            return self.search_documents(search_query)

        return StructuredTool.from_function(
            func=search_documents,
            name="search_documents",
            description=(
                "Search the indexed OpenRAG knowledge base. "
                "Use returned chunk_id values for inline citations."
            ),
        )
