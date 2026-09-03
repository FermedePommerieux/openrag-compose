import json
from types import SimpleNamespace

import pytest

import agent
from utils.langflow_utils import normalize_retrieval_tool_event, strip_untrusted_fence_recursive


@pytest.mark.asyncio
async def test_async_langflow_chat_extracts_sources_from_structured_tool_results(monkeypatch):
    response = SimpleNamespace(
        output=[
            SimpleNamespace(
                results=[
                    {
                        "text": "purple elephants dancing",
                        "filename": "sdk_test_doc.md",
                        "mimetype": "text/markdown",
                        "page": 0,
                    }
                ]
            )
        ]
    )

    async def fake_async_response(*_args, **_kwargs):
        return "answer", "response-id", response

    monkeypatch.setattr(agent, "async_response", fake_async_response)

    response_text, response_id, sources = await agent.async_langflow_chat(
        object(),
        "flow-id",
        "prompt",
        "user-id",
        store_conversation=False,
    )

    assert (response_text, response_id) == ("answer", "response-id")
    assert sources[0]["filename"] == "sdk_test_doc.md"
    assert sources[0]["text"] == "purple elephants dancing"
    assert sources[0]["mimetype"] == "text/markdown"
    assert sources[0]["page"] == 0
    assert sources[0]["score"] == 0


@pytest.mark.asyncio
async def test_async_langflow_chat_ignores_assistant_message_text_without_tool_results(monkeypatch):
    response = SimpleNamespace(output=[])

    async def fake_async_response(*_args, **_kwargs):
        return "The document says the animals are purple.", "response-id", response

    monkeypatch.setattr(agent, "async_response", fake_async_response)

    _, _, sources = await agent.async_langflow_chat(
        object(),
        "flow-id",
        "prompt",
        "user-id",
        store_conversation=False,
    )

    assert sources == []


@pytest.mark.asyncio
async def test_non_streaming_citation_fallback_preserves_exact_chunk_id(monkeypatch):
    response = SimpleNamespace(output=[])

    async def fake_async_response(*_args, **_kwargs):
        return "Supported fact. (Source: TEST_CHUNK_ID)", "response-id", response

    monkeypatch.setattr(agent, "async_response", fake_async_response)

    _, _, sources = await agent.async_langflow_chat(
        object(),
        "flow-id",
        "prompt",
        "user-id",
        store_conversation=False,
    )

    assert sources == [
        {
            "filename": "",
            "text": "",
            "score": 0,
            "page": None,
            "mimetype": None,
            "chunk_id": "TEST_CHUNK_ID",
            "id": "TEST_CHUNK_ID",
        }
    ]


def test_streamed_tool_artifact_becomes_frontend_results_and_keeps_provenance():
    chunk = {
        "type": "response.output_item.done",
        "item": {
            "type": "tool_call",
            "tool_name": "search_documents",
            "results": {
                "content": '[{"chunk_id":"TEST_CHUNK_ID"}]',
                "artifact": [
                    {
                        "filename": "invoice.pdf",
                        "text": "STREAM CANARY",
                        "page": 1,
                        "document_id": "TEST_DOCUMENT_ID",
                        "chunk_id": "TEST_CHUNK_ID",
                        "source_url": "/api/source-files/TEST_DOCUMENT_ID.token",
                        "chunk_index": 2,
                        "chunking_strategy": "character",
                    }
                ],
            },
        },
    }

    normalize_retrieval_tool_event(chunk)

    assert chunk["item"]["results"] == [
        {
            "filename": "invoice.pdf",
            "text": "STREAM CANARY",
            "page": 1,
            "document_id": "TEST_DOCUMENT_ID",
            "chunk_id": "TEST_CHUNK_ID",
            "source_url": "/api/source-files/TEST_DOCUMENT_ID.token",
            "chunk_index": 2,
            "chunking_strategy": "character",
        }
    ]


def test_streamed_metadata_tool_artifact_becomes_clickable_frontend_source():
    source = {
        "filename": "invoice-march-2024.pdf",
        "text": "<<<UNTRUSTED_DOC_CHUNK>>>\nMarch evidence\n<<<END_UNTRUSTED_DOC_CHUNK>>>",
        "page": 1,
        "document_id": "MARCH_2024_DOCUMENT_ID",
        "chunk_id": "MARCH_2024_CHUNK_ID",
        "source_url": "/api/source-files/MARCH_2024_DOCUMENT_ID.token",
    }
    chunk = {
        "type": "response.output_item.done",
        "item": {
            # Langflow's live Responses stream emits this tool as a
            # ``function_call`` even though history normalizes it to
            # ``tool_call``. Both transport variants must expose sources.
            "type": "function_call",
            "tool_name": "document_search_with_metadata",
            "results": json.dumps(
                {
                    "content": '[{"chunk_id":"MARCH_2024_CHUNK_ID"}]',
                    "artifact": [source],
                }
            ),
        },
    }

    normalize_retrieval_tool_event(chunk)
    strip_untrusted_fence_recursive(chunk)

    assert chunk["item"]["results"] == [
        {
            **source,
            "text": "March evidence",
        }
    ]


@pytest.mark.parametrize("encoding_layers", [1, 2, 3, 4])
def test_streamed_json_tool_message_becomes_unfenced_frontend_results(encoding_layers):
    source = {
        "filename": "invoice.pdf",
        "text": "<<<UNTRUSTED_DOC_CHUNK>>>\nSTREAM CANARY\n<<<END_UNTRUSTED_DOC_CHUNK>>>",
        "page": 1,
        "document_id": "TEST_DOCUMENT_ID",
        "chunk_id": "TEST_CHUNK_ID",
        "source_url": "https://example.test/api/source-files/TEST_DOCUMENT_ID.token",
    }
    results = json.dumps(
        {
            "content": '[{"chunk_id":"TEST_CHUNK_ID"}]',
            "artifact": [source],
        }
    )
    for _ in range(encoding_layers - 1):
        results = json.dumps(results)

    chunk = {
        "type": "response.output_item.done",
        "item": {
            "type": "tool_call",
            "tool_name": "search_documents",
            "results": results,
        },
    }

    normalize_retrieval_tool_event(chunk)
    strip_untrusted_fence_recursive(chunk)

    assert chunk["item"]["results"] == [
        {
            **source,
            "text": "STREAM CANARY",
        }
    ]


def test_streamed_python_repr_tool_message_is_not_parsed():
    results = "{'artifact': [{'chunk_id': 'TEST_CHUNK_ID'}]}"
    chunk = {
        "type": "response.output_item.done",
        "item": {
            "tool_name": "search_documents",
            "results": results,
        },
    }

    normalize_retrieval_tool_event(chunk)

    assert chunk["item"]["results"] == results


def test_streamed_retrieval_artifact_does_not_require_repeated_tool_name():
    source = {
        "filename": "invoice.pdf",
        "text": "STREAM CANARY",
        "document_id": "TEST_DOCUMENT_ID",
        "chunk_id": "TEST_CHUNK_ID",
    }
    chunk = {
        "type": "response.output_item.done",
        "item": {
            "type": "tool_call",
            "results": json.dumps({"artifact": [source]}),
        },
    }

    normalize_retrieval_tool_event(chunk)

    assert chunk["item"]["results"] == [source]
