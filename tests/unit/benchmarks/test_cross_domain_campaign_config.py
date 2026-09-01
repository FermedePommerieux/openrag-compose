import json
from pathlib import Path

CONFIG = (
    Path(__file__).parents[3] / "benchmarks/discovery/configs/cross-domain-closure-sample-v1.json"
)


def _config() -> dict:
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def test_closure_sample_is_predeclared_stratified_and_reproducible():
    config = _config()
    cases = config["documentary_target_validation_grid"]

    assert len(cases) >= 30
    assert [case["sampling_order"] for case in cases] == list(range(1, len(cases) + 1))
    assert len({case["query"] for case in cases}) == len(cases)
    assert len({case["sampling_family"] for case in cases}) >= 8
    assert sum(case["sampling_family"] == "pathological_candidate" for case in cases) >= 1
    assert "anti_cherry_picking" in config["sampling_methodology"]
    assert config["evidence_context"]["production_defaults_changed"] is False


def test_target_probe_hard_grid_calibrates_dimensions_separately():
    defaults = _config()["documentary_target_validation_defaults"]
    grid = defaults["validation_configurations"]

    assert {row["target_threshold"] for row in grid} == {200, 250, 300}
    assert {row["validation_probe_size"] for row in grid} == {25, 50}
    assert {row["hard_safety_limit"] for row in grid} == {400, 500, 750}
    assert defaults["fixed_limits"] == [250, 400, 500]
    assert defaults["max_depth"] == 8
    assert "batch_size" not in defaults
    assert "diagnostic_guard_rationale" in defaults


def test_campaign_contains_required_known_closures_without_q4_plans():
    config = _config()
    cases = config["documentary_target_validation_grid"]
    queries = {case["query"] for case in cases}

    assert {
        "Tous les échanges avec Orange au sujet de la fibre.",
        "Tous les documents relatifs à une panne réseau intermittente.",
        "Tous les échanges concernant le renouvellement du contrat Alpha.",
        "Retrouve les correspondances concernant la succession Dupont.",
        "Donne-moi tous les échanges avec l’administration sur le projet Surface pastorale.",
    } <= queries
    assert all("fixed_queries" not in case for case in cases)


def test_campaign_keeps_generic_regression_families_cross_domain():
    cases = _config()["documentary_target_validation_grid"]
    families = {case["sampling_family"] for case in cases}
    queries = {case["query"] for case in cases}

    assert {
        "contractual_history",
        "technical_incident",
        "person_entity_chronology",
        "precise_entity_lookup",
        "correspondence_investigation",
        "project_history",
        "multi_party_topic",
        "small_documentary_component",
    } <= families
    assert {
        "Facture FR62442289",
        "Tous les documents relatifs à une panne réseau intermittente.",
        "Historique d’un projet de rénovation de bâtiment.",
        "Échanges entre banque, comptable et fournisseur.",
    } <= queries
