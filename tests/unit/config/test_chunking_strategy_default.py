"""Default and compatibility policy for document chunking."""

from config.config_manager import ConfigManager


def test_new_install_uses_hybrid_chunking_by_default(tmp_path):
    config = ConfigManager(config_file=str(tmp_path / "config.yaml")).load_config()

    assert config.knowledge.chunking_strategy == "hybrid"


def test_legacy_config_without_chunking_choice_upgrades_to_hybrid(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text("knowledge:\n  chunk_size: 1000\n")

    config = ConfigManager(config_file=str(config_file)).load_config()

    assert config.knowledge.chunking_strategy == "hybrid"


def test_explicit_character_choice_is_preserved(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text("knowledge:\n  chunking_strategy: character\n")

    config = ConfigManager(config_file=str(config_file)).load_config()

    assert config.knowledge.chunking_strategy == "character"
