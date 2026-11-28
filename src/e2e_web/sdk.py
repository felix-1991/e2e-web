"""Browser lifetime helpers; no discovery, scheduler or test runner."""

from collections.abc import Callable
from contextlib import AbstractContextManager, ExitStack
from pathlib import Path
import re
import sys
import threading
from typing import Any

from .browser import BrowserEngine
from .config import _BrowserSettings
from .errors import E2EError
from .session import WebSession
from .semantics import init_script
from .types import ObservationConfig


class Web:
    """A browser host reusable across isolated sessions in any test framework."""

    def __init__(self, *, headless: bool | None = None, executable_path: str | None = None,
                 browser_type: str = "chromium", cdp_url: str | None = None,
                 cdp_headers: dict[str, str] | None = None,
                 test_id_attribute: str = "data-testid",
                 launch_options: dict[str, Any] | None = None,
                 provider: Callable[[], AbstractContextManager[str]] | None = None):
        if not isinstance(test_id_attribute, str) or not re.fullmatch(r"[A-Za-z_:][A-Za-z0-9_.:-]*", test_id_attribute):
            raise E2EError("INVALID_ARGUMENT", "test_id_attribute requires an attribute name")
        self._test_id_attribute = test_id_attribute
        options = dict(launch_options or {})
        if provider is not None and (not callable(provider) or cdp_url is not None or browser_type != "chromium"):
            raise E2EError("INVALID_ARGUMENT", "provider requires Chromium and an exclusive CDP lease factory")
        if cdp_headers is not None and not (cdp_url or provider is not None):
            raise E2EError("INVALID_ARGUMENT", "cdp_headers requires a CDP endpoint or provider")
        self._cdp_headers = dict(cdp_headers or {})
        self._provider = provider
        self._lease = ExitStack()
        if not cdp_url and provider is None:
            settings = _BrowserSettings.resolve(
                headless=headless if headless is not None else options.get("headless"),
            )
            options["headless"] = settings.headless
            if browser_type == "chromium":
                options.setdefault("chromium_sandbox", sys.platform != "linux")
            if executable_path:
                options["executable_path"] = executable_path
            elif executable_path is None and browser_type == "chromium" and settings.executable_path:
                options.setdefault("executable_path", settings.executable_path)
        self._options = dict(browser_type=browser_type, cdp_url=cdp_url, launch_options=options)
        self._playwright = self._browser = None
        self._sessions: list[WebSession] = []
        self._owner = threading.get_ident()
        self._closed = False

    def __enter__(self):
        self._start()
        return self

    def __exit__(self, exc_type, exc, traceback):
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            for session in self._sessions:
                session._interrupted = True
        try:
            self.close()
        except Exception:
            if exc is None:
                raise
        return False

    def _start(self):
        """Lazily acquire a browser, keeping Playwright on the calling thread."""
        if self._closed:
            raise E2EError("SESSION_CLOSED", "browser host is closed")
        if threading.get_ident() != self._owner:
            raise E2EError("CONCURRENT_OPERATION", "browser host belongs to another thread")
        if self._browser is not None:
            return
        from playwright.sync_api import sync_playwright
        self._playwright = sync_playwright().start()
        try:
            self._playwright.selectors.register("e2e_private", script="""(() => {
                const all = root => globalThis[Symbol.for('e2e.web.semantic.v1')]?.secureFields(root)
                    ?? [root.documentElement || root];
                return {queryAll: all, query: root => all(root)[0]};
            })()""")
            if self._test_id_attribute != "data-testid":
                self._playwright.selectors.set_test_id_attribute(self._test_id_attribute)
            browser_type = self._options["browser_type"]
            if browser_type not in {"chromium", "firefox", "webkit"}:
                raise E2EError("INVALID_ARGUMENT", "browser_type must be chromium, firefox or webkit")
            driver = getattr(self._playwright, browser_type)
            endpoint = self._options["cdp_url"]
            if self._provider is not None:
                endpoint = self._lease.enter_context(self._provider())
                if not isinstance(endpoint, str) or not endpoint.strip():
                    raise E2EError("INVALID_ARGUMENT", "provider lease requires a CDP endpoint")
            if endpoint:
                if browser_type != "chromium":
                    raise E2EError("INVALID_ARGUMENT", "CDP requires chromium")
                try:
                    connect_options = {"headers": self._cdp_headers} if self._cdp_headers else {}
                    self._browser = driver.connect_over_cdp(endpoint, **connect_options)
                except Exception:
                    # A gateway error can echo its access token or signed endpoint.
                    raise E2EError("BROWSER_CONNECTION_FAILED", "Chromium CDP connection failed") from None
            else:
                self._browser = driver.launch(**self._options["launch_options"])
        except BaseException:
            try:
                self._playwright.stop()
            finally:
                self._playwright = None
                self._lease.close()
            raise

    def session(self, *, name: str, app_identity: str, base_url: str | None = None,
                context_options: dict[str, Any] | None = None,
                init_scripts: tuple[str | Path, ...] = (),
                observation: ObservationConfig | None = None,
                upload_roots: tuple[str | Path, ...] | None = None, **options) -> WebSession:
        """Create one owned browser context, with optional storage_state/video settings."""
        self._start()
        context = self._browser.new_context(**(context_options or {}))
        try:
            # A single script guarantees tracking runs before caller bootstrap code.
            scripts = [init_script(self._test_id_attribute)]
            for script in init_scripts:
                if isinstance(script, Path):
                    scripts.append(script.read_text(encoding='utf-8'))
                elif isinstance(script, str):
                    scripts.append(script)
                else:
                    raise E2EError("INVALID_ARGUMENT", "init_scripts requires JavaScript strings or Path objects")
            context.add_init_script(script='\n;\n'.join(scripts))
            page = context.new_page()
            engine = BrowserEngine(page, base_url=base_url, observation=observation,
                                   upload_roots=(tuple(Path(p) for p in upload_roots) if upload_roots is not None else None), own_initial=True,
                                   test_id_attribute=self._test_id_attribute)
            engine.masked_vision = True

            def release_context():
                """Release the context and stop retaining completed test sessions."""
                try:
                    context.close()
                finally:
                    if session in self._sessions:
                        self._sessions.remove(session)

            session = WebSession(engine, name=name, app_identity=app_identity, cleanup=release_context, **options)
            self._sessions.append(session)
            return session
        except BaseException:
            context.close()
            raise

    @staticmethod
    def attach(page: Any, *, name: str, app_identity: str, base_url: str | None = None,
               observation: ObservationConfig | None = None,
               upload_roots: tuple[str | Path, ...] | None = None, **options) -> WebSession:
        """Borrow a synchronous Page; session.close never closes caller resources."""
        if not callable(getattr(page, "get_by_role", None)):
            raise E2EError("INVALID_ARGUMENT", "attach requires a synchronous Playwright Page")
        import inspect
        if inspect.iscoroutinefunction(getattr(page, "goto", None)):
            raise E2EError("INVALID_ARGUMENT", "native async Page is not supported by the synchronous SDK")
        engine = BrowserEngine(page, base_url=base_url, observation=observation,
                               upload_roots=(tuple(Path(p) for p in upload_roots) if upload_roots is not None else None))
        try:
            return WebSession(engine, name=name, app_identity=app_identity, **options)
        except BaseException:
            engine.detach_events()
            raise

    def close(self):
        """Close owned sessions and browser, releasing every resource even on errors."""
        if self._closed:
            return
        if threading.get_ident() != self._owner:
            raise E2EError("CONCURRENT_OPERATION", "close host on its creating thread")
        if any(s._lock.locked() for s in self._sessions):
            raise E2EError("CONCURRENT_OPERATION", "finish active session operations before closing host")
        self._closed = True
        errors = []
        try:
            for session in tuple(self._sessions):
                try:
                    session.close()
                except Exception:
                    errors.append("session cleanup failed")
        finally:
            try:
                if self._browser:
                    self._browser.close()
            except Exception:
                errors.append("browser cleanup failed")
            finally:
                try:
                    if self._playwright:
                        self._playwright.stop()
                finally:
                    self._lease.close()
        if errors:
            raise E2EError("CLEANUP_FAILED", "; ".join(errors))
