"""Environment loading without a browser or model connection."""

import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock

import pytest

from e2e_web import ChatCompletionsExecutor, DeepSeekExecutor, E2EError, Web, load_env


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    monkeypatch.setattr(os, "environ", os.environ.copy())


def test_default_file_and_environment_precedence(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("E2E_CONFIG_EXISTING", "shell")
    monkeypatch.delenv("E2E_CONFIG_NEW", raising=False)
    (tmp_path / ".env").write_text(
        "E2E_CONFIG_EXISTING=file\nE2E_CONFIG_NEW=loaded\n", encoding="utf-8"
    )
    assert load_env() is True
    assert os.environ["E2E_CONFIG_EXISTING"] == "shell"
    assert os.environ["E2E_CONFIG_NEW"] == "loaded"
    assert load_env(override=True) is True
    assert os.environ["E2E_CONFIG_EXISTING"] == "file"


def test_missing_file_does_not_search_parent(tmp_path, monkeypatch):
    monkeypatch.delenv("E2E_CONFIG_PARENT", raising=False)
    (tmp_path / ".env").write_text("E2E_CONFIG_PARENT=unexpected\n", encoding="utf-8")
    child = tmp_path / "child"
    child.mkdir()
    monkeypatch.chdir(child)
    assert load_env() is False
    assert "E2E_CONFIG_PARENT" not in os.environ
    assert load_env("missing.env") is False


def test_existing_empty_environment_value_is_preserved(tmp_path, monkeypatch):
    monkeypatch.setenv("E2E_CONFIG_EMPTY", "")
    path = tmp_path / ".env"
    path.write_text("E2E_CONFIG_EMPTY=from-file\n", encoding="utf-8")
    load_env(str(path))
    assert os.environ["E2E_CONFIG_EMPTY"] == ""


def test_import_automatically_loads_sdk_configuration(tmp_path):
    values = {
        "E2E_MODEL_ENDPOINT": "https://gateway.test/chat/completions",
        "E2E_MODEL": "file-model", "E2E_MODEL_API_KEY": "file-key",
        "DEEPSEEK_MODEL": "deepseek-test", "DEEPSEEK_API_KEY": "deepseek-key",
        "DEEPSEEK_BASE_URL": "https://deepseek.test/v1",
        "DEEPSEEK_SUPPORTS_VISION": "true",
        "E2E_HEADLESS": "false",
        "E2E_CHROMIUM_EXECUTABLE": "/configured/chrome",
        "E2E_OBSERVATION_MAX_NODES": "1500",
        "E2E_OBSERVATION_MAX_BYTES": "1048576",
    }
    (tmp_path / ".env").write_text(
        "\n".join(f"{key}={value}" for key, value in values.items()), encoding="utf-8"
    )
    env = dict(os.environ)
    for key in values:
        env.pop(key, None)
    env["E2E_HEADLESS"] = "true"
    env.pop("PYTHON_DOTENV_DISABLED", None)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    subprocess.run(
        [sys.executable, "-c", """
import os
from unittest.mock import Mock, patch
from e2e_web import ChatCompletionsExecutor, DeepSeekExecutor, ObservationConfig, Web
assert os.environ['E2E_MODEL'] == 'file-model'
generic = ChatCompletionsExecutor()
assert (generic.endpoint, generic.model, generic.api_key) == (
    'https://gateway.test/chat/completions', 'file-model', 'file-key')
deepseek = DeepSeekExecutor()
assert (deepseek.endpoint, deepseek.model, deepseek.api_key) == (
    'https://deepseek.test/v1/chat/completions', 'deepseek-test', 'deepseek-key')
assert deepseek.supports_vision is True
playwright = Mock()
page = playwright.chromium.launch.return_value.new_context.return_value.new_page.return_value
page.context.pages = [page]
with patch('playwright.sync_api.sync_playwright', return_value=Mock(start=Mock(return_value=playwright))):
    with Web() as web:
        with web.session(name='env-limits', app_identity='test') as session:
            assert session.engine.observation == ObservationConfig(max_nodes=1500, max_bytes=1048576)
        with Web.attach(page, name='explicit-limits', app_identity='test',
                        observation=ObservationConfig(max_nodes=80)) as session:
            assert session.engine.observation == ObservationConfig(max_nodes=80)
playwright.chromium.launch.assert_called_once_with(headless=True, executable_path='/configured/chrome')
"""],
        cwd=tmp_path, env=env, check=True, capture_output=True,
    )


def test_explicit_model_arguments_override_environment(monkeypatch):
    monkeypatch.setenv("E2E_MODEL_ENDPOINT", "https://gateway.test/chat/completions")
    monkeypatch.setenv("E2E_MODEL", "env-model")
    monkeypatch.setenv("E2E_MODEL_API_KEY", "shell-key")
    monkeypatch.setenv("DEEPSEEK_MODEL", "env-model")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "env-key")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://env.test")
    monkeypatch.setenv("DEEPSEEK_SUPPORTS_VISION", "true")
    explicit = ChatCompletionsExecutor(model="explicit-model", api_key="")
    assert explicit.model == "explicit-model" and explicit.api_key == ""
    explicit = DeepSeekExecutor(model="explicit-model", api_key="explicit-key",
                                base_url="https://explicit.test", supports_vision=False)
    assert explicit.model == "explicit-model" and explicit.api_key == "explicit-key"
    assert explicit.endpoint == "https://explicit.test/chat/completions"
    assert explicit.supports_vision is False


def test_missing_model_configuration_fails_early(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for key in ("E2E_MODEL_ENDPOINT", "E2E_MODEL", "E2E_MODEL_API_KEY",
                "DEEPSEEK_MODEL", "DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_SUPPORTS_VISION"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(E2EError, match="E2E_MODEL_ENDPOINT"):
        ChatCompletionsExecutor()
    with pytest.raises(E2EError, match="DEEPSEEK_API_KEY"):
        DeepSeekExecutor()
    executor = DeepSeekExecutor(model="test", api_key="fake")
    assert executor.endpoint == "https://api.deepseek.com/chat/completions"
    assert executor.supports_vision is False


@pytest.fixture
def playwright_driver(monkeypatch):
    playwright = Mock()
    monkeypatch.setattr("playwright.sync_api.sync_playwright", lambda: Mock(start=lambda: playwright))
    return playwright


def test_browser_defaults_and_overrides(monkeypatch, playwright_driver):
    monkeypatch.setenv("E2E_HEADLESS", " ")
    monkeypatch.delenv("E2E_CHROMIUM_EXECUTABLE", raising=False)
    with Web():
        playwright_driver.chromium.launch.assert_called_once_with(headless=True)

    monkeypatch.setenv("E2E_HEADLESS", " OFF ")
    monkeypatch.setenv("E2E_CHROMIUM_EXECUTABLE", "/env/chrome")
    with Web():
        playwright_driver.chromium.launch.assert_called_with(headless=False, executable_path="/env/chrome")
    with Web(headless=True, executable_path="/explicit/chrome", launch_options={"headless": False}):
        playwright_driver.chromium.launch.assert_called_with(headless=True, executable_path="/explicit/chrome")
    with Web(launch_options={"executable_path": "/launch/chrome", "headless": True}):
        playwright_driver.chromium.launch.assert_called_with(headless=True, executable_path="/launch/chrome")
    with Web(browser_type="firefox"):
        playwright_driver.firefox.launch.assert_called_once_with(headless=False)


def test_invalid_browser_setting_names_the_variable(monkeypatch, playwright_driver):
    monkeypatch.setenv("E2E_HEADLESS", "invalid-value")
    with pytest.raises(E2EError, match="E2E_HEADLESS"):
        Web()
    with Web(headless=False):
        assert playwright_driver.chromium.launch.call_args.kwargs["headless"] is False
    with Web(cdp_url="http://browser.test"):
        playwright_driver.chromium.connect_over_cdp.assert_called_once_with("http://browser.test")
