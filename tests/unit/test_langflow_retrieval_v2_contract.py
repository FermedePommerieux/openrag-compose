"""Contract guards for the default Langflow chat retrieval path.

These tests deliberately inspect the exported flow instead of importing the
custom component: Langflow loads extensions in its own container, while this
repository's unit environment does not install ``lfx``.
"""

import importlib.util
import inspect
import json
import sys
import types
from pathlib import Path

from utils.langflow_utils import parse_knowledge_chunks

ROOT = Path(__file__).resolve().parents[2]
FLOW_PATH = ROOT / "flows" / "openrag_agent.json"
COMPONENT_PATH = ROOT / "custom_components" / "openrag" / "backend_retrieval.py"
PROMPT_PATH = ROOT / "flows" / "components" / "openrag_agent_system_prompt.md"
AGENT_COMPONENT_PATH = ROOT / "flows" / "components" / "openrag_agent.py"


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
    assert "coverage.complete=true" in prompt
    assert "three evidence paths" in prompt
    assert "scope_exhaustive=true" in prompt
    assert "never print the raw id as the scope" in prompt
    assert "needs no confirmation" in prompt


def test_gpt_56_tool_agent_uses_openai_responses_transport():
    flow = json.loads(FLOW_PATH.read_text(encoding="utf-8"))
    agent = next(
        node
        for node in flow["data"]["nodes"]
        if node["data"]["node"].get("display_name") == "Agent"
    )
    source = AGENT_COMPONENT_PATH.read_text(encoding="utf-8")

    assert agent["data"]["node"]["template"]["code"]["value"] == source
    assert 'provider == "openai"' in source
    assert 'model_name.startswith("gpt-5.6")' in source
    assert 'overrides["use_responses_api"] = True' in source


def test_backend_retrieval_tool_is_thin_and_embedded_verbatim():
    flow = json.loads(FLOW_PATH.read_text(encoding="utf-8"))
    retrieval = next(
        node
        for node in flow["data"]["nodes"]
        if node["data"].get("type") == "ext:openrag:OpenRAGBackendRetrievalComponent@extra"
    )
    code = COMPONENT_PATH.read_text(encoding="utf-8")

    assert retrieval["data"]["node"]["template"]["code"]["value"] == code
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
            "schema_version": "1.0",
            "entity": {"id": "urn:openrag:document:42", "type": "document"},
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

    assert captured == {
        "timeout": 300.0,
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
            "responseProfile": "langflow",
        },
    }
    assert "<<<UNTRUSTED_DOC_CHUNK>>>" in result[0].text
    citations = parse_knowledge_chunks({"artifact": [{"data": result[0].__dict__}]})
    citation_fields = {
        key: value
        for key, value in search_result.items()
        if key not in {"text", "source_provenance"}
    }
    assert {key: citations[0][key] for key in citation_fields} == citation_fields

    built_tool = tool.build_tool()
    assert built_tool["response_format"] == "content_and_artifact"
    guard_metadata = built_tool["metadata"]["openrag_retrieval_guard"]
    assert guard_metadata == {
        "version": 1,
        "filter_fingerprint": guard_metadata["filter_fingerprint"],
        "scope_policy_id": "documentary-prov-o",
        "scope_policy_version": 1,
    }
    assert len(guard_metadata["filter_fingerprint"]) == 64
    assert "user-1" not in json.dumps(guard_metadata)
    assert list(inspect.signature(built_tool["func"]).parameters) == [
        "search_query",
        "read_document_id",
        "cursor",
        "scope_exhaustive",
        "multi_query_discovery",
    ]
    content, artifact = built_tool["func"]("where is the archive?")
    model_payload = json.loads(content)
    assert model_payload["results"][0]["chunk_id"] == "chunk-42"
    assert "source_url" not in model_payload["results"][0]
    assert "source_provenance" not in model_payload["results"][0]
    assert model_payload["documents"] == [{"document_id": "document-42", "filename": "archive.pdf"}]
    assert artifact == [
        {
            **search_result,
            "text": (
                "<<<UNTRUSTED_DOC_CHUNK>>>\nuntrusted document text\n<<<END_UNTRUSTED_DOC_CHUNK>>>"
            ),
        }
    ]
    assert artifact[0]["source_provenance"] == search_result["source_provenance"]
    assert "JSON(text_key=" not in content


def test_model_projection_preserves_structured_retrieval_warnings(monkeypatch):
    module = _load_component_with_langflow_stubs(monkeypatch)
    payload = {
        "results": [{"chunk_id": "partial", "text": "partial evidence"}],
        "warnings": [
            {
                "code": "multi_query_planner_failed",
                "message": "planner unavailable",
            }
        ],
        "requested_retrieval_profile": {"version": 1, "mode": "hybrid"},
        "effective_retrieval_profile": {"version": 1, "mode": "lexical"},
        "retrieval_execution_complete": False,
        "retrieval_failure_codes": ["multi_query_planner_failed"],
    }

    compact = module._model_payload(payload)

    assert compact["warnings"] == payload["warnings"]
    assert "warning" not in compact
    assert compact["retrieval_execution_complete"] is False
    assert compact["requested_retrieval_profile"]["mode"] == "hybrid"
    assert compact["effective_retrieval_profile"]["mode"] == "lexical"


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
                    "mode": "exhaustive",
                    "complete": False,
                    "document_id": "document-42",
                    "filename": "archive.pdf",
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
        read_document_id="document-42",
        cursor="cursor-1",
    )

    assert captured["payload"]["evidenceMode"] == "exhaustive"
    assert captured["payload"]["documentId"] == "document-42"
    assert captured["payload"]["cursor"] == "cursor-1"
    assert captured["payload"]["batchSize"] == 50
    assert captured["payload"]["responseProfile"] == "langflow"
    model_coverage = json.loads(content)["coverage"]
    assert model_coverage["complete"] is False
    assert model_coverage["filename"] == "archive.pdf"
    assert "document_id" not in model_coverage
    assert artifact[0]["chunk_id"] == "chunk-42"


def test_archive_exhaustive_wording_cannot_select_document_read(monkeypatch):
    """Topic wording never guesses one internal document id."""
    module = _load_component_with_langflow_stubs(monkeypatch)
    captured: dict = {}

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"results": []}

    class _Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def post(self, _url, *, headers, json):
            captured.update(headers=headers, payload=json)
            return _Response()

    monkeypatch.setattr(module.httpx, "Client", _Client)
    tool = module.OpenRAGBackendRetrievalComponent.__new__(module.OpenRAGBackendRetrievalComponent)
    tool.openrag_retrieval_url = "http://openrag-backend:8000/search"
    tool.jwt_token = "user-jwt"
    tool.filter_expression = ""
    tool.number_of_results = 10

    built_tool = tool.build_tool()
    built_tool["func"]("recherche exhaustive complète sur toute l'archive TVA 2017")

    assert captured["payload"]["evidenceMode"] == "focused"
    assert captured["payload"]["documentId"] is None
    assert captured["payload"]["batchSize"] == 20
    assert captured["payload"]["responseProfile"] == "langflow"


def test_explicit_scope_investigation_routes_to_scope_exhaustive(monkeypatch):
    module = _load_component_with_langflow_stubs(monkeypatch)
    captured: dict = {}

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "results": [],
                "model_results": [],
                "documents": [
                    {
                        "document_id": "document-42",
                        "scope_context_relations": [
                            {
                                "role": "contained_in",
                                "target_type": "email_archive",
                            }
                        ],
                    }
                ],
                "coverage": {
                    "mode": "scope_exhaustive",
                    "complete": False,
                    "scope_policy_id": "documentary-prov-o",
                    "scope_policy_version": 1,
                    "identity_shared_aliases_resolved": 1,
                    "relations_traversed": {"total": 12, "by_classification": []},
                    "relations_context_only": {"total": 4, "by_classification": []},
                    "relations_excluded_by_policy": {
                        "total": 8,
                        "by_classification": [],
                    },
                    "relations_unclassified": {"total": 0, "by_classification": []},
                    "documents_discovered": 0,
                    "stop_reason": "max_depth",
                },
            }

    class _Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def post(self, _url, *, headers, json):
            captured.update(headers=headers, payload=json)
            return _Response()

    monkeypatch.setattr(module.httpx, "Client", _Client)
    tool = module.OpenRAGBackendRetrievalComponent.__new__(module.OpenRAGBackendRetrievalComponent)
    tool.openrag_retrieval_url = "http://openrag-backend:8000/search"
    tool.jwt_token = "user-jwt"
    tool.filter_expression = ""
    tool.number_of_results = 10

    built_tool = tool.build_tool()
    content, _artifact = built_tool["func"](
        "tous les échanges avec l'administration sur Surface pastorale",
        scope_exhaustive=True,
    )

    assert captured["payload"]["evidenceMode"] == "scope_exhaustive"
    assert captured["payload"]["documentId"] is None
    assert captured["payload"]["responseProfile"] == "langflow"
    compact = json.loads(content)
    assert compact["coverage"] == {
        "mode": "scope_exhaustive",
        "complete": False,
        "scope_policy_id": "documentary-prov-o",
        "scope_policy_version": 1,
        "identity_shared_aliases_resolved": 1,
        "relations_traversed": {"total": 12, "by_classification": []},
        "relations_context_only": {"total": 4, "by_classification": []},
        "relations_excluded_by_policy": {"total": 8, "by_classification": []},
        "relations_unclassified": {"total": 0, "by_classification": []},
        "documents_discovered": 0,
        "stop_reason": "max_depth",
    }
    assert compact["documents"] == [
        {
            "document_id": "document-42",
            "scope_context_relations": [{"role": "contained_in", "target_type": "email_archive"}],
        }
    ]
