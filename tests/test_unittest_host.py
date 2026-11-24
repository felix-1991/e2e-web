"""Run directly with unittest as well as pytest; no SDK runner involved."""

import unittest
import os

from e2e_web import CacheConfig, Web, WebSession


class StateEngine:
    """A host-supplied adapter with no browser dependency."""

    name = "host-engine"

    def observe(self, *, timeout):
        return {"url": "https://example.test", "tree": '- heading "Ready"'}

    def perform(self, action, *, timeout):
        raise AssertionError("no actions expected")


class UnittestIntegration(unittest.TestCase):
    """unittest owns setup, teardown, assertions and outcome."""

    def setUp(self):
        self.session = WebSession(StateEngine(), name=self.id(), app_identity="host", cache=CacheConfig(mode="off"))
        self.addCleanup(self.session.close)

    def test_framework_manages_session(self):
        observation = self.session.observe()
        self.session.verify(lambda: self.assertIn("Ready", observation["tree"]))


@unittest.skipUnless(os.getenv("E2E_WEB_BROWSER_TESTS") == "1", "enable real browser tests")
class BrowserUnittestIntegration(unittest.TestCase):
    """Prove unittest's setUp/addCleanup can own real browser SDK resources."""

    def setUp(self):
        self.host = Web()
        self.addCleanup(self.host.close)
        self.session = self.host.session(name=self.id(), app_identity="unittest-demo", cache=CacheConfig(mode="off"))
        self.addCleanup(self.session.close)

    def test_real_browser_deterministic_assertion(self):
        self.session.page.set_content('<h1>Framework owned browser</h1>')
        self.session.expect(self.session.page.get_by_role("heading")).to_have_text("Framework owned browser")
        self.assertEqual(self.session.steps[-1].status, "passed")


if __name__ == "__main__":
    unittest.main()
