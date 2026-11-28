"""JSON, redaction, timeout and input-template implementation helpers."""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
import unicodedata
from typing import Any
from urllib.parse import quote, quote_plus

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


def truncate_utf8(text: str, limit: int) -> str:
    """Cut at a character boundary instead of splitting a multi-byte sequence."""
    encoded = text.encode()
    if len(encoded) <= limit:
        return text
    return encoded[:max(0, limit)].decode("utf-8", "ignore")


# Headroom reserved for the instruction, the prompt markup and a repair section.
INSTRUCTION_RESERVE = 4096
MIN_OBSERVATION_BYTES = 1024
IMAGE_TILE_PX = 28


def image_patch_tokens(width: int, height: int) -> int:
    """Conservative token bound for one image, as tile-based vision encoders bill it."""
    return max(1, -(-max(1, width) // IMAGE_TILE_PX)) * max(1, -(-max(1, height) // IMAGE_TILE_PX))


def png_size(header: bytes) -> tuple[int, int]:
    """The pixel size of a PNG, from its fixed-layout header."""
    if len(header) < 24 or not header.startswith(b"\x89PNG\r\n\x1a\n") or header[12:16] != b"IHDR":
        raise E2EError("INVALID_ARGUMENT", "input budgeting requires a PNG screenshot")
    return int.from_bytes(header[16:20], "big"), int.from_bytes(header[20:24], "big")


def trim_observation(view: dict, budget: int) -> dict:
    """Trim one observation to a byte budget, keeping the node ids a reference needs.

    A clipped view is marked truncated, so nothing can read an absence out of it.
    """
    if len(canonical(view).encode()) <= budget:
        return view
    trimmed = dict(view, truncated=True, tree="")
    trimmed["frames"] = [frame for frame in view.get("frames") or () if frame.get("tree") == "<frame unavailable>"]
    trimmed["nodes"] = []
    if len(canonical(trimmed).encode()) > budget:
        raise E2EError("STEP_BUDGET_EXHAUSTED", "request metadata exceeds the model input budget")
    kept = []
    for node in view.get("nodes") or ():
        kept.append(node)
        if len(canonical(dict(trimmed, nodes=kept)).encode()) > budget:
            kept.pop()
            break
    trimmed["nodes"] = kept
    tree = view.get("tree") or ""
    room = budget - len(canonical(trimmed).encode()) - 16
    if tree and room > 32:
        prefix = truncate_utf8(tree, room)
        while len(canonical(prefix).encode()) - 2 > room:
            prefix = prefix[:len(prefix) // 2]
        trimmed["tree"] = prefix
    return trimmed


def bound_output(value: Any, *, max_lines: int, max_bytes: int) -> Any:
    """Keep one tool result a receipt, so it cannot multiply by every later turn."""
    text = value if isinstance(value, str) else canonical(value)
    lines = text.splitlines()
    size = len(text.encode())
    if len(lines) <= max_lines and size <= max_bytes:
        return value
    notice = f"\n[tool output truncated: showing a bounded part of {len(lines)} line(s), {size} byte(s)]"
    body = truncate_utf8("\n".join(lines[:max_lines]), max_bytes - len(notice.encode()))
    if isinstance(value, str):
        return body + notice

    # JSON escapes in the preview are escaped again inside its string wrapper.
    # Measure the actual envelope before choosing how much of the preview fits.
    low, high = 0, len(body)
    while low < high:
        end = (low + high + 1) // 2
        preview = {"truncated": True, "text": body[:end] + notice}
        if len(canonical(preview).encode()) <= max_bytes:
            low = end
        else:
            high = end - 1
    return {"truncated": True, "text": body[:low] + notice}


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
        if not all(isinstance(part, str) and part for part in (secret.name, secret.value, secret.purpose)):
            raise E2EError("INVALID_ARGUMENT", "secret name, value and purpose must be nonempty text")
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
        if isinstance(value, (list, tuple)):
            return [self.redact(v) for v in value]
        return value

    def project(self, params: dict[str, Any]) -> tuple[dict, dict[str, str]]:
        """Project secrets to handles and Unique values to separately tracked slots."""
        unique: dict[str, str] = {}
        staged = Ledger()
        staged.secrets.update(self.secrets)

        def walk(value: Any, path: str, depth: int) -> Any:
            if depth > 32:
                raise E2EError("INVALID_ARGUMENT", "params exceed 32 levels")
            if isinstance(value, Secret):
                staged.register(value)
                return {"kind": "secret", "name": value.name, "purpose": value.purpose}
            if isinstance(value, Unique):
                if not isinstance(value.value, str) or not value.value:
                    raise E2EError("INVALID_ARGUMENT", "unique input must be nonempty")
                slot = digest(path)[:16]
                unique[slot] = value.value
                return value.value
            if isinstance(value, dict):
                if not all(isinstance(k, str) for k in value):
                    raise E2EError("INVALID_ARGUMENT", "params keys must be strings")
                return {k: walk(v, path + "/" + k.replace("~", "~0").replace("/", "~1"), depth + 1) for k, v in value.items()}
            if isinstance(value, (list, tuple)):
                return [walk(v, path + "/" + str(i), depth + 1) for i, v in enumerate(value)]
            return value

        projected = walk(params, "", 0)
        if len(canonical(projected).encode()) > 65536:
            raise E2EError("INVALID_ARGUMENT", "params exceed 64 KiB")
        values = list(unique.values())
        if any(a in b or b in a for i, a in enumerate(values) for b in values[i + 1:]):
            raise E2EError("INVALID_ARGUMENT", "unique values overlap; use distinct values")
        result = staged.redact(projected)
        self.secrets.update(staged.secrets)
        return result, unique


def _leaf_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _leaf_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _leaf_strings(child)


_MARKER = re.compile(r"\{\{e2e_unique:([^}|]+)(?:\|([a-z]+))?\}\}")


def _slug(value: str) -> str:
    """The lower-case path segment an app derives from a display name."""
    parts: list[str] = []
    dash = False
    for char in value.lower():
        if unicodedata.category(char)[0] in "LN":
            parts.append(char)
            dash = False
        elif not dash:
            parts.append("-")
            dash = True
    return "".join(parts).strip("-")


def _encoded(encoding: str, value: str) -> str:
    """One spelling a recorded screen or URL gives a run-unique value."""
    if encoding == "":
        return value
    if encoding == "uri":
        return quote(value, safe="")
    if encoding == "form":
        return quote_plus(value, safe="")
    if encoding == "slug":
        return _slug(value)
    if encoding == "flat":
        return " ".join(value.split())
    raise ValueError(f"unknown template encoding {encoding}")


def _spellings(value: str) -> list[tuple[str, str]]:
    """Every distinct way a recording can spell one value, longest first."""
    found = {"": value}
    for encoding in ("uri", "form", "slug", "flat"):
        try:
            text = _encoded(encoding, value)
        except ValueError:
            continue
        if text and text != value:
            found.setdefault(encoding, text)
    return sorted(found.items(), key=lambda pair: len(pair[1]), reverse=True)


def _marker(slot: str, encoding: str = "") -> str:
    return f"{{{{e2e_unique:{slot}{'|' + encoding if encoding else ''}}}}}"


def templates_collide(params: dict, slots: dict[str, str]) -> bool:
    """Whether substituting one run's unique values could rewrite another value.

    Two unique values that read into each other, or a literal that already contains
    one (or an encoded spelling of one), would make the key and the recording
    ambiguous between two different calls.
    """
    if not slots:
        return False
    values = list(slots.values())
    if any(a in b or b in a for i, a in enumerate(values) for b in values[i + 1:]):
        return True
    owners: dict[str, str] = {}
    for slot, value in slots.items():
        for spelling in _spellings(value):
            text = spelling[1]
            owner = owners.get(text)
            if owner is not None and owner != slot:
                return True
            owners[text] = slot
    marked = {text for text in owners if text}
    seen = set()
    for text in _leaf_strings(params):
        if text in values and text not in seen:
            seen.add(text)
        elif any(unique in text for unique in marked):
            return True
    return False


def template(value: Any, slots: dict[str, str], *, expand: bool = False) -> Any:
    """Substitute per-run unique strings recursively in JSON string values.

    A recorded value may be spelled as given, URI-encoded, form-encoded, as a
    slug, or collapsed onto one line. Each spelling gets its own marker so a
    replay can rebuild the spelling the app used.
    """
    if isinstance(value, str):
        if expand:
            def replace(match: re.Match) -> str:
                slot, encoding = match.group(1), match.group(2) or ""
                actual = slots.get(slot)
                if actual is None:
                    return match.group(0)
                try:
                    return _encoded(encoding, actual)
                except ValueError:
                    return match.group(0)
            return _MARKER.sub(replace, value)
        # Replace only literal segments: a marker written for an earlier spelling
        # can never be matched by a later one.
        segments: list[tuple[bool, str]] = [(True, value)]
        for slot, actual in sorted(slots.items(), key=lambda pair: len(pair[1]), reverse=True):
            for encoding, spelling in _spellings(actual):
                if not spelling:
                    continue
                marker = _marker(slot, encoding)
                following: list[tuple[bool, str]] = []
                for literal, part in segments:
                    if not literal or spelling not in part:
                        following.append((literal, part))
                        continue
                    pieces = part.split(spelling)
                    for index, piece in enumerate(pieces):
                        if piece:
                            following.append((True, piece))
                        if index < len(pieces) - 1:
                            following.append((False, marker))
                segments = following
        return "".join(part for _, part in segments)
    if isinstance(value, dict):
        return {k: template(v, slots, expand=expand) for k, v in value.items()}
    if isinstance(value, list):
        return [template(v, slots, expand=expand) for v in value]
    return value
