import json
from pathlib import Path

from benchmarks.discovery.cross_domain_horizon import build_campaign_plan


def test_cross_domain_plan_changes_only_the_candidate_horizon():
    plan = build_campaign_plan(
        gt1_query="gt1 query",
        gt2_query="gt2 query",
        evidence_context={"corpus_digest": "stable"},
    )
    experiments = plan["sensitivity_experiments"]

    assert len(experiments) == 18
    assert {row["ground_truth"] for row in experiments} == {"gt1", "gt2"}
    assert {row["repeat"] for row in experiments} == {1, 2, 3}
    assert {
        (row["lexical_candidates"], row["dense_candidates"]) for row in experiments
    } == {(50, 50), (100, 100), (200, 200)}
    assert {
        (
            row["query_count"],
            row["rrf_k"],
            row["seed_budget"],
            row["max_depth"],
            row["max_entities"],
            row["max_documents"],
            row["batch_size"],
        )
        for row in experiments
    } == {(1, 60, 100, 8, 500, 250, 50)}


def test_cross_domain_evidence_preserves_fail_closed_coverage_and_reports_drift():
    root = Path(__file__).resolve().parents[3]
    analysis = json.loads(
        (
            root
            / "benchmarks/discovery/results/cross-domain-q1-candidate-horizons-analysis.json"
        ).read_text(encoding="utf-8")
    )
    summaries = {
        (row["ground_truth"], row["candidate_horizon"]): row
        for row in analysis["summaries"]
    }

    assert analysis["corpus"]["comparable"] is True
    assert summaries[("gt1", 50)]["coverage_success_rate"] == 1.0
    assert summaries[("gt1", 100)]["coverage_success_rate"] == 1.0
    assert summaries[("gt1", 200)]["coverage_success_rate"] == 1.0
    assert summaries[("gt2", 50)]["coverage_success_rate"] == 1.0
    assert summaries[("gt2", 100)]["coverage_success_rate"] == 0.0
    assert summaries[("gt2", 200)]["coverage_success_rate"] == 0.0
    assert summaries[("gt2", 100)]["strict"]["coverage"]["status_code"] == (
        "document_limit_reached"
    )
    assert summaries[("gt2", 200)]["strict"]["coverage"]["status_code"] == (
        "document_limit_reached"
    )
    assert analysis["determinism"]["all_groups_seed_identity_sets_stable"] is True
    assert analysis["determinism"]["all_groups_scope_identity_sets_stable"] is True
    assert analysis["determinism"]["all_groups_ordered_identities_stable"] is False
    assert analysis["determinism"]["all_groups_metrics_identical"] is False
    assert analysis["determinism"]["status"] == "FAIL"
    assert analysis["determinism"]["audit"]["quality_interpretation_authorized"] is False
    assert analysis["decision"] == {
        "candidate_horizon_recommendation": "KEEP 50/50",
        "rationale": (
            "The change gate failed: ordered retrieval was not deterministic across all "
            "groups, and GT2 100/100 and 200/200 reached the fail-closed document limit. "
            "Keeping 50/50 is a no-change safety decision, not a claim of quality "
            "superiority."
        ),
        "qwen_readiness": "BLOCKED",
        "final_conclusion": "GT2 FROZEN - CROSS-DOMAIN EVIDENCE INSUFFICIENT",
    }
    assert "never" in analysis["unjudged_policy"].lower()
