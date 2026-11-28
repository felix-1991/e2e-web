"""Check the README's published metadata and execute its two getting-started examples."""

from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import stat
from threading import Thread
import tomllib
from urllib.parse import parse_qs, urlsplit

import pytest

from e2e_web import Web


ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
browser_required = pytest.mark.skipif(
    os.getenv("E2E_WEB_BROWSER_TESTS") != "1", reason="set E2E_WEB_BROWSER_TESTS=1",
)
SEARCH_PAGE = """
<!doctype html>
<meta charset="utf-8">
<form action="/results">
  <label>Search <input name="q" type="search"></label>
  <button>Search</button>
</form>
"""
RESULTS_PAGE = """
<!doctype html>
<meta charset="utf-8">
<section aria-label="Search results">
  <a href="https://docs.example.test/graphs">分布式系统入门</a>
</section>
"""
RESULTS = [{"title": "分布式系统入门", "url": "https://docs.example.test/graphs"}]


def test_readme_metadata_and_local_links():
    readme = README.read_text(encoding="utf-8")
    with (ROOT / "pyproject.toml").open("rb") as config:
        project = tomllib.load(config)["project"]

    names = re.search(r"发行包名称为 `([\w-]+)`，Python 导入名称为 `([\w.]+)`", readme)
    assert names, "README must document the distribution and import names"
    assert names[1] == project["name"]
    assert names[2] == Web.__module__.split(".", 1)[0]
    python = re.search(r"需要 Python (\d+\.\d+) 或更新版本", readme)
    assert python and f">={python[1]}" == project["requires-python"]
    assert not project.get("scripts") and not project.get("gui-scripts")

    for target in re.findall(r"\]\(([^\s)]+)\)", readme):
        if "://" not in target:
            path = target.split("#", 1)[0]
            assert not path or (ROOT / path).exists(), f"Missing README link target: {target}"


def test_readme_python_examples_compile():
    readme = README.read_text(encoding="utf-8")
    examples = list(re.finditer(r"^```python\n(.*?)^```$", readme, re.M | re.S))
    assert examples, "README must include Python examples"
    for example in examples:
        start = readme.count("\n", 0, example.start(1))
        compile("\n" * start + example[1], str(README), "exec")


def run_readme_example(heading):
    readme = README.read_text(encoding="utf-8")
    section = re.search(rf"^#{{2,6}} {re.escape(heading)}\n(.*?)(?=^#{{1,6}} |\Z)", readme, re.M | re.S)
    assert section, f"Missing README section: {heading}"
    snippet = re.search(r"^```python\n(.*?)^```$", section[1], re.M | re.S)
    assert snippet, f"Missing Python example: {heading}"
    start = readme.count("\n", 0, section.start(1) + snippet.start(1))
    namespace = {}
    exec(compile("\n" * start + snippet[1], str(README), "exec"), namespace)
    return namespace


@pytest.mark.browser
@browser_required
def test_readme_quickstart_runs_without_a_model(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in ("E2E_MODEL_ENDPOINT", "E2E_MODEL", "E2E_MODEL_API_KEY", "DEEPSEEK_MODEL", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    run_readme_example("运行第一个浏览器操作")


def latest_request(messages):
    """The newest decision payload, with stateful screen updates applied."""
    nodes, request = {}, None
    for message in messages:
        if message.get("role") != "user" or not isinstance(message.get("content"), str):
            continue
        try:
            payload = json.loads(message["content"])
        except ValueError:
            continue
        if not isinstance(payload, dict) or "observation" not in payload:
            continue
        observation = payload["observation"]
        changes = observation.get("changes")
        if "nodes" in observation:
            nodes = {node["id"]: node for node in observation["nodes"]}
        elif changes is not None:
            for node in changes.get("added", []):
                nodes[node["id"]] = node
            for item in changes.get("changed", []):
                nodes.setdefault(item["id"], {"id": item["id"]}).update(item.get("now") or {})
                for key in item.get("unset", []):
                    nodes[item["id"]].pop(key, None)
            for node_id in changes.get("removed", []):
                nodes.pop(node_id, None)
        prior = request or {}
        history = prior.get("history", []) + payload.get("history", [])
        request = {**prior, **payload, "history": history,
                   "observation": dict(observation, nodes=list(nodes.values()))}
    return request


@pytest.fixture
def search_service():
    calls = Counter()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            url = urlsplit(self.path)
            if url.path == "/search":
                self.reply(SEARCH_PAGE, "text/html")
            elif url.path == "/results" and parse_qs(url.query).get("q") == ["分布式系统"]:
                self.reply(RESULTS_PAGE, "text/html")
            else:
                self.send_error(404)

        def do_POST(self):
            if self.path != "/chat/completions":
                self.send_error(404)
                return
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            request = latest_request(body["messages"])
            kind = request["kind"]
            calls[kind] += 1
            nodes = request["observation"]["nodes"]
            result_visible = any(node["role"] == "link" and node["name"] == RESULTS[0]["title"] for node in nodes)
            if kind == "act":
                query = request["params"]["query"]
                assert query == "分布式系统"
                if result_visible:
                    answer = {"done": True, "summary": "Search results are visible"}
                else:
                    searchbox = next(node for node in nodes if node["role"] == "searchbox")
                    if searchbox.get("value") == query:
                        action = {"op": "click", "target": {"by": "role", "value": "button", "name": "Search"}}
                    else:
                        action = {"op": "fill", "target": {"by": "label", "value": "Search"}, "value": query}
                    answer = {"action": action}
            elif kind == "assert":
                answer = {"passed": result_visible, "reason": "Search result link is present in the observation"}
            else:
                assert kind == "extract"
                answer = {"value": RESULTS if result_visible else []}
            self.reply(json.dumps({"choices": [{"message": {"content": json.dumps(answer)}}]}), "application/json")

        def reply(self, body, content_type):
            self.send_response(200)
            self.send_header("Content-Type", content_type + "; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))

        def log_message(self, *_):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}", calls
        finally:
            server.shutdown()
            thread.join(timeout=2)


@pytest.mark.browser
@browser_required
def test_readme_natural_language_search(search_service, tmp_path, monkeypatch):
    url, calls = search_service
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_URL", url + "/search")
    monkeypatch.setenv("E2E_MODEL_ENDPOINT", url + "/chat/completions")
    monkeypatch.setenv("E2E_MODEL", "readme-test-model")
    monkeypatch.setenv("E2E_MODEL_API_KEY", "")
    monkeypatch.setenv("E2E_VISION", "false")
    monkeypatch.setenv("CI", "false")

    example = run_readme_example("用自然语言操作页面")

    assert example["result"] == RESULTS
    assert calls == {"act": 3, "assert": 2, "extract": 1}
    evidence = tmp_path / ".e2e/evidence/search/steps.json"
    steps = json.loads(evidence.read_text(encoding="utf-8"))
    assert [step["api"] for step in steps] == ["act", "wait_for", "assert", "extract"]
    assert all(step["status"] == "passed" for step in steps)

    (recording,) = (tmp_path / ".e2e/cache").glob("*.json")
    entry = json.loads(recording.read_text(encoding="utf-8"))
    assert [action["op"] for action in entry["actions"]] == ["fill", "click"]
    assert entry["key"] == recording.stem == steps[0]["cache"]["key"]
    if os.name == "posix":
        assert stat.S_IMODE(recording.stat().st_mode) == 0o600
