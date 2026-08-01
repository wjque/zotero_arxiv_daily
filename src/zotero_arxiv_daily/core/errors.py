"""Application error taxonomy and process exit codes."""

from __future__ import annotations

from enum import IntEnum


class ExitCode(IntEnum):
    """Stable exit codes for CLI automation."""

    SUCCESS = 0
    CONFIGURATION = 2
    DEPENDENCY_UNAVAILABLE = 3
    OPERATIONAL_FAILURE = 4


class ApplicationError(Exception):
    """Base class for expected application failures."""

    exit_code = ExitCode.OPERATIONAL_FAILURE


class ConfigurationError(ApplicationError):
    """Raised when configuration is malformed or violates an invariant."""

    exit_code = ExitCode.CONFIGURATION


class ExternalServiceError(ApplicationError):
    """Raised when a required external boundary cannot be reached safely."""

    exit_code = ExitCode.DEPENDENCY_UNAVAILABLE


class SecurityError(ApplicationError):
    """Raised when protected data cannot be encrypted, authenticated, or safely decoded."""
