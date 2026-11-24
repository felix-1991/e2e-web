"""Regression checks for the host/agent boundaries ported from e2e."""

import json

import pytest

from e2e_web import CacheConfig, E2EError, FunctionExecutor, Secret, Tool, WebSession
from e2e_web.browser import navigation_url
from test_contract import Engine, target


@pytest.mark.parametrize("value", ["file:///etc/passwd", "data:text/html,x", "javascript:alert(1)",
                                   "view-source:file:///x", "blob:https://example.test/id", "https://",
                                   "https://example.test:bad", "https://example.test/\npath", "about:blank#x"])
def test_navigation_refuses_unsafe_or_malformed_urls(value):
    with pytest.raises(E2EError, match="POLICY_DENIED"):
        navigation_url(value, "https://example.test")


@pytest.mark.parametrize(("value", "expected"), [("/form", "https://example.test/form"),
                                                  ("https://other.test", "https://other.test"),
                                                  ("about:blank", "about:blank")])
def test_navigation_allows_relative_http_and_blank(value, expected):
    assert navigation_url(value, "https://example.test") == expected


def test_refused_actions_are_repaired_without_side_effects():
    seen, calls = [], []
    replies = iter([
        {"action": {"op": "evaluate", "source": "untrusted"}},
        {"action": {"op": "tool", "name": "seed", "arguments": {"item": 1}}},
        {"action": {"op": "tool", "name": "seed", "arguments": {"item": "ok"}}},
        {"done": True},
    ])

    def complete(request, *, timeout):
        seen.append(json.loads(json.dumps(request)))
        return next(replies)

    tool = Tool("seed", "Seed", {"type": "object", "properties": {"item": {"type": "string"}}},
                lambda args: calls.append(args), mutates=True)
    with WebSession(Engine(), name="repair", app_identity="app", executor=FunctionExecutor(complete),
                    tools=(tool,), cache=CacheConfig(mode="off")) as session:
        result = session.agent.act("Seed once")
        assert result.actions == 1 and result.model_calls == 4
    assert calls == [{"item": "ok"}]
    assert [event["dispatched"] for event in seen[2]["history"]] == [False, False]


def test_three_refusals_stop_and_conflicting_envelopes_never_dispatch():
    engine = Engine()
    executor = FunctionExecutor(lambda request, **kw: {"done": True, "action": {"op": "click", "target": target()}})
    with WebSession(engine, name="invalid", app_identity="app", executor=executor,
                    cache=CacheConfig(mode="off")) as session:
        with pytest.raises(E2EError, match="MODEL_OUTPUT_INVALID"):
            session.agent.act("Ambiguous response")
        assert session.steps[-1].model_calls == 3
        assert not engine.actions


def test_prior_handoff_is_redacted_and_judgment_is_independent():
    seen = []
    secret = Secret("token", "private-handoff-token")

    def complete(request, *, timeout):
        seen.append(request)
        if request["kind"] == "extract":
            return {"value": "observed"}
        return {"done": True, "summary": "created " + secret.value} if request["kind"] == "act" else {"passed": True}

    with WebSession(Engine(), name="context", app_identity="app", executor=FunctionExecutor(complete),
                    secrets=(secret,), cache=CacheConfig(mode="off")) as session:
        session.agent.act("First")
        session.agent.act("Continue")
        session.agent.assert_("Check independently")
        assert session.agent.extract("Read", schema={"type": "string"}) == "observed"
    assert seen[1]["prior_steps"][0]["explanation"] == "created <secret:token>"
    assert "prior_steps" not in seen[2] and "history" not in seen[2]
    assert "prior_steps" not in seen[3] and "history" not in seen[3]
    assert secret.value not in json.dumps(seen)


def test_secret_engine_without_authorization_fails_closed():
    engine = Engine()
    engine.fill_secret = None
    executor = FunctionExecutor(lambda request, **kw: {"action": {"op": "fill_secret", "target": target(), "secret": "token"}})
    with WebSession(engine, name="secret", app_identity="app", executor=executor,
                    cache=CacheConfig(mode="off")) as session:
        with pytest.raises(E2EError, match="authorized secret"):
            session.agent.act("Fill", {"token": Secret("token", "private-token-2468")})
        assert not engine.actions
