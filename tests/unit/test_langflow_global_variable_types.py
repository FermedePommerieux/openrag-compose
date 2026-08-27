"""Contracts for Langflow global-variable field types."""

from types import SimpleNamespace

from api.settings.langflow_sync import (
    _langflow_global_variable_type,
    _required_generic_global_values,
)
from utils.langflow_headers import (
    LANGFLOW_QUERY_FILTER_HEADER,
    LANGFLOW_QUERY_FILTER_VARIABLE,
)


def test_ingest_run_id_is_generic_routing_metadata():
    assert _langflow_global_variable_type("OPENRAG_INGEST_RUN_ID") == "Generic"
    assert _langflow_global_variable_type("OPENRAG_INGEST_TOKEN") == "Credential"
    assert _langflow_global_variable_type("OPENRAG_QUERY_FILTER") == "Generic"
    assert LANGFLOW_QUERY_FILTER_HEADER == (
        f"X-Langflow-Global-Var-{LANGFLOW_QUERY_FILTER_VARIABLE}"
    )


def test_startup_migrates_ingest_run_id_even_before_first_request(monkeypatch):
    monkeypatch.setattr(
        "config.settings.get_langflow_docling_url",
        lambda: "http://docling:5001",
    )
    monkeypatch.setattr(
        "config.settings.get_langflow_opensearch_url",
        lambda: "https://opensearch:9200",
    )
    config = SimpleNamespace(
        knowledge=SimpleNamespace(index_name="documents", embedding_model="embedding"),
        providers=SimpleNamespace(
            watsonx=SimpleNamespace(project_id=None, endpoint=None),
            ollama=SimpleNamespace(endpoint=None),
        ),
    )

    values = _required_generic_global_values(config)

    assert values["OPENRAG_INGEST_RUN_ID"] == "OPENRAG_INGEST_RUN_ID"
    assert values["OPENRAG_QUERY_FILTER"] == "{}"
