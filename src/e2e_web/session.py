"""Framework-independent synchronous session and bounded agent operations."""

from __future__ import annotations

import base64
import binascii
from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict, replace
import os
from pathlib import Path
import re
import threading
import time
from typing import Any, Callable
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator, ValidationError

from ._utils import (Deadline, INSTRUCTION_RESERVE, Ledger, MIN_OBSERVATION_BYTES, _leaf_strings, bound_output,
                     canonical, digest, image_patch_tokens, normalize, png_size, positive, template,
                     templates_collide, trim_observation, truncate_utf8)
from .actions import TAB_OPS, VISUAL_OPS, press_sequence, recordable_action, validate_action
from .browser import BrowserEngine
from .cache import (FileCacheStore, SCHEMA, app_relative, complete_view, delta, delta_evidenced,
                    end_baseline, input_targets, matches_end, opens_with_navigate, origin, route,
                    same_origin, same_route, valid_entry)
from .config import _AgentSettings
from .models import _model_step
from .screen_update import ScreenPresenter
from .errors import AssertionFailed, E2EError, ReplayStale, StepTimeout
from .types import ActResult, AgentConfig, CacheConfig, Cancellation, Engine, Executor, Secret, StepRecord, Tool, Unique, VisionMode

# Request-side budgets: one tool result or history entry is a receipt, not a transcript.
MAX_TOOL_OUTPUT_LINES, MAX_TOOL_OUTPUT_BYTES = 400, 16384
MAX_HISTORY_ENTRIES, MAX_HISTORY_BYTES, MAX_RESULT_BYTES = 24, 24576, 2048
MAX_PRIOR_LABEL_BYTES, MAX_PRIOR_HANDOFF_BYTES, MAX_PRIOR_BYTES = 256, 700, 8192
MAX_TRACE_INPUT_CHARS = 4096
# Rollout reasons that make a recorded end state unprovable, mirroring upstream STALE_REASONS.
STALE_REASONS = frozenset({"invalid-entry", "wrong-context", "target-not-found", "target-ambiguous",
                           "viewport-changed", "action-failed", "action-uncertain", "end-mismatch"})
# Failures the model can act on: it sees what was tried and what went wrong, then
# chooses another control instead of the whole step ending.
RECOVERABLE_ACTION_CODES = frozenset({"NODE_STALE", "LOCATOR_NOT_FOUND", "LOCATOR_AMBIGUOUS", "ACTION_FAILED",
                                      "ACTION_MAY_HAVE_COMMITTED", "POLICY_DENIED"})
MAX_ACTION_FAILURES = 5
MAX_SAME_ACTION_FAILURES = 2
# Scheduled backoff for a recorded target that has not rendered yet.
RELOCATION_RETRY_DELAYS = (0.1, 0.3, 0.6, 1.0, 3.0)
MAX_RELOCATION_WAIT = 15
# Values a model reads off the screen or reckons from the calendar belong to
# this run: a replay that typed last run's value would act out a stale flow.
DATE_OR_TIME = re.compile(
    r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?\b"
    r"|\b\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}\b"
    r"|\b(?:[01]?\d|2[0-3]):[0-5]\d(?::[0-5]\d)?\b",
    re.A,
)
MAX_SHOWN_TEXTS = 2000
# A commit or a navigation needs longer to show than a keystroke.
SETTLE_SECONDS = {
    'click': 2.0, 'double_click': 2.0, 'right_click': 1.5, 'long_press': 1.5,
    'click_at': 2.0, 'press': 1.5, 'press_at': 1.5, 'select': 0.8, 'select_at': 0.8,
    'check': 0.8, 'uncheck': 0.8, 'drag': 1.5, 'drag_at': 1.5, 'upload': 0.8,
    'fill': 0.4, 'type': 0.4, 'type_at': 0.4, 'hover': 0.4, 'hover_at': 0.4,
    'scroll': 0.5, 'scroll_to': 0.5, 'scroll_find': 0.5,
    'navigate': 2.0, 'back': 2.0, 'forward': 2.0, 'reload': 2.0,
}


def _repair_note(exc: E2EError) -> str:
    """Quote the rejected response, so one repair can actually correct it."""
    note = f"Previous response was rejected: {truncate_utf8(str(exc), 500)}. Do not repeat it."
    raw = truncate_utf8(str(getattr(exc, "raw_text", "") or ""), 2000)
    return note + (f" previous response: {raw}" if raw else "") + " Return a corrected JSON object."


def _fixed_bytes(request: dict) -> int:
    """Bytes the request carries besides its observation and the image itself."""
    envelope = {key: value for key, value in request.items() if key != "screenshot"}
    envelope["observation"] = None
    return len(canonical(envelope).encode()) - len("null")


def _shrink_context(request: dict) -> dict:
    """Give a too-large request less history, then less prior-step handoff."""
    shrunk = dict(request)
    # Handoffs from earlier steps go first: the current step's own actions and
    # screen are what the model needs to decide with.
    if shrunk.pop("prior_steps", None) is not None:
        return shrunk
    history = shrunk.get("history")
    if isinstance(history, list) and history:
        # Count real entries, so the omission notice this function adds cannot
        # read as a new entry and stall the next pass at the same size.
        entries = [entry for entry in history if not (isinstance(entry, dict) and set(entry) == {"notice"})]
        keep = 8 if len(entries) > 8 else 2 if len(entries) > 2 else 0
        if keep < len(entries):
            kept = entries[-keep:] if keep else []
            if kept:
                kept = [dict(notice=f"[{len(entries) - len(kept)} earlier actions omitted]"), *kept]
            shrunk["history"] = kept
            return shrunk
    if any(isinstance(entry, dict) and isinstance(entry.get("result"), str) and len(entry["result"]) > 512
           for entry in shrunk.get("history") or ()):
        return dict(shrunk, history=[dict(entry, result=truncate_utf8(entry["result"], 512))
                                     if isinstance(entry, dict) and isinstance(entry.get("result"), str)
                                     else entry for entry in shrunk.get("history") or ()])
    return request


def _fit_request(request: dict, limit: int) -> dict:
    """Trim one request under a model context, whatever executor will make the call."""
    observation = request.get("observation")
    if not isinstance(observation, dict):
        return request
    image = 0
    if request.get("screenshot"):
        try:
            width, height = png_size(base64.b64decode(request["screenshot"][:32], validate=True))
        except (ValueError, binascii.Error):
            raise E2EError("INVALID_ARGUMENT", "input budgeting requires a PNG screenshot") from None
        image = image_patch_tokens(width, height)
    for _ in range(5):
        available = limit - _fixed_bytes(request) - INSTRUCTION_RESERVE - image
        if available >= MIN_OBSERVATION_BYTES:
            break
        shrunk = _shrink_context(request)
        if shrunk is request:
            break
        request = shrunk
    remaining = limit - _fixed_bytes(request) - image
    available = min(remaining, max(MIN_OBSERVATION_BYTES, remaining - INSTRUCTION_RESERVE))
    if len(canonical(observation).encode()) <= available:
        return request
    return dict(request, observation=trim_observation(observation, available))


def _shrink_request(request: dict) -> dict:
    """Make room for a retry after a provider rejected the request as too large."""
    shrunk = dict(request)
    shrunk.pop("prior_steps", None)
    history = shrunk.get("history")
    if isinstance(history, list) and len(history) > 8:
        shrunk["history"] = history[-8:]
    view = shrunk.get("observation")
    if isinstance(view, dict):
        size = len(canonical(view).encode())
        if size > MIN_OBSERVATION_BYTES:
            shrunk["observation"] = trim_observation(view, max(MIN_OBSERVATION_BYTES, size // 2))
        elif view.get("nodes"):
            shrunk["observation"] = dict(view, nodes=view["nodes"][:max(1, len(view["nodes"]) // 2)], truncated=True)
    return shrunk


def _normal(text: str) -> str:
    return normalize(text).casefold()


def _word_in(text: str, needle: str) -> bool:
    """Whether text carries need as a whole word, not inside a longer token."""
    start = 0
    while (at := text.find(needle, start)) >= 0:
        before = text[at - 1] if at else ' '
        end = at + len(needle)
        after = text[end] if end < len(text) else ' '
        if not before.isalnum() and not after.isalnum():
            return True
        start = at + 1
    return False


class WebSession:
    """A single isolated test/automation scope, owned by the caller's framework."""

    def __init__(self, engine: Engine, *, executor: Executor | None = None,
                 judge: Executor | None = None, project: str = "default", name: str,
                 app_identity: str, agent: AgentConfig | None = None,
                 cache: CacheConfig | None = None, tools: tuple[Tool, ...] = (),
                 timeout: float | None = None, cancellation: Cancellation | None = None,
                 artifacts_dir: str | Path | None = None,
                 secrets: tuple[Secret, ...] = (), on_step: Callable[[StepRecord], None] | None = None,
                 cleanup: Callable[[], None] | None = None):
        self.engine, self.executor, self.judge = engine, executor, judge or executor
        self.config = agent or AgentConfig()
        self._vision = ('on_demand' if self.config.vision == 'on_demand'
                        else _AgentSettings.resolve(vision=self.config.vision).vision)
        for value, label in ((self.config.timeout, "timeout"), (self.config.judgment_timeout, "judgment_timeout"),
                             (self.config.action_timeout, "action_timeout")):
            positive(value, label)
        for value in (self.config.max_actions, self.config.max_model_calls):
            if type(value) is not int or not 1 <= value <= 1000:
                raise E2EError("INVALID_ARGUMENT", "action/model-call ceilings must be integers from 1 to 1000")
        if self.config.max_input_tokens is not None and (type(self.config.max_input_tokens) is not int
                                                         or self.config.max_input_tokens < 1024):
            raise E2EError("INVALID_ARGUMENT", "max_input_tokens must be an integer of at least 1024")
        self.cancellation = cancellation or Cancellation()
        self._end = time.monotonic() + positive(timeout, "session timeout") if timeout is not None else None
        self._owner, self._lock = threading.get_ident(), threading.Lock()
        self._closed, self._cleanup, self._on_step = False, cleanup, on_step
        self._interrupted = False
        self._popups: list[WebSession] = []
        self._popup_parent: WebSession | None = None
        self._ledger = Ledger()
        for secret in secrets:
            self._ledger.register(secret)
        self.tools = {tool.name: tool for tool in tools}
        if len(self.tools) != len(tools):
            raise E2EError("INVALID_ARGUMENT", "tool names must be unique")
        for tool in tools:
            Draft202012Validator.check_schema(tool.schema)
        self._identity = dict(project=project, test=name, app=app_identity, engine=engine.name,
                              agent=self.config.name, context=digest(self._ledger.redact(self.config.context)),
                              policy=15 if isinstance(engine, BrowserEngine) else 5)
        # Where the app itself lives, once its first screen is observed. A
        # recording made there follows the app to another deployment of it.
        self._app_origin: str | None = None
        if isinstance(engine, BrowserEngine) and engine.test_id_attribute != "data-testid":
            self._identity["test_id_attribute"] = engine.test_id_attribute
        self._cache_config = cache or CacheConfig()
        ci = os.getenv("CI", "").lower() not in {"", "0", "false"}
        self._mode = self._cache_config.mode or ("read-only" if ci else "read-write")
        if self._mode not in {"off", "read-only", "read-write"}:
            raise E2EError("INVALID_ARGUMENT", "invalid cache mode")
        if type(self._cache_config.attempt) is not int or self._cache_config.attempt < 0:
            raise E2EError("INVALID_ARGUMENT", "cache attempt must be a non-negative integer")
        if self._cache_config.strict:
            if self._mode == "off":
                raise E2EError("INVALID_ARGUMENT", "strict cache cannot be off")
            self._mode = "read-only"
        self._store = self._cache_config.store or FileCacheStore(self._cache_config.directory, writable=self._mode == "read-write")
        self.steps: list[StepRecord] = []
        self.screen = ScreenPresenter()
        self._pending: list[dict] = []
        self._occurrences: Counter = Counter()
        self.artifacts_dir = Path(artifacts_dir).resolve() if artifacts_dir else None
        self.agent = Agent(self)

    @property
    def total_usage(self) -> dict[str, Any]:
        """Model spend across every step of this session, as the providers reported it."""
        total: dict[str, Any] = {}
        for step in self.steps:
            for key, value in step.model_usage.items():
                if key == "peak_prompt_tokens":
                    total[key] = max(total.get(key, 0), value)
                elif key == "observation_bytes":
                    total[key] = total.get(key, 0) + value
                elif isinstance(value, (int, float)):
                    total[key] = round(total.get(key, 0) + value, 6)
        return total

    @property
    def page(self):
        """Native Playwright escape hatch; its direct calls are caller-managed."""
        self._ensure_open()
        return getattr(self.engine, "page", None)

    @property
    def browser(self):
        """The deterministic Playwright Page, including frames, routes and events."""
        return self.page

    def open_popup(self, instruction: str, params: dict | None = None, *, name: str,
                   timeout: float | None = None, vision: VisionMode | None = None) -> WebSession:
        """Execute one action, then own a session on the resulting Playwright popup."""
        from playwright.sync_api import TimeoutError as PlaywrightTimeout

        page = self.page
        if not isinstance(self.engine, BrowserEngine):
            raise E2EError("INVALID_ARGUMENT", "open_popup requires a Playwright session")
        if not isinstance(name, str) or not normalize(name):
            raise E2EError("INVALID_ARGUMENT", "popup session name must be nonempty text")
        deadline = Deadline(self.config.timeout if timeout is None else timeout, self.cancellation, self._end)
        popup = None

        def close_popup():
            if not popup.is_closed():
                popup.close()

        try:
            with page.expect_popup(timeout=deadline.remaining() * 1000) as event:
                try:
                    self.agent.act_once(instruction, params, timeout=deadline.remaining(), vision=vision)
                except BaseException:
                    if event.is_done():
                        try:
                            popup = event.value
                        except Exception:
                            pass
                    raise
            popup = event.value
            popup.wait_for_load_state("domcontentloaded", timeout=deadline.remaining() * 1000)
            deadline.remaining()
            self._ensure_open()

            # Include descendants opened by the popup during the triggering action
            # or page load. Transfer their listeners along with their tab ownership.
            transferred = {popup}
            for candidate in page.context.pages:
                if candidate is not popup and candidate.opener() in transferred:
                    transferred.add(candidate)
            ancestor = self
            while ancestor is not None:
                ancestor.engine._tabs.exclude(transferred)
                for candidate in transferred:
                    ancestor.engine.detach_page(candidate)
                ancestor = ancestor._popup_parent

            def release_popup():
                try:
                    try:
                        child.engine._tabs.close_owned_pages()
                    finally:
                        close_popup()
                finally:
                    self._popups.remove(child)

            child = WebSession(
                BrowserEngine(popup, observation=self.engine.observation, upload_roots=self.engine.upload_roots,
                              test_id_attribute=self.engine.test_id_attribute),
                name=name, app_identity=self._identity["app"], project=self._identity["project"],
                executor=self.executor, judge=self.judge, agent=replace(self.config, vision=self._vision),
                cache=replace(self._cache_config, mode=self._mode, store=self._store),
                tools=tuple(self.tools.values()), cancellation=self.cancellation,
                secrets=tuple(self._ledger.secrets.values()), on_step=self._on_step, cleanup=release_popup,
                artifacts_dir=self.artifacts_dir / "popups" / digest(name) if self.artifacts_dir else None,
            )
            child._end, child._ledger, child._lock = self._end, self._ledger, self._lock
            child.engine._tabs.excluded.difference_update(transferred)
            child._popup_parent = self
            child.engine.masked_vision = self.engine.masked_vision
            self._popups.append(child)
            return child
        except BaseException as exc:
            if popup is not None:
                try:
                    close_popup()
                except Exception:
                    pass
            if isinstance(exc, PlaywrightTimeout):
                raise StepTimeout("STEP_TIMEOUT", "popup did not open and load within the action timeout") from None
            raise

    def __enter__(self) -> WebSession:
        self._ensure_open()
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        self._interrupted = isinstance(exc, (KeyboardInterrupt, SystemExit))
        try:
            self.close()
        except Exception:
            if exc is None:
                raise
        return False

    def _ensure_open(self):
        """Playwright objects remain confined to their creating thread."""
        if threading.get_ident() != self._owner:
            raise E2EError("CONCURRENT_OPERATION", "use a separate session on each thread")
        if self._closed:
            raise E2EError("SESSION_CLOSED", "session is closed")

    @contextmanager
    def _step(self, api: str, instruction: str, timeout: float):
        """Record one operation with a shared deadline and non-reentrant guard."""
        self._ensure_open()
        if not isinstance(instruction, str) or not normalize(instruction):
            raise E2EError("INVALID_ARGUMENT", "instruction must be nonempty text")
        if not self._lock.acquire(blocking=False):
            raise E2EError("CONCURRENT_OPERATION", "finish the current operation before starting another")
        start = time.monotonic()
        # The model holds no screen from a previous step.
        self.screen.reset()
        row = StepRecord(len(self.steps), api, self._ledger.redact(instruction))
        self.steps.append(row)
        try:
            deadline = Deadline(timeout, self.cancellation, self._end)
            deadline.remaining()
            yield row, deadline
            deadline.remaining()
            row.status = "passed"
        except BaseException as exc:
            row.status = "failed"
            row.error = {"code": getattr(exc, "code", "CALLBACK_FAILED"),
                         "message": self._ledger.redact(str(exc))[:1000]}
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                self._interrupted = True
            if isinstance(exc, E2EError):
                exc.args = (self._ledger.redact(str(exc)),)
                exc.details = self._ledger.redact(exc.details)
            raise
        finally:
            row.duration = time.monotonic() - start
            self._lock.release()
            if self._on_step:
                try:
                    self._on_step(row)
                except Exception:
                    pass

    def _observe(self, deadline: Deadline, *, fallback=False) -> dict:
        """One fresh redacted observation under the current operation deadline."""
        remaining = deadline.remaining()
        fallback = fallback and isinstance(self.engine, BrowserEngine) and self.engine.masked_vision and not self._ledger.secrets
        try:
            observation = self.engine.observe(timeout=remaining - min(2, remaining / 3) if fallback else remaining)
        except StepTimeout:
            deadline.remaining()
            if not fallback:
                raise
            observation = dict(url=self.engine.current_url, tree='', nodes=[], frames=[], truncated=True,
                               tree_unavailable=True, **self.engine._tabs.summary())
        deadline.remaining()
        if self._app_origin is None:
            location = observation.get("url")
            if isinstance(location, str) and urlsplit(location).scheme in {"http", "https"}:
                self._app_origin = origin(location)
        if self._ledger.secrets:
            for node in observation.get('nodes', []):
                node.pop('selection', None)
        self._observed_url = observation.get("url")
        return self._ledger.redact(observation)

    def _same_origin(self, recorded: str | None, recorded_app: str | None, live: str | None) -> bool:
        """Compare an origin against the run's own app origin."""
        return same_origin(recorded, recorded_app, live, self._app_origin)

    def _current_origin(self, deadline: Deadline, *, refresh: bool = False, action: dict | None = None,
                        recorded_app: str | None = None) -> str:
        # Playwright supplies its live URL without rebuilding document observations.
        url = getattr(self.engine, "current_url", None)
        if url is None:
            if refresh:
                self._observe(deadline)
            url = self._observed_url
        source = origin(url)
        if action is not None and action["op"] == "navigate":
            resolve = getattr(self.engine, "resolve_navigation", None)
            if callable(resolve):
                destination = origin(resolve(action["url"]))
                if (recorded_app and self._app_origin and recorded_app != self._app_origin
                        and recorded_app in (source, destination)):
                    raise ReplayStale("REPLAY_STALE", "navigation origin changed to the former deployment")
                return digest([recorded_app if recorded_app and item == self._app_origin else item
                               for item in (source, destination)])
        return source

    def observe(self, *, timeout: float | None = None) -> dict:
        """Read current semantics without confirming or invalidating a trace."""
        with self._step("observe", "Observe current screen", timeout if timeout is not None else self.config.timeout) as (_, deadline):
            return self._observe(deadline)

    def _confirm(self):
        for pending in self._pending:
            pending["confirmed"] = True

    def verify(self, assertion: Callable[[], Any], *, label: str = "Deterministic assertion",
               timeout: float | None = None) -> None:
        """Run a native assertion; no exception (and no False) confirms prior acts."""
        with self._step("verify", label, timeout if timeout is not None else self.config.judgment_timeout) as (_, deadline):
            try:
                if assertion() is False:
                    raise AssertionError(label)
            except AssertionError:
                raise AssertionFailed("ASSERTION_FAILED", label) from None
            deadline.remaining()
            self._confirm()

    def expect(self, locator, *, timeout: float = 5):
        """Playwright auto-waiting matchers that confirm preceding act recordings."""
        from playwright.sync_api import expect
        session = self
        positive(timeout, "assertion timeout")
        matcher = expect(locator)

        class VerifiedExpectation:
            """Only assertion methods are forwarded; all pass through verification."""

            def __getattr__(self, name):
                if not name.startswith(("to_", "not_to_")):
                    raise AttributeError(name)
                method = getattr(matcher, name)

                def check(*args, **kwargs):
                    with session._step("verify", name, timeout) as (_, deadline):
                        requested = kwargs.pop("timeout", timeout * 1000)
                        positive(requested, "matcher timeout")
                        try:
                            method(*args, timeout=min(requested, deadline.remaining() * 1000), **kwargs)
                        except AssertionError:
                            raise AssertionFailed("ASSERTION_FAILED", name) from None
                        deadline.remaining()
                        session._confirm()
                return check

        return VerifiedExpectation()

    def screenshot(self, path: str | Path, *, timeout: float = 5) -> Path:
        """Save an explicit screenshot; fail closed when any secret was registered."""
        if self._ledger.secrets:
            raise E2EError("POLICY_DENIED", "screenshots disabled while secrets are registered")
        with self._step("screenshot", "Capture screenshot", timeout) as (_, deadline):
            data = self.engine.screenshot(timeout=deadline.remaining())  # type: ignore[attr-defined]
            destination = Path(path).resolve()
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            return destination

    def _evict(self, key: str):
        if self._mode == "read-write" and self._store.writable:
            try:
                self._store.delete(key)
            except Exception:
                pass

    def close(self) -> None:
        """Settle cache and close the resources this session owns exactly once."""
        if self._closed:
            return
        self._ensure_open()
        if self._lock.locked():
            raise E2EError("CONCURRENT_OPERATION", "cannot close during an active step")
        self._closed = True
        errors = []
        try:
            for popup in reversed(tuple(self._popups)):
                popup._interrupted |= self._interrupted
                try:
                    popup.close()
                except Exception:
                    errors.append("popup session cleanup failed")
            for pending in self._pending:
                row = pending["row"]
                row.cache["write"] = "read-only"
                if self._interrupted or self._mode != "read-write" or not self._store.writable:
                    continue
                try:
                    if not pending["confirmed"]:
                        self._store.delete(pending["key"])
                        row.cache["write"] = "unconfirmed"
                    elif pending["replayed"]:
                        row.cache["write"] = "kept"
                    else:
                        self._store.write(pending["key"], pending["entry"])
                        row.cache["write"] = "saved"
                except Exception:
                    row.cache["write"] = "storage-error"
            if self.artifacts_dir:
                try:
                    self.artifacts_dir.mkdir(parents=True, exist_ok=True)
                    (self.artifacts_dir / "steps.json").write_text(canonical(self._ledger.redact([asdict(s) for s in self.steps])) + "\n")
                except OSError:
                    errors.append("could not write step artifacts")
        finally:
            if isinstance(self.engine, BrowserEngine):
                try:
                    self.engine.detach_events()
                except Exception:
                    errors.append("page listener cleanup failed")
            if self._cleanup:
                try:
                    self._cleanup()
                except Exception:
                    errors.append("browser cleanup failed")
        if errors:
            raise E2EError("CLEANUP_FAILED", "; ".join(errors))


class Agent:
    """Natural-language operations bound to one WebSession."""

    def __init__(self, session: WebSession):
        self._s = session

    def _params(self, params):
        self._s._ensure_open()
        if self._s._lock.locked():
            raise E2EError("CONCURRENT_OPERATION", "finish the current operation before starting another")
        if params is not None and not isinstance(params, dict):
            raise E2EError("INVALID_ARGUMENT", "params must be an object")
        return self._s._ledger.project(params or {})

    def _limit(self, value, configured, label):
        value = configured if value is None else value
        if type(value) is not int or not 1 <= value <= configured:
            raise E2EError("INVALID_ARGUMENT", f"{label} must be from 1 to the configured ceiling {configured}")
        return value

    def _poll_observation(self, deadline, ready, *, fallback=False, wait=0.35):
        s = self._s
        observation = s._observe(deadline, fallback=fallback)
        if not isinstance(s.engine, BrowserEngine):
            return observation
        end = time.monotonic() + min(wait, max(0, deadline.remaining() - 0.05))
        while not observation.get('tree_unavailable') and not ready(observation):
            delay = end - time.monotonic()
            if delay <= 0 or deadline.remaining() <= 0.05:
                break
            time.sleep(min(0.05, delay, deadline.remaining() - 0.025))
            if deadline.remaining() <= 0.025:
                break
            observation = s._observe(deadline, fallback=fallback)
        return observation

    def _observe_after_action(self, deadline, before, *, fallback=False, op=None):
        def evidence(view):
            fields = ('url', 'title', 'tree', 'frames', 'tabs', 'tab_count', 'viewport')
            visible = [node['id'] for node in view.get('nodes', []) if node.get('in_viewport')]
            return [view.get(key) for key in fields], visible

        previous = evidence(before)
        return self._poll_observation(
            deadline,
            lambda after: after.get("dialogs") or after.get("downloads") or evidence(after) != previous,
            fallback=fallback, wait=SETTLE_SECONDS.get(op, 0.35))

    def _settle_observe(self, deadline, *, fallback=False, wait=1.5):
        """Wait until the screen holds still, so a recording keeps the settled end state."""
        s = self._s
        observation = s._observe(deadline, fallback=fallback)
        if not isinstance(s.engine, BrowserEngine):
            return observation
        end = time.monotonic() + min(wait, max(0, deadline.remaining() - 0.05))
        while True:
            delay = min(0.15, end - time.monotonic())
            if delay <= 0 or deadline.remaining() <= 0.05:
                return observation
            time.sleep(delay)
            following = s._observe(deadline, fallback=fallback)
            settled = all(following.get(key) == observation.get(key) for key in ('url', 'title', 'tree', 'frames'))
            observation = following
            if settled:
                return observation

    def _note_shown(self, view: dict) -> None:
        """Remember what the step's screens displayed, for derived-value detection.

        A field's own value is left out: a value echoing the field it went into
        says nothing about where the value came from.
        """
        shown = getattr(self, '_shown_texts', None)
        if shown is None:
            return
        for node in view.get('nodes') or ():
            if node.get('editable') or node.get('value'):
                continue
            for text in (node.get('name'), node.get('text')):
                if isinstance(text, str) and text.strip():
                    shown.add(_normal(text))
        while len(shown) > MAX_SHOWN_TEXTS:
            shown.discard(next(iter(shown)))

    def _target_purpose(self, action: dict) -> str:
        """The purpose the observed field the action addresses reported."""
        view = getattr(self, '_last_observation', None) or {}
        target = action.get('target') or {}
        nodes = view.get('nodes') or ()
        if target.get('by') == 'ref' and target.get('observation') == view.get('id'):
            node = next((item for item in nodes if item.get('id') == target.get('value')), None)
        elif target.get('by') in {'role', 'semantic'}:
            node = next((item for item in nodes if item.get('role') == target.get('value')
                         and item.get('name') == target.get('name', item.get('name'))), None)
        else:
            node = None
        return str(node.get('purpose', '')) if node else ''

    def _relocated_descriptor(self, action: dict) -> dict | None:
        """A portable descriptor for the node a live action's stale ref named."""
        if not isinstance(self._s.engine, BrowserEngine):
            return None
        target = action.get('target')
        if not isinstance(target, dict) or target.get('by') != 'ref':
            return None
        view = getattr(self, '_last_observation', None) or {}
        node = next((item for item in view.get('nodes') or () if item.get('id') == target.get('value')), None)
        if (target.get('observation') != view.get('id') or node is None or node.get('secure') or node.get('purpose') in {'password', 'one-time-code'}
                or not (node.get('name') or node.get('test_id'))):
            return None
        descriptor = dict(by='semantic', value=node['role'], name=node.get('name', ''),
                          test_id=node.get('test_id', ''))
        for field in ('href', 'level', 'input_type', 'description'):
            if node.get(field):
                descriptor[field] = node[field]
        if node.get('frame'):
            descriptor.update(frame=node['frame'], frame_identity=node['frame_identity'])
        return descriptor

    def _derived_reason(self, action: dict, instruction: str, params: dict) -> str | None:
        """Why a typed value belongs to this run, or None when it may replay.

        The rules mirror `derivedReason` upstream: a literal the caller spelled
        out replays; a value read off the screen or the calendar, or typed into
        a one-time-code field, does not. A recording that replayed one would
        type the previous run's data.
        """
        if action.get('op') not in {'fill', 'type'}:
            return None
        value = action.get('value')
        if not isinstance(value, str) or not value.strip():
            return None
        needle = _normal(value)
        literal = _normal(instruction)
        param_texts = [_normal(text) for text in _leaf_strings(params) if isinstance(text, str)]
        if self._target_purpose(action) == 'one-time-code':
            spelled = _word_in(literal, needle) or any(text == needle for text in param_texts)
            return None if len(needle) > 1 and spelled else 'one-time-code'
        if needle in literal or any(needle in text for text in param_texts):
            return None
        if getattr(self, '_pixels_shown', False):
            return 'pixels'
        data_shaped = not any(character.isspace() for character in needle) and any(character.isdigit() for character in needle)
        for text in getattr(self, '_shown_texts', ()):
            if text == needle:
                return 'whole-node'
            if data_shaped and _word_in(text, needle):
                return 'minted-token'
        return 'date' if DATE_OR_TIME.search(value) else None

    @staticmethod
    def _replay_inputs(action: dict) -> list[str]:
        """Every string a replay would type, open or select."""
        value = action.get("value")
        if isinstance(value, str):
            inputs = [value]
        elif isinstance(value, list):
            inputs = [item for item in value if isinstance(item, str)]
        elif isinstance(value, dict):
            inputs = [item for item in value.values() if isinstance(item, str)]
        else:
            inputs = []
        if isinstance(action.get("url"), str):
            inputs.append(action["url"])
        return inputs

    def _verbatim_actions(self, actions: list[dict]) -> list[dict] | None:
        """The recording's actions, or None when a replay would run a different value.

        Redaction rewrites a string that happened to contain a secret, and a trimmed
        input would replay half of a URL or of a typed value, so such an entry is kept
        for reference but never replayed.
        """
        s = self._s
        for action in actions:
            for text in self._replay_inputs(action):
                if s._ledger.redact(text) != text or len(text) > MAX_TRACE_INPUT_CHARS:
                    return None
        return actions

    def _prior_steps(self) -> list[dict]:
        """Bounded handoffs, redacted before truncation so no secret fragment survives a cut."""
        s = self._s
        items, size, dropped = [], 2, 0  # Include the JSON array delimiters.
        for step in reversed(s.steps[:-1]):
            item = dict(api=step.api,
                        instruction=truncate_utf8(s._ledger.redact(step.instruction), MAX_PRIOR_LABEL_BYTES),
                        status=step.status,
                        explanation=truncate_utf8(s._ledger.redact(step.explanation), MAX_PRIOR_HANDOFF_BYTES),
                        replayed=step.cache.get("mode") == "replay")
            cost = len(canonical(item).encode()) + bool(items)
            if size + cost > MAX_PRIOR_BYTES:
                dropped += len(s.steps) - 1 - len(items)
                break
            size += cost
            items.append(item)
        ordered = list(reversed(items))
        if dropped:
            ordered.insert(0, dict(notice=f"[{dropped} earlier step(s) omitted]"))
        return ordered

    @staticmethod
    def _bounded_history(history: list[dict]) -> list[dict]:
        """Keep the newest turns and clip one oversized result instead of replaying it every turn."""
        trimmed = []
        for entry in history:
            if isinstance(entry.get("result"), (dict, list)) or (isinstance(entry.get("result"), str)
                                                                 and len(entry["result"].encode()) > MAX_RESULT_BYTES):
                entry = dict(entry, result=bound_output(entry["result"], max_lines=MAX_TOOL_OUTPUT_LINES,
                                                        max_bytes=MAX_RESULT_BYTES))
            trimmed.append(entry)
        kept, size = [], 2
        for entry in reversed(trimmed):
            cost = len(canonical(entry).encode()) + bool(kept)
            if kept and (size + cost > MAX_HISTORY_BYTES or len(kept) >= MAX_HISTORY_ENTRIES):
                break
            size += cost
            kept.append(entry)
        ordered = list(reversed(kept))
        if len(ordered) < len(trimmed):
            ordered.insert(0, dict(notice=f"[{len(trimmed) - len(ordered)} earlier actions omitted]"))
        return ordered

    def _ask(self, kind, instruction, params, row, deadline, budget, *, history=None,
             schema=None, repair=None, vision=None, single_action=False,
             capture_requested=False, explore=False, after_action=None, observation=None):
        s = self._s
        vision = s._vision if vision is None else vision
        executor = s.executor if kind == "act" else (s.executor or s.judge) if explore else s.judge
        if executor is None:
            raise E2EError("MODEL_UNAVAILABLE", "configure an executor or judge")
        if row.model_calls >= budget:
            raise E2EError("STEP_BUDGET_EXHAUSTED", "model-call budget exhausted")
        if observation is None:
            observation = (self._observe_after_action(deadline, after_action, fallback=bool(vision)) if after_action
                           else s._observe(deadline, fallback=bool(vision)))
        request = dict(kind=kind, instruction=s._ledger.redact(instruction), params=params,
                       context=s._ledger.redact(s.config.context), observation=observation)
        if explore:
            request['explore'] = True
        if kind == 'act' and vision == 'on_demand' and not s._ledger.secrets:
            request['vision_mode'] = 'on_demand'
        if single_action:
            request["single_action"] = True
        if kind == 'act' and not explore and s.config.stateful and vision is False:
            # The built-in adapter keeps one conversation per step and sends
            # differences after the first screen; a custom executor ignores this.
            request["stateful"] = True
        self._last_observation = request['observation']
        self._note_shown(request['observation'])
        self._last_visual = None
        if history is not None:
            request["history"] = self._bounded_history(history)
            if not explore:
                request["prior_steps"] = self._prior_steps()
            request["tools"] = [dict(name=t.name, description=t.description, schema=t.schema, mutates=t.mutates)
                                for t in s.tools.values() if not explore or not t.mutates]
        if schema is not None:
            request["schema"] = schema
        if repair:
            request["repair"] = repair
        if vision is True or (vision == 'on_demand' and (capture_requested or observation.get('tree_unavailable'))):
            if s._ledger.secrets:
                raise E2EError("POLICY_DENIED", "vision disabled while secrets are registered")
            capture = getattr(s.engine, 'capture_vision', None)
            if callable(capture):
                data, visual = capture(timeout=deadline.remaining())
                request['visual'] = visual
                self._last_visual = visual
            else:
                data = s.engine.screenshot(timeout=deadline.remaining())
            request["screenshot"] = base64.b64encode(data).decode()
            self._pixels_shown = True
        if s.config.max_input_tokens is not None:
            request = _fit_request(request, s.config.max_input_tokens)
        row.model_calls += 1
        payload, answer = s._ledger.redact(request), None
        for attempt in range(3):
            if attempt:
                if row.model_calls >= budget:
                    raise E2EError("STEP_BUDGET_EXHAUSTED", "model-call budget exhausted")
                row.model_calls += 1
            native = getattr(s.engine.observe, "__func__", None) is BrowserEngine.observe
            # Stable aliases let an acting step refer back to its action history.
            usage_scope = _model_step.set(
                (row.model_usage, native, s.screen if native and kind == "act" else None,
                 len(history or ()), s.config.max_input_tokens))
            try:
                answer = executor.complete(payload, timeout=deadline.remaining())
                deadline.remaining()
            except E2EError as exc:
                # A rejected request or malformed response must not end a whole step.
                if exc.code == "CONTEXT_OVERFLOW" and attempt < 2:
                    request = _shrink_request(request)
                    payload = s._ledger.redact(dict(request, repair=_repair_note(exc)))
                    continue
                if exc.code in {"MODEL_OUTPUT_INVALID", "MODEL_REFUSED"} and attempt < 2:
                    payload = s._ledger.redact(dict(request, repair=_repair_note(exc)))
                    continue
                raise
            except Exception:
                raise E2EError("MODEL_PROVIDER_FAILED", "executor failed") from None
            finally:
                _model_step.reset(usage_scope)
            if isinstance(answer, dict) and len(canonical(answer).encode()) <= 1048576:
                return s._ledger.redact(answer)
            if attempt == 2:
                break
            payload = s._ledger.redact(dict(request, repair="Return one JSON object under 1 MiB."))
        raise E2EError("MODEL_OUTPUT_INVALID", "executor must return a bounded JSON object")

    def _dispatch_replay(self, action, row, deadline, allowed_secrets, *, recover_missing):
        """Return False only for a missing semantic target before dispatch."""
        engine = self._s.engine
        target = action.get('target', {})
        if (recover_missing and target.get('by') == 'semantic' and action['op'] not in {'scroll_find', 'fill_secret'}
                and getattr(engine.perform, '__func__', None) is BrowserEngine.perform):
            # A slow render is not a stale recording, so a missing target is retried
            # under the step deadline before the step hands back to the model.
            resolved = None
            for delay in RELOCATION_RETRY_DELAYS:
                try:
                    resolved = engine.target(target, timeout=min(deadline.remaining(), 5))
                    break
                except E2EError as exc:
                    if exc.code != 'LOCATOR_NOT_FOUND' or deadline.remaining() <= delay + 0.05:
                        if exc.code == 'LOCATOR_NOT_FOUND':
                            return False
                        raise
                    time.sleep(delay)
                except Exception as exc:
                    raise engine.translate(exc) from None
            if resolved is None:
                return False
            try:
                self._dispatch(action, row, deadline, 'replay', allowed_secrets, resolved=resolved)
            finally:
                resolved.handle.dispose()
        else:
            self._dispatch(action, row, deadline, 'replay', allowed_secrets)
        return True

    def _action_budget(self, deadline: Deadline) -> float:
        """One browser operation's own budget, never longer than the step has left."""
        return min(deadline.remaining(), self._s.config.action_timeout)

    @staticmethod
    def _action_error(exc: E2EError, deadline: Deadline) -> E2EError:
        """A timeout the step still has time for is an action the model can retry."""
        if isinstance(exc, StepTimeout) and deadline.remaining() > 0.05:
            return E2EError("ACTION_FAILED", "the control did not become actionable within the action budget")
        return exc

    def _dispatch(self, action, row, deadline, source, allowed_secrets, *, mask_value=False, resolved=None):
        s = self._s
        if isinstance(s.engine, BrowserEngine):
            active_tab = s.engine.active_tab
            if source == 'model' and active_tab != self._last_observation.get('active_tab', 1):
                raise E2EError('NODE_STALE', 'active tab changed since the last observation')
        if action['op'] in VISUAL_OPS:
            visual = getattr(self, '_last_visual', None)
            if s._ledger.secrets or (source != 'replay' and (not visual or visual['id'] != action['screenshot'])):
                raise E2EError('POLICY_DENIED', 'coordinate actions require vision in the current model request and no registered secrets')
        tool = s.tools.get(action.get("name")) if action["op"] == "tool" else None
        counts_action = tool.mutates if tool else True
        event = dict(index=row.actions + 1, source=source, action=s._ledger.redact(action))
        one_time_code = False
        counted = False

        def before_action(is_one_time_code=False):
            nonlocal one_time_code, counted
            one_time_code = is_one_time_code or mask_value
            if one_time_code:
                if source == 'replay':
                    raise ReplayStale('REPLAY_STALE', 'recording targets a one-time-code field; reset the page before retrying')
                event['action']['value'] = '<masked>'
            if s.config.on_before_action:
                s.config.on_before_action(event)
                deadline.remaining()
            if counts_action:
                row.actions += 1
            counted = True

        def relocate_after_stale(exc):
            """Retry a live action whose observed element was replaced.

            The model addressed a node from the last observation. If the app
            re-rendered it, the recorded role/name still names the control; the
            action is retried once against the relocated node instead of
            spending another model turn. A secure field is never retried this
            way, because the original action's authorization checks did not run.
            """
            if (source == 'replay' or exc.code != 'NODE_STALE'
                    or not isinstance(s.engine, BrowserEngine)
                    or action['op'] in {'tool', 'fill_secret', 'scroll_find'} | VISUAL_OPS | TAB_OPS):
                raise self._action_error(exc, deadline) from None
            descriptor = self._relocated_descriptor(action)
            if descriptor is None:
                raise self._action_error(exc, deadline) from None
            try:
                resolved = s.engine.target(descriptor, timeout=min(deadline.remaining(), 5))
            except E2EError:
                raise self._action_error(exc, deadline) from None
            try:
                if inspect_input:
                    with s.engine._input_target(descriptor, timeout=self._action_budget(deadline),
                                                resolved=resolved) as (target, otp):
                        if otp:
                            raise E2EError("NODE_STALE", "relocated input now expects a one-time code")
                        if not counted:
                            before_action()
                        return s.engine._perform(action, timeout=self._action_budget(deadline), input_target=target)
                if not counted:
                    before_action()
                return s.engine._perform(action, timeout=self._action_budget(deadline), input_target=resolved)
            finally:
                resolved.handle.dispose()

        perform = s.engine.perform
        inspect_input = (getattr(perform, '__func__', None) is BrowserEngine.perform and 'target' in action
                         and action['op'] in {'fill', 'type', 'press'})
        if not inspect_input:
            before_action()
        if action["op"] == "tool":
            if tool is None:
                raise E2EError("POLICY_DENIED", "unknown project tool")
            try:
                Draft202012Validator(tool.schema).validate(action["arguments"])
            except ValidationError:
                raise E2EError("INVALID_ARGUMENT", "tool arguments failed schema validation") from None
            try:
                produced = tool.handler(action["arguments"])
            except E2EError:
                raise
            except Exception as exc:
                # A project tool's own failure is an action the model can correct,
                # not a crash of the step; redact before it reaches history.
                raise E2EError("ACTION_FAILED", s._ledger.redact(str(exc))[:400] or "project tool failed") from None
            result = bound_output(s._ledger.redact(produced),
                                  max_lines=MAX_TOOL_OUTPUT_LINES, max_bytes=MAX_TOOL_OUTPUT_BYTES)
        elif action["op"] == "fill_secret":
            secret_name = action["secret"]
            if secret_name not in allowed_secrets or secret_name not in s._ledger.secrets:
                raise E2EError("POLICY_DENIED", "secret was not declared in this act's params")
            fill = getattr(s.engine, "fill_secret", None)
            if not callable(fill):
                raise E2EError("POLICY_DENIED", "engine does not implement authorized secret filling")
            try:
                result = fill(action["target"], s._ledger.secrets[secret_name], timeout=self._action_budget(deadline))
            except E2EError as exc:
                raise self._action_error(exc, deadline) from None
        else:
            if "<secret:" in canonical(action):
                raise E2EError("POLICY_DENIED", "secret handles require fill_secret")
            try:
                if source == 'replay' and action['op'] in VISUAL_OPS:
                    result = s.engine._perform_visual(action, timeout=self._action_budget(deadline), replay=True)
                elif inspect_input:
                    input_end = time.monotonic() + self._action_budget(deadline)
                    with s.engine._input_target(action['target'], timeout=self._action_budget(deadline),
                                                resolved=resolved) as (target, otp):
                        before_action(otp)
                        remaining = min(deadline.remaining(), input_end - time.monotonic())
                        if remaining <= 0:
                            raise StepTimeout("STEP_TIMEOUT", "input target resolution exhausted the action budget")
                        result = s.engine._perform(action, timeout=remaining, input_target=target)
                elif resolved is not None:
                    result = s.engine._perform(action, timeout=self._action_budget(deadline), input_target=resolved)
                else:
                    result = perform(action, timeout=self._action_budget(deadline))
            except E2EError as exc:
                result = relocate_after_stale(exc)
        deadline.remaining()
        if result is not None:
            canonical(result)
        if s.config.on_action:
            s.config.on_action(event)
            deadline.remaining()
        return s._ledger.redact(result), one_time_code

    def act(self, instruction: str, params: dict | None = None, *, timeout: float | None = None,
            max_actions: int | None = None, max_model_calls: int | None = None,
            vision: VisionMode | None = None) -> ActResult:
        """Run a bounded multi-action flow or replay a verified recording."""
        return self._act(instruction, params, timeout=timeout, max_actions=max_actions,
                         max_model_calls=max_model_calls, vision=vision, single_action=False)

    def act_once(self, instruction: str, params: dict | None = None, *, timeout: float | None = None,
                 max_model_calls: int | None = None, vision: VisionMode | None = None) -> ActResult:
        """Complete after dispatching one action, with no model confirmation round."""
        return self._act(instruction, params, timeout=timeout, max_actions=1,
                         max_model_calls=max_model_calls, vision=vision, single_action=True)

    def _act(self, instruction, params, *, timeout, max_actions, max_model_calls, vision, single_action):
        s = self._s
        vision = s._vision if vision is None else vision
        if type(vision) is not bool and vision != 'on_demand':
            raise E2EError('INVALID_ARGUMENT', 'act vision must be a boolean or on_demand')
        if not isinstance(instruction, str) or not normalize(instruction):
            raise E2EError("INVALID_ARGUMENT", "instruction must be nonempty text")
        actions_limit = self._limit(max_actions, s.config.max_actions, "max_actions")
        model_limit = self._limit(max_model_calls, s.config.max_model_calls, "max_model_calls")
        raw_params = params
        params, slots = self._params(raw_params)
        allowed_secrets = set()
        param_strings = set()

        def collect(value):
            if isinstance(value, Secret):
                allowed_secrets.add(value.name)
            elif isinstance(value, Unique):
                param_strings.add(value.value)
            elif isinstance(value, dict):
                for child in value.values():
                    collect(child)
            elif isinstance(value, (list, tuple)):
                for child in value:
                    collect(child)
            elif isinstance(value, str):
                param_strings.add(value)

        collect(raw_params)

        def upload_is_bound(action):
            return isinstance(s.engine, BrowserEngine) and all(path in param_strings for path in action["value"])

        step_input = dict(instruction=normalize(s._ledger.redact(instruction)), params=template(params, slots))
        if single_action:
            step_input["single_action"] = True
        signature = digest(step_input)
        api = "act_once" if single_action else "act"
        with s._step(api, instruction, timeout if timeout is not None else s.config.timeout) as (row, deadline):
            occurrence = s._occurrences[signature]
            s._occurrences[signature] += 1
            provenance = dict(project=s._identity["project"], test=s._identity["test"], signature=signature,
                              occurrence=occurrence, agent=s.config.name)
            identity = dict(s._identity, context=digest(s._ledger.redact(s.config.context)))
            key = digest({"schema": SCHEMA, **identity, **provenance})
            row.cache.update(mode="off" if s._mode == "off" else "miss", key=key)
            self._shown_texts: set[str] = set()
            self._pixels_shown = False
            before = s._observe(deadline, fallback=bool(vision))
            browser = s.engine if isinstance(s.engine, BrowserEngine) else None
            collision = templates_collide(params, slots)
            replayable_tab = not collision and not before.get('tree_unavailable') and (browser is not None or before.get('active_tab', 1) == 1)
            tab_states = [browser._tabs.fingerprint()] if browser and s._mode != 'off' else []
            track_tabs = before.get('tab_count', 1) > 1 or before.get('active_tab', 1) != 1
            start_origin = s._current_origin(deadline) if s._mode != "off" and replayable_tab else None
            history, recorded, action_origins = [], [], []
            settle_budget, last_op = 0.35, None
            keep_recording = False
            gap_recorded = False
            complete_start = complete_view(before)
            cacheable = (s._mode == "read-write" and replayable_tab and complete_start)
            try:
                entry = None
                if s._mode != "off" and s._cache_config.attempt and not s._cache_config.strict:
                    row.cache["reason"] = "retry"
                elif s._mode != "off" and replayable_tab:
                    try:
                        entry = s._store.read(key)
                    except Exception:
                        entry = None
                    if entry is not None and not valid_entry(entry, key):
                        if s._cache_config.strict:
                            raise ReplayStale("REPLAY_STALE", "recording is invalid")
                        entry = None
                    if entry is None and s._cache_config.strict and hasattr(s._store, "entries"):
                        for old_key, old in s._store.entries():
                            if old_key != key and valid_entry(old, old_key, allow_legacy=True) and old["provenance"] == provenance:
                                raise ReplayStale("REPLAY_STALE", "same step was recorded under a different context or cache policy; re-record it")
                if entry is not None and not complete_start:
                    if s._cache_config.strict:
                        raise ReplayStale("REPLAY_STALE", "starting observation is incomplete")
                    row.cache["reason"] = "incomplete-observation"
                    entry = None
                if entry is not None:
                    entry = dict(entry)
                    for field in ("start", "end", "appeared", "gone", "actions", "summary"):
                        if field in entry:
                            entry[field] = template(entry[field], slots, expand=True)
                    tabs = entry.get('tab_states')
                    start_tab_matches = bool(tab_states and tab_states[0] == tabs[0]) if tabs else before.get('active_tab', 1) == 1
                    pixel_step = any(action['op'] in VISUAL_OPS for action in entry['actions'])
                    if pixel_step and (not vision or not browser or s._ledger.secrets):
                        if s._cache_config.strict:
                            raise ReplayStale("REPLAY_STALE", "coordinate replay requires enabled vision and no registered secrets")
                        start_tab_matches = False
                    # A recording that begins by navigating is not bound to the page it was recorded on.
                    moved_first = opens_with_navigate(entry["actions"])
                    if (not start_tab_matches
                            or not s._same_origin(entry["start_origin"], entry.get("app_origin"), start_origin)
                            or (not moved_first and not same_route(before["url"], entry["start"]))):
                        if s._cache_config.strict:
                            raise ReplayStale("REPLAY_STALE", "recorded starting origin or route changed")
                    else:
                        if s._cache_config.strict and entry["actions"][-1]["op"] == "gap":
                            raise ReplayStale("REPLAY_STALE", "recorded prefix requires live model handoff")
                        # Verify the whole upload plan before replaying any preceding actions.
                        for action in entry["actions"]:
                            if action["op"] == "upload":
                                if not upload_is_bound(action):
                                    raise ReplayStale("REPLAY_STALE", "recorded upload requires paths supplied in this step's params")
                                s.engine._resolve_upload_files(action["value"])
                        if sum(action["op"] != "gap" for action in entry["actions"]) > actions_limit:
                            raise E2EError("STEP_BUDGET_EXHAUSTED", "recording exceeds action budget")
                        reason = "end-mismatch"
                        notice = "Recorded actions already ran. End-state check failed."
                        screens, previous_url = [], before.get("url")
                        try:
                            for index, (action, expected_origin) in enumerate(zip(entry["actions"], entry["action_origins"], strict=True)):
                                if tabs:
                                    if not browser._tabs.wait_for_fingerprint(tabs[index + 1], deadline.remaining()):
                                        raise ReplayStale('REPLAY_STALE', 'recorded tab identity or URL changed before action')
                                elif browser and browser.active_tab != 1:
                                    raise ReplayStale('REPLAY_STALE', 'recording left the initial tab')
                                if not s._same_origin(expected_origin, entry.get("app_origin"),
                                                       s._current_origin(deadline, refresh=True, action=action,
                                                                         recorded_app=entry.get("app_origin"))):
                                    raise ReplayStale("REPLAY_STALE", "origin changed during replay; reset the page before retrying")
                                if action.get('op') == 'gap':
                                    reason = 'gap'
                                    notice = ("The recorded prefix was replayed; the next value is "
                                              "this run's own data.")
                                    break
                                if action['op'] in VISUAL_OPS:
                                    action = browser.replay_pixels(action, timeout=deadline.remaining())
                                if not self._dispatch_replay(action, row, deadline, allowed_secrets,
                                                             recover_missing=True):
                                    if (not s._same_origin(expected_origin, entry.get("app_origin"),
                                                           s._current_origin(deadline, refresh=True, action=action, recorded_app=entry.get("app_origin")))
                                            or (browser._tabs.fingerprint() != tabs[index + 1] if tabs else browser.active_tab != 1)):
                                        raise ReplayStale("REPLAY_STALE", "origin or tab changed during relocation")
                                    reason = "target-not-found"
                                    notice = "The next recorded target was missing. That action was not dispatched."
                                    history.append({"action": action, "source": "replay", "dispatched": False})
                                    break
                                history.append({"action": action, "source": "replay"})
                                # Keep the screen the replay arrived on, so an effect a previous
                                # run left behind is not counted as this run's. The screen after the
                                # last action is that action's own result, never a baseline.
                                if browser is not None and index + 1 < len(entry["actions"]):
                                    current = s.engine.current_url
                                    if current != previous_url:
                                        previous_url = current
                                        screens.append(s._observe(deadline, fallback=bool(vision)))
                            else:
                                # The screen the replay arrived on, not its final one: an effect a
                                # previous run left behind must not count as this run's.
                                after = self._poll_observation(
                                    deadline,
                                    lambda view: matches_end(entry, end_baseline(screens, view["url"], before), view)
                                    and (not tabs or browser._tabs.fingerprint() == tabs[-1]),
                                    wait=max(0.35, entry.get("end_wait_ms", 0) / 1000))
                                if (browser._tabs.fingerprint() != tabs[-1] if tabs else after.get('active_tab', 1) != 1):
                                    raise ReplayStale('REPLAY_STALE', 'the recording left the initial tab')
                                baseline = end_baseline(screens, after["url"], before)
                                end_origin = s._current_origin(deadline)
                                if not s._same_origin(entry["end_origin"], entry.get("app_origin"), end_origin):
                                    raise ReplayStale("REPLAY_STALE", "recorded ending origin changed; reset the page before retrying")
                                pixels_match = not pixel_step or browser.current_pixel_signature(timeout=deadline.remaining()) == entry['end_pixels']
                                # A single-action step is one input by definition; its echo
                                # is the effect it claims. A multi-action flow needs another
                                # anchor to prove the app reacted.
                                targets = () if single_action else input_targets(entry["actions"])
                                first_pixel = next((item["screenshot"] for item in entry["actions"]
                                                    if item["op"] in VISUAL_OPS), None)
                                evidenced = ((pixel_step and pixels_match and first_pixel != entry["end_pixels"])
                                             or (tabs and tabs[0] != tabs[-1])
                                             or (baseline is before and start_origin != end_origin
                                                 and entry["start_origin"] != entry["end_origin"])
                                             or delta_evidenced(baseline, after, targets, recorded=entry))
                                if matches_end(entry, baseline, after) and pixels_match and evidenced:
                                    row.cache["mode"] = "replay"
                                    row.explanation = entry.get("summary", "replayed verified actions")
                                    s._pending.append(dict(key=key, entry=entry, row=row, replayed=True, confirmed=False))
                                    return ActResult(entry.get("summary", "replayed verified actions"), row.actions, 0, row.cache)
                        except E2EError as exc:
                            if isinstance(exc, ReplayStale) or exc.code in {"STEP_TIMEOUT", "CANCELLED", "POLICY_DENIED", "STEP_BUDGET_EXHAUSTED"}:
                                raise
                            # Two candidates for one recorded target is a stale recording,
                            # never a guess: the fail-closed contract is not tunable.
                            if exc.code == "LOCATOR_AMBIGUOUS":
                                s._evict(key)
                                raise ReplayStale("REPLAY_STALE",
                                                  "recorded target is ambiguous on this screen") from None
                            # Other failures do not prove the recording wrong, so the step
                            # hands back to the model without discarding an unproven entry.
                            if exc.code == "ACTION_MAY_HAVE_COMMITTED":
                                # The action may have reached the app before it failed:
                                # replay hands over rather than typing or clicking twice.
                                reason = "action-uncertain"
                                notice = ("The next recorded action may have committed before it failed. "
                                          "Check the current screen before repeating it.")
                                row.cache["uncertain_action"] = f"recorded action {index + 1}"
                                cacheable = False
                            else:
                                reason = "target-not-found" if exc.code in {"LOCATOR_NOT_FOUND", "NODE_STALE"} else "action-failed"
                                notice = f"The next recorded action could not be replayed ({reason})."
                                if exc.code == "ACTION_FAILED":
                                    cacheable = False
                            row.cache["reason"] = reason
                        if single_action or s._cache_config.strict:
                            raise ReplayStale("REPLAY_STALE", "recorded end-state delta did not hold")
                        row.cache.update(mode="handoff", reason=reason)
                        history.append({"notice": notice + " Continue from the current screen; do not repeat completed actions."})
                        cacheable = False
                        keep_recording = reason in {"target-not-found", "action-failed", "action-uncertain", "gap"}
                        executed = any(entry.get('source') == 'replay' and entry.get('dispatched') is not False
                                       for entry in history)
                        if not executed and reason in {"target-not-found", "action-failed"}:
                            # A replay that performed nothing is a miss: the model
                            # starts from the step's own start screen, so its flow is
                            # a complete recording that can replace the stale one.
                            cacheable = s._mode == "read-write" and complete_start
                            row.cache['replay_prefix'] = 0
                        elif not keep_recording:
                            s._evict(key)
                repeats = Counter()
                failures, failed_actions = 0, Counter()
                last_fill = None
                warned_fill = False
                refusals = 0
                stale_refusals = 0
                summary = "action completed"
                initial_observation = None if history else before
                record_arrivals = []
                previous_record_url = before["url"]
                capture_requested = False
                screenshot_corrected = False
                after_action = None
                while True:
                    answer = self._ask("act", instruction, params, row, deadline, model_limit,
                                       history=history, vision=vision, single_action=single_action,
                                       capture_requested=capture_requested, after_action=after_action, observation=initial_observation)
                    initial_observation = None
                    after_action = None
                    try:
                        if "action" in answer and set(answer) == {"action"}:
                            action = validate_action(answer["action"])
                            if single_action and action.get('times', 1) > 1:
                                raise E2EError('MODEL_OUTPUT_INVALID', 'act_once allows only one key press')
                            if action["op"] == "tool":
                                tool = s.tools.get(action["name"])
                                if tool is None:
                                    raise E2EError("MODEL_OUTPUT_INVALID", "unknown project tool")
                                try:
                                    Draft202012Validator(tool.schema).validate(action["arguments"])
                                except ValidationError:
                                    raise E2EError("MODEL_OUTPUT_INVALID", "tool arguments failed schema validation") from None
                        elif answer.get("done") is True and set(answer) <= {"done", "summary"} and isinstance(answer.get("summary", ""), str):
                            if single_action:
                                raise E2EError("MODEL_OUTPUT_INVALID", "act_once needs an action before completion")
                        elif set(answer) == {"blocked"} and isinstance(answer["blocked"], str) and answer["blocked"]:
                            pass
                        else:
                            raise E2EError("MODEL_OUTPUT_INVALID", "return exactly one action, done with optional string summary, or blocked reason")
                    except E2EError as exc:
                        refusals += 1
                        if refusals >= 3:
                            raise
                        history.append(dict(error=dict(code=exc.code, message=str(exc)), dispatched=False))
                        continue
                    refusals = 0
                    if answer.get("blocked"):
                        raise E2EError("AUTOMATION_UNSUPPORTED", str(answer["blocked"]))
                    if answer.get("done") is True:
                        summary = str(answer.get("summary", "step completed"))[:1000]
                        break
                    if action['op'] == 'observe':
                        history.append(dict(action=action))
                        continue
                    if action['op'] == 'screenshot':
                        if s._ledger.secrets:
                            raise E2EError('POLICY_DENIED', 'screenshots disabled while secrets are registered')
                        if vision != 'on_demand':
                            message = ('Use the attached image and current visual.id when available.'
                                       if vision else 'Use a node ref from the latest text observation.')
                            if screenshot_corrected:
                                raise E2EError('MODEL_OUTPUT_INVALID', f'Repeated unavailable screenshot request. {message}')
                            screenshot_corrected = True
                            feedback = (f'Screenshot requests are unavailable in this step. {message} '
                                        'Return blocked with a reason if the target cannot be located.')
                            history.append(dict(error=dict(code='MODEL_OUTPUT_INVALID', message=feedback), dispatched=False))
                            continue
                        capture_requested = True
                        history.append({'notice': 'Viewport screenshot attached to the next observation.'})
                        continue
                    tool = s.tools.get(action.get("name")) if action["op"] == "tool" else None
                    mutates = tool.mutates if tool else True
                    sequence = press_sequence(action)
                    if mutates and row.actions + len(sequence) > actions_limit:
                        raise E2EError("STEP_BUDGET_EXHAUSTED", "action budget exhausted")
                    stable_action = dict(action)
                    for field in ('target', 'to'):
                        target = stable_action.get(field)
                        if target and target['by'] == 'ref':
                            stable_action[field] = {key: value for key, value in target.items() if key != 'observation'}
                    fill = None
                    target = action.get("target", {})
                    if (action["op"] == "fill" and target.get("by") == "ref"
                            and target["observation"] == self._last_observation.get("id")):
                        fill = (target["value"], action["value"])
                        if fill == last_fill:
                            node = next((n for n in self._last_observation.get("nodes", [])
                                         if n.get("id") == target["value"]), {})
                            if (node.get("editable") and node.get("value") == action["value"]
                                    and node.get("value_exact") is True):
                                if warned_fill:
                                    raise E2EError("STEP_NO_CONCLUSION", "repeated fill on an already satisfied input")
                                history.append(dict(notice="Input already has the requested value. Check the candidates or proceed.",
                                                    dispatched=False))
                                after_action = self._last_observation
                                warned_fill = True
                                continue
                    marker = digest(dict(action=stable_action, observation={k:v for k,v in self._last_observation.items() if k != 'id'}))
                    repeats[marker] += 1
                    if repeats[marker] > 2:
                        raise E2EError("STEP_NO_CONCLUSION", "same action repeated on an unchanged screen")
                    if not complete_view(self._last_observation) or (not browser and action['op'] in TAB_OPS | VISUAL_OPS):
                        cacheable = False
                    if cacheable and browser:
                        current_url = self._last_observation["url"]
                        if recorded and current_url != previous_record_url:
                            record_arrivals.append(self._last_observation)
                        previous_record_url = current_url
                        tab_states.append(browser._tabs.fingerprint())
                        track_tabs |= self._last_observation.get('tab_count', 1) > 1 or action['op'] in TAB_OPS
                    action_origin = s._current_origin(deadline, action=action) if cacheable else None
                    recorded_url = None
                    if cacheable and browser and action["op"] == "navigate":
                        destination = browser.resolve_navigation(action["url"])
                        # A path-relative navigation must retain the path it resolved
                        # to here, even if replay starts in another directory.
                        supplied = urlsplit(action["url"])
                        relative = not supplied.netloc and not supplied.scheme
                        app = s._app_origin or (origin(destination) if relative else None)
                        recorded_url = app_relative(destination, app) if app else destination
                        if recorded_url != destination and browser.resolve_navigation(recorded_url) != destination:
                            recorded_url = destination
                    sent, one_time_code = 0, False
                    try:
                        for item in sequence:
                            result, one_time_code = self._dispatch(item, row, deadline, 'model', allowed_secrets,
                                                                  mask_value=one_time_code)
                            sent += 1
                    except E2EError as exc:
                        retryable = exc.code in RECOVERABLE_ACTION_CODES and not sent and not single_action
                        if not retryable:
                            raise
                        failures += 1
                        failed_actions[marker] += 1
                        if failures >= MAX_ACTION_FAILURES or failed_actions[marker] >= MAX_SAME_ACTION_FAILURES:
                            raise
                        stale_refusals += 1
                        # The screen may have moved on, so the next look is a fresh one.
                        cacheable = False
                        after_action = None
                        failure = dict(error=dict(code=exc.code, message=str(exc)[:512]))
                        if exc.code == "ACTION_MAY_HAVE_COMMITTED":
                            failure["notice"] = "The action may already have taken effect. Check the screen before repeating it."
                        else:
                            failure["dispatched"] = False
                        history.append(failure)
                        continue
                    last_fill = None if one_time_code else fill
                    warned_fill = False
                    stale_refusals = 0
                    last_op = action['op']
                    settle_budget = max(settle_budget, SETTLE_SECONDS.get(action['op'], 0.35))
                    if mutates and action['op'] not in {'wait', 'resize_viewport', 'switch_tab', 'close_tab'}:
                        after_action = self._last_observation
                    derived = (self._derived_reason(action, instruction, params)
                               if not one_time_code and action['op'] in {'fill', 'type'} else None)
                    if derived:
                        row.cache['derived'] = derived
                    if derived and cacheable and recorded and not gap_recorded and not track_tabs:
                        # Keep the proven prefix; replay stops before the typed
                        # value and hands over so the model derives it afresh.
                        recorded.append(dict(op='gap', reason=derived))
                        action_origins.append(action_origin)
                        gap_recorded = True
                    elif one_time_code or derived or not recordable_action(action):
                        cacheable = False
                    elif gap_recorded:
                        pass
                    elif action['op'] == 'upload' and not upload_is_bound(action):
                        cacheable = False
                    elif cacheable:
                        recorder = getattr(s.engine, 'record_action', None)
                        recording = recorder(action) if callable(recorder) else action
                        if recording is None:
                            cacheable = False
                        else:
                            if recorded_url is not None:
                                recording = dict(recording, url=recorded_url)
                            recorded.append(recording)
                            action_origins.append(action_origin)
                    history_action = dict(action, value='<masked>') if one_time_code else action
                    entry = dict(action=history_action, result=result)
                    target = action.get("target", {})
                    if target.get("by") == "ref" and not one_time_code and action["op"] != "fill_secret":
                        # A transient control can disappear before the next decision. Keep its
                        # observed label so history still identifies what was actually used.
                        node = next((n for n in self._last_observation.get("nodes", [])
                                     if n.get("id") == target["value"]), None)
                        if node:
                            entry["observed_target"] = dict(role=node.get("role"), name=node.get("name", "")[:160])
                    history.append(entry)
                    if single_action:
                        break
                row.explanation = summary
                committed_at = time.monotonic()
                if not single_action or cacheable:
                    # Record the screen once it holds still: a half-loaded page would store an
                    # end state the next run can never reach.
                    after = (self._settle_observe(deadline, fallback=bool(vision), wait=settle_budget)
                             if cacheable
                             else (self._observe_after_action(deadline, after_action, fallback=bool(vision),
                                                              op=last_op) if after_action
                                   else s._observe(deadline, fallback=bool(vision))))
                    settled_at = time.monotonic()
                    baseline = end_baseline(record_arrivals, after["url"], before)
                    route_changed = not same_route(baseline["url"], after["url"])
                    appeared, gone = delta(baseline, after, route_moved=route_changed)
                    end_origin = s._current_origin(deadline) if cacheable else None
                    track_tabs |= after.get('tab_count', 1) > 1
                    if cacheable and browser and track_tabs:
                        tab_states.append(browser._tabs.fingerprint())
                    tab_change = browser and track_tabs and tab_states and tab_states[0] != tab_states[-1]
                    visual_actions = [item for item in recorded if item['op'] in VISUAL_OPS]
                    end_pixels = (browser.current_pixel_signature(timeout=deadline.remaining())
                                  if cacheable and visual_actions and not s._ledger.secrets else None)
                    pixels_changed = end_pixels and end_pixels != visual_actions[0]['screenshot']
                    if (cacheable and recorded and complete_view(after)
                        and (appeared or gone or pixels_changed or tab_change or route_changed
                             or (baseline is before and start_origin != end_origin))):
                        trace = dict(schema=SCHEMA, key=key, provenance=provenance,
                                     start=route(before["url"]), end=route(after["url"]),
                                     appeared=appeared, gone=gone, actions=recorded, summary=summary)
                        # A recorded input that redaction rewrote would replay a different
                        # value than the run proved, so the entry is kept only for reference.
                        verbatim = self._verbatim_actions(recorded)
                        if verbatim is None:
                            trace['truncated'] = True
                        else:
                            trace['actions'] = verbatim
                        trace = s._ledger.redact(trace)
                        for field in ("start", "end", "appeared", "gone", "actions", "summary"):
                            trace[field] = template(trace[field], slots)
                        if end_pixels:
                            trace['end_pixels'] = end_pixels
                        if browser and track_tabs:
                            trace['tab_states'] = tab_states
                        # Wait as long again on the next run as the effect took to show here.
                        trace['end_wait_ms'] = int(max(0.0, settled_at - committed_at) * 1000) + 10000
                        trace.update(start_origin=start_origin, end_origin=end_origin, action_origins=action_origins)
                        if s._app_origin:
                            trace['app_origin'] = s._app_origin
                        s._pending.append(dict(key=key, entry=trace, row=row, replayed=False, confirmed=False))
                    elif not keep_recording and s._mode == "read-write" and replayable_tab and complete_start and complete_view(after):
                        s._evict(key)
                return ActResult(summary, row.actions, row.model_calls, row.cache)
            except BaseException as exc:
                if (not keep_recording and complete_start and replayable_tab and not isinstance(exc, (KeyboardInterrupt, SystemExit))
                    and getattr(exc, "code", "") not in {"MODEL_UNAVAILABLE", "MODEL_PROVIDER_FAILED", "CANCELLED", "STEP_BUDGET_EXHAUSTED"}):
                    s._evict(key)
                raise

    def _judge(self, instruction, params, row, deadline, budget, vision):
        for repair in (None, "Return a JSON object with a boolean passed and a string reason."):
            result = self._ask("assert", instruction, params, row, deadline, budget, repair=repair, vision=vision)
            if type(result.get("passed")) is bool and isinstance(result.get("reason", ""), str):
                row.explanation = result.get("reason", "")[:1000]
                return result
        raise E2EError("MODEL_OUTPUT_INVALID", "judgment failed validation after one repair")

    def assert_(self, instruction: str, params: dict | None = None, *, timeout: float | None = None,
                vision: bool | None = None, explore: bool = False, max_actions: int | None = None,
                max_model_calls: int | None = None) -> None:
        """Judge fresh state, repairing malformed judgment once; never replay a verdict."""
        s = self._s
        if type(explore) is not bool or (not explore and (max_actions is not None or max_model_calls is not None)):
            raise E2EError('INVALID_ARGUMENT', 'assertion action and model budgets require explore=True')
        params, _ = self._params(params)
        with s._step("assert", instruction, timeout if timeout is not None else s.config.judgment_timeout) as (row, deadline):
            if explore:
                actions_limit = self._limit(max_actions if max_actions is not None else min(10, s.config.max_actions),
                                            s.config.max_actions, 'max_actions')
                model_limit = self._limit(max_model_calls if max_model_calls is not None else min(6, s.config.max_model_calls),
                                          s.config.max_model_calls, 'max_model_calls')
                result = self._assert_exploring(instruction, params, row, deadline, actions_limit, model_limit, vision)
                row.explanation = result.get('reason', '')[:1000]
            else:
                result = self._judge(instruction, params, row, deadline, s.config.max_model_calls, vision)
            if not result["passed"]:
                raise AssertionFailed("ASSERTION_FAILED", str(result.get("reason", instruction)))
            s._confirm()

    def _assert_exploring(self, instruction, params, row, deadline, actions_limit, model_limit, vision):
        history, after_action = [], None
        while row.model_calls < model_limit:
            answer = self._ask('assert', instruction, params, row, deadline, model_limit,
                               history=history, vision=vision, explore=True, after_action=after_action)
            after_action = None
            if (type(answer.get('passed')) is bool and set(answer) <= {'passed', 'reason'}
                and isinstance(answer.get('reason', ''), str)):
                return answer
            if set(answer) == {'blocked'} and isinstance(answer['blocked'], str):
                raise AssertionFailed('ASSERTION_INCONCLUSIVE', answer['blocked'])
            if set(answer) != {'action'}:
                raise E2EError('MODEL_OUTPUT_INVALID', 'exploring assertion requires a scroll action or a verdict')
            action = validate_action(answer['action'])
            if action['op'] == 'observe':
                history.append(dict(action=action))
                continue
            tool = self._s.tools.get(action.get('name')) if action['op'] == 'tool' else None
            if action['op'] not in {'scroll', 'scroll_to', 'scroll_find'} and (tool is None or tool.mutates):
                raise E2EError('POLICY_DENIED', 'exploring assertions only allow scrolling and read-only project tools')
            if tool is None and row.actions >= actions_limit:
                raise AssertionFailed('ASSERTION_INCONCLUSIVE', 'assertion scroll budget exhausted')
            result, _ = self._dispatch(action, row, deadline, 'model', set())
            visible = [dict(role=node['role'], name=node['name'][:100], value=node.get('value', '')[:64])
                       for node in self._last_observation.get('nodes', []) if node.get('in_viewport')]
            history.append(dict(action=action, result=result, visible_nodes=visible[:20]))
            if tool is None:
                after_action = self._last_observation
        raise AssertionFailed('ASSERTION_INCONCLUSIVE', 'assertion model-call budget exhausted')

    def wait_for(self, instruction: str, params: dict | None = None, *, timeout: float | None = None,
                 interval: float = 1, max_model_calls: int | None = None, vision: bool | None = None) -> None:
        """Poll fresh judgments under one deadline and model-call ceiling."""
        s = self._s
        params, _ = self._params(params)
        positive(interval, "interval")
        budget = self._limit(max_model_calls, s.config.max_model_calls, "max_model_calls")
        with s._step("wait_for", instruction, timeout if timeout is not None else s.config.judgment_timeout) as (row, deadline):
            while True:
                if self._judge(instruction, params, row, deadline, budget, vision)["passed"]:
                    s._confirm()
                    return
                time.sleep(min(interval, deadline.remaining()))

    def extract(self, instruction: str, *, schema: Any, timeout: float | None = None, vision: bool | None = None) -> Any:
        """Validate JSON Schema or a Pydantic model, with exactly one repair round."""
        s = self._s
        model_type = schema if hasattr(schema, "model_json_schema") and hasattr(schema, "model_validate") else None
        document = schema.model_json_schema() if model_type else schema
        Draft202012Validator.check_schema(document)
        validator = Draft202012Validator(document)
        with s._step("extract", instruction, timeout if timeout is not None else s.config.judgment_timeout) as (row, deadline):
            for repair in (None, "Previous extraction failed schema validation. Return a corrected value matching schema."):
                result = self._ask("extract", instruction, {}, row, deadline, s.config.max_model_calls,
                                   schema=document, repair=repair, vision=vision)
                if "value" not in result:
                    continue
                try:
                    validator.validate(result["value"])
                    return model_type.model_validate(result["value"]) if model_type else result["value"]
                except (ValidationError, ValueError):
                    continue
            raise E2EError("MODEL_OUTPUT_INVALID", "extraction failed schema validation after one repair")
