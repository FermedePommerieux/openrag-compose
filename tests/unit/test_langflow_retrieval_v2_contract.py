"""Contract guards for the default Langflow chat retrieval path.

These tests deliberately inspect the exported flow instead of importing the
custom component: Langflow loads extensions in its own container, while this
repository's unit environment does not install ``lfx``.
"""

import importlib.util
import json
import sys
import types
from pathlib import Path

from utils.langflow_utils import parse_knowledge_chunks

ROOT = Path(__file__).resolve().parents[2]
FLOW_PATH = ROOT / "flows" / "openrag_agent.json"
COMPONENT_PATH = ROOT / "custom_components" / "openrag" / "backend_retrieval.py"
PROMPT_PATH = ROOT / "flows" / "components" / "openrag_agent_system_prompt.md"


def test_default_agent_uses_only_backend_retrieval_tool():
    flow = json.loads(FLOW_PATH.read_text(encoding="utf-8"))
    nodes = flow["data"]["nodes"]
    agent = next(node for node in nodes if node["data"]["node"].get("display_name") == "Agent")
    retrieval = next(
        node
        for node in nodes
        if node["data"].get("type") == "ext:openrag:OpenRAGBackendRetrievalComponent@extra"
    )

    tool_edges = [
        edge
        for edge in flow["data"]["edges"]
        if edge.get("target") == agent["id"]
        and edge.get("data", {}).get("targetHandle", {}).get("fieldName") == "tools"
    ]
    assert any(edge.get("source") == retrieval["id"] for edge in tool_edges)
    assert all(
        "OpenSearchVectorStoreComponentMultimodalMultiEmbedding" not in edge.get("source", "")
        for edge in tool_edges
    )


def test_default_agent_uses_versioned_documentalist_prompt():
    from config.config_manager import DEFAULT_SYSTEM_PROMPT

    flow = json.loads(FLOW_PATH.read_text(encoding="utf-8"))
    agent = next(
        node
        for node in flow["data"]["nodes"]
        if node["data"]["node"].get("display_name") == "Agent"
    )
    prompt = PROMPT_PATH.read_text(encoding="utf-8").rstrip("\n")

    assert agent["data"]["node"]["template"]["system_prompt"]["value"] == prompt
    assert DEFAULT_SYSTEM_PROMPT == prompt
    assert "normal path for every prompt" in prompt
    assert "There is no separate archive-search mode" in prompt
    assert "noise_accounting" in prompt
    assert "The human decides what the documents prove" in prompt
    assert agent["data"]["node"]["template"]["max_iterations"]["value"] == 128


def test_gpt_56_tool_agent_uses_openai_responses_transport():
    """GPT-5.6 reasoning plus function tools must not use Chat Completions."""
    flow = json.loads(FLOW_PATH.read_text(encoding="utf-8"))
    agent = next(
        node
        for node in flow["data"]["nodes"]
        if node["data"]["node"].get("display_name") == "Agent"
    )
    embedded = agent["data"]["node"]["template"]["code"]["value"]
    source = (ROOT / "flows" / "components" / "openrag_agent.py").read_text(encoding="utf-8")

    assert embedded == source
    assert 'provider == "openai"' in source
    assert 'model_name.startswith("gpt-5.6")' in source
    assert 'overrides["use_responses_api"] = True' in source
    assert 'reasoning_effort"] = "none"' not in source


def test_backend_retrieval_tool_is_thin_and_embedded_verbatim():
    flow = json.loads(FLOW_PATH.read_text(encoding="utf-8"))
    retrieval = next(
        node
        for node in flow["data"]["nodes"]
        if node["data"].get("type") == "ext:openrag:OpenRAGBackendRetrievalComponent@extra"
    )
    code = COMPONENT_PATH.read_text(encoding="utf-8")
    search_context = retrieval["data"]["node"]["template"]["filter_expression"]

    assert retrieval["data"]["node"]["template"]["code"]["value"] == code
    assert search_context["value"] == "OPENRAG_QUERY_FILTER"
    assert search_context["_input_type"] == "StrInput"
    assert search_context["input_types"] == []
    assert search_context["multiline"] is False
    assert search_context["load_from_db"] is True
    assert 'value="OPENRAG_QUERY_FILTER"' in code
    assert "load_from_db=True" in code
    assert "MultilineInput" not in code
    assert "client.post(" in code
    assert '"scoreThreshold": score_threshold' in code
    assert "from opensearch" not in code.lower()
    assert "reciprocal_rank_fusion" not in code
    assert 'headers["Authorization"]' in code


def _load_component_with_langflow_stubs(monkeypatch):
    """Load the extension exactly enough to exercise its HTTP boundary."""

    class _Input:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class _Data:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class _StructuredTool:
        @classmethod
        def from_function(cls, **kwargs):
            return kwargs

    modules = {
        "langchain_core": types.ModuleType("langchain_core"),
        "langchain_core.tools": types.ModuleType("langchain_core.tools"),
        "lfx": types.ModuleType("lfx"),
        "lfx.base": types.ModuleType("lfx.base"),
        "lfx.base.langchain_utilities": types.ModuleType("lfx.base.langchain_utilities"),
        "lfx.base.langchain_utilities.model": types.ModuleType(
            "lfx.base.langchain_utilities.model"
        ),
        "lfx.io": types.ModuleType("lfx.io"),
        "lfx.schema": types.ModuleType("lfx.schema"),
        "lfx.schema.data": types.ModuleType("lfx.schema.data"),
    }
    modules["langchain_core.tools"].StructuredTool = _StructuredTool
    modules["lfx.base.langchain_utilities.model"].LCToolComponent = type("LCToolComponent", (), {})
    for input_name in ("IntInput", "MultilineInput", "Output", "SecretStrInput", "StrInput"):
        setattr(modules["lfx.io"], input_name, _Input)
    modules["lfx.schema.data"].Data = _Data
    for module_name, module in modules.items():
        monkeypatch.setitem(sys.modules, module_name, module)

    module_name = "test_backend_retrieval_component"
    spec = importlib.util.spec_from_file_location(module_name, COMPONENT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)
    return module


def test_backend_tool_forwards_request_and_preserves_provenance(monkeypatch):
    """Simulated SearchService result → tool Data → citation parser contract."""
    module = _load_component_with_langflow_stubs(monkeypatch)
    captured: dict = {}
    search_result = {
        "document_id": "document-42",
        "chunk_id": "chunk-42",
        "connector_file_id": "drive-file-42",
        "source_url": "/api/source-files/document-42.token",
        "source_provenance": {
            "entity": {"id": "urn:openrag:document:42", "type": "document"},
            "relations": [{"role": "attached_to", "target": "mail-42"}],
            "large_repeated_value": "metadata must stay out of model context" * 100,
        },
        "source_entity_id": "urn:openrag:document:42",
        "source_relation_target_ids": ["mail-42"],
        "source_relation_roles": ["attached_to"],
        "retrieval_relation_paths": [
            {
                "relation_role": "reply_to",
                "from_document_id": "document-42",
                "to_document_id": "document-41",
            }
        ],
        "retrieval_plane": "context",
        "retrieval_relation_depth": 1,
        "retrieval_channels": ["provenance"],
        "retrieval_relevance": {
            "level": "contextual",
            "reason": "Reached through one explicit high-signal PROV-O relation hop.",
            "probability_calibrated": False,
            "human_validation_required": True,
            "relation_depth": 1,
        },
        "filename": "archive.pdf",
        "page": 3,
        "chunk_index": 7,
        "chunking_strategy": "hybrid",
        "text": "untrusted document text",
    }

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"results": [search_result]}

    class _Client:
        def __init__(self, **kwargs):
            captured["timeout"] = kwargs["timeout"]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def post(self, url, *, headers, json):
            captured.update(url=url, headers=headers, payload=json)
            return _Response()

    monkeypatch.setattr(module.httpx, "Client", _Client)
    tool = module.OpenRAGBackendRetrievalComponent.__new__(module.OpenRAGBackendRetrievalComponent)
    tool.openrag_retrieval_url = "http://openrag-backend:8000/search"
    tool.jwt_token = "user-jwt"
    tool.filter_expression = json.dumps(
        {"filters": {"owners": ["user-1"]}, "limit": 4, "scoreThreshold": 0.25}
    )
    tool.number_of_results = 10

    result = tool.search_documents("where is the archive?")

    timeout = captured.pop("timeout")
    assert timeout.connect == 10.0
    assert timeout.read == timeout.write == timeout.pool == 2_400.0
    assert captured == {
        "url": "http://openrag-backend:8000/search",
        "headers": {"Authorization": "Bearer user-jwt"},
        "payload": {
            "query": "where is the archive?",
            "filters": {"owners": ["user-1"]},
            "limit": 4,
            "scoreThreshold": 0.25,
            "evidenceMode": "focused",
            "documentId": None,
            "cursor": "",
            "batchSize": 20,
            "progressId": None,
        },
    }
    assert "<<<UNTRUSTED_DOC_CHUNK>>>" in result[0].text
    citations = parse_knowledge_chunks({"artifact": [{"data": result[0].__dict__}]})
    assert citations[0]["chunk_id"] == "chunk-42"
    assert citations[0]["document_id"] == "document-42"
    assert citations[0]["source_url"] == "/api/source-files/document-42.token"
    assert citations[0]["filename"] == "archive.pdf"

    built_tool = tool.build_tool()
    assert built_tool["response_format"] == "content_and_artifact"
    content, artifact = built_tool["func"]("where is the archive?")
    model_payload = json.loads(content)
    assert model_payload["results"][0]["chunk_id"] == "chunk-42"
    assert "source_url" not in model_payload["results"][0]
    assert "source_provenance" not in content
    assert model_payload["documents"] == [
        {
            "document_id": "document-42",
            "filename": "archive.pdf",
            "source_entity_id": "urn:openrag:document:42",
            "source_relation_target_ids": ["mail-42"],
            "source_relation_roles": ["attached_to"],
            "retrieval_relation_paths": [
                {
                    "relation_role": "reply_to",
                    "from_document_id": "document-42",
                    "to_document_id": "document-41",
                }
            ],
            "retrieval_plane": "context",
            "retrieval_relation_depth": 1,
            "retrieval_channels": ["provenance"],
            "retrieval_relevance": {
                "level": "contextual",
                "reason": "Reached through one explicit high-signal PROV-O relation hop.",
                "probability_calibrated": False,
                "human_validation_required": True,
                "relation_depth": 1,
            },
        }
    ]
    assert artifact == [
        {
            **search_result,
            "text": (
                "<<<UNTRUSTED_DOC_CHUNK>>>\nuntrusted document text\n<<<END_UNTRUSTED_DOC_CHUNK>>>"
            ),
        }
    ]
    assert artifact[0]["source_url"] == "/api/source-files/document-42.token"
    assert artifact[0]["source_provenance"]["entity"]["type"] == "document"
    assert "JSON(text_key=" not in content


def test_backend_tool_forwards_exhaustive_cursor_and_coverage(monkeypatch):
    module = _load_component_with_langflow_stubs(monkeypatch)
    captured: dict = {}

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "results": [
                    {
                        "document_id": "document-42",
                        "chunk_id": "chunk-42",
                        "text": "evidence",
                    }
                ],
                "coverage": {
                    "complete": False,
                    "covered_chunks": 20,
                    "total_chunks": 80,
                    "next_cursor": "next-page",
                },
            }

    class _Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def post(self, url, *, headers, json):
            captured.update(payload=json)
            return _Response()

    monkeypatch.setattr(module.httpx, "Client", _Client)
    tool = module.OpenRAGBackendRetrievalComponent.__new__(module.OpenRAGBackendRetrievalComponent)
    tool.openrag_retrieval_url = "http://openrag-backend:8000/search"
    tool.jwt_token = "user-jwt"
    tool.filter_expression = ""
    tool.number_of_results = 10

    built_tool = tool.build_tool()
    content, artifact = built_tool["func"](
        "audit the document",
        evidence_mode="exhaustive",
        document_id="document-42",
        cursor="cursor-1",
        batch_size=25,
    )

    assert captured["payload"]["evidenceMode"] == "exhaustive"
    assert captured["payload"]["documentId"] == "document-42"
    assert captured["payload"]["cursor"] == "cursor-1"
    assert json.loads(content)["coverage"]["complete"] is False
    assert artifact[0]["chunk_id"] == "chunk-42"


def test_legacy_hierarchical_synthesis_cannot_replace_source_evidence(monkeypatch):
    module = _load_component_with_langflow_stubs(monkeypatch)
    payload = {
        "results": [
            {
                "document_id": "document-42",
                "chunk_id": "chunk-42",
                "filename": "mail.eml",
                "text": "raw source text that must stay out of the final model context",
            }
        ],
        "audit_synthesis": {
            "strategy": "hierarchical_verified_map_reduce",
            "complete": True,
            "verified": True,
            "findings": [
                {
                    "finding_id": "audit-final-finding-1",
                    "statement": "A source-validated administrative exchange occurred.",
                    "chunk_ids": ["chunk-42"],
                    "document_ids": ["document-42"],
                }
            ],
            "coverage": {"chunks_total": 1, "chunks_covered": 1},
        },
    }

    compact = module._model_payload(payload)

    assert compact["results"][0]["chunk_id"] == "chunk-42"
    assert "raw source text" in compact["results"][0]["text"]
    assert compact["evidence_chunks_available"] == 1
    assert "audit_synthesis" not in compact
    assert payload["results"][0]["text"].startswith("raw source text")


def test_large_source_evidence_is_not_hidden_by_a_legacy_audit_budget(monkeypatch):
    module = _load_component_with_langflow_stubs(monkeypatch)
    source_text = "x" * 50_000
    payload = {
        "results": [
            {
                "document_id": "document-42",
                "chunk_id": "chunk-42",
                "text": source_text,
            }
        ]
    }

    compact = module._model_payload(payload)

    assert compact["results"][0]["text"] == source_text
    assert "raw_evidence_omitted" not in compact


def test_legacy_unverified_synthesis_cannot_withhold_source_evidence(monkeypatch):
    module = _load_component_with_langflow_stubs(monkeypatch)
    payload = {
        "results": [
            {
                "document_id": "document-42",
                "chunk_id": "chunk-42",
                "text": "raw evidence",
            }
        ],
        "audit_synthesis": {
            "complete": True,
            "verified": False,
            "findings": [],
        },
    }

    compact = module._model_payload(payload)

    assert compact["results"][0]["text"] == "raw evidence"
    assert "audit_synthesis" not in compact
    assert "raw_evidence_omitted" not in compact


def test_historical_exhaustive_intent_uses_normal_provenance_search(monkeypatch):
    """Chat intent no longer starts an archive-wide document-read cascade."""
    module = _load_component_with_langflow_stubs(monkeypatch)
    calls: list[dict] = []

    class _Response:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class _Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def post(self, _url, *, headers, json):
            calls.append(json)
            if json["evidenceMode"] == "focused":
                return _Response(
                    {
                        "results": [
                            {
                                "document_id": "doc-a",
                                "chunk_id": "doc-a-direct",
                                "filename": "a.eml",
                                "text": "ranked",
                                "retrieval_plane": "direct",
                            },
                            {
                                "document_id": "doc-b",
                                "chunk_id": "doc-b-context",
                                "filename": "b.pdf",
                                "text": "ranked",
                                "retrieval_plane": "context",
                                "retrieval_relation_depth": 1,
                                "retrieval_relation_paths": [
                                    {
                                        "from_document_id": "doc-a",
                                        "to_document_id": "doc-b",
                                        "relation_role": "reply_to",
                                    }
                                ],
                                "retrieval_relevance": {
                                    "level": "contextual",
                                    "reason": "One PROV-O hop.",
                                },
                            },
                        ],
                        "retrieval_planes": {
                            "direct": {"documents": 1},
                            "context": {"documents": 1, "fixpoint_reached": True},
                        },
                        "noise_accounting": {"intentional_context_documents": 1},
                    }
                )
            document_id = json["documentId"]
            continuation = bool(json["cursor"])
            complete = document_id == "doc-a" or continuation
            return _Response(
                {
                    "results": [
                        {
                            "document_id": document_id,
                            "chunk_id": f"{document_id}-chunk-{2 if continuation else 1}",
                            "text": f"evidence for {document_id}",
                        }
                    ],
                    "coverage": {
                        "mode": "exhaustive",
                        "document_id": document_id,
                        "complete": complete,
                        "covered_chunks": 80 if continuation else 1,
                        "total_chunks": 1 if document_id == "doc-a" else 80,
                        "next_cursor": None if complete else "doc-b-next",
                    },
                }
            )

    monkeypatch.setattr(module.httpx, "Client", _Client)
    tool = module.OpenRAGBackendRetrievalComponent.__new__(module.OpenRAGBackendRetrievalComponent)
    tool.openrag_retrieval_url = "http://openrag-backend:8000/search"
    tool.jwt_token = "user-jwt"
    tool.filter_expression = json.dumps(
        {"filters": {}, "limit": 10, "retrievalIntent": "exhaustive"}
    )
    tool.number_of_results = 10

    content, artifact = tool.build_tool()["func"]("surface pastorale DDT")
    payload = json.loads(content)

    assert [call["evidenceMode"] for call in calls] == ["focused"]
    assert {item["chunk_id"] for item in artifact} == {"doc-a-direct", "doc-b-context"}
    assert "coverage" not in payload
    assert payload["retrieval_planes"]["context"]["fixpoint_reached"] is True
    assert payload["noise_accounting"]["intentional_context_documents"] == 1
    doc_b_manifest = next(item for item in payload["documents"] if item["document_id"] == "doc-b")
    assert doc_b_manifest["retrieval_relation_paths"][0]["relation_role"] == "reply_to"
    assert doc_b_manifest["retrieval_plane"] == "context"
