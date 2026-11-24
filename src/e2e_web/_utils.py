"""JSON, redaction, timeout and input-template implementation helpers."""

from __future__ import annotations

import hashlib
import json
import math
import time
import unicodedata
from typing import Any

from .errors import E2EError, StepTimeout
from .types import Cancellation, Secret, Unique


def canonical(value: Any) -> str:
    """Encode only finite JSON values, with stable object ordering."""
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise E2EError("INVALID_ARGUMENT", "expected finite JSON data") from exc


def digest(value: Any) -> str:
    """Hash canonical JSON for identity and observation comparisons."""
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def normalize(value: str) -> str:
    """Normalize instruction identity without changing internal whitespace."""
    return unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n")).strip()


def positive(value: float, label: str) -> float:
    """Require finite positive durations and configuration limits."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise E2EError("INVALID_ARGUMENT", f"{label} must be finite and positive")
    return value


class Deadline:
    """One remaining budget for observations, model calls and actions."""

    def __init__(self, seconds: float, cancellation: Cancellation, session_end: float | None = None):
        self.end = min(time.monotonic() + positive(seconds, "timeout"), session_end or float("inf"))
        self.cancellation = cancellation

    def remaining(self) -> float:
        """Fail before an operation starts if its deadline or cancellation fired."""
        if self.cancellation.event.is_set():
            raise E2EError("CANCELLED", "session cancelled")
        remaining = self.end - time.monotonic()
        if remaining <= 0:
            raise StepTimeout("STEP_TIMEOUT", "operation deadline elapsed")
        return remaining


class Ledger:
    """Keep secret values outside executor requests and persistent SDK artifacts."""

    def __init__(self):
        self.secrets: dict[str, Secret] = {}

    def register(self, secret: Secret) -> None:
        """Reject ambiguous names and empty values before observing a page."""
        if not secret.name or not secret.value:
            raise E2EError("INVALID_ARGUMENT", "secret name and value must be nonempty")
        previous = self.secrets.get(secret.name)
        if previous is not None and previous != secret:
            raise E2EError("INVALID_ARGUMENT", "secret name already registered with another value")
        self.secrets[secret.name] = secret

    def redact(self, value: Any) -> Any:
        """Redact strings recursively, including dictionary keys."""
        if isinstance(value, str):
            for secret in sorted(self.secrets.values(), key=lambda s: len(s.value), reverse=True):
                value = value.replace(secret.value, f"<secret:{secret.name}>")
            return value
        if isinstance(value, dict):
            return {self.redact(k): self.redact(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.redact(v) for v in value]
        return value

    def project(self, params: dict[str, Any]) -> tuple[dict, dict[str, str]]:
        """Project secrets to handles and Unique values to separately tracked slots."""
        unique: dict[str, str] = {}

        def walk(value: Any, path: str, depth: int) -> Any:
            if depth > 32:
                raise E2EError("INVALID_ARGUMENT", "params exceed 32 levels")
            if isinstance(value, Secret):
                self.register(value)
                return {"kind": "secret", "name": value.name, "purpose": value.purpose}
            if isinstance(value, Unique):
                if not value.value:
                    raise E2EError("INVALID_ARGUMENT", "unique input must be nonempty")
                slot = digest(path)[:16]
                unique[slot] = value.value
                return value.value
            if isinstance(value, dict):
                if not all(isinstance(k, str) for k in value):
                    raise E2EError("INVALID_ARGUMENT", "params keys must be strings")
                return {k: walk(v, path + "/" + k.replace("~", "~0").replace("/", "~1"), depth + 1) for k, v in value.items()}
            if isinstance(value, list):
                return [walk(v, path + "/" + str(i), depth + 1) for i, v in enumerate(value)]
            return value

        projected = walk(params, "", 0)
        if len(canonical(projected).encode()) > 65536:
            raise E2EError("INVALID_ARGUMENT", "params exceed 64 KiB")
        values = list(unique.values())
        if any(a in b or b in a for i, a in enumerate(values) for b in values[i + 1:]):
            raise E2EError("INVALID_ARGUMENT", "unique values overlap; use distinct values")
        return self.redact(projected), unique


def template(value: Any, slots: dict[str, str], *, expand: bool = False) -> Any:
    """Substitute per-run unique strings recursively in JSON string values."""
    if isinstance(value, str):
        for slot, actual in sorted(slots.items(), key=lambda x: len(x[1]), reverse=True):
            marker = f"{{{{e2e_unique:{slot}}}}}"
            value = value.replace(marker, actual) if expand else value.replace(actual, marker)
        return value
    if isinstance(value, dict):
        return {k: template(v, slots, expand=expand) for k, v in value.items()}
    if isinstance(value, list):
        return [template(v, slots, expand=expand) for v in value]
    return value
