"""Run a local demo or a custom web task through pytest."""

import sys

import pytest

from e2e_web import AgentConfig, CacheConfig, E2EError, Web

if __package__:
    from . import _example_support as support
else:
    import _example_support as support


@pytest.fixture(scope="session")
def example_settings():
    try:
        task = support.resolve_task()
        model = support.create_model(support.model_provider("compatible"))
    except (ValueError, E2EError) as exc:
        pytest.fail(str(exc), pytrace=False)
    return task, model


@pytest.fixture(scope="session")
def web_host():
    """Reuse a browser, not application state, across tests."""
    with Web() as host:
        yield host


@pytest.fixture
def web_session(example_settings, web_host, request):
    """Open one isolated context per test."""
    task, model = example_settings
    with web_host.session(
        name=request.node.nodeid, app_identity=task.url or "example-profile", executor=model,
        agent=AgentConfig(timeout=120, judgment_timeout=60), cache=CacheConfig(mode="off"),
    ) as session:
        yield session


def test_web_task(web_session, example_settings):
    task, _ = example_settings
    task.open(web_session.page)
    web_session.agent.act(task.instruction)
    web_session.agent.assert_(task.expected)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, *sys.argv[1:]]))
