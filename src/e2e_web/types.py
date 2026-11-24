"""Public configuration, result objects and integration protocols."""

from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from typing import Any, Callable, Literal, Protocol

VisionMode = bool | Literal['on_demand']


class Executor(Protocol):
    """Decision seam: observe/action logic remains owned by the harness."""

    def complete(self, request: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        """Return one action, verdict or extraction under the remaining timeout."""
        ...


class CacheStore(Protocol):
    """Custom stores return untrusted JSON; the SDK validates every entry."""

    writable: bool

    def read(self, key: str) -> dict[str, Any] | None: ...
    def write(self, key: str, entry: dict[str, Any]) -> None: ...
    def delete(self, key: str) -> None: ...


@dataclass(frozen=True)
class CacheConfig:
    """Replay storage settings; CI defaults to read-only when mode is omitted."""

    mode: Literal["off", "read-only", "read-write"] | None = None
    directory: Path | str = ".e2e/cache"
    strict: bool = False
    store: CacheStore | None = None


@dataclass(frozen=True)
class AgentConfig:
    """Session ceilings and vision default; per-call overrides may only lower budgets."""

    timeout: float = 30
    judgment_timeout: float = 30
    max_actions: int = 50
    max_model_calls: int = 60
    context: str = ""
    name: str = "default"
    on_before_action: Callable[[dict[str, Any]], None] | None = None
    on_action: Callable[[dict[str, Any]], None] | None = None
    vision: VisionMode | None = None


@dataclass(frozen=True)
class ObservationConfig:
    """Global limits across the main document and its frames."""

    max_nodes: int = 200
    max_elements: int = 10000
    max_bytes: int = 65536
    max_frames: int = 20
    max_depth: int = 8


@dataclass(frozen=True)
class Secret:
    """A named secret; only the SDK resolves its value for a field fill."""

    name: str
    value: str = field(repr=False)
    purpose: str = "credential"


@dataclass(frozen=True)
class Unique:
    """Per-run input stored as a template slot in a replay recording."""

    value: str


@dataclass
class ActResult:
    """Result of one act, independent of pytest/unittest reporting."""

    summary: str
    actions: int
    model_calls: int
    cache: dict[str, Any]


@dataclass
class StepRecord:
    """Safe inspectable step events; retained in session.steps."""

    index: int
    api: str
    instruction: str
    status: str = "running"
    actions: int = 0
    model_calls: int = 0
    duration: float = 0
    cache: dict[str, Any] = field(default_factory=dict)
    error: dict[str, str] | None = None
    explanation: str = ""


@dataclass(frozen=True)
class Tool:
    """Trusted project tool; mutations spend the action budget and disable recording."""

    name: str
    description: str
    schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], Any]
    mutates: bool = False


class Engine(Protocol):
    """Useful for custom adapters and deterministic contract tests."""

    name: str

    def observe(self, *, timeout: float) -> dict[str, Any]: ...
    def perform(self, action: dict[str, Any], *, timeout: float) -> Any: ...

    def fill_secret(self, descriptor: dict[str, Any], secret: Secret, *, timeout: float) -> None:
        """Authorize an enabled editable sink; password purpose requires a password input."""
        ...


@dataclass
class Cancellation:
    """Cooperative cancellation token shared with the host framework."""

    event: Event = field(default_factory=Event)

    def cancel(self) -> None:
        """Prevent the next operation; an in-flight synchronous call must return first."""
        self.event.set()
