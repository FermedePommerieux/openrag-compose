from pathlib import Path

import pytest

from benchmarks.discovery.cross_domain_review import (
    build_compact_candidate_capture,
    build_metadata_recovery_plan,
    build_review_universe,
    load_review_spec,
    write_review_artifacts,
)


def _spec() -> dict:
    return {
        "schema_version": 1,
        "review_id": "technical-fibre-v1",
        "domain": "technical connectivity correspondence",
        "canonical_query": "All fibre correspondence",
        "lane_parameters": {"lexical_size": 200, "dense_size": 200, "rrf_k": 60},
        "queries": [
            {
                "query_id": "canonical",
                "text": "All fibre correspondence",
                "candidate_class": "canonical",
                "lanes": ["lexical", "dense", "rrf"],
            },
            {
                "query_id": "outside-control",
                "text": "Telecom works",
                "candidate_class": "control",
                "lanes": ["lexical"],
            },
        ],
        "metadata_recovery": {
            "enabled": True,
            "relation_target_types": ["email_thread"],
            "review_horizon": 500,
        },
    }


def _item(occurrence: str, thread: str, text: str = "") -> dict:
    return {
        "occurrence_id": occurrence,
        "document_id": f"doc-{occurrence}",
        "source_entity_id": occurrence,
        "text": text,
        "source_provenance": {
            "entity": {"label": f"Title {occurrence}"},
            "relations": [
                {
                    "role": "member_of",
                    "target": {"id": thread, "type": "email_thread"},
                }
            ],
        },
    }


def test_review_spec_rejects_model_like_candidate_class(tmp_path: Path):
    path = tmp_path / "review.yaml"
    path.write_text(
        """schema_version: 1
review_id: invalid
domain: technical
canonical_query: fibre
queries:
  - query_id: q1
    text: fibre
    candidate_class: relevant
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="candidate_class"):
        load_review_spec(path)


def test_metadata_recovery_uses_only_declared_relation_target_types():
    capture = {
        "captures": [
            {
                "results": [
                    {
                        **_item("occ-a", "thread-a"),
                        "source_provenance": {
                            "relations": [
                                {
                                    "role": "member_of",
                                    "target": {
                                        "id": "thread-a",
                                        "type": "email_thread",
                                    },
                                },
                                {
                                    "role": "archived_in",
                                    "target": {
                                        "id": "archive-a",
                                        "type": "email_archive",
                                    },
                                },
                            ]
                        },
                    }
                ]
            }
        ]
    }

    plan = build_metadata_recovery_plan(_spec(), capture)

    assert plan is not None
    assert plan["metadata_anchors"] == ["thread-a"]
    assert "archive-a" not in plan["metadata_anchors"]


def test_review_universe_keeps_labels_empty_and_marks_outside_closure():
    captures = [
        {
            "captures": [
                {
                    "query_id": "canonical",
                    "text": "All fibre correspondence",
                    "lane": "rrf",
                    "results": [
                        {**_item("occ-a", "thread-a"), "rrf_occurrence_rank": 1},
                        {**_item("occ-b", "thread-a"), "rrf_occurrence_rank": 2},
                    ],
                },
                {
                    "query_id": "outside-control",
                    "text": "Telecom works",
                    "lane": "lexical",
                    "results": [{**_item("occ-c", "thread-c"), "lexical_occurrence_rank": 3}],
                },
            ]
        }
    ]

    result = build_review_universe(_spec(), captures, baseline_occurrences={"occ-a"})

    assert result["human_review_complete"] is False
    assert result["candidate_count"] == 3
    assert result["component_count"] == 2
    assert all(row["human_label"] == "" for row in result["documents"])
    assert all(row["human_label"] == "" for row in result["components"])
    by_occurrence = {row["occurrence_id"]: row for row in result["documents"]}
    assert by_occurrence["occ-a"]["current_closure_member"] is True
    assert by_occurrence["occ-b"]["review_priority"] == "outside_baseline_candidate"
    assert by_occurrence["occ-c"]["review_priority"] == "control_candidate"


def test_review_artifacts_preserve_empty_label_columns(tmp_path: Path):
    capture = {
        "captures": [
            {
                "query_id": "canonical",
                "lane": "lexical",
                "results": [{**_item("occ-a", "thread-a"), "lexical_occurrence_rank": 1}],
            }
        ]
    }
    universe = build_review_universe(_spec(), [capture])
    json_path = tmp_path / "review.json"
    documents_path = tmp_path / "documents.csv"
    components_path = tmp_path / "components.csv"

    write_review_artifacts(
        universe,
        json_path=json_path,
        documents_csv_path=documents_path,
        components_csv_path=components_path,
    )

    assert '"human_label": ""' in json_path.read_text(encoding="utf-8")
    assert "human_label" in documents_path.read_text(encoding="utf-8").splitlines()[0]
    assert "human_label" in components_path.read_text(encoding="utf-8").splitlines()[0]


def test_compact_capture_deduplicates_lane_text_and_keeps_labels_empty():
    item = {
        **_item("occ-a", "thread-a", text="x" * 5_000),
        "lexical_occurrence_rank": 1,
    }
    capture = {
        "captures": [
            {
                "query_id": "canonical",
                "lane": "lexical",
                "raw_chunk_hits": 20,
                "results": [item, item],
            }
        ]
    }

    compact = build_compact_candidate_capture(_spec(), [capture])

    assert compact["candidate_count"] == 1
    assert compact["documents"][0]["human_label"] == ""
    assert "text_preview" not in compact["documents"][0]
    assert len(compact["documents"][0]["text_preview_sha256"]) == 64
    assert compact["lane_summaries"][0]["returned_occurrences"] == 1
