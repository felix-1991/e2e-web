"""SDK contracts, exercised through the public session API."""

import json
import threading

import pytest

from e2e_web import (AgentConfig, AssertionFailed, CacheConfig, Cancellation, E2EError,
                     FunctionExecutor, ReplayStale, Secret, Tool, Unique, WebSession)
from e2e_web.actions import validate_action


class Engine:
    """A generic form surface for deterministic protocol tests."""

    name = "fake-web-v1"

    def __init__(self):
        self.value, self.result, self.url = "", "", "https://app.example/form"
        self.actions = []

    def observe(self, *, timeout):
        return {"url": self.url, "tree": f'- textbox "Input": {self.value}\n- button "Apply"\n- status: {self.result}'}

    def perform(self, action, *, timeout):
        self.actions.append(action)
        if action["op"] == "fill":
            self.value = action["value"]
        elif action["op"] == "click":
            self.result = "Applied " + self.value
        elif action["op"] == "navigate":
            self.url = action["url"]

    def fill_secret(self, descriptor, secret, *, timeout):
        self.perform(dict(op="fill", target=descriptor, value=secret.value), timeout=timeout)


def target(role="textbox", name="Input"):
    return dict(by="role", value=role, name=name)


def brain(request, *, timeout):
    if request["kind"] == "act":
        actions = request["params"].get("actions") or [
            dict(op="fill", target=target(), value=request["params"]["value"]),
            dict(op="click", target=target("button", "Apply")),
        ]
        index = len(request["history"])
        return {"action": actions[index]} if index < len(actions) else {"done": True, "summary": "Applied input"}
    if request["kind"] == "assert":
        return {"passed": request["params"].get("expected", "Applied") in request["observation"]["tree"], "reason": "current status"}
    return {"value": {"total": 3}}


@pytest.fixture
def make_session(tmp_path):
    sessions = []

    def create(**kwargs):
        options = dict(name="test", app_identity="app", executor=FunctionExecutor(brain),
                       cache=CacheConfig(directory=tmp_path / "cache", mode="read-write"))
        options.update(kwargs)
        session = WebSession(Engine(), **options)
        sessions.append(session)
        return session
    yield create
    for session in sessions:
        session.close()


def record(make_session):
    with make_session() as session:
        session.agent.act("Apply input", {"value": "first"})
        session.agent.assert_("Input was applied")


def test_verified_replay_and_live_judgment(make_session):
    record(make_session)
    with make_session() as session:
        result = session.agent.act("Apply input", {"value": "first"})
        assert result.cache["mode"] == "replay" and result.model_calls == 0 and result.actions == 2
        session.agent.assert_("Input was applied")
        assert session.steps[-1].model_calls == 1
    assert result.cache["write"] == "kept"


@pytest.mark.parametrize("starts_with_navigation", [False, True])
def test_strict_replay_rejects_a_different_start_origin(make_session, tmp_path, starts_with_navigation):
    params = {"value": "first"}
    if starts_with_navigation:
        params = {"actions": [{"op": "navigate", "url": "https://app.example/next"}]}
    with make_session() as first:
        first.agent.act("Apply input", params)
        first.verify(lambda: first.engine.actions)
    strict = CacheConfig(directory=tmp_path / "cache", strict=True)
    with make_session(cache=strict) as other:
        other.engine.url = "https://other.example/form"
        with pytest.raises(ReplayStale, match="starting origin"):
            other.agent.act("Apply input", params)
        assert not other.engine.actions and other.steps[-1].model_calls == 0


@pytest.mark.parametrize("follow_up", [False, True])
def test_replay_stops_when_navigation_lands_at_a_different_origin(make_session, follow_up):
    actions = [{"op": "navigate", "url": "https://app.example/next"}]
    if follow_up:
        actions.append(dict(op="click", target=target("button", "Apply")))
    with make_session() as first:
        first.agent.act("Navigate", {"actions": actions})
        first.verify(lambda: first.engine.url.endswith("/next"))
    with make_session() as second:
        perform = second.engine.perform

        def redirect(action, *, timeout):
            perform(action, timeout=timeout)
            if action["op"] == "navigate":
                second.engine.url = "https://other.example/next"

        second.engine.perform = redirect
        with pytest.raises(ReplayStale, match="origin changed"):
            second.agent.act("Navigate", {"actions": actions})
        assert second.engine.actions == actions[:1] and second.steps[-1].model_calls == 0


def test_legacy_recording_detected_in_strict_mode_and_refreshed_locally(make_session, tmp_path):
    record(make_session)
    path = next((tmp_path / "cache").glob("*.json"))
    legacy = json.loads(path.read_text())
    path.unlink()
    for field in ("start_origin", "end_origin", "action_origins"):
        del legacy[field]
    legacy["key"] = "a" * 64
    (path.parent / (legacy["key"] + ".json")).write_text(json.dumps(legacy))

    with make_session(cache=CacheConfig(directory=path.parent, strict=True)) as session:
        with pytest.raises(ReplayStale, match="re-record"):
            session.agent.act("Apply input", {"value": "first"})
        assert session.engine.actions == []
    record(make_session)
    with make_session(cache=CacheConfig(directory=path.parent, strict=True)) as session:
        assert session.agent.act("Apply input", {"value": "first"}).cache["mode"] == "replay"


def test_replayed_handoff_reaches_next_act(make_session):
    record(make_session)
    seen = []

    def executor(request, *, timeout):
        seen.append(request)
        return {"done": True}

    with make_session(executor=FunctionExecutor(executor)) as session:
        session.agent.act("Apply input", {"value": "first"})
        session.agent.act("Continue from applied result")
    assert len(seen) == 1
    prior = seen[0]["prior_steps"][0]
    assert prior["replayed"] is True and prior["explanation"] == "Applied input"


def test_no_assertion_no_cache(make_session, tmp_path):
    with make_session() as session:
        session.agent.act("Apply input", {"value": "first"})
        assert session.agent.extract("Read count", schema={"type": "object"}) == {"total": 3}
    assert not list((tmp_path / "cache").glob("*.json"))


def test_failed_assertion_and_prior_verification(make_session, tmp_path):
    with pytest.raises(AssertionFailed):
        with make_session() as session:
            session.agent.act("Apply input", {"value": "first"})
            session.verify(lambda: "Applied" in session.engine.result)
            session.agent.assert_("Wrong expected", {"expected": "Missing"})
    assert len(list((tmp_path / "cache").glob("*.json"))) == 1


def test_unique_reuses_recording_with_fresh_value(make_session):
    with make_session() as first:
        first.agent.act("Apply unique", {"value": Unique("run-one")})
        first.agent.assert_("Input was applied")
    with make_session() as second:
        result = second.agent.act("Apply unique", {"value": Unique("run-two")})
        assert result.cache["mode"] == "replay"
        assert second.engine.result == "Applied run-two"
        second.agent.assert_("Input was applied")


def test_secret_handles_never_reach_requests_or_files(make_session, tmp_path):
    seen = []
    secret = Secret("password", "private-needle-2468")

    def executor(req, *, timeout):
        seen.append(req)
        if req["kind"] == "act":
            if not req["history"]:
                return {"action": dict(op="fill_secret", target=target(), secret="password")}
            return {"done": True}
        return {"passed": True}

    with make_session(executor=FunctionExecutor(executor), artifacts_dir=tmp_path / "evidence") as session:
        session.agent.act("Fill credential", {"password": secret})
        session.agent.assert_("Field filled")
        assert session.engine.value == secret.value
        with pytest.raises(E2EError, match="POLICY_DENIED"):
            session.screenshot(tmp_path / "no.png")
    assert secret.value not in json.dumps(seen)
    for path in tmp_path.rglob("*.json"):
        assert secret.value not in path.read_text()


def test_undeclared_secret_is_denied(make_session):
    executor = FunctionExecutor(lambda req, **kw: {"action": dict(op="fill_secret", target=target(), secret="not-declared")})
    with make_session(executor=executor) as session:
        with pytest.raises(E2EError, match="POLICY_DENIED"):
            session.agent.act("Fill")
        assert not session.engine.actions


def test_strict_corrupt_and_rekeyed(make_session, tmp_path):
    record(make_session)
    strict = CacheConfig(directory=tmp_path / "cache", strict=True)
    with make_session(cache=strict, agent=AgentConfig(context="changed")) as session:
        with pytest.raises(ReplayStale, match="different context"):
            session.agent.act("Apply input", {"value": "first"})
    path = next((tmp_path / "cache").glob("*.json"))
    path.write_text("broken")
    with make_session(cache=strict) as session:
        with pytest.raises(ReplayStale, match="invalid"):
            session.agent.act("Apply input", {"value": "first"})


def test_read_only_and_ci_defaults(make_session, monkeypatch, tmp_path):
    monkeypatch.setenv("CI", "true")
    with make_session(cache=CacheConfig(directory=tmp_path / "cache")) as session:
        result = session.agent.act("New step", {"value": "first"})
        session.agent.assert_("Applied")
        assert result.cache["mode"] == "miss"
    assert not list((tmp_path / "cache").glob("*.json"))


def test_deadline_cancellation_and_budgets(make_session):
    cancellation = Cancellation()
    cancellation.cancel()
    with make_session(cancellation=cancellation) as session:
        with pytest.raises(E2EError, match="CANCELLED"):
            session.agent.act("No work", {"value": "first"})
    with make_session() as session:
        with pytest.raises(E2EError, match="STEP_BUDGET_EXHAUSTED"):
            session.agent.act("Budget", {"value": "first"}, max_actions=1)
        with pytest.raises(E2EError, match="INVALID_ARGUMENT"):
            session.agent.act("Cannot raise ceiling", max_model_calls=10000)


def test_act_once_stops_after_dispatch_and_repairs_a_premature_done(make_session):
    action = dict(op="fill", target=target(), value="first")
    calls = []

    def decide(request, *, timeout):
        calls.append(request)
        return {"done": True} if len(calls) == 1 else {"action": action}

    with make_session(executor=FunctionExecutor(decide), cache=CacheConfig(mode="off")) as session:
        result = session.agent.act_once("Fill the input")
        assert (result.actions, result.model_calls) == (1, 2)
        assert session.engine.actions == [action]
        assert session.steps[-1].api == "act_once"
        assert all(call["single_action"] for call in calls)
        assert calls[1]["history"][0]["dispatched"] is False


def test_act_once_replays_its_own_recording_without_repeating_side_effects(make_session):
    action = dict(op="fill", target=target(), value="first")
    params = {"actions": [action]}
    with make_session() as session:
        ordinary = session.agent.act("Fill the input", params)
        session.verify(lambda: session.engine.value == "first")
    with make_session() as session:
        single = session.agent.act_once("Fill the input", params)
        session.verify(lambda: session.engine.value == "first")
        assert (single.actions, single.model_calls) == (1, 1)
        assert single.cache["key"] != ordinary.cache["key"]
    with make_session() as session:
        replayed = session.agent.act_once("Fill the input", params)
        assert (replayed.actions, replayed.model_calls, replayed.cache["mode"]) == (1, 0, "replay")
        session.verify(lambda: session.engine.value == "first")
    with make_session() as session:
        perform = session.engine.perform
        session.engine.perform = lambda act, *, timeout: perform({**act, "value": "changed"}, timeout=timeout)
        with pytest.raises(ReplayStale, match="end-state"):
            session.agent.act_once("Fill the input", params)
        assert len(session.engine.actions) == 1
        assert session.steps[-1].model_calls == 0


def test_judgment_and_extract_repair_once(make_session):
    replies = iter([{"passed": "yes"}, {"passed": True}, {"value": {"total": "bad"}}, {"value": {"total": 3}}])
    with make_session(executor=FunctionExecutor(lambda req, **kw: next(replies))) as session:
        session.agent.assert_("ready")
        assert session.steps[-1].model_calls == 2
        value = session.agent.extract("count", schema={"type": "object", "properties": {"total": {"type": "integer"}}, "required": ["total"]})
        assert value == {"total": 3}
        assert session.steps[-1].model_calls == 2


def test_agent_methods_inherit_session_vision_and_allow_an_explicit_override(make_session, monkeypatch):
    requests = []

    def decide(request, *, timeout):
        requests.append(request)
        return {"act": {"done": True}, "assert": {"passed": True}, "extract": {"value": 3}}[request["kind"]]

    monkeypatch.setattr(Engine, "screenshot", lambda self, *, timeout: b"image", raising=False)
    monkeypatch.setenv("E2E_VISION", "true")
    with make_session(executor=FunctionExecutor(decide), cache=CacheConfig(mode="off")) as session:
        monkeypatch.setenv("E2E_VISION", "false")
        session.agent.act("Complete")
        session.agent.assert_("Ready")
        session.agent.wait_for("Loaded")
        assert session.agent.extract("Count", schema={"type": "integer"}) == 3
        session.agent.act("Complete in text mode", vision=False)
    assert [request.get("screenshot") for request in requests] == ["aW1hZ2U="] * 4 + [None]

    monkeypatch.setenv("E2E_VISION", "true")
    with make_session(executor=FunctionExecutor(decide), agent=AgentConfig(vision=False)) as session:
        session.agent.assert_("Ready")
        session.agent.assert_("Ready with vision", vision=True)
    assert [request.get("screenshot") for request in requests[-2:]] == [None, "aW1hZ2U="]


def test_environment_vision_respects_secret_policy(make_session, monkeypatch):
    monkeypatch.setenv("E2E_VISION", "true")
    with make_session(secrets=(Secret("password", "test-password"),)) as session:
        with pytest.raises(E2EError, match="vision disabled while secrets are registered"):
            session.agent.assert_("Ready")


def test_wait_for_polls_with_one_model_budget(make_session):
    replies = iter([{"passed": False}, {"passed": True}])
    with make_session(executor=FunctionExecutor(lambda req, **kw: next(replies))) as session:
        session.agent.wait_for("ready", interval=0.001, timeout=1)
        assert session.steps[-1].model_calls == 2


def test_custom_tool_schema_and_gap(make_session, tmp_path):
    called = []
    tool = Tool("seed", "Seed data", {"type": "object", "required": ["item"]}, lambda args: called.append(args), mutates=True)
    with make_session(tools=(tool,)) as session:
        result = session.agent.act("Seed", {"actions": [dict(op="tool", name="seed", arguments={"item": "example"})]})
        session.verify(lambda: len(called) == 1)
        assert result.actions == 1
    assert not list((tmp_path / "cache").glob("*.json"))


def test_hooks_and_reentrancy(make_session):
    events = []
    config = AgentConfig(on_before_action=lambda event: events.append(("before", event["source"])),
                         on_action=lambda event: events.append(("after", event["source"])))
    with make_session(agent=config) as session:
        session.agent.act("Apply", {"value": "first"})
        assert events == [("before", "model"), ("after", "model")] * 2
        with pytest.raises(E2EError, match="CONCURRENT_OPERATION"):
            session.verify(lambda: session.observe())


def test_close_idempotent_and_thread_affinity(make_session):
    session = make_session()
    errors = []

    def other_thread():
        try:
            session.observe()
        except E2EError as exc:
            errors.append(exc.code)
    thread = threading.Thread(target=other_thread)
    thread.start()
    thread.join()
    assert errors == ["CONCURRENT_OPERATION"]
    session.close()
    session.close()
    with pytest.raises(E2EError, match="SESSION_CLOSED"):
        session.observe()


@pytest.mark.parametrize("action", [
    {"op": []},
    {"op": "click", "target": {"by": []}},
    {"op": "evaluate", "source": "arbitrary JS"},
    {"op": "right_click", "target": target(), "modifiers": ["Shift", "Shift"]},
    {"op": "click", "target": target(), "modifiers": ["Enter"]},
    {"op": "hover_at", "x": 12, "y": 4},
    {"op": "resize_viewport", "width": 199, "height": 800},
    {"op": "resize_viewport", "width": 800, "height": 4097},
    {"op": "resize_viewport", "width": 800, "height": 360.0},
    {"op": "scroll_find", "target": target(), "direction": "right", "steps": 3, "container": {}},
    {"op": "click", "target": {"by": "semantic", "value": "button", "name": "Edit", "within": {"role": "row"}}},
    {"op": "select", "target": target(), "value": [{"index": True}]},
    {"op": "select", "target": target(), "value": {"label": "North", "value": "N"}},
    {"op": "select", "target": target(), "value": [["North"]]},
    {"op": "type", "value": "test", "replace": "true"},
    {"op": "type", "target": target(), "value": "test", "replace": True},
    {"op": "press", "value": "Tab", "target": None},
])
def test_malformed_actions_fail_before_execution(action):
    with pytest.raises(E2EError, match="MODEL_OUTPUT_INVALID"):
        validate_action(action)
