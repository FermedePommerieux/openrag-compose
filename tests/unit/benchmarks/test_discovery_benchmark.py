from __future__ import annotations

import copy

import pytest

from benchmarks.discovery.control import aggregate_control_capture, build_remote_plan
from benchmarks.discovery.corpus import corpus_changed, snapshot_visible_corpus
from benchmarks.discovery.ground_truth import validate_ground_truth
from benchmarks.discovery.metrics import compute_metrics, coverage_success_rate
from benchmarks.discovery.review import build_review_rows


def _definition() -> dict:
    return {
        "schema_version": 1,
        "benchmark_id": "fixture-v1",
        "benchmark_version": 1,
        "document_metric_unit": "source_occurrence",
        "queries": [{"query_id": "canonical", "text": "all relevant exchanges"}],
        "documents": [
            {
                "occurrence_id": "occ-a1",
                "document_id": "doc-a1",
                "source_entity_id": "entity-a1",
                "component_id": "component-a",
                "state": "relevant",
            },
            {
                "occurrence_id": "occ-a2",
                "document_id": "doc-a2",
                "source_entity_id": "entity-a2",
                "component_id": "component-a",
                "state": "relevant",
            },
            {
                "occurrence_id": "occ-b1",
                "document_id": "shared-content",
                "source_entity_id": "entity-b1",
                "component_id": "component-b",
                "state": "relevant",
            },
            {
                "occurrence_id": "occ-b2",
                "document_id": "shared-content",
                "source_entity_id": "entity-b2",
                "component_id": "component-b",
                "state": "relevant",
            },
            {
                "occurrence_id": "occ-c1",
                "document_id": "doc-c1",
                "source_entity_id": "entity-c1",
                "component_id": "component-c",
                "state": "relevant",
            },
            {
                "occurrence_id": "occ-n1",
                "document_id": "doc-n1",
                "source_entity_id": "entity-n1",
                "state": "not_relevant",
            },
            {
                "occurrence_id": "occ-u1",
                "document_id": "doc-u1",
                "source_entity_id": "entity-u1",
                "state": "uncertain",
            },
        ],
        "components": [
            {
                "component_id": "component-a",
                "type": "email_thread",
                "state": "relevant",
                "required_occurrence_ids": ["occ-a1", "occ-a2"],
            },
            {
                "component_id": "component-b",
                "type": "message_attachments",
                "state": "relevant",
                "required_occurrence_ids": ["occ-b1", "occ-b2"],
            },
            {
                "component_id": "component-c",
                "type": "standalone_document",
                "state": "relevant",
                "required_occurrence_ids": ["occ-c1"],
            },
        ],
    }


def _control_definition() -> dict:
    definition = _definition()
    definition["components"][0]["human_decision"] = "CORE"
    definition["components"][0]["source_component_id"] = "thread-a"
    definition["control_search"] = {
        "schema_version": 1,
        "lane_parameters": {
            "lexical_size": 100,
            "dense_size": 200,
            "metadata_size": 1000,
            "rrf_k": 60,
        },
        "metadata_recovery": {
            "enabled": True,
            "anchors_from_component_source_ids": True,
        },
        "passes": [
            {
                "pass_id": "lexical-broad",
                "method": "lexical broad",
                "queries": [
                    {
                        "query_id": "generic-term",
                        "text": "generic subject",
                        "lanes": ["lexical"],
                        "review_horizon": 10,
                        "proposed_label": "POTENTIAL_CORE",
                    }
                ],
            }
        ],
    }
    return definition


def _hit(entity: str, document: str, **extra) -> dict:
    return {"source_entity_id": entity, "document_id": document, **extra}


def test_ground_truth_validates_explicit_occurrences_and_components():
    validate_ground_truth(_definition())
    invalid = _definition()
    invalid["documents"][0].pop("source_entity_id")
    with pytest.raises(ValueError, match="source_entity_id"):
        validate_ground_truth(invalid)


def test_control_plan_is_entirely_definition_driven():
    plan = build_remote_plan(_control_definition())
    assert plan["queries"] == [
        {
            "query_id": "generic-term",
            "text": "generic subject",
            "lanes": ["lexical"],
            "review_horizon": 10,
            "proposed_label": "POTENTIAL_CORE",
            "pass_id": "lexical-broad",
            "method": "lexical broad",
        }
    ]
    assert plan["metadata_anchors"] == ["thread-a"]


def test_control_capture_excludes_reviewed_and_preserves_component_trace():
    definition = _control_definition()
    capture = {
        "captures": [
            {
                "query_id": "generic-term",
                "lane": "lexical",
                "results": [
                    {
                        "occurrence_id": "occ-a1",
                        "source_entity_id": "occ-a1",
                        "document_id": "doc-a1",
                        "lexical_rank": 1,
                        "lexical_occurrence_rank": 1,
                    },
                    {
                        "occurrence_id": "occ-new",
                        "source_entity_id": "occ-new",
                        "document_id": "doc-new",
                        "filename": "new.eml",
                        "text": "From: A\nTo: B\nCandidate",
                        "lexical_rank": 2,
                        "lexical_occurrence_rank": 2,
                        "source_relation_target_ids": ["thread-a"],
                    },
                ],
            }
        ]
    }
    result = aggregate_control_capture(definition, capture)
    assert result["reviewed_occurrences"] == 7
    assert result["outside_candidates_total"] == 1
    candidate = result["candidates"][0]
    assert candidate["candidate_id"] == "CONTROL-EXT-CORE-001"
    assert candidate["known_component_id"] == "component-a"
    assert candidate["new_component"] is False
    assert candidate["occurrence_ids"] == ["occ-new"]
    assert candidate["best_lexical_rank"] == 2
    assert result["inside_reviewed_intersections_by_pass"]["lexical-broad"] == [
        "occ-a1"
    ]


def test_control_capture_groups_new_thread_occurrences_deterministically():
    definition = _control_definition()
    relation = {
        "role": "member_of",
        "target": {"id": "thread-new", "type": "email_thread"},
    }
    capture = {
        "captures": [
            {
                "query_id": "generic-term",
                "lane": "lexical",
                "results": [
                    {
                        "occurrence_id": "occ-new-2",
                        "source_entity_id": "occ-new-2",
                        "document_id": "doc-new-2",
                        "lexical_rank": 3,
                        "lexical_occurrence_rank": 2,
                        "source_provenance": {"relations": [relation]},
                    },
                    {
                        "occurrence_id": "occ-new-1",
                        "source_entity_id": "occ-new-1",
                        "document_id": "doc-new-1",
                        "lexical_rank": 2,
                        "lexical_occurrence_rank": 1,
                        "source_provenance": {"relations": [relation]},
                    },
                ],
            }
        ]
    }
    result = aggregate_control_capture(definition, capture)
    assert result["outside_candidates_total"] == 1
    candidate = result["candidates"][0]
    assert candidate["candidate_id"] == "CONTROL-CORE-001"
    assert candidate["new_component"] is True
    assert candidate["occurrence_ids"] == ["occ-new-1", "occ-new-2"]
    assert candidate["best_lexical_rank"] == 2


def test_entire_component_seeded_and_false_positive_precision():
    metrics = compute_metrics(
        _definition(),
        [
            _hit("entity-a1", "doc-a1"),
            _hit("entity-a2", "doc-a2"),
            _hit("entity-n1", "doc-n1"),
        ],
        k=3,
    )
    assert metrics["seed_document_recall"]["value"] == 2 / 5
    assert metrics["seed_component_recall"]["value"] == 1 / 3
    assert metrics["precision"]["value"] == 2 / 3
    assert metrics["component_analysis"][0]["seeded"] is True
    assert metrics["component_analysis"][0]["first_seed_document_id"] == "doc-a1"


def test_one_seed_recovers_component_only_after_prov_o():
    metrics = compute_metrics(
        _definition(),
        [_hit("entity-a1", "doc-a1")],
        k=1,
        closure_documents=[
            _hit("entity-a1", "doc-a1"),
            _hit("entity-a2", "doc-a2"),
        ],
        coverage={"complete": True, "status_code": "complete", "failure_codes": []},
    )
    assert metrics["seed_document_recall"]["value"] == 1 / 5
    assert metrics["post_prov_o_document_recall"]["value"] == 2 / 5
    assert metrics["document_recovery_gain"] == 1
    assert metrics["recovery_multiplier"] == 2
    assert metrics["post_prov_o_component_recall"]["value"] == 1 / 3


def test_component_and_standalone_can_be_totally_missed():
    metrics = compute_metrics(
        _definition(),
        [_hit("entity-a1", "doc-a1")],
        k=1,
        closure_documents=[_hit("entity-a1", "doc-a1"), _hit("entity-a2", "doc-a2")],
    )
    rows = {row["component_id"]: row for row in metrics["component_analysis"]}
    assert rows["component-b"]["seeded"] is False
    assert rows["component-b"]["reached_after_closure"] is False
    assert rows["component-b"]["isolated_or_connected"] == "connected"
    assert rows["component-b"]["miss_analysis"]["cause"] == "unknown"
    assert rows["component-c"]["seeded"] is False
    assert rows["component-c"]["reached_after_closure"] is False
    assert rows["component-c"]["isolated_or_connected"] == "isolated"
    documents = {row["occurrence_id"]: row for row in metrics["document_analysis"]}
    assert documents["occ-b1"]["miss_analysis"]["cause"] == "unknown"
    assert documents["occ-c1"]["isolated_or_connected"] == "isolated"


def test_uncertain_seed_is_excluded_from_strict_precision():
    metrics = compute_metrics(
        _definition(),
        [_hit("entity-a1", "doc-a1"), _hit("entity-u1", "doc-u1")],
        k=2,
    )
    assert metrics["precision"]["value"] == 1.0
    assert metrics["precision"]["denominator"] == 1
    assert metrics["precision"]["review_coverage"] == 0.5
    assert metrics["precision"]["reliable"] is False


def test_duplicate_chunks_do_not_duplicate_one_occurrence():
    duplicated = _hit("entity-a1", "doc-a1")
    metrics = compute_metrics(_definition(), [duplicated, duplicated, duplicated], k=3)
    assert metrics["seed_occurrences"] == 1
    assert metrics["seed_document_recall"]["numerator"] == 1


def test_same_document_id_multiple_occurrences_remain_distinct():
    metrics = compute_metrics(
        _definition(),
        [
            _hit("entity-b1", "shared-content"),
            _hit("entity-b2", "shared-content"),
        ],
        k=2,
    )
    assert metrics["seed_occurrences"] == 2
    assert metrics["seed_document_recall"]["numerator"] == 2


def test_k_variants_use_one_ranked_capture_without_changing_data():
    seeds = [
        _hit("entity-a1", "doc-a1"),
        _hit("entity-n1", "doc-n1"),
        _hit("entity-c1", "doc-c1"),
    ]
    at_one = compute_metrics(_definition(), seeds, k=1)
    at_three = compute_metrics(_definition(), seeds, k=3)
    assert at_one["seed_document_recall"]["numerator"] == 1
    assert at_three["seed_document_recall"]["numerator"] == 2
    assert at_one["available_seed_chunks"] == at_three["available_seed_chunks"] == 3
    assert len(at_one["seed_capture"]) == 1
    assert len(at_three["seed_capture"]) == 3
    assert at_one["seed_occurrence_ids"] == ["entity-a1"]
    components = {row["component_id"]: row for row in at_one["component_analysis"]}
    assert components["component-c"]["present_outside_k"] is True
    assert components["component-c"]["best_rrf_rank"] == 3


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        ({"lexical_rank": 1}, "lexical"),
        ({"dense_rank": 1}, "dense"),
        ({"lexical_rank": 2, "dense_rank": 1, "rrf_rank": 1}, "both"),
        ({"rrf_rank": 1}, "none"),
    ],
)
def test_lexical_dense_and_rrf_channel_classification(extra, expected):
    metrics = compute_metrics(
        _definition(),
        [_hit("entity-a1", "doc-a1", **extra)],
        k=1,
    )
    assert metrics["component_analysis"][0]["retrieval_channel"] == expected


def test_incomplete_coverage_is_not_recall():
    metrics = compute_metrics(
        _definition(),
        [_hit("entity-a1", "doc-a1")],
        k=1,
        closure_documents=[_hit("entity-a1", "doc-a1")],
        coverage={
            "complete": False,
            "status_code": "document_read_incomplete",
            "failure_codes": ["document_read_incomplete"],
        },
    )
    assert metrics["coverage_complete"] is False
    assert metrics["seed_document_recall"]["value"] == 1 / 5
    assert metrics["post_prov_o_document_recall"]["value"] == 1 / 5
    rate = coverage_success_rate([metrics, {"coverage_complete": True}])
    assert rate["value"] == 0.5


def test_dls_visible_corpus_counts_occurrences_and_distinct_documents():
    files = [
        _hit("entity-1", "shared", source_entity_system="archive-a"),
        _hit("entity-2", "shared", source_entity_system="archive-b"),
        _hit("entity-3", "unique", connector_type="local"),
    ]

    def get_json(_url):
        return {
            "files": files,
            "total": 3,
            "page": 1,
            "page_size": 1000,
            "next_cursor": None,
            "prefetched_pages": [],
        }

    snapshot = snapshot_visible_corpus("https://example.test", get_json=get_json)
    assert snapshot["visible_occurrences"] == 3
    assert snapshot["distinct_document_ids"] == 2
    assert snapshot["distinct_source_entity_ids"] == 3
    assert snapshot["sources"] == ["archive-a", "archive-b", "local"]
    assert snapshot["complete"] is True
    changed = copy.deepcopy(snapshot)
    changed["occurrence_identity_sha256"] = "different"
    assert corpus_changed(snapshot, changed) is True


def test_metric_computation_and_review_export_are_deterministic():
    definition = _definition()
    seeds = [_hit("entity-a1", "doc-a1"), _hit("entity-a2", "doc-a2")]
    assert compute_metrics(definition, seeds, k=2) == compute_metrics(definition, seeds, k=2)

    focused = {"results": [_hit("urn:seed", "doc-seed", score=0.02, filename="seed.eml")]}
    scope = {
        "documents": [
            {
                "source_entity_id": "urn:seed",
                "document_id": "doc-seed",
                "filename": "seed.eml",
                "source_provenance": {
                    "entity": {"id": "urn:seed", "label": "Subject", "type": "email_message"},
                    "relations": [],
                },
            },
            {
                "source_entity_id": "urn:graph",
                "document_id": "doc-graph",
                "filename": "graph.eml",
                "source_provenance": {
                    "entity": {"id": "urn:graph", "label": "Reply", "type": "email_message"},
                    "relations": [],
                },
            },
        ],
        "results": [
            {
                "source_entity_id": "urn:seed",
                "document_id": "doc-seed",
                "chunk_index": 0,
                "text": "Subject\nFrom: Alice <a@example.test>\nTo: Bob <b@example.test>\nBody",
            },
            {
                "source_entity_id": "urn:graph",
                "document_id": "doc-graph",
                "chunk_index": 0,
                "text": "Reply\nFrom: Bob <b@example.test>\nTo: Alice <a@example.test>\nBody",
            },
        ],
        "graph": {
            "edges": [
                {
                    "source_entity_id": "urn:graph",
                    "role": "reply_to",
                    "target_entity_id": "urn:seed",
                }
            ]
        },
    }
    rows = build_review_rows(focused, scope)
    assert rows == build_review_rows(focused, scope)
    by_id = {row["source_entity_id"]: row for row in rows}
    assert by_id["urn:seed"]["depth"] == 0
    assert by_id["urn:graph"]["depth"] == 1
    assert by_id["urn:graph"]["path_roles"] == "reply_to"
    assert by_id["urn:seed"]["sender"] == "Alice <a@example.test>"
