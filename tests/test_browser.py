"""Opt-in real browser and model-wire integration, with no real model charges."""

from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import threading

import pytest

from e2e_web import CacheConfig, ChatCompletionsExecutor, E2EError, FunctionExecutor, ReplayStale, Secret, StepTimeout, Web

pytestmark = [pytest.mark.browser, pytest.mark.skipif(os.getenv("E2E_WEB_BROWSER_TESTS") != "1", reason="set E2E_WEB_BROWSER_TESTS=1")]

HTML = '''<!doctype html><html><body>
<h1>Workspace</h1><label>Query<input id="query"></label><button id="apply">Apply</button>
<p role="status" id="result"></p><p id="clock"></p>
<label>Region<select><option>North</option><option>South</option></select></label>
<label>Terms<input type="checkbox"></label><label>Attachment<input type="file" id="upload"></label>
<iframe id="child" src="/frame"></iframe>
<a href="/popup" target="_blank">Open detail</a><a href="/download" download>Download</a>
<button id="dialog">Open dialog</button>
<script>
document.querySelector('#apply').onclick=()=>document.querySelector('#result').textContent='Saved '+document.querySelector('#query').value;
document.querySelector('#clock').textContent='unrelated '+Math.random();
document.querySelector('#dialog').onclick=()=>alert('confirm');
</script></body></html>'''


@pytest.fixture
def server():
    calls = Counter()

    class Handler(BaseHTTPRequestHandler):
        """Own a test website and a compatible model stub."""

        def do_GET(self):
            if self.path == "/frame":
                body = '<label>Frame input<input></label><button onclick="document.querySelector(\'p\').textContent=\'Frame saved\'">Frame save</button><p role="status"></p>'
            elif self.path == "/download":
                self.send_response(200)
                self.send_header("Content-Disposition", 'attachment; filename="sample.txt"')
                self.end_headers()
                self.wfile.write(b"download evidence")
                return
            elif self.path == "/popup":
                body = '<h1>Detail</h1>'
            else:
                body = HTML
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode())

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            request = json.loads(body["messages"][-1]["content"])
            kind = request["kind"]
            calls[kind] += 1
            if kind == "act":
                index = len(request["history"])
                actions = request["params"]["actions"]
                answer = {"action": actions[index]} if index < len(actions) else {"done": True, "summary": "task complete"}
            elif kind == "assert":
                answer = {"passed": request["params"].get("expected", "Saved") in json.dumps(request["observation"]), "reason": "visible status"}
            else:
                answer = {"value": {"title": request["observation"]["title"]}}
            wire = json.dumps({"choices": [{"message": {"content": json.dumps(answer)}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(wire)

        def log_message(self, *_):
            """Keep browser test logs focused on failures."""

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{http.server_port}", calls
    http.shutdown()
    http.server_close()
    thread.join(timeout=2)


@pytest.fixture
def host():
    with Web() as host:
        yield host


def target(role, name, **kwargs):
    return dict(by="role", value=role, name=name, **kwargs)


def test_installed_sdk_model_transport_replay_and_dynamic_page(host, server, tmp_path):
    url, calls = server
    model = ChatCompletionsExecutor(endpoint=url + "/model", model="stub")
    params = {"actions": [dict(op="fill", target=target("textbox", "Query"), value="Graphs"),
                          dict(op="click", target=target("button", "Apply"))]}
    options = dict(name="save", app_identity="workspace", executor=model,
                   cache=CacheConfig(directory=tmp_path / "cache", mode="read-write"))
    with host.session(**options, artifacts_dir=tmp_path / "first") as first:
        first.page.goto(url)
        result = first.agent.act("Save query", params)
        first.expect(first.page.get_by_role("status")).to_have_text("Saved Graphs")
        assert result.model_calls == 3
        first.screenshot(tmp_path / "proof.png")
    before = calls["act"]
    options["cache"] = CacheConfig(directory=tmp_path / "cache", strict=True)
    with host.session(**options) as second:
        second.page.goto(url)
        result = second.agent.act("Save query", params)
        assert result.cache["mode"] == "replay" and result.model_calls == 0
        second.agent.assert_("Query saved", {"expected": "Saved Graphs"})
        second.agent.wait_for("Query ready", {"expected": "Saved Graphs"}, interval=0.01)
        assert second.agent.extract("Title", schema={"type": "object", "required": ["title"]}) == {"title": ""}
    assert calls["act"] == before and calls["assert"] == 2 and calls["extract"] == 1
    assert (tmp_path / "proof.png").stat().st_size > 0
    assert (tmp_path / "first" / "steps.json").is_file()


def test_form_observation_matches_the_controls_used_by_playwright(host):
    with host.session(name="form", app_identity="form", cache=CacheConfig(mode="off")) as session:
        session.page.set_content("""
            <label>Search<input type="search"></label>
            <input type="submit" value="Send">
            <label><span>Region</span><select>
              <option value="v_17">North</option><option value="v_29">South</option>
              <optgroup label="Unavailable" disabled><option value="v_33">East</option></optgroup>
            </select></label>
            <form>
              <input type="hidden" name="hidden">
              <label>Languages<select multiple>
                <option selected>Chinese</option><option>French</option>
              </select></label>
            </form>
            <form hidden><button>Hidden button</button></form>
        """)
        before = session.observe()
        for role, name in (("searchbox", "Search"), ("button", "Send"),
                           ("combobox", "Region"), ("listbox", "Languages")):
            assert any(node["role"] == role and node["name"] == name for node in before["nodes"])
            assert session.page.get_by_role(role, name=name, exact=True).count() == 1
            assert f'- {role} "{name}"' in before["tree"]
        assert not any(node["name"] == "Hidden button" for node in before["nodes"])
        assert '  - option "North" [selected]' in before["tree"]
        assert '  - option "East" [disabled]' in before["tree"]
        assert '  - option "Chinese" [selected]' in before["tree"]
        assert 'v_17' not in before["tree"]
        region = next(node for node in before["nodes"] if node["role"] == "combobox" and node["name"] == "Region")
        session.engine.perform(dict(op="select", target=target(region["role"], region["name"]), value="South"), timeout=3)
        after = session.observe()
        assert '  - option "South" [selected]' in after["tree"]
        assert '  - option "North" [selected]' not in after["tree"]
        assert session.page.get_by_role("combobox", name="Region").input_value() == "v_29"


def test_strict_replay_rejects_relative_navigation_with_a_different_base_origin(host, tmp_path):
    def decide(request, *, timeout):
        if request["history"]:
            return {"done": True}
        return {"action": {"op": "navigate", "url": "/form"}}

    def serve(route):
        route.fulfill(content_type="text/html", body="<h1>Form</h1>")

    options = dict(name="navigation", app_identity="app", executor=FunctionExecutor(decide))
    with host.session(**options, base_url="https://app.example",
                      cache=CacheConfig(directory=tmp_path, mode="read-write")) as first:
        first.page.route("**/*", serve)
        first.agent.act("Open the form")
        first.expect(first.page.get_by_role("heading", name="Form")).to_be_visible()
    with host.session(**options, base_url="https://other.example",
                      cache=CacheConfig(directory=tmp_path, strict=True)) as second:
        second.page.route("**/*", serve)
        with pytest.raises(ReplayStale, match="origin changed"):
            second.agent.act("Open the form")
        assert second.page.url == "about:blank"
        assert second.steps[-1].actions == second.steps[-1].model_calls == 0


def test_frame_upload_native_routes_popup_download_dialog_and_borrowed_page(host, server, tmp_path):
    url, _ = server
    with host.session(name="owner", app_identity="workspace", cache=CacheConfig(mode="off")) as owner:
        page = owner.page
        page.goto(url)
        upload = tmp_path / "upload.txt"
        upload.write_text("sample")
        actions = [
            dict(op="fill", target=target("textbox", "Frame input", frame="#child"), value="nested"),
            dict(op="click", target=target("button", "Frame save", frame="#child")),
            dict(op="select", target=target("combobox", "Region"), value="South"),
            dict(op="check", target=target("checkbox", "Terms")),
            dict(op="upload", target=dict(by="css", value="#upload"), value=[str(upload)]),
        ]

        def execute(request, *, timeout):
            index = len(request["history"])
            return {"action": actions[index]} if index < len(actions) else {"done": True}

        with Web.attach(page, name="borrowed", app_identity="workspace", executor=FunctionExecutor(execute),
                        upload_roots=(tmp_path,), cache=CacheConfig(mode="off")) as session:
            session.agent.act("Perform external scenario")
            session.expect(page.frame_locator("#child").get_by_role("status")).to_have_text("Frame saved")
            assert session.observe()["frames"]
            page.route("**/api/check", lambda route: route.fulfill(json={"ok": True}))
            assert page.evaluate("fetch('/api/check').then(r=>r.json())") == {"ok": True}
            page.unroute("**/api/check")
            with page.expect_popup() as popup:
                page.get_by_role("link", name="Open detail").click()
            session.expect(popup.value.get_by_role("heading")).to_have_text("Detail")
            popup.value.close()
            with page.expect_download() as download:
                page.get_by_role("link", name="Download", exact=True).click()
            download.value.save_as(tmp_path / "download.txt")
            assert (tmp_path / "download.txt").read_text() == "download evidence"
            page.once("dialog", lambda dialog: dialog.accept())
            page.get_by_role("button", name="Open dialog").click()
        assert not page.is_closed()
        page.get_by_label("Query").fill("still owned by caller")


def test_host_manages_nested_popup_sessions_with_one_context_manager(server):
    url, calls = server
    params = {"actions": [{"op": "click", "target": target("link", "Open detail")}]}
    model = ChatCompletionsExecutor(endpoint=url + "/model", model="stub")
    with Web() as web:
        session = web.session(name="home", app_identity="app", executor=model, cache=CacheConfig(mode="off"))
        opener = session.page
        opener.goto(url)
        popup = session.open_popup("Open the first tab", params, name="results")
        popup_page = popup.page
        popup.expect(popup_page.get_by_role("heading")).to_have_text("Detail")
        popup_page.set_content(f'<a href="{url}/popup" target="_blank">Open detail</a>')
        child = popup.open_popup("Open the next tab", params, name="detail")
        child_page = child.page
        child.expect(child_page.get_by_role("heading")).to_have_text("Detail")
        with popup_page.expect_popup() as grandchild:
            popup_page.evaluate("window.open('about:blank')")
        assert session.observe()["tab_count"] == 1
        assert popup.observe()["tab_count"] == 2
        assert child.observe()["tab_count"] == 1
        popup.close()
        assert grandchild.value.is_closed()
        assert not opener.is_closed()
        assert calls["act"] == 2

    assert all(page.is_closed() for page in (opener, popup_page, child_page))
    for closed_session in (session, popup, child):
        with pytest.raises(E2EError, match="SESSION_CLOSED"):
            closed_session.observe()


def test_popup_from_borrowed_page_closes_only_the_popup_and_times_out_once(host, server):
    url, calls = server
    owner = host.session(name="owner", app_identity="app")
    opener = owner.page
    opener.goto(url)
    model = ChatCompletionsExecutor(endpoint=url + "/model", model="stub")
    with Web.attach(opener, name="borrowed", app_identity="app", executor=model,
                    cache=CacheConfig(mode="off")) as session:
        popup = session.open_popup("Open detail", {"actions": [{"op": "click", "target": target("link", "Open detail")}]},
                                   name="details")
        popup_page = popup.page
        with pytest.raises(StepTimeout, match="popup did not open"):
            session.open_popup("Click apply", {"actions": [{"op": "click", "target": target("button", "Apply")}]},
                               name="missing", timeout=1)
        assert calls["act"] == 2

    assert popup_page.is_closed()
    assert not opener.is_closed()


def test_nested_frames_and_context_init_scripts(host, server, tmp_path):
    url, _ = server
    script = tmp_path / "init.js"
    script.write_text("window.fromFile = 'initialized';")
    with host.session(name="nested", app_identity="app", cache=CacheConfig(mode="off"),
                      init_scripts=("window.fromSource = true;", script)) as session:
        session.page.goto(url)
        assert session.page.evaluate("window.fromSource && window.fromFile === 'initialized'")
        session.page.frame_locator("#child").locator("body").evaluate(
            "body => { const f = document.createElement('iframe'); f.src='/frame'; body.append(f); }")
        nested = session.page.frame_locator("#child").frame_locator("iframe")
        nested.get_by_label("Frame input").wait_for()
        observations = session.observe()["frames"]
        deep = next(frame for frame in observations if len(frame["frame"]) == 2)
        session.engine.perform(dict(op="fill", target=dict(by="label", value="Frame input", frame=deep["frame"]),
                                    value="deep input"), timeout=3)
        session.expect(nested.get_by_label("Frame input")).to_have_value("deep input")
        assert nested.locator("body").evaluate("window.fromFile") == "initialized"
        with session.page.expect_popup() as popup:
            session.page.get_by_role("link", name="Open detail").click()
        assert popup.value.evaluate("window.fromSource && window.fromFile === 'initialized'")
        popup.value.close()


@pytest.mark.parametrize("field", ['<input aria-label="Credential">',
                                  '<input type="password" aria-label="Credential" readonly>',
                                  '<input type="password" aria-label="Credential" disabled>',
                                  '<button aria-label="Credential">Submit</button>'])
def test_password_secret_refuses_wrong_or_uneditable_sink(host, field):
    with host.session(name="denied-secret", app_identity="app", cache=CacheConfig(mode="off")) as session:
        session.page.set_content(field)
        with pytest.raises(E2EError, match="POLICY_DENIED"):
            session.engine.fill_secret(dict(by="label", value="Credential"),
                                       Secret("password", "private-password", purpose="password"), timeout=2)


def test_password_secret_replay_reauthorizes_sink(host, tmp_path):
    calls = []

    def execute(request, *, timeout):
        calls.append(request)
        return {"action": dict(op="fill_secret", target=dict(by="label", value="Credential"), secret="password")} if not request["history"] else {"done": True}

    options = dict(name="secret-replay", app_identity="app", executor=FunctionExecutor(execute),
                   cache=CacheConfig(directory=tmp_path / "cache", mode="read-write"))
    secret = Secret("password", "private-password", purpose="password")
    for field_type in ("password", "text"):
        with host.session(**options) as session:
            session.page.set_content(f'<input type="{field_type}" aria-label="Credential" oninput="document.querySelector(\'p\').textContent=\'Filled\'"><p role="status"></p>')
            if field_type == "password":
                session.agent.act("Fill password", {"password": secret})
                session.expect(session.page.get_by_role("status")).to_have_text("Filled")
            else:
                before = len(calls)
                with pytest.raises(E2EError, match="POLICY_DENIED"):
                    session.agent.act("Fill password", {"password": secret})
                assert len(calls) == before
                assert session.page.get_by_label("Credential").input_value() == ""
