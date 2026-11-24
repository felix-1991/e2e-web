"""Run a local demo or a custom web task through unittest."""

import unittest

from e2e_web import AgentConfig, CacheConfig, E2EError, Web

if __package__:
    from . import _example_support as support
else:
    import _example_support as support


class ExternalTask(unittest.TestCase):
    """Existing unittest suites can adopt the SDK one test at a time."""

    def setUp(self):
        try:
            self.task = support.resolve_task()
            model = support.create_model(support.model_provider("compatible"))
        except (ValueError, E2EError) as exc:
            self.fail(str(exc))
        self.host = Web()
        self.addCleanup(self.host.close)
        self.session = self.host.session(
            name=self.id(), app_identity=self.task.url or "example-profile", executor=model,
            agent=AgentConfig(timeout=120, judgment_timeout=60), cache=CacheConfig(mode="off"),
        )
        self.addCleanup(self.session.close)

    def test_web_task(self):
        self.task.open(self.session.page)
        self.session.agent.act(self.task.instruction)
        self.session.agent.assert_(self.task.expected)


if __name__ == "__main__":
    unittest.main()
