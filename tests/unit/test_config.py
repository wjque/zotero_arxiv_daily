from __future__ import annotations

from pathlib import Path

import pytest

from zotero_arxiv_daily.core.config import load_config
from zotero_arxiv_daily.core.errors import ConfigurationError


def test_default_recommendation_output_language_is_english() -> None:
    config = load_config(environment={})

    assert config.output_language == "en"
    assert config.feedback_activation_interval_days == 7
    assert config.llm_request_token_limit == 12_000
    assert config.llm_request_byte_limit == 65_536
    assert config.state_encryption_key is None


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
    with pytest.raises(ConfigurationError, match="feedback_activation_interval_days"):
        load_config(environment={"ZAD_FEEDBACK_ACTIVATION_INTERVAL_DAYS": "6"})


def test_refinement_preference_context_requires_an_explicit_enabled_refinement_path() -> None:
    config = load_config(
        environment={
            "ZAD_LLM_REFINEMENT_ENABLED": "true",
            "ZAD_LLM_PREFERENCE_CONTEXT_APPROVED": "true",
        }
    )

    assert config.llm_refinement_enabled
    assert config.llm_preference_context_approved
    with pytest.raises(ConfigurationError, match="requires llm_refinement_enabled"):
        load_config(environment={"ZAD_LLM_PREFERENCE_CONTEXT_APPROVED": "true"})


def test_state_encryption_key_is_separate_and_bounded() -> None:
    config = load_config(environment={"ZAD_STATE_ENCRYPTION_KEY": "state-passphrase-1234"})

    assert config.state_encryption_key == "state-passphrase-1234"
    with pytest.raises(ConfigurationError, match="state_encryption_key"):
        load_config(environment={"ZAD_STATE_ENCRYPTION_KEY": "too-short"})


def test_llm_budget_configuration_is_bounded_and_typed() -> None:
    config = load_config(
        environment={
            "ZAD_LLM_JUDGE_BATCH_SIZE": "12",
            "ZAD_LLM_EXPLANATION_BATCH_SIZE": "6",
            "ZAD_LLM_REQUEST_TOKEN_LIMIT": "8000",
            "ZAD_LLM_REQUEST_BYTE_LIMIT": "32768",
            "ZAD_LLM_MAX_REQUESTS": "3",
            "ZAD_LLM_RETRIES": "2",
            "ZAD_LLM_MAX_OUTPUT_TOKENS": "8000",
        }
    )

    assert config.llm_judge_batch_size == 12
    assert config.llm_explanation_batch_size == 6
    assert config.llm_max_requests == 3
    assert config.llm_retries == 2
    with pytest.raises(ConfigurationError, match="llm_request_byte_limit"):
        load_config(environment={"ZAD_LLM_REQUEST_BYTE_LIMIT": "1024"})


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
