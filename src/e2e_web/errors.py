"""Stable exceptions for third-party test runners and host SDKs."""

from typing import Any


class E2EError(Exception):
    """SDK failure with a machine-readable code and optional safe details."""

    def __init__(self, code: str, message: str, *, details: Any = None):
        self.code, self.details = code, details
        super().__init__(f"{code}: {message}")


class AssertionFailed(E2EError, AssertionError):
    """Natural-language or deterministic verification did not pass."""


class StepTimeout(E2EError, TimeoutError):
    """The session or step deadline elapsed."""


class ReplayStale(E2EError):
    """An existing recording cannot be trusted in this context."""
