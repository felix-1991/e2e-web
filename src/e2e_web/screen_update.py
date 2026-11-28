"""Send a whole screen once, then only what changed since the model last looked.

Node ids are stable for as long as an element exists, so a line the model already
holds still names the same element. A screen that changed mostly goes out whole
again, and a truncated listing never diffs: the model cannot read an absence out
of a listing that was cut short.
"""

from typing import Any

MAX_DIFF_LINES = 60
MAX_DIFF_SHARE = 0.5
# Keys that describe the screen rather than one node, so they travel with every update.
SCREEN_FIELDS = ("url", "title", "tabs", "tab_count", "active_tab", "viewport", "document", "modal",
                 "dialogs", "downloads")


class ScreenPresenter:
    """Stable node aliases plus the difference the model has not seen yet."""

    def __init__(self) -> None:
        self._aliases: dict[str, str] = {}
        self._next = 0
        self._records: dict[str, dict[str, Any]] = {}
        self._revision: str | None = None
        self._truncated = False
        self.diffed = False
        # Stateful model turns: the wire messages this step has exchanged, and
        # how many history entries the last message already carried.
        self.messages: list[dict] = []
        self.history_seen = 0

    def alias(self, node_id: str) -> str:
        """One alias per element for the whole step, so an id never shifts under the model."""
        alias = self._aliases.get(node_id)
        if alias is None:
            self._next += 1
            alias = self._aliases[node_id] = f"n{self._next}"
        return alias

    def aliases(self, view: dict) -> dict[str, str]:
        return {node["id"]: self.alias(node["id"]) for node in view.get("nodes") or ()}

    def resolver(self) -> dict[str, str]:
        return {alias: node_id for node_id, alias in self._aliases.items()}

    def reset(self) -> None:
        """Start a new step: the model no longer holds any screen."""
        self.__init__()

    def restart_conversation(self) -> None:
        """Forget the wire conversation while keeping stable aliases for a full look."""
        self.messages = []
        self.history_seen = 0
        self._records = {}
        self._revision = None
        self._truncated = False

    def clone(self) -> "ScreenPresenter":
        """A copy to advance for one request; the step adopts it only on success."""
        other = ScreenPresenter()
        other._aliases = dict(self._aliases)
        other._next = self._next
        other._records = {node_id: dict(record) for node_id, record in self._records.items()}
        other._revision = self._revision
        other._truncated = self._truncated
        other.diffed = self.diffed
        other.messages = [dict(message) for message in self.messages]
        other.history_seen = self.history_seen
        return other

    def adopt(self, other: "ScreenPresenter") -> None:
        """Keep the state a successful request advanced to."""
        self._aliases = other._aliases
        self._next = other._next
        self._records = other._records
        self._revision = other._revision
        self._truncated = other._truncated
        self.diffed = other.diffed
        self.messages = other.messages
        self.history_seen = other.history_seen

    def present(self, view: dict) -> dict:
        """The whole screen, or the difference from the one the model already holds."""
        self.diffed = False
        records = {node["id"]: node for node in view.get("nodes") or ()}
        if self._revision is None or self._truncated or view.get("truncated"):
            return self._open(view, records)
        added = [node_id for node_id in records if node_id not in self._records]
        removed = [node_id for node_id in self._records if node_id not in records]
        # Applying a delta preserves survivors in their old order and appends
        # additions. Send the ordered list when that would reconstruct a different screen.
        survivors = [node_id for node_id in self._records if node_id in records]
        if survivors + added != list(records):
            return self._open(view, records)
        changed = []
        for node_id, node in records.items():
            before = self._records.get(node_id)
            if before is None:
                continue
            update = {key: value for key, value in node.items() if key not in before or before[key] != value}
            dropped = [key for key in before if key not in node]
            if update or dropped:
                changed.append({"id": node_id,
                                "was": {key: before[key] for key in update if key in before},
                                "now": update,
                                **({"unset": dropped} if dropped else {})})
        touched = len(added) + len(removed) + len(changed)
        if touched > MAX_DIFF_LINES or touched > MAX_DIFF_SHARE * max(1, len(records)):
            return self._open(view, records)
        since = self._revision
        self._records, self._revision = records, view.get("id")
        self.diffed = True
        update = {key: view[key] for key in SCREEN_FIELDS if key in view}
        update["id"] = view.get("id")
        update["changes"] = {
            "since": since,
            "added": [records[node_id] for node_id in added],
            "changed": changed,
            "removed": removed,
            "unchanged": len(records) - len(added) - len(changed),
        }
        if view.get("truncated"):
            update["truncated"] = True
        return update

    def _open(self, view: dict, records: dict[str, dict[str, Any]]) -> dict:
        self._records, self._revision = records, view.get("id")
        self._truncated = bool(view.get("truncated"))
        return view
