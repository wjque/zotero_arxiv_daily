from __future__ import annotations

from pathlib import Path

import pytest

from zotero_arxiv_daily.core.config import load_config
from zotero_arxiv_daily.core.errors import ConfigurationError


def test_default_recommendation_output_language_is_english() -> None:
    assert load_config(environment={}).output_language == "en"


def test_configuration_precedence_is_defaults_file_environment_then_cli(tmp_path: Path) -> None:
    config_path = tmp_path / "settings.toml"
    config_path.write_text(
        'zotero_base_url = "http://localhost:23119"\noutput_language = "en"\n', encoding="utf-8"
    )

    config = load_config(
        config_path=config_path,
        environment={"ZAD_OUTPUT_LANGUAGE": "ja", "ZAD_PUBLIC_OUTPUT": "true"},
        overrides={"output_language": "zh-CN"},
    )

    assert config.zotero_base_url == "http://localhost:23119"
    assert config.output_language == "zh-CN"
    assert config.public_output is True


def test_configuration_rejects_non_local_zotero_url() -> None:
    with pytest.raises(ConfigurationError, match="localhost"):
        load_config(overrides={"zotero_base_url": "https://example.test"})


def test_configuration_rejects_unknown_file_key(tmp_path: Path) -> None:
    config_path = tmp_path / "settings.json"
    config_path.write_text('{"unexpected": true}', encoding="utf-8")

    with pytest.raises(ConfigurationError, match="unsupported"):
        load_config(config_path=config_path, environment={})


def test_configuration_validates_model_timeout_and_candidate_limit() -> None:
    config = load_config(
        environment={
            "ZAD_DEEPSEEK_TIMEOUT_SECONDS": "60",
            "ZAD_RECOMMENDATION_CANDIDATE_LIMIT": "40",
        }
    )

    assert config.deepseek_timeout_seconds == 60.0
    assert config.recommendation_candidate_limit == 40
    with pytest.raises(ConfigurationError, match="candidate_limit"):
        load_config(environment={"ZAD_RECOMMENDATION_CANDIDATE_LIMIT": "39"})


def test_configuration_loads_structured_bounded_watchlists(tmp_path: Path) -> None:
    path = tmp_path / "settings.toml"
    path.write_text(
        '[[watched_authors]]\nname = "Saining Xie"\naliases = ["Xie Saining"]\n'
        '[[watched_institutions]]\nname = "DeepMind"\naliases = ["Google DeepMind"]\n',
        encoding="utf-8",
    )

    config = load_config(config_path=path, environment={})

    assert config.watched_authors[0].name == "Saining Xie"
    assert config.watched_institutions[0].aliases == ("Google DeepMind",)
