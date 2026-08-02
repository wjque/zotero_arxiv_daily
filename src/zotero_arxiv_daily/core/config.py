"""Configuration loading and validation with explicit precedence."""

from __future__ import annotations

import json
import os
import re
import tomllib
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from zotero_arxiv_daily.core.errors import ConfigurationError

_GITHUB_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_LOCAL_ZOTERO_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_ENVIRONMENT_KEYS = {
    "ZAD_ZOTERO_BASE_URL": "zotero_base_url",
    "ZAD_LOCAL_DATABASE_PATH": "local_database_path",
    "ZAD_DEEPSEEK_API_KEY": "deepseek_api_key",
    "ZAD_DEEPSEEK_TIMEOUT_SECONDS": "deepseek_timeout_seconds",
    "ZAD_RECOMMENDATION_CANDIDATE_LIMIT": "recommendation_candidate_limit",
    "ZAD_GITHUB_REPOSITORY": "github_repository",
    "ZAD_GITHUB_TOKEN": "github_token",
    "ZAD_PAGES_PASSPHRASE": "pages_passphrase",
    "ZAD_PUBLIC_OUTPUT": "public_output",
    "ZAD_OUTPUT_LANGUAGE": "output_language",
    "ZAD_AUTHOR_PREFERENCE_BONUS": "author_preference_bonus",
    "ZAD_INSTITUTION_PREFERENCE_BONUS": "institution_preference_bonus",
    "ZAD_IDENTITY_BONUS_CAP": "identity_bonus_cap",
    "ZAD_RECOMMENDATION_SUPPRESSION_DAYS": "recommendation_suppression_days",
}
_FILE_KEYS = frozenset(_ENVIRONMENT_KEYS.values()) | {"watched_authors", "watched_institutions"}


@dataclass(frozen=True, slots=True)
class ConfiguredIdentity:
    """Structured watched identity loaded from TOML or JSON."""

    name: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AppConfig:
    """Non-secret defaults and externally supplied operational settings."""

    zotero_base_url: str = "http://127.0.0.1:23119"
    local_database_path: str = "runtime/zotero.sqlite3"
    deepseek_api_key: str | None = None
    deepseek_timeout_seconds: float = 60.0
    recommendation_candidate_limit: int = 40
    github_repository: str | None = None
    github_token: str | None = None
    pages_passphrase: str | None = None
    public_output: bool = False
    output_language: str = "en"
    watched_authors: tuple[ConfiguredIdentity, ...] = ()
    watched_institutions: tuple[ConfiguredIdentity, ...] = ()
    author_preference_bonus: float = 0.75
    institution_preference_bonus: float = 0.5
    identity_bonus_cap: float = 1.0
    recommendation_suppression_days: int = 14

    def validate(self) -> None:
        """Validate values that are safe to check before an operation starts."""

        parsed_url = urlparse(self.zotero_base_url)
        if parsed_url.scheme != "http" or parsed_url.hostname not in _LOCAL_ZOTERO_HOSTS:
            raise ConfigurationError(
                "zotero_base_url must be an HTTP URL hosted by localhost, 127.0.0.1, or ::1"
            )
        if self.github_repository and not _GITHUB_REPOSITORY.fullmatch(self.github_repository):
            raise ConfigurationError("github_repository must use the owner/repository format")
        if not self.output_language.strip():
            raise ConfigurationError("output_language must not be empty")
        if not 10 <= self.deepseek_timeout_seconds <= 120:
            raise ConfigurationError("deepseek_timeout_seconds must be between 10 and 120")
        if not 40 <= self.recommendation_candidate_limit <= 80:
            raise ConfigurationError("recommendation_candidate_limit must be between 40 and 80")
        if self.public_output and self.pages_passphrase:
            raise ConfigurationError("pages_passphrase must be unset when public_output is enabled")
        if not 0 <= self.author_preference_bonus <= 1:
            raise ConfigurationError("author_preference_bonus must be between zero and one")
        if not 0 <= self.institution_preference_bonus <= 1:
            raise ConfigurationError("institution_preference_bonus must be between zero and one")
        if not 0 <= self.identity_bonus_cap <= 1:
            raise ConfigurationError("identity_bonus_cap must be between zero and one")
        if not 1 <= self.recommendation_suppression_days <= 30:
            raise ConfigurationError("recommendation_suppression_days must be between 1 and 30")


def load_config(
    *,
    config_path: Path | None = None,
    environment: Mapping[str, str] | None = None,
    overrides: Mapping[str, object] | None = None,
) -> AppConfig:
    """Load configuration using defaults, file, environment, and CLI overrides."""

    values: dict[str, object] = {}
    if config_path is not None:
        values.update(_read_config_file(config_path))
    values.update(_read_environment(environment if environment is not None else os.environ))
    if overrides:
        values.update({key: value for key, value in overrides.items() if value is not None})
    normalized_values = _normalize_values(values)
    defaults = AppConfig()
    config = AppConfig(
        zotero_base_url=_string_value(
            normalized_values, "zotero_base_url", defaults.zotero_base_url
        ),
        local_database_path=_string_value(
            normalized_values, "local_database_path", defaults.local_database_path
        ),
        deepseek_api_key=_optional_string_value(normalized_values, "deepseek_api_key"),
        deepseek_timeout_seconds=_float_value(
            normalized_values, "deepseek_timeout_seconds", defaults.deepseek_timeout_seconds
        ),
        recommendation_candidate_limit=_int_value(
            normalized_values,
            "recommendation_candidate_limit",
            defaults.recommendation_candidate_limit,
        ),
        github_repository=_optional_string_value(normalized_values, "github_repository"),
        github_token=_optional_string_value(normalized_values, "github_token"),
        pages_passphrase=_optional_string_value(normalized_values, "pages_passphrase"),
        public_output=_bool_value(normalized_values, "public_output", defaults.public_output),
        output_language=_string_value(
            normalized_values, "output_language", defaults.output_language
        ),
        watched_authors=_identity_list(normalized_values, "watched_authors"),
        watched_institutions=_identity_list(normalized_values, "watched_institutions"),
        author_preference_bonus=_float_value(
            normalized_values, "author_preference_bonus", defaults.author_preference_bonus
        ),
        institution_preference_bonus=_float_value(
            normalized_values,
            "institution_preference_bonus",
            defaults.institution_preference_bonus,
        ),
        identity_bonus_cap=_float_value(
            normalized_values, "identity_bonus_cap", defaults.identity_bonus_cap
        ),
        recommendation_suppression_days=_int_value(
            normalized_values,
            "recommendation_suppression_days",
            defaults.recommendation_suppression_days,
        ),
    )
    config.validate()
    return config


def _read_config_file(path: Path) -> dict[str, object]:
    if not path.is_file():
        raise ConfigurationError(f"configuration file does not exist: {path}")
    try:
        if path.suffix.lower() == ".toml":
            raw: object = tomllib.loads(path.read_text(encoding="utf-8"))
        elif path.suffix.lower() == ".json":
            raw = json.loads(path.read_text(encoding="utf-8"))
        else:
            raise ConfigurationError("configuration file must use a .toml or .json extension")
    except (OSError, json.JSONDecodeError, tomllib.TOMLDecodeError) as error:
        raise ConfigurationError(f"unable to read configuration file: {path}") from error
    if not isinstance(raw, dict):
        raise ConfigurationError("configuration root must be an object")
    unknown = set(raw).difference(_FILE_KEYS)
    if unknown:
        raise ConfigurationError(f"unsupported configuration key(s): {', '.join(sorted(unknown))}")
    return dict(raw)


def _read_environment(environment: Mapping[str, str]) -> dict[str, object]:
    return {
        config_key: environment[environment_key]
        for environment_key, config_key in _ENVIRONMENT_KEYS.items()
        if environment_key in environment and environment[environment_key] != ""
    }


def _normalize_values(values: Mapping[str, object]) -> dict[str, object]:
    normalized = dict(values)
    if "public_output" in normalized:
        normalized["public_output"] = _parse_bool(normalized["public_output"], "public_output")
    for name in ("deepseek_api_key", "github_repository", "github_token", "pages_passphrase"):
        if name in normalized and not isinstance(normalized[name], str):
            raise ConfigurationError(f"{name} must be a string")
    for name in ("zotero_base_url", "local_database_path", "output_language"):
        if name in normalized and not isinstance(normalized[name], str):
            raise ConfigurationError(f"{name} must be a string")
    for name in (
        "deepseek_timeout_seconds",
        "recommendation_candidate_limit",
        "author_preference_bonus",
        "institution_preference_bonus",
        "identity_bonus_cap",
        "recommendation_suppression_days",
    ):
        value = normalized.get(name)
        if isinstance(value, str):
            try:
                normalized[name] = float(value)
            except ValueError as error:
                raise ConfigurationError(f"{name} must be numeric") from error
        if name in normalized and not isinstance(normalized[name], (int, float)):
            raise ConfigurationError(f"{name} must be numeric")
    return normalized


def _identity_list(values: Mapping[str, object], name: str) -> tuple[ConfiguredIdentity, ...]:
    raw = values.get(name, [])
    if not isinstance(raw, list) or len(raw) > 32:
        raise ConfigurationError(f"{name} must be an array with at most 32 entries")
    identities: list[ConfiguredIdentity] = []
    for entry in raw:
        if (
            not isinstance(entry, dict)
            or not set(entry) <= {"name", "aliases"}
            or "name" not in entry
        ):
            raise ConfigurationError(f"{name} entries require only name and optional aliases")
        identity_name = entry["name"]
        aliases = entry.get("aliases", [])
        if not isinstance(identity_name, str) or not isinstance(aliases, list):
            raise ConfigurationError(f"{name} names and aliases must be strings")
        if len(aliases) > 8 or not all(isinstance(alias, str) for alias in aliases):
            raise ConfigurationError(f"{name} entries may contain at most 8 string aliases")
        identity_values = (identity_name, *aliases)
        if any(not value.strip() or len(value.encode("utf-8")) > 160 for value in identity_values):
            raise ConfigurationError(f"{name} contains an invalid identity")
        normalized = {
            " ".join(
                re.sub(r"[^\w]+", " ", unicodedata.normalize("NFKC", value).casefold()).split()
            )
            for value in identity_values
        }
        if len(normalized) != len(identity_values):
            raise ConfigurationError(f"{name} contains duplicate normalized aliases")
        identities.append(ConfiguredIdentity(identity_name, tuple(aliases)))
    return tuple(identities)


def _parse_bool(value: object, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        match value.casefold():
            case "true" | "1" | "yes":
                return True
            case "false" | "0" | "no":
                return False
    raise ConfigurationError(f"{field} must be a boolean")


def _string_value(values: Mapping[str, object], name: str, default: str) -> str:
    value = values.get(name, default)
    if isinstance(value, str):
        return value
    raise AssertionError(f"{name} was not normalized")


def _optional_string_value(values: Mapping[str, object], name: str) -> str | None:
    value = values.get(name)
    if value is None or isinstance(value, str):
        return value
    raise AssertionError(f"{name} was not normalized")


def _bool_value(values: Mapping[str, object], name: str, default: bool) -> bool:
    value = values.get(name, default)
    if isinstance(value, bool):
        return value
    raise AssertionError(f"{name} was not normalized")


def _float_value(values: Mapping[str, object], name: str, default: float) -> float:
    value = values.get(name, default)
    if isinstance(value, (int, float)):
        return float(value)
    raise AssertionError(f"{name} was not normalized")


def _int_value(values: Mapping[str, object], name: str, default: int) -> int:
    value = values.get(name, default)
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    raise ConfigurationError(f"{name} must be an integer")
