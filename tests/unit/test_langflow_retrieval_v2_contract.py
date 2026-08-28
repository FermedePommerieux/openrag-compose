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
    assert "never prove" in prompt


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
        "timeout": 30.0,
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
        },
    }
    assert "<<<UNTRUSTED_DOC_CHUNK>>>" in result[0].text
    citations = parse_knowledge_chunks({"artifact": [{"data": result[0].__dict__}]})
    assert {key: citations[0][key] for key in search_result if key != "text"} == {
        key: value for key, value in search_result.items() if key != "text"
    }

    built_tool = tool.build_tool()
    assert built_tool["response_format"] == "content_and_artifact"
    content, artifact = built_tool["func"]("where is the archive?")
    assert json.loads(content)["results"][0]["chunk_id"] == "chunk-42"
    assert artifact == [
        {
            **search_result,
            "text": (
                "<<<UNTRUSTED_DOC_CHUNK>>>\nuntrusted document text\n<<<END_UNTRUSTED_DOC_CHUNK>>>"
            ),
        }
    ]
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
