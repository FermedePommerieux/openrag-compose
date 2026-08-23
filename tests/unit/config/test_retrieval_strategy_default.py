"""Upgrade policy for Retrieval v2's Standard strategy."""

from config.config_manager import ConfigManager


def test_new_install_uses_rrf_standard_default(tmp_path):
    config = ConfigManager(config_file=str(tmp_path / "config.yaml")).load_config()

    assert config.knowledge.retrieval_strategy == "rrf"


def test_legacy_config_without_retrieval_choice_upgrades_to_rrf(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text("knowledge:\n  chunk_size: 1000\n")

    config = ConfigManager(config_file=str(config_file)).load_config()

    assert config.knowledge.retrieval_strategy == "rrf"


def test_explicit_legacy_weighted_choice_is_preserved(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text("knowledge:\n  retrieval_strategy: weighted\n")

    config = ConfigManager(config_file=str(config_file)).load_config()

    assert config.knowledge.retrieval_strategy == "weighted"
