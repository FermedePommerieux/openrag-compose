"""Flow-update provenance must be exact and must never imply an upstream source."""

from config.paths import get_flows_source_metadata


def test_flow_source_metadata_points_to_published_branch_and_revision(monkeypatch):
    monkeypatch.setenv("OPENRAG_FLOWS_SOURCE_REPOSITORY", "FermedePommerieux/openrag-compose")
    monkeypatch.setenv(
        "OPENRAG_FLOWS_SOURCE_BRANCH", "pommerieux/v0.6.0-retrieval-v2"
    )
    monkeypatch.setenv(
        "OPENRAG_FLOWS_SOURCE_REVISION",
        "92a40e9922e12fd7aa06b53bf841b061b41d4818",
    )

    assert get_flows_source_metadata() == {
        "repository": "FermedePommerieux/openrag-compose",
        "branch": "pommerieux/v0.6.0-retrieval-v2",
        "revision": "92a40e9922e12fd7aa06b53bf841b061b41d4818",
        "branch_url": (
            "https://github.com/FermedePommerieux/openrag-compose/tree/"
            "pommerieux/v0.6.0-retrieval-v2"
        ),
        "revision_url": (
            "https://github.com/FermedePommerieux/openrag-compose/tree/"
            "92a40e9922e12fd7aa06b53bf841b061b41d4818/flows"
        ),
    }


def test_flow_source_metadata_fails_closed_without_exact_revision(monkeypatch):
    monkeypatch.setenv("OPENRAG_FLOWS_SOURCE_REPOSITORY", "langflow-ai/openrag")
    monkeypatch.setenv("OPENRAG_FLOWS_SOURCE_BRANCH", "main")
    monkeypatch.setenv("OPENRAG_FLOWS_SOURCE_REVISION", "main")

    assert get_flows_source_metadata() is None
