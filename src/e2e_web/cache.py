"""Verified action recordings; this is not the Node trace-1 wire format."""

from collections import Counter
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any
from urllib.parse import urlsplit

from ._utils import canonical, digest
from .actions import recordable_action, validate_action

SCHEMA = "e2e-web-python-trace-1"
MAX_BYTES = 1048576
HASH = re.compile(r"^[a-f0-9]{64}$")
ALERT_LINE = re.compile(r"^- (?:alert|alertdialog)(?=[:\s]|$)")


class FileCacheStore:
    """Atomic local cache, replaceable with an implementation of CacheStore."""

    def __init__(self, directory: str | Path, *, writable: bool = True):
        self.directory, self.writable = Path(directory).expanduser().resolve(), writable

    def _path(self, key: str) -> Path:
        """Only digest names may become filesystem path components."""
        if not HASH.fullmatch(key):
            raise ValueError("cache key must be a SHA-256 digest")
        return self.directory / f"{key}.json"

    def read(self, key: str) -> dict[str, Any] | None:
        """Read bounded untrusted JSON; callers validate the wire shape."""
        try:
            with self._path(key).open("rb") as stream:
                wire = stream.read(MAX_BYTES + 1)
            if len(wire) > MAX_BYTES:
                return {"invalid": True}
            return json.loads(wire)
        except FileNotFoundError:
            return None
        except (OSError, ValueError, RecursionError):
            return {"invalid": True}

    def write(self, key: str, entry: dict[str, Any]) -> None:
        """Persist a whole entry through a private temporary file."""
        if not self.writable:
            return
        wire = canonical(entry).encode()
        if len(wire) > MAX_BYTES:
            raise ValueError("cache entry exceeds 1 MiB")
        self.directory.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=self.directory, prefix=".trace-")
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(wire)
            os.replace(temporary, self._path(key))
        finally:
            Path(temporary).unlink(missing_ok=True)

    def delete(self, key: str) -> None:
        """Evict only entries from a writable store."""
        if self.writable:
            self._path(key).unlink(missing_ok=True)

    def entries(self):
        """List digest-named entries for strict rekey detection."""
        for path in self.directory.glob("*.json"):
            if HASH.fullmatch(path.stem):
                yield path.stem, self.read(path.stem)


def valid_entry(entry: Any, key: str, *, allow_legacy: bool = False) -> bool:
    """Validate custom stores and file stores at the same replay boundary."""
    try:
        if not isinstance(entry, dict) or entry.get("schema") != SCHEMA or entry.get("key") != key:
            return False
        if len(canonical(entry).encode()) > MAX_BYTES or not isinstance(entry.get("provenance"), dict):
            return False
        if not isinstance(entry.get("start"), str) or not isinstance(entry.get("end"), str):
            return False
        if not isinstance(entry.get("summary", ""), str) or len(entry.get("summary", "")) > 1000:
            return False
        actions = entry.get("actions")
        if not isinstance(actions, list) or not 1 <= len(actions) <= 1000:
            return False
        origin_fields = {"start_origin", "end_origin", "action_origins"}
        if not allow_legacy or origin_fields & entry.keys():
            origins = entry.get("action_origins")
            if not isinstance(origins, list) or len(origins) != len(actions):
                return False
            if not all(isinstance(value, str) and HASH.fullmatch(value)
                       for value in [entry.get("start_origin"), entry.get("end_origin"), *origins]):
                return False
        for action in actions:
            validate_action(action)
            if not recordable_action(action):
                return False
            if any(action.get(field, {}).get('by') == 'ref' for field in ('target', 'to')):
                return False
        for field in ("appeared", "gone"):
            if not isinstance(entry.get(field), list) or len(entry[field]) > 8 or not all(isinstance(s, str) and len(s) <= 4096 for s in entry[field]):
                return False
        return bool(entry["appeared"] or entry["gone"] or entry["start"] != entry["end"]
                    or entry.get("start_origin") != entry.get("end_origin"))
    except Exception:
        return False


def route(url: str) -> str:
    """Compare the route separately from the hashed origin."""
    parsed = urlsplit(url)
    return parsed.path + ("?" + parsed.query if parsed.query else "") + ("#" + parsed.fragment if parsed.fragment else "")


def origin(url: str) -> str:
    """Bind recordings to scheme, hostname and effective port without storing them."""
    parsed = urlsplit(url)
    default_ports = {"http": 80, "https": 443}
    port = parsed.port
    return digest([parsed.scheme, parsed.hostname, default_ports.get(parsed.scheme) if port is None else port])


def lines(observation: dict, *, alerts_only: bool = False) -> Counter:
    """Count semantic lines by scope, identifying alert roles before adding frame prefixes."""
    trees = [("", observation.get("tree", ""))]
    trees.extend((f"frame:{frame['selector']} ", frame.get("tree", "")) for frame in observation.get("frames", []))
    result = Counter()
    for prefix, tree in trees:
        for raw in tree.splitlines():
            line = raw.strip()
            if line and (len(prefix) + len(line) <= 4096 or alerts_only) and (not alerts_only or ALERT_LINE.match(line)):
                result[prefix + line] += 1
    return result


def delta(before: dict, after: dict) -> tuple[list[str], list[str]]:
    """Capture changed semantic anchors, announcements first."""
    a, b = lines(before), lines(after)
    alerts = lines(before, alerts_only=True) | lines(after, alerts_only=True)

    def choose(counts):
        return sorted(counts.elements(),
                      key=lambda line: (line not in alerts,
                                        not any(mark in line for mark in ("status", "checked", "selected")),
                                        len(line), line))[:8]

    return choose(b - a), choose(a - b)


def matches_end(entry: dict, before: dict, after: dict) -> bool:
    """Verify the observed delta happened again, with no unexpected alert."""
    initial, final = lines(before), lines(after)
    appeared, gone = Counter(entry["appeared"]), Counter(entry["gone"])
    if route(after["url"]) != entry["end"] or appeared - (final - initial) or gone - (initial - final):
        return False
    new_alerts = lines(after, alerts_only=True) - lines(before, alerts_only=True)
    return not (new_alerts - appeared)
