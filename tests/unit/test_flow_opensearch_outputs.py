"""Contract guards for the bundled OpenSearch multimodal flow component.

The agent flow uses the separate Retrieval v2 thin tool. These assertions
cover only the flows that embed the OpenSearch multimodal component directly.
"""

import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CANONICAL_COMPONENT = ROOT / "custom_components/openrag/opensearch_multimodal.py"
FLOW_PATHS = (
    ROOT / "flows/ingestion_flow.json",
    ROOT / "flows/openrag_nudges.json",
    ROOT / "flows/openrag_url_mcp.json",
)
OPENSEARCH_COMPONENT_TYPE = "ext:openrag:OpenSearchVectorStoreComponentMultimodalMultiEmbedding@extra"


def _load_flow(flow_path: Path) -> dict:
    return json.loads(flow_path.read_text(encoding="utf-8"))


def _opensearch_nodes(flow: dict) -> list[dict]:
    """Find the component by its stable Langflow type, never by generated id."""
    return [
        node
        for node in flow["data"]["nodes"]
        if node.get("data", {}).get("type") == OPENSEARCH_COMPONENT_TYPE
    ]


def _canonical_code() -> str:
    return CANONICAL_COMPONENT.read_text(encoding="utf-8")


def test_canonical_opensearch_component_returns_list_data_and_fences_text():
    """Search results are ``list[Data]`` before Langflow serializes them as JSON."""
    code = _canonical_code()
    module = ast.parse(code)
    search_documents = next(
        node
        for node in ast.walk(module)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "search_documents"
    )

    assert ast.unparse(search_documents.returns) == "list[Data]"
    assert "raw_list = [Data(text=hit[\"page_content\"], **hit[\"metadata\"]) for hit in raw]" in code
    assert "return raw_list" in code

    # VULN-13906: retrieved chunks remain explicit untrusted data before they
    # are returned to an LLM-facing flow.
    assert 'UNTRUSTED_CHUNK_FENCE_START = "<<<UNTRUSTED_DOC_CHUNK>>>"' in code
    assert 'UNTRUSTED_CHUNK_FENCE_END = "<<<END_UNTRUSTED_DOC_CHUNK>>>"' in code
    assert "def fence_untrusted_text(text: str) -> str:" in code
    assert '"page_content": fence_untrusted_text(hit["_source"].get("text", ""))' in code
    assert 'source["text"] = fence_untrusted_text(source["text"])' in code


def test_direct_opensearch_flows_embed_canonical_json_component():
    """Every direct consumer embeds the exact canonical component and JSON output."""
    canonical_code = _canonical_code()

    for flow_path in FLOW_PATHS:
        nodes = _opensearch_nodes(_load_flow(flow_path))
        assert len(nodes) == 1, f"{flow_path.name} must contain one direct OpenSearch component"

        node = nodes[0]
        assert node["data"]["node"]["template"]["code"]["value"] == canonical_code

        outputs = node["data"]["node"]["outputs"]
        search_output = next(output for output in outputs if output.get("name") == "search_results")
        assert search_output["method"] == "search_documents"
        assert search_output["types"] == ["JSON"]
        assert search_output["selected"] == "JSON"
        assert all(output.get("name") != "dataframe" for output in outputs)


def test_agent_flow_is_not_subject_to_direct_opensearch_component_contract():
    """The agent uses Retrieval v2's thin tool, covered by its dedicated test."""
    agent_flow = _load_flow(ROOT / "flows/openrag_agent.json")

    assert not _opensearch_nodes(agent_flow)
