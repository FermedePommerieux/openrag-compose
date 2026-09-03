import asyncio
import random
import re
from typing import Any

import httpx

from utils.logging_config import get_logger

logger = get_logger(__name__)

_UNTRUSTED_FENCE_START = "<<<UNTRUSTED_DOC_CHUNK>>>"
_UNTRUSTED_FENCE_END = "<<<END_UNTRUSTED_DOC_CHUNK>>>"
_RETRIEVAL_TOOL_NAMES = frozenset(
    {
        "search_documents",
        "document_search_with_metadata",
    }
)
_SOURCE_CITATION_PATTERN = re.compile(
    r"\(Source:\s*(?:`([A-Za-z0-9][A-Za-z0-9_.:-]{0,511})`|"
    r"([A-Za-z0-9][A-Za-z0-9_.:-]{0,511}))\s*\)"
)


def extract_source_citation_ids(text: str, *, maximum: int = 100) -> list[str]:
    """Extract unique exact chunk identifiers from model citations.

    The agent contract requires ``(Source: <chunk_id>)``. A model may wrap the
    identifier in one balanced pair of Markdown code backticks; this is a
    presentation-only variation, so accept it without weakening identifier
    validation. Deliberately reject bare identifiers, unbalanced Markdown and
    whitespace-bearing labels such as filenames: non-streaming provenance is
    hydrated by exact immutable identifiers, never by fuzzy text or filename
    lookup. The bound prevents an untrusted response from causing an unbounded
    OpenSearch terms query.
    """
    if not isinstance(text, str) or not text or maximum < 1:
        return []
    citations: list[str] = []
    seen: set[str] = set()
    for match in _SOURCE_CITATION_PATTERN.finditer(text):
        chunk_id = match.group(1) or match.group(2)
        if chunk_id in seen:
            continue
        seen.add(chunk_id)
        citations.append(chunk_id)
        if len(citations) >= maximum:
            break
    return citations


class LangflowNotReadyError(Exception):
    """Raised when Langflow fails to become ready within the retry limit."""


async def wait_for_langflow(
    langflow_http_client: httpx.AsyncClient | None = None,
    max_retries: int = 10,
    base_delay: float = 1.0,
    max_delay: float = 20.0,
) -> None:
    """Wait for Langflow to be ready with exponential backoff and jitter.

    Args:
        langflow_http_client: The httpx client to use for health checks. If None,
            falls back to clients.langflow_http_client from config.settings.
        max_retries: Maximum number of retry attempts.
        base_delay: Initial delay in seconds before the first retry.
        max_delay: Upper bound in seconds for the retry delay.

    Raises:
        LangflowNotReadyError: If Langflow fails to become ready within the retry limit.
    """
    if langflow_http_client is None:
        from config.settings import clients

        langflow_http_client = clients.langflow_http_client

    for attempt in range(max_retries):
        display_attempt: int = attempt + 1

        logger.info(
            "Verifying whether the Langflow service is ready...",
            attempt=display_attempt,
            max_retries=max_retries,
        )

        try:
            response = await langflow_http_client.get("/health", timeout=5.0)
            status_code = response.status_code

            if status_code == 200:
                logger.info(
                    "Successfully verified that the Langflow service is ready.",
                    attempt=display_attempt,
                    max_retries=max_retries,
                    status_code=status_code,
                )
                return
            else:
                logger.warning(
                    "The Langflow service is not ready. Encountered a non-200 HTTP status_code.",
                    attempt=display_attempt,
                    max_retries=max_retries,
                    status_code=status_code,
                )
        except Exception as e:
            logger.warning(
                "The Langflow service is not ready.",
                attempt=display_attempt,
                max_retries=max_retries,
                error=str(e),
            )

        if attempt < max_retries - 1:
            delay = min(base_delay * (2**attempt), max_delay)
            delay = random.uniform(delay / 2, delay)

            logger.debug(
                "Retry the Langflow service readiness check after a delay (seconds).",
                attempt=display_attempt,
                max_retries=max_retries,
                delay=delay,
            )

            await asyncio.sleep(delay)

    message: str = "Failed to verify whether the Langflow service is ready."
    logger.error(message)
    raise LangflowNotReadyError(message)


def fence_untrusted_text(text: str) -> str:
    """Wrap text (e.g. an uploaded document body) in untrusted-data fence markers.

    Mirrors flows/components/opensearch_multimodal.py::fence_untrusted_text for the
    non-Langflow (direct chat / upload-context) paths so the same system-prompt rule
    applies regardless of which path fed the content into the conversation.

    Any literal fence markers already present in `text` are escaped first, so a
    poisoned document can't embed a fake end-of-fence marker to break out of the
    untrusted section and have its continuation misread as trusted.
    """
    if not text:
        return text
    escaped = text.replace(_UNTRUSTED_FENCE_START, "\\" + _UNTRUSTED_FENCE_START).replace(
        _UNTRUSTED_FENCE_END, "\\" + _UNTRUSTED_FENCE_END
    )
    return f"{_UNTRUSTED_FENCE_START}\n{escaped}\n{_UNTRUSTED_FENCE_END}"


def strip_untrusted_fence(text: str) -> str:
    """Remove the untrusted-data fence markers before surfacing retrieved text as a citation.

    The retrieval tool wraps chunk text in these markers (see
    flows/components/opensearch_multimodal.py::fence_untrusted_text) so the LLM
    treats it as data, not instructions. Users should never see the raw markers.
    """
    if not text:
        return text
    stripped = text
    if stripped.startswith(_UNTRUSTED_FENCE_START):
        stripped = stripped[len(_UNTRUSTED_FENCE_START) :].lstrip("\n")
    if stripped.endswith(_UNTRUSTED_FENCE_END):
        stripped = stripped[: -len(_UNTRUSTED_FENCE_END)].rstrip("\n")
    # Restore any embedded fence-marker literals that fence_untrusted_text escaped,
    # so citations show the document's original text, not the escaped form.
    stripped = stripped.replace("\\" + _UNTRUSTED_FENCE_START, _UNTRUSTED_FENCE_START)
    stripped = stripped.replace("\\" + _UNTRUSTED_FENCE_END, _UNTRUSTED_FENCE_END)
    return stripped


def strip_untrusted_fence_recursive(obj: Any) -> None:
    """Recursively strip untrusted-fence markers from every "text" string field
    found anywhere in a streamed SSE chunk payload, in place (VULN-13906).

    The live chat UI always streams (stream=True), which bypasses the
    citation-building fence-stripping in async_langflow_chat entirely — that
    non-streaming path is dead code for real traffic. Client code
    (frontend tool-call trace panel, citation click-through popup) reads
    retrieved chunk text straight out of the raw SSE stream, so stripping has
    to happen here, on every chunk, before it's yielded. A no-op on any text
    that isn't actually fenced, so this is safe to apply unconditionally.
    """
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "text" and isinstance(value, str):
                obj[key] = strip_untrusted_fence(value)
            else:
                strip_untrusted_fence_recursive(value)
    elif isinstance(obj, list):
        for item in obj:
            strip_untrusted_fence_recursive(item)


def normalize_retrieval_tool_event(chunk_data: Any) -> None:
    """Expose a retrieval ToolMessage artifact as native frontend results.

    Depending on the SDK and authentication event path, Langflow exposes
    ``item.results`` either as a ToolMessage dict or with up to three observed
    JSON encoding layers around that dict. Retrieval v2 deliberately returns
    ``content_and_artifact``; the JSON content is model-facing while the
    artifact is the stable source contract. Promote that artifact at the
    backend SSE boundary so the frontend receives ``results: list[chunk]``.
    Decoding is bounded to four layers and accepts only a final JSON object; legacy
    Python repr strings remain untouched.
    """
    if not isinstance(chunk_data, dict) or chunk_data.get("type") != "response.output_item.done":
        return
    item = chunk_data.get("item")
    if not isinstance(item, dict) or item.get("type") != "tool_call":
        return
    tool_name = item.get("tool_name") or item.get("name")
    if tool_name is not None and tool_name not in _RETRIEVAL_TOOL_NAMES:
        return
    results = item.get("results")
    for _ in range(4):
        if not isinstance(results, str):
            break
        import json

        try:
            results = json.loads(results)
        except (json.JSONDecodeError, TypeError):
            return
    if not isinstance(results, dict):
        return
    artifact = results.get("artifact")
    if isinstance(artifact, list):
        item["results"] = artifact


def parse_knowledge_chunks(results: Any) -> list[dict]:
    """Parse and standardize knowledge chunks from Langflow output formats."""
    import json

    if not results:
        return []

    items = []
    if hasattr(results, "model_dump"):
        results = results.model_dump()

    if isinstance(results, dict):
        if "artifact" in results and isinstance(results["artifact"], list):
            items = results["artifact"]
        elif "content" in results and isinstance(results["content"], str):
            try:
                items = json.loads(results["content"])
            except Exception as e:
                logger.warning(
                    f"Failed to parse content JSON in parse_knowledge_chunks: {e}",
                    content_preview=str(results["content"])[:200],
                )
        else:
            items = [results]
    elif isinstance(results, list):
        items = results
    elif isinstance(results, str):
        try:
            items = json.loads(results)
        except Exception as e:
            logger.warning(
                f"Failed to parse raw string JSON in parse_knowledge_chunks: {e}",
                raw_preview=str(results)[:200],
            )

    if not isinstance(items, list):
        return []

    parsed_chunks = []
    for item in items:
        if hasattr(item, "model_dump"):
            item = item.model_dump()

        data = item.get("data") if isinstance(item, dict) and "data" in item else item

        if isinstance(data, dict) and ("text" in data or "filename" in data or "chunk_id" in data):
            chunk_id = data.get("chunk_id") or data.get("id") or ""
            parsed_chunk = {
                "filename": data.get("filename", ""),
                "text": strip_untrusted_fence(data.get("text", "")),
                "score": data.get("score", 0),
                "page": data.get("page"),
                "mimetype": data.get("mimetype"),
                "chunk_id": chunk_id,
                "id": chunk_id,
                "embedding_model": data.get("embedding_model"),
                "parser": data.get("parser"),
                "chunk_size": data.get("chunk_size"),
                "chunk_overlap": data.get("chunk_overlap"),
            }
            if "source_url" in data:
                parsed_chunk["source_url"] = data.get("source_url")
            # Retrieval v2 owns these identity fields.  Keep them intact
            # through the Langflow response parser so citations can link a
            # result back to the indexed document and the exact chunk.  Do not
            # add absent keys: normalized legacy payloads remain idempotent.
            for provenance_field in (
                "document_id",
                "connector_file_id",
                "chunk_index",
                "chunking_strategy",
                "source_relative_path",
                "source_path_ancestors",
            ):
                if provenance_field in data:
                    parsed_chunk[provenance_field] = data.get(provenance_field)
            parsed_chunks.append(parsed_chunk)

    return parsed_chunks
