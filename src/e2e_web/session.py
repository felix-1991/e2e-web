"""Framework-independent synchronous session and bounded agent operations."""

from __future__ import annotations

import base64
from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict, replace
import os
from pathlib import Path
import threading
import time
from typing import Any, Callable

from jsonschema import Draft202012Validator, ValidationError

from ._utils import Deadline, Ledger, canonical, digest, normalize, positive, template
from .actions import VISUAL_OPS, press_sequence, recordable_action, validate_action
from .browser import BrowserEngine
from .cache import FileCacheStore, SCHEMA, delta, matches_end, origin, route, valid_entry
from .config import _AgentSettings
from .errors import AssertionFailed, E2EError, ReplayStale, StepTimeout
from .types import ActResult, AgentConfig, CacheConfig, Cancellation, Engine, Executor, Secret, StepRecord, Tool, VisionMode


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
        for value, label in ((self.config.timeout, "timeout"), (self.config.judgment_timeout, "judgment_timeout")):
            positive(value, label)
        for value in (self.config.max_actions, self.config.max_model_calls):
            if type(value) is not int or not 1 <= value <= 1000:
                raise E2EError("INVALID_ARGUMENT", "action/model-call ceilings must be integers from 1 to 1000")
        self.cancellation = cancellation or Cancellation()
        self._end = time.monotonic() + positive(timeout, "session timeout") if timeout is not None else None
        self._owner, self._lock = threading.get_ident(), threading.Lock()
        self._closed, self._cleanup, self._on_step = False, cleanup, on_step
        self._interrupted = False
        self._popups: list[WebSession] = []
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
                              policy=5 if isinstance(engine, BrowserEngine) else 4)
        self._cache_config = cache or CacheConfig()
        ci = os.getenv("CI", "").lower() not in {"", "0", "false"}
        self._mode = self._cache_config.mode or ("read-only" if ci else "read-write")
        if self._mode not in {"off", "read-only", "read-write"}:
            raise E2EError("INVALID_ARGUMENT", "invalid cache mode")
        if self._cache_config.strict:
            if self._mode == "off":
                raise E2EError("INVALID_ARGUMENT", "strict cache cannot be off")
            self._mode = "read-only"
        self._store = self._cache_config.store or FileCacheStore(self._cache_config.directory, writable=self._mode == "read-write")
        self.steps: list[StepRecord] = []
        self._pending: list[dict] = []
        self._occurrences: Counter = Counter()
        self.artifacts_dir = Path(artifacts_dir).resolve() if artifacts_dir else None
        self.agent = Agent(self)

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
            self.engine._tabs.exclude(popup)
            popup.wait_for_load_state("domcontentloaded", timeout=deadline.remaining() * 1000)
            deadline.remaining()
            self._ensure_open()

            def release_popup():
                try:
                    try:
                        child.engine._tabs.close_owned_pages()
                    finally:
                        close_popup()
                finally:
                    self._popups.remove(child)

            child = WebSession(
                BrowserEngine(popup, observation=self.engine.observation, upload_roots=self.engine.upload_roots),
                name=name, app_identity=self._identity["app"], project=self._identity["project"],
                executor=self.executor, judge=self.judge, agent=replace(self.config, vision=self._vision),
                cache=replace(self._cache_config, mode=self._mode, store=self._store),
                tools=tuple(self.tools.values()), cancellation=self.cancellation,
                secrets=tuple(self._ledger.secrets.values()), on_step=self._on_step, cleanup=release_popup,
                artifacts_dir=self.artifacts_dir / "popups" / digest(name) if self.artifacts_dir else None,
            )
            child._end, child._ledger, child._lock = self._end, self._ledger, self._lock
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

    def _observe(self, deadline: Deadline) -> dict:
        """One fresh redacted observation under the current operation deadline."""
        observation = self.engine.observe(timeout=deadline.remaining())
        deadline.remaining()
        if self._ledger.secrets:
            for node in observation.get('nodes', []):
                node.pop('selection', None)
        self._observed_url = observation.get("url")
        return self._ledger.redact(observation)

    def _current_origin(self, deadline: Deadline, *, refresh: bool = False, action: dict | None = None) -> str:
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
                return digest([source, origin(resolve(action["url"]))])
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
        if params is not None and not isinstance(params, dict):
            raise E2EError("INVALID_ARGUMENT", "params must be an object")
        return self._s._ledger.project(params or {})

    def _limit(self, value, configured, label):
        value = configured if value is None else value
        if type(value) is not int or not 1 <= value <= configured:
            raise E2EError("INVALID_ARGUMENT", f"{label} must be from 1 to the configured ceiling {configured}")
        return value

    def _poll_observation(self, deadline, ready):
        s = self._s
        observation = s._observe(deadline)
        if not isinstance(s.engine, BrowserEngine):
            return observation
        end = time.monotonic() + min(0.35, max(0, deadline.remaining() - 0.05))
        while not ready(observation):
            delay = end - time.monotonic()
            if delay <= 0 or deadline.remaining() <= 0.05:
                break
            time.sleep(min(0.05, delay, deadline.remaining() - 0.025))
            if deadline.remaining() <= 0.025:
                break
            observation = s._observe(deadline)
        return observation

    def _observe_after_action(self, deadline, before):
        def evidence(view):
            fields = ('url', 'title', 'tree', 'frames', 'tabs', 'tab_count', 'viewport')
            visible = [node['id'] for node in view.get('nodes', []) if node.get('in_viewport')]
            return [view.get(key) for key in fields], visible

        previous = evidence(before)
        return self._poll_observation(deadline, lambda after: evidence(after) != previous)

    def _ask(self, kind, instruction, params, row, deadline, budget, *, history=None,
             schema=None, repair=None, vision=None, single_action=False,
             capture_requested=False, explore=False, after_action=None):
        s = self._s
        vision = s._vision if vision is None else vision
        executor = s.executor if kind == "act" else (s.executor or s.judge) if explore else s.judge
        if executor is None:
            raise E2EError("MODEL_UNAVAILABLE", "configure an executor or judge")
        if row.model_calls >= budget:
            raise E2EError("STEP_BUDGET_EXHAUSTED", "model-call budget exhausted")
        observation = self._observe_after_action(deadline, after_action) if after_action else s._observe(deadline)
        request = dict(kind=kind, instruction=s._ledger.redact(instruction), params=params,
                       context=s._ledger.redact(s.config.context), observation=observation)
        if explore:
            request['explore'] = True
        if kind == 'act' and vision == 'on_demand' and not s._ledger.secrets:
            request['vision_mode'] = 'on_demand'
        if single_action:
            request["single_action"] = True
        self._last_observation = request['observation']
        self._last_visual = None
        if history is not None:
            request["history"] = history
            if not explore:
                prior, size = [], 2  # Include the JSON array delimiters.
                for step in reversed(s.steps[:-1]):
                    item = s._ledger.redact(dict(api=step.api, instruction=step.instruction[:256],
                                                 status=step.status, explanation=step.explanation[:700],
                                                 replayed=step.cache.get("mode") == "replay"))
                    size += len(canonical(item).encode()) + bool(prior)
                    if size > 8192:
                        break
                    prior.append(item)
                request["prior_steps"] = list(reversed(prior))
            request["tools"] = [dict(name=t.name, description=t.description, schema=t.schema, mutates=t.mutates)
                                for t in s.tools.values() if not explore or not t.mutates]
        if schema is not None:
            request["schema"] = schema
        if repair:
            request["repair"] = repair
        if vision is True or (vision == 'on_demand' and capture_requested):
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
        row.model_calls += 1
        try:
            answer = executor.complete(s._ledger.redact(request), timeout=deadline.remaining())
            deadline.remaining()
        except E2EError:
            raise
        except Exception:
            raise E2EError("MODEL_PROVIDER_FAILED", "executor failed") from None
        if not isinstance(answer, dict) or len(canonical(answer).encode()) > 1048576:
            raise E2EError("MODEL_OUTPUT_INVALID", "executor must return a bounded JSON object")
        return s._ledger.redact(answer)

    def _dispatch(self, action, row, deadline, source, allowed_secrets, *, mask_value=False):
        s = self._s
        if isinstance(s.engine, BrowserEngine):
            active_tab = s.engine.active_tab
            if source == 'replay' and active_tab != 1:
                raise ReplayStale('REPLAY_STALE', 'the recording left the initial tab')
            if source == 'model' and active_tab != self._last_observation.get('active_tab', 1):
                raise E2EError('NODE_STALE', 'active tab changed since the last observation')
        if action['op'] in VISUAL_OPS:
            visual = getattr(self, '_last_visual', None)
            if s._ledger.secrets or source != 'model' or not visual or visual['id'] != action['screenshot']:
                raise E2EError('POLICY_DENIED', 'coordinate actions require vision in the current model request and no registered secrets')
        tool = s.tools.get(action.get("name")) if action["op"] == "tool" else None
        counts_action = tool.mutates if tool else True
        event = dict(index=row.actions + 1, source=source, action=s._ledger.redact(action))
        one_time_code = False

        def before_action(is_one_time_code=False):
            nonlocal one_time_code
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
            result = tool.handler(action["arguments"])
        elif action["op"] == "fill_secret":
            secret_name = action["secret"]
            if secret_name not in allowed_secrets or secret_name not in s._ledger.secrets:
                raise E2EError("POLICY_DENIED", "secret was not declared in this act's params")
            fill = getattr(s.engine, "fill_secret", None)
            if not callable(fill):
                raise E2EError("POLICY_DENIED", "engine does not implement authorized secret filling")
            result = fill(action["target"], s._ledger.secrets[secret_name], timeout=deadline.remaining())
        else:
            if "<secret:" in canonical(action):
                raise E2EError("POLICY_DENIED", "secret handles require fill_secret")
            if inspect_input:
                with s.engine._input_target(action['target'], timeout=deadline.remaining()) as (target, otp):
                    before_action(otp)
                    result = s.engine._perform(action, timeout=deadline.remaining(), input_target=target)
            else:
                result = perform(action, timeout=deadline.remaining())
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
        params, slots = self._params(params)
        actions_limit = self._limit(max_actions, s.config.max_actions, "max_actions")
        model_limit = self._limit(max_model_calls, s.config.max_model_calls, "max_model_calls")
        allowed_secrets = set()

        def collect(value):
            if isinstance(value, dict):
                if value.get("kind") == "secret":
                    allowed_secrets.add(value["name"])
                for child in value.values():
                    collect(child)
            elif isinstance(value, list):
                for child in value:
                    collect(child)
        collect(params)
        step_input = dict(instruction=normalize(s._ledger.redact(instruction)), params=template(params, slots))
        if single_action:
            step_input["single_action"] = True
        signature = digest(step_input)
        occurrence = s._occurrences[signature]
        s._occurrences[signature] += 1
        provenance = dict(project=s._identity["project"], test=s._identity["test"], signature=signature,
                          occurrence=occurrence, agent=s.config.name)
        identity = dict(s._identity, context=digest(s._ledger.redact(s.config.context)))
        key = digest({"schema": SCHEMA, **identity, **provenance})
        api = "act_once" if single_action else "act"
        with s._step(api, instruction, timeout if timeout is not None else s.config.timeout) as (row, deadline):
            row.cache.update(mode="off" if s._mode == "off" else "miss", key=key)
            before = s._observe(deadline)
            replayable_tab = before.get('active_tab', 1) == 1
            start_origin = s._current_origin(deadline) if s._mode != "off" and replayable_tab else None
            history, recorded, action_origins = [], [], []
            cacheable = s._mode == "read-write" and replayable_tab
            try:
                entry = None
                if s._mode != "off" and replayable_tab:
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
                if entry is not None:
                    entry = dict(entry)
                    for field in ("start", "end", "appeared", "gone", "actions", "summary"):
                        if field in entry:
                            entry[field] = template(entry[field], slots, expand=True)
                    if start_origin != entry["start_origin"] or route(before["url"]) != entry["start"]:
                        if s._cache_config.strict:
                            raise ReplayStale("REPLAY_STALE", "recorded starting origin or route changed")
                    else:
                        if len(entry["actions"]) > actions_limit:
                            raise E2EError("STEP_BUDGET_EXHAUSTED", "recording exceeds action budget")
                        try:
                            for action, expected_origin in zip(entry["actions"], entry["action_origins"], strict=True):
                                if s._current_origin(deadline, refresh=True, action=action) != expected_origin:
                                    raise ReplayStale("REPLAY_STALE", "origin changed during replay; reset the page before retrying")
                                self._dispatch(action, row, deadline, "replay", allowed_secrets)
                                history.append({"action": action, "source": "replay"})
                            after = self._poll_observation(deadline, lambda view: view.get('active_tab', 1) != 1
                                                           or matches_end(entry, before, view))
                            if after.get('active_tab', 1) != 1:
                                raise ReplayStale('REPLAY_STALE', 'the recording left the initial tab')
                            if s._current_origin(deadline) != entry["end_origin"]:
                                raise ReplayStale("REPLAY_STALE", "recorded ending origin changed; reset the page before retrying")
                            if matches_end(entry, before, after):
                                row.cache["mode"] = "replay"
                                row.explanation = entry.get("summary", "replayed verified actions")
                                s._pending.append(dict(key=key, entry=entry, row=row, replayed=True, confirmed=False))
                                return ActResult(entry.get("summary", "replayed verified actions"), row.actions, 0, row.cache)
                        except E2EError as exc:
                            if isinstance(exc, ReplayStale) or exc.code in {"STEP_TIMEOUT", "CANCELLED", "POLICY_DENIED", "STEP_BUDGET_EXHAUSTED"}:
                                raise
                            s._evict(key)
                            raise ReplayStale("REPLAY_STALE", "replay action failed; reset the page before retrying") from None
                        if single_action or s._cache_config.strict:
                            raise ReplayStale("REPLAY_STALE", "recorded end-state delta did not hold")
                        row.cache.update(mode="handoff", reason="end-mismatch")
                        history.append({"notice": "Recorded actions already ran. End-state check failed. Continue from the current screen; do not repeat side effects blindly."})
                        cacheable = False
                        s._evict(key)
                repeats = Counter()
                refusals = 0
                stale_refusals = 0
                summary = "action completed"
                capture_requested = False
                after_action = None
                while True:
                    answer = self._ask("act", instruction, params, row, deadline, model_limit,
                                       history=history, vision=vision, single_action=single_action,
                                       capture_requested=capture_requested, after_action=after_action)
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
                    if action['op'] == 'screenshot':
                        if vision != 'on_demand' or s._ledger.secrets:
                            raise E2EError('POLICY_DENIED', 'screenshot requests require on_demand vision and no registered secrets')
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
                    marker = digest(dict(action=stable_action, observation={k:v for k,v in self._last_observation.items() if k != 'id'}))
                    repeats[marker] += 1
                    if repeats[marker] > 2:
                        raise E2EError("STEP_NO_CONCLUSION", "same action repeated on an unchanged screen")
                    action_origin = s._current_origin(deadline, action=action) if cacheable else None
                    sent, one_time_code = 0, False
                    try:
                        for item in sequence:
                            result, one_time_code = self._dispatch(item, row, deadline, 'model', allowed_secrets,
                                                                  mask_value=one_time_code)
                            sent += 1
                    except E2EError as exc:
                        if sent or single_action or exc.code != 'NODE_STALE' or action['op'] == 'tool':
                            raise
                        stale_refusals += 1
                        if stale_refusals >= 3:
                            raise
                        cacheable = False
                        history.append(dict(error=dict(code=exc.code, message=str(exc)), dispatched=False))
                        continue
                    stale_refusals = 0
                    if mutates and action['op'] not in {'wait', 'resize_viewport', 'switch_tab', 'close_tab'}:
                        after_action = self._last_observation
                    if one_time_code or not recordable_action(action):
                        cacheable = False
                    elif cacheable:
                        recorder = getattr(s.engine, 'record_action', None)
                        recording = recorder(action) if callable(recorder) else action
                        if recording is None:
                            cacheable = False
                        else:
                            recorded.append(recording)
                            action_origins.append(action_origin)
                    history_action = dict(action, value='<masked>') if one_time_code else action
                    history.append(dict(action=history_action, result=result))
                    if single_action:
                        break
                row.explanation = summary
                if not single_action or cacheable:
                    after = self._observe_after_action(deadline, after_action) if after_action else s._observe(deadline)
                    appeared, gone = delta(before, after)
                    end_origin = s._current_origin(deadline) if cacheable else None
                    if (cacheable and recorded and after.get('active_tab', 1) == 1
                        and (appeared or gone or start_origin != end_origin or route(before["url"]) != route(after["url"]))):
                        trace = dict(schema=SCHEMA, key=key, provenance=provenance,
                                     start=route(before["url"]), end=route(after["url"]),
                                     appeared=appeared, gone=gone, actions=recorded, summary=summary)
                        trace = s._ledger.redact(trace)
                        for field in ("start", "end", "appeared", "gone", "actions", "summary"):
                            trace[field] = template(trace[field], slots)
                        trace.update(start_origin=start_origin, end_origin=end_origin, action_origins=action_origins)
                        s._pending.append(dict(key=key, entry=trace, row=row, replayed=False, confirmed=False))
                    elif s._mode == "read-write" and replayable_tab:
                        s._evict(key)
                return ActResult(summary, row.actions, row.model_calls, row.cache)
            except BaseException as exc:
                if (replayable_tab and not isinstance(exc, (KeyboardInterrupt, SystemExit))
                    and getattr(exc, "code", "") not in {"MODEL_UNAVAILABLE", "MODEL_PROVIDER_FAILED", "CANCELLED"}):
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
