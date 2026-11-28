"""Verified action recordings; this is not the Node trace-1 wire format."""

from collections import Counter
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any
from urllib.parse import unquote, urlsplit

from ._utils import canonical, digest
from .actions import TAB_OPS, VISUAL_OPS, recordable_action, validate_action

SCHEMA = "e2e-web-python-trace-1"
MAX_BYTES = 1048576
HASH = re.compile(r"^[a-f0-9]{64}$")
ANNOUNCEMENT_ROLES = frozenset({"alert", "alertdialog", "status"})
ALERT_ROLES = frozenset({"alert", "alertdialog"})
MAX_ANCHORS = 8
# Why a recording ends before a typed value: replay hands over so the model
# derives this run's value instead of typing last run's.
GAP_REASONS = frozenset({"pixels", "whole-node", "minted-token", "date", "one-time-code"})

# A segment shaped like a value the app mints per record. A route that keeps such a
# value would never match again, so a create/detail screen could not replay.
PARAM = ":id"
_MINTED_SEGMENT = (
    re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I),
    re.compile(r"^[0-9a-f]{8,}$", re.I),
    re.compile(r"^\d+$"),
    re.compile(r"^(?=(?:[^\d]*\d){2})(?=.*[A-Za-z])[A-Za-z0-9_-]{12,}$"),
    re.compile(r"^[A-Za-z]+-\d{2,}$"),
    re.compile(r"\s"),
)
_MINTED_VALUE = _MINTED_SEGMENT + (
    re.compile(r"^(?=(?:[^\d]*\d){2})(?=.*[A-Za-z])[\w.~+/=:-]{12,}$"),
    re.compile(r"^\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?$"),
)

# Text that cannot read the same on the next run: a minted token, a duration, a date,
# an ISO instant or a clock time. A clock is never a step's result.
VOLATILE_TEXT = (
    re.compile(r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}(?::?\d{2})?)?", re.I),
    re.compile(r"\b(?=[A-Za-z0-9_-]*\d)(?=[A-Za-z0-9_-]*[A-Za-z])[A-Za-z0-9_-]{12,}\b", re.A),
    re.compile(r"\b\d+\s*(?:ms|s|secs?|seconds?|m|mins?|minutes?|h|hrs?|hours?|d|days?|weeks?|months?|years?)\b", re.I),
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),
    re.compile(r"\b(?:[01]?\d|2[0-3]):[0-5]\d(?::[0-5]\d)?(?=\b|[ap]\.?m\b)", re.I),
    re.compile(r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2}(?:st|nd|rd|th)?\b", re.I),
    re.compile(r"\b\d{1,2}(?:st|nd|rd|th)?\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b", re.I),
)
# A count that names what it counts is the step's own result, not data, when it is the
# only thing the step made appear. CJK counters follow the digits with no separator.
COUNT_TEXT = (
    re.compile(r"\b\d+\s*(?:to|-|\u2013)\s*\d+\s+of\s+\d+\b", re.I),
    re.compile(r"\b\d+\s+(?:results?|items?|rows?|entries|records?|matches|total)\b", re.I),
    re.compile(r"(?<!第)\b\d+\s*(?:件|個|个|項目?|项目?|条|筆|笔|行|개|건|행)"
               r"(?=$|[^\w]|[の中をがはも]|です|입니다|记录|記錄|記録|结果|結果)"),
)
_QUOTED = re.compile(r'"((?:[^"\\]|\\.)*)"')


def _minted(value: str, patterns=_MINTED_SEGMENT) -> bool:
    return value != "" and any(pattern.search(value) for pattern in patterns)


def _decode(segment: str) -> str:
    try:
        return unquote(segment)
    except Exception:
        return segment


def route(url: str) -> str:
    """Compare the route separately from the hashed origin, minted values read as :id.

    A recording of a create or detail screen names the route it opened, and that
    route carries the id the run minted; reading it as a placeholder is what lets
    the same step replay against a different record.
    """
    parsed = urlsplit(url)
    path, search, fragment = parsed.path, parsed.query, parsed.fragment
    if fragment.startswith("/"):
        path, search, fragment = fragment.split("?", 1)[0], "&".join(p for p in (search, fragment.partition("?")[2]) if p), ""
    segments = [PARAM if _minted(_decode(raw)) else raw for raw in path.split("/") if raw]
    terms = sorted(f"{PARAM if _minted(k, _MINTED_VALUE) else k}={PARAM if _minted(v, _MINTED_VALUE) else v}"
                   for k, _, v in (part.partition("=") for part in search.split("&") if part))
    return "/" + "/".join(segments) + ("?" + "&".join(terms) if terms else "")


def same_route(recorded: str, live: str) -> bool:
    """Whether two locations name the same screen."""
    return route(recorded) == route(live)


def same_origin(recorded: str | None, recorded_app: str | None,
                live: str | None, live_app: str | None) -> bool:
    """Whether two origins are the same app, even on another deployment.

    A recording made on the app's own origin follows the app to the origin the
    replay runs on; a screen on any other origin keeps its origin and only
    matches itself.
    """
    if recorded is None or live is None:
        return recorded == live
    if recorded_app and live_app and recorded == recorded_app:
        return live == live_app
    return recorded == live


def app_relative(url: str, app_origin: str) -> str:
    """A navigation on the app's own origin as a path, so it follows the app.

    Credentials and an origin that is not the app's leave the URL verbatim.
    """
    parsed = urlsplit(url)
    if (parsed.scheme not in {"http", "https"} or parsed.username is not None
            or parsed.password is not None or parsed.path.startswith("//") or origin(url) != app_origin):
        return url
    return (parsed.path or "/") + (f"?{parsed.query}" if parsed.query else "") + (
        f"#{parsed.fragment}" if parsed.fragment else "")


def opens_with_navigate(actions: list[dict]) -> bool:
    """A recording that starts by navigating is not bound to the page it was recorded on."""
    first = next((action for action in actions if action["op"] not in {"tool", "resize_viewport"}), None)
    if not first or first["op"] != "navigate":
        return False
    url = first["url"]
    return url.startswith("/") or bool(urlsplit(url).scheme)



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
        """Read the current directory, including entries written by another store."""
        return ((path.stem, self.read(path.stem)) for path in self.directory.glob("*.json")
                if HASH.fullmatch(path.stem))


def valid_entry(entry: Any, key: str, *, allow_legacy: bool = False) -> bool:
    """Validate custom stores and file stores at the same replay boundary."""
    try:
        if not isinstance(entry, dict) or entry.get("schema") != SCHEMA or entry.get("key") != key:
            return False
        if len(canonical(entry).encode()) > MAX_BYTES or not isinstance(entry.get("provenance"), dict):
            return False
        # A recording whose inputs had to be rewritten never replays: it would run a
        # different value than the one the run proved.
        if entry.get("truncated") is True:
            return False
        wait = entry.get("end_wait_ms")
        if wait is not None and (type(wait) is not int or not 0 <= wait <= 600000):
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
        if 'app_origin' in entry and (not isinstance(entry['app_origin'], str) or not HASH.fullmatch(entry['app_origin'])):
            return False
        tabs = entry.get("tab_states")
        if tabs is not None and (not isinstance(tabs, list) or len(tabs) != len(actions) + 2
                                 or not all(isinstance(v, str) and HASH.fullmatch(v) for v in tabs)):
            return False
        real = 0
        for index, action in enumerate(actions):
            if action.get('op') == 'gap':
                if index != len(actions) - 1:
                    return False
                if set(action) != {'op', 'reason'} or action.get('reason') not in GAP_REASONS or real == 0:
                    return False
                continue
            real += 1
            validate_action(action)
            if action["op"] in TAB_OPS and tabs is None:
                return False
            if action["op"] in VISUAL_OPS and not HASH.fullmatch(action["screenshot"]):
                return False
            if not recordable_action(action):
                return False
            if any(action.get(field, {}).get('by') == 'ref' for field in ('target', 'to', 'container')):
                return False
        if real == 0:
            return False
        pixels = entry.get('end_pixels')
        if any(action['op'] in VISUAL_OPS for action in actions):
            if not isinstance(pixels, str) or not HASH.fullmatch(pixels):
                return False
        elif pixels is not None:
            return False
        for field in ("appeared", "gone"):
            if not isinstance(entry.get(field), list) or len(entry[field]) > 8 or not all(isinstance(s, str) and len(s) <= 4096 for s in entry[field]):
                return False
        return bool(entry["appeared"] or entry["gone"] or entry["start"] != entry["end"]
                    or entry.get("start_origin") != entry.get("end_origin") or (tabs and tabs[0] != tabs[-1]) or pixels)
    except Exception:
        return False


def origin(url: str) -> str:
    """Bind recordings to scheme, hostname and effective port without storing them."""
    parsed = urlsplit(url)
    default_ports = {"http": 80, "https": 443}
    port = parsed.port
    return digest([parsed.scheme, parsed.hostname, default_ports.get(parsed.scheme) if port is None else port])


# Child state is meaningful within its owning composite widget.
_WIDGET_CHILDREN = {
    "combobox": {"option"}, "listbox": {"option"}, "radiogroup": {"radio"},
    "row": {"checkbox", "radio", "switch", "button", "gridcell", "cell"},
    "tablist": {"tab"}, "tree": {"treeitem"}, "treeitem": {"treeitem"},
    "menu": {"menuitemcheckbox", "menuitemradio", "menuitem"}, "menubar": {"menuitemcheckbox", "menuitemradio", "menuitem"},
}
# A container's accessible name usually repeats what its leaves say, so leaves are
# preferred when the anchor budget is short.
_CONTAINER_ROLES = frozenset({
    "article", "banner", "complementary", "contentinfo", "dialog", "alertdialog", "feed", "form",
    "grid", "group", "list", "listbox", "listitem", "main", "menu", "menubar", "navigation", "none",
    "presentation", "region", "row", "rowgroup", "table", "tabpanel", "tablist", "toolbar", "tree",
    "treegrid", "treeitem", "generic", "document", "application", "directory", "scrollbar",
})


def _entries(observation: dict) -> list[tuple[str, str]]:
    """Document-ordered (line, role) pairs, scoped by frame and owning composite widget."""
    trees = [("", observation.get("tree", ""))]
    trees.extend((f"frame:{frame['selector']} ", frame.get("tree", "")) for frame in observation.get("frames", []))
    result: list[tuple[str, str]] = []
    for prefix, tree in trees:
        owners: list[tuple[int, str, str]] = []
        pending = tree.splitlines()
        for position, raw in enumerate(pending):
            line = raw.strip()
            if not line:
                continue
            indent = len(raw) - len(raw.lstrip())
            while owners and indent <= owners[-1][0]:
                owners.pop()
            role = line[2:].partition(" ")[0].rstrip(":") if line.startswith("- ") else ""
            if role in _WIDGET_CHILDREN:
                owners.append((indent, role, line))
            else:
                for _, owner_role, header in reversed(owners):
                    if role in _WIDGET_CHILDREN[owner_role]:
                        line = header + " " + line
                        break
            if line and len(prefix) + len(line) <= 4096:
                result.append((prefix + line, role))
    return result


def lines(observation: dict, *, alerts_only: bool = False) -> Counter:
    """Count semantic lines by scope, identifying alert roles before adding frame prefixes."""
    counted = Counter()
    for line, role in _entries(observation):
        if not alerts_only or role in ALERT_ROLES:
            counted[line] += 1
    return counted


def _text_of(line: str) -> str:
    """The label a semantic line carries, from its quoted name and value."""
    return " ".join(_QUOTED.findall(line))


def _is_text_volatile(line: str) -> bool:
    text = _text_of(line)
    if _role(line) not in ANNOUNCEMENT_ROLES and re.fullmatch(r"\d+", text):
        return True
    return any(pattern.search(text) for pattern in VOLATILE_TEXT)


def _count_is_data(line: str, shapes: set[str], route_moved: bool) -> bool:
    text = _text_of(line)
    if re.search(r"第\s*\d", text) or not any(pattern.search(text) for pattern in COUNT_TEXT):
        return False
    # A count the other side already showed with other digits is screen data, not this
    # step's result; so is a count on a screen the step navigated to.
    return route_moved or _count_shape(line) in shapes


def _count_shape(line: str) -> str:
    return re.sub(r"\d+", "#", line)


def _role(line: str) -> str:
    """The role of a rendered line, after any frame and widget-owner prefix."""
    at = line.find("- ")
    body = line[at + 2:] if at >= 0 else line
    return body.partition(" ")[0].rstrip(":")


def _is_alert(line: str) -> bool:
    return _role(line) in ALERT_ROLES


def _alert_shape(line: str) -> str:
    shaped = line
    for pattern in VOLATILE_TEXT:
        shaped = pattern.sub("#", shaped)
    return shaped


def delta(before: dict, after: dict, *, route_moved: bool = False) -> tuple[list[str], list[str]]:
    """Capture changed semantic anchors: announcements first, then leaves, then containers."""
    a, b = lines(before), lines(after)
    appeared, gone = b - a, a - b
    # Cancel a ticking alert on both sides, retaining the original spelling of
    # unmatched instances for template substitution and diagnostics.
    _, added_alerts = _split_alerts(appeared)
    _, lost_alerts = _split_alerts(gone)
    shared = added_alerts & lost_alerts
    for side in (appeared, gone):
        remaining = shared.copy()
        for line in list(side):
            if _is_alert(line):
                shape = _alert_shape(line)
                cancel = min(side[line], remaining[shape])
                side[line] -= cancel
                remaining[shape] -= cancel
                if not side[line]:
                    del side[line]
    alerts = lines(before, alerts_only=True) | lines(after, alerts_only=True)
    listed = _entries(after) + _entries(before)
    order = {line: index for index, (line, _) in enumerate(listed)}
    roles = {line: role for line, role in listed}
    leaves = _leaves(after) | _leaves(before)

    def expand(counter):
        # Multiplicity matters: two rows appearing is not one row.
        return [line for line, count in counter.items() for _ in range(count)]

    def classify(side):
        shapes = {_count_shape(line) for line in ({*appeared, *gone} - set(side))}
        stable, with_counts = [], []
        for line in expand(side):
            alert = _is_alert(line)
            volatile = _is_text_volatile(line) or _count_is_data(line, shapes, route_moved)
            if alert or not volatile:
                stable.append(line)
            if alert or not _is_text_volatile(line):
                with_counts.append(line)
        return stable, with_counts

    appeared_stable, appeared_counts = classify(appeared)
    gone_stable, gone_counts = classify(gone)

    def pick(stable, with_counts, other_stable, side):
        kept = stable or (with_counts if other_stable else expand(side))
        ranked = sorted(kept, key=lambda line: (_rank(line, alerts, leaves, roles), order.get(line, 0)))
        return ranked[:MAX_ANCHORS]

    return (pick(appeared_stable, appeared_counts, gone_stable, appeared),
            pick(gone_stable, gone_counts, appeared_stable, gone))


def _leaves(observation: dict) -> set[str]:
    """Lines that have no listed child, so a container cannot crowd them out of the budget."""
    leaves = set()
    for prefix, tree in [("", observation.get("tree", ""))] + [
            (f"frame:{frame['selector']} ", frame.get("tree", "")) for frame in observation.get("frames", [])]:
        previous = None
        for raw in tree.splitlines():
            line = raw.strip()
            if not line or not line.startswith("- "):
                continue
            indent = len(raw) - len(raw.lstrip())
            if previous and indent <= previous[0]:
                leaves.add(previous[1])
            previous = (indent, prefix + line)
        if previous:
            leaves.add(previous[1])
    return leaves


def _rank(line: str, alerts: Counter, leaves: set[str], roles: dict[str, str]) -> int:
    if line in alerts or _is_alert(line):
        return 0
    role = roles.get(line) or _role(line)
    return 1 if line in leaves or role not in _CONTAINER_ROLES else 2


def complete_view(view: dict) -> bool:
    """Missing nodes or frames cannot establish disappearance or rule out new alerts."""
    return not (view.get("truncated") or view.get("tree_unavailable") or
                any(node.get("options_truncated") for node in view.get("nodes", [])) or
                any(frame.get("tree") == "<frame unavailable>" for frame in view.get("frames", [])))


def end_baseline(screens: list[dict], end_route: str, before: dict) -> dict:
    """The first screen a replay saw after it last arrived on the recorded end route.

    Anchors are the step's own effect. A row a previous run left behind is already
    on screen when the replay arrives there, so measuring against the step's start
    would count it as this run's effect. A step that leaves the end route and comes
    back starts a new stretch: the earlier arrival is not its ending baseline.
    """
    # During a run the full URL identifies the actual record and deployment.
    # A stored route is the fallback for callers with only a portable trace.
    exact = bool(urlsplit(end_route).scheme)
    baseline = None
    for view in screens:
        url = view.get("url", "")
        if (url if exact else route(url)) == end_route:
            if baseline is None:
                baseline = view
        else:
            baseline = None
    return baseline if baseline is not None else before


def _anchor_identity(line: str) -> tuple[str, str]:
    """The control an anchor names: its role and quoted name."""
    quoted = _QUOTED.search(line)
    return _role(line), quoted.group(1) if quoted else ""


def input_targets(actions: list[dict]) -> set[tuple[str, str]]:
    """The controls a recorded flow wrote into, whose value echo is no evidence."""
    targets = set()
    for action in actions:
        if action["op"] not in {"fill", "type", "fill_secret", "select", "upload", "check", "uncheck"}:
            continue
        target = action.get("target")
        if not isinstance(target, dict) or target.get("by") not in {"role", "semantic"}:
            continue
        name = target.get("name")
        role = target.get("value")
        if isinstance(role, str) and isinstance(name, str) and name:
            targets.add((role, name))
    return targets


def delta_evidenced(baseline: dict, final: dict, targets=(), *, recorded=None) -> bool:
    """Whether something actually moved during the replay, not merely held still.

    A control the flow typed into echoes the typed value as its own anchor; that
    echo is the input, not the app's outcome, so it proves nothing on its own.
    Every anchor other than those changed controls still is evidence.
    """
    if not complete_view(baseline) or not complete_view(final):
        return False
    if route(final.get("url", "")) != route(baseline.get("url", "")):
        return True
    initial, after = (_comparison_lines(view) for view in (baseline, final))
    appeared, gone = after - initial, initial - after
    if recorded is not None:
        for changes, key in ((appeared, "appeared"), (gone, "gone")):
            plain, alerts = _split_alerts(Counter(recorded[key]))
            changes &= plain + alerts
    if not appeared and not gone:
        return False
    if not targets:
        return True
    targets = set(targets)
    changed = {_anchor_identity(line) for line in appeared} & {_anchor_identity(line) for line in gone}
    echoes = changed & targets
    return (any(_anchor_identity(line) not in echoes for line in appeared)
            or any(_anchor_identity(line) not in echoes for line in gone))


def _split_alerts(counter: Counter) -> tuple[Counter, Counter]:
    """A delta as exact non-alert lines and volatile-text alert shapes."""
    plain, alerts = Counter(), Counter()
    for line, count in counter.items():
        if _is_alert(line):
            alerts[_alert_shape(line)] += count
        else:
            plain[line] += count
    return plain, alerts


def _comparison_lines(view: dict) -> Counter:
    plain, alerts = _split_alerts(lines(view))
    return plain + alerts


def matches_end(entry: dict, before: dict, after: dict) -> bool:
    """Verify the observed delta happened again, with no unexpected alert.

    An announcement's text often carries the clock, so its shape is what
    repeats; every other line must read exactly as it was recorded.
    """
    if not complete_view(before) or not complete_view(after):
        return False
    initial, final = lines(before), lines(after)
    if route(after.get("url", "")) != entry["end"]:
        return False
    before_plain, before_alerts = _split_alerts(initial)
    after_plain, after_alerts = _split_alerts(final)
    recorded_plain, recorded_alerts = _split_alerts(Counter(entry["appeared"]))
    gone_plain, gone_alerts = _split_alerts(Counter(entry["gone"]))
    return (not (recorded_plain - (after_plain - before_plain))
            and not (gone_plain - (before_plain - after_plain))
            and recorded_alerts == after_alerts - before_alerts
            and not (gone_alerts - (before_alerts - after_alerts)))
