"""Cache matching, request budgeting and screen updates brought over from upstream."""

import json

import pytest

from e2e_web import (AgentConfig, CacheConfig, E2EError, FunctionExecutor, Secret, StepRecord, Unique,
                     WebSession)
from e2e_web._utils import bound_output, templates_collide, truncate_utf8
from e2e_web.cache import (delta, delta_evidenced, end_baseline, matches_end, opens_with_navigate,
                           route, same_route, valid_entry)
from e2e_web.screen_update import ScreenPresenter
from test_contract import Engine, brain, target


def view(tree, url="https://app.example/form", **extra):
    return dict(url=url, tree=tree, frames=(), **extra)


def recording(before, after, **extra):
    appeared, gone = delta(before, after)
    return dict(appeared=appeared, gone=gone, end=route(after["url"]), **extra)


def test_route_reads_minted_values_as_placeholders():
    assert route("https://app.example/orders/8f3c2a1b") == "/orders/:id"
    assert route("https://app.example/orders/1234") == "/orders/:id"
    assert route("https://app.example/invoices/INV-2041") == "/invoices/:id"
    assert route("https://app.example/docs/getting-started") == "/docs/getting-started"
    assert route("https://app.example/companies-v2/list") == "/companies-v2/list"
    assert same_route("https://app.example/orders/8f3c2a1b", "https://other.example/orders/99112233")
    assert not same_route("https://app.example/orders/8f3c2a1b", "https://app.example/invoices/8f3c2a1b")


def test_route_keeps_hash_routing_and_normalizes_query_terms():
    assert route("https://app.example/#/reports/42") == "/reports/:id"
    assert route("https://app.example/list?page=2&sort=name") == "/list?page=:id&sort=name"
    assert route("https://app.example/list?b=1&a=2") == route("https://app.example/list?a=2&b=1")


def test_a_recording_that_navigates_first_is_not_bound_to_its_start_page():
    assert opens_with_navigate([{"op": "navigate", "url": "https://x/"}, {"op": "click"}])
    assert not opens_with_navigate([{"op": "click"}, {"op": "navigate", "url": "https://x/"}])
    assert not opens_with_navigate([{"op": "resize_viewport", "width": 900, "height": 700}])


def test_a_clock_is_dropped_when_a_stable_anchor_moved():
    before = view('- text "10:41:02"\n- status ""')
    after = view('- text "10:41:03"\n- status "Saved"')
    appeared, gone = delta(before, after)
    assert appeared == ['- status "Saved"'] and gone == ['- status ""']


def test_a_clock_is_kept_when_it_is_the_only_change():
    before, after = view('- text "10:41:02"'), view('- text "10:41:03"')
    appeared, gone = delta(before, after)
    assert appeared == ['- text "10:41:03"'] and gone == ['- text "10:41:02"']


@pytest.mark.parametrize("text", ["2026-11-27", "3 items", "42", "12 seconds", "2026-11-27T10:41:02Z"])
def test_dates_counts_and_bare_numbers_are_volatile_beside_a_stable_anchor(text):
    before = view(f'- text "{text}"\n- status ""')
    after = view(f'- text "{text.replace("3 items", "4 items").replace("42", "43")}"\n- status "Saved"')
    appeared, gone = delta(before, after)
    assert appeared == ['- status "Saved"'] and gone == ['- status ""']


def test_a_count_is_the_result_when_it_is_the_only_thing_the_step_made_appear():
    before, after = view("- status \"\""), view('- status "0 records imported"')
    appeared, gone = delta(before, after)
    assert appeared == ['- status "0 records imported"']


def test_announcements_come_before_leaves_and_containers():
    rows = "\n".join(f'- listitem "Row {index}"' for index in range(8))
    before = view(f'- list ""\n{rows}')
    after = view(f'- list ""\n{rows}\n- paragraph "" text="Filler one two three"\n- alert "Deploy failed"')
    appeared, _ = delta(before, after)
    assert appeared[0] == '- alert "Deploy failed"'


def test_end_baseline_reads_the_arrival_screen_not_the_final_one():
    before = view("- status \"\"", "https://app.example/start")
    arrival = view('- list ""', "https://app.example/list")
    final = view('- list ""\n- listitem "New order"', "https://app.example/list")
    assert end_baseline([arrival, final], "/list", before) is arrival
    assert end_baseline([], "/list", before) is before


def test_a_row_a_previous_run_left_behind_does_not_prove_a_new_one():
    arrival = view('- list ""\n- listitem "Existing order"')
    final = view('- list ""\n- listitem "Existing order"')
    entry = dict(appeared=['- listitem "New order"'], gone=[], end="/form")
    assert not matches_end(entry, arrival, final)
    fresh = view('- list ""\n- listitem "New order"')
    assert matches_end(entry, view('- list ""'), fresh)


def test_delta_evidenced_requires_the_screen_to_have_moved():
    still = view("- status \"\"")
    assert not delta_evidenced(still, still)
    assert delta_evidenced(still, view('- status "Saved"'))
    assert delta_evidenced(view("", "https://app.example/a"), view("", "https://app.example/b"))


def test_a_recording_that_rewrote_an_input_never_replays():
    entry = dict(schema="e2e-web-python-trace-1", key="a" * 64, provenance=dict(test="t"),
                 start="/form", end="/form", appeared=[], gone=['- status ""'],
                 actions=[dict(op="click", target=target())], summary="s",
                 start_origin="b" * 64, end_origin="b" * 64, action_origins=["b" * 64])
    assert valid_entry(entry, "a" * 64)
    assert valid_entry(dict(entry, truncated=False), "a" * 64)
    assert not valid_entry(dict(entry, truncated=True), "a" * 64)
    assert valid_entry(dict(entry, end_wait_ms=12000), "a" * 64)
    assert not valid_entry(dict(entry, end_wait_ms=-1), "a" * 64)


def test_unique_values_that_read_into_a_literal_are_rejected():
    assert not templates_collide({"name": "Acme"}, {})
    assert not templates_collide({"name": "Acme"}, {"slot": "Acme"})
    assert templates_collide({"name": "Acme", "note": "Acme HQ"}, {"slot": "Acme"})
    assert templates_collide({}, {"a": "Acme", "b": "Acme"})
    assert templates_collide({}, {"a": "Acme", "b": "Acme-North"})


def test_tool_output_is_bounded_before_it_repeats_in_every_turn():
    text = "\n".join(f"line {index}" for index in range(1000))
    clipped = bound_output(text, max_lines=400, max_bytes=16384)
    assert len(clipped.splitlines()) == 401 and "tool output truncated" in clipped
    assert bound_output("short", max_lines=400, max_bytes=16384) == "short"
    structured = bound_output({"items": list(range(5000))}, max_lines=400, max_bytes=4096)
    assert structured["truncated"] is True and "tool output truncated" in structured["text"]


def test_truncation_never_splits_a_multibyte_character():
    assert truncate_utf8("字段字段", 7) == "字段"
    assert truncate_utf8("abc", 3) == "abc"


def test_prior_steps_redact_before_cutting_so_no_secret_fragment_survives():
    seen = []

    def complete(request, *, timeout):
        seen.append(request)
        return {"done": True}

    secret = Secret("token", "s3cr3t-value-that-crosses-the-cut")
    with WebSession(Engine(), name="prior", app_identity="app", executor=FunctionExecutor(complete),
                    secrets=(secret,), cache=CacheConfig(mode="off")) as session:
        session.steps.append(StepRecord(0, "act", "x" * 250 + secret.value, status="passed"))
        session.steps[-1].explanation = "y" * 690 + secret.value
        session.agent.act("Continue")
    prior = seen[0]["prior_steps"][0]
    # Cutting first would leave a readable prefix of the value behind.
    assert secret.value[:12] not in json.dumps(prior)
    assert "<secret:token>" not in prior["instruction"]  # the marker itself is cut, never the value


def test_prior_steps_keep_a_whole_secret_marker_when_it_fits():
    seen = []

    def complete(request, *, timeout):
        seen.append(request)
        return {"done": True}

    secret = Secret("token", "s3cr3t")
    with WebSession(Engine(), name="prior2", app_identity="app", executor=FunctionExecutor(complete),
                    secrets=(secret,), cache=CacheConfig(mode="off")) as session:
        session.steps.append(StepRecord(0, "act", f"fill {secret.value}", status="passed"))
        session.agent.act("Continue")
    assert seen[0]["prior_steps"][0]["instruction"] == "fill <secret:token>"


def test_prior_steps_declare_what_they_left_out():
    seen = []

    def complete(request, *, timeout):
        seen.append(request)
        return {"done": True}

    with WebSession(Engine(), name="omitted", app_identity="app", executor=FunctionExecutor(complete),
                    cache=CacheConfig(mode="off")) as session:
        for index in range(40):
            session.steps.append(StepRecord(index, "act", f"step {index} " + "x" * 200, status="passed"))
        session.agent.act("Continue")
    assert seen[0]["prior_steps"][0]["notice"].endswith("earlier step(s) omitted]")


def test_a_malformed_provider_response_is_repaired_once_instead_of_ending_the_step():
    attempts = []

    def complete(request, *, timeout):
        attempts.append(request)
        if len(attempts) == 1:
            error = E2EError("MODEL_OUTPUT_INVALID", "provider did not return a JSON decision object")
            error.raw_text = "here is my plan, in prose"
            raise error
        assert "previous response" in request["repair"] and "in prose" in request["repair"]
        return {"done": True}

    with WebSession(Engine(), name="repair", app_identity="app", executor=FunctionExecutor(complete),
                    cache=CacheConfig(mode="off")) as session:
        assert session.agent.act("Apply input").model_calls >= 2
    assert len(attempts) == 2 and session.steps[-1].status == "passed"


def test_screen_presenter_sends_the_whole_screen_once_then_only_the_change():
    def screen(identifier, values):
        return dict(id=identifier, url="https://app.example/form", title="Form",
                    viewport=dict(width=800, height=600),
                    nodes=[dict(id=key, role="button", name=value) for key, value in values.items()])

    rows = dict(a="Save", b="Cancel", c="Delete", d="Help", e="Back", f="Next")
    presenter = ScreenPresenter()
    first = presenter.present(screen("s1", rows))
    assert not presenter.diffed and len(first["nodes"]) == 6
    assert presenter.alias("a") == "n1" and presenter.alias("b") == "n2"

    added = dict(rows, g="More")
    second = presenter.present(screen("s2", added))
    assert presenter.diffed and "nodes" not in second
    assert second["changes"]["since"] == "s1"
    assert [node["id"] for node in second["changes"]["added"]] == ["g"]
    assert second["changes"]["removed"] == [] and second["changes"]["unchanged"] == 6

    renamed = dict(added, b="Cancelled")
    third = presenter.present(screen("s3", renamed))
    assert third["changes"]["changed"] == [{"id": "b", "was": {"name": "Cancel"}, "now": {"name": "Cancelled"}}]

    removed = dict(renamed)
    removed.pop("c")
    fourth = presenter.present(screen("s4", removed))
    assert fourth["changes"]["removed"] == ["c"]

    # An unchanged screen carries no node data at all.
    fifth = presenter.present(screen("s5", removed))
    assert fifth["changes"]["added"] == [] and fifth["changes"]["changed"] == []
    assert fifth["changes"]["unchanged"] == len(removed)


def test_screen_presenter_falls_back_to_a_whole_screen_when_most_of_it_changed():
    def screen(identifier, label):
        return dict(id=identifier, url="https://app.example/form", nodes=[
            dict(id=f"n{index}", role="text", name=f"{label} {index}") for index in range(4)])

    presenter = ScreenPresenter()
    presenter.present(screen("s1", "row"))
    replaced = presenter.present(screen("s2", "replacement"))
    assert not presenter.diffed and len(replaced["nodes"]) == 4
    assert presenter.alias("n0") == "n1"


def test_screen_presenter_never_diffs_a_truncated_listing():
    presenter = ScreenPresenter()
    presenter.present(dict(id="s1", url="/form", nodes=[dict(id="a", role="button", name="Save")]))
    truncated = presenter.present(dict(id="s2", url="/form", truncated=True,
                                       nodes=[dict(id="a", role="button", name="Save")]))
    assert not presenter.diffed and "nodes" in truncated
    again = presenter.present(dict(id="s3", url="/form", nodes=[dict(id="a", role="button", name="Save")]))
    assert "nodes" in again


def test_screen_presenter_resets_between_steps():
    presenter = ScreenPresenter()
    presenter.present(dict(id="s1", url="/form", nodes=[dict(id="a", role="button", name="Save")]))
    presenter.reset()
    full = presenter.present(dict(id="s2", url="/form", nodes=[dict(id="a", role="button", name="Save")]))
    assert "nodes" in full and presenter.alias("a") == "n1"


class BigEngine(Engine):
    """A screen dense enough to exceed a small model budget."""

    def observe(self, *, timeout):
        rows = "\n".join(f'- text "row {index} ' + "x" * 40 + '"' for index in range(120))
        return {"url": "https://app.example/form", "tree": rows,
                "nodes": [dict(id=f"n{index}", role="text", name=f"value {index} " + "y" * 40)
                          for index in range(120)]}


def test_the_harness_trims_an_observation_for_any_executor():
    seen = []

    def complete(request, *, timeout):
        seen.append(request)
        return {"done": True}

    with WebSession(BigEngine(), name="budget", app_identity="app",
                    executor=FunctionExecutor(complete), agent=AgentConfig(max_input_tokens=2048),
                    cache=CacheConfig(mode="off")) as session:
        session.agent.act("Read the rows")
    observation = seen[0]["observation"]
    assert observation["truncated"] is True
    from e2e_web._utils import canonical
    assert len(canonical(observation).encode()) <= 2048
    assert observation["url"] == "https://app.example/form"
    assert len(seen[0]["observation"]["nodes"]) < 120


def test_a_transport_failure_is_retried_before_the_step_fails(monkeypatch):
    from io import BytesIO
    from urllib.error import HTTPError
    from e2e_web import DeepSeekExecutor, models

    attempts = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, limit):
            return json.dumps({"choices": [{"message": {"content": '{"done":true}'}}]}).encode()

    def send(request, *, timeout):
        attempts.append(timeout)
        if len(attempts) < 3:
            raise HTTPError(request.full_url, 503, "unavailable", {}, BytesIO(b"try later"))
        return Response()

    monkeypatch.setattr(models, "urlopen", send)
    monkeypatch.setattr(models.time, "sleep", lambda seconds: None)
    executor = DeepSeekExecutor(model="local", api_key="test")
    assert executor.complete({"kind": "act"}, timeout=5) == {"done": True}
    assert len(attempts) == 3


def test_a_context_overflow_is_classified_and_the_request_shrinks(monkeypatch):
    from io import BytesIO
    from urllib.error import HTTPError
    from e2e_web import DeepSeekExecutor, models

    bodies = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, limit):
            return json.dumps({"choices": [{"message": {"content": '{"done":true}'}}]}).encode()

    def send(request, *, timeout):
        bodies.append(request.data)
        if len(bodies) == 1:
            raise HTTPError(request.full_url, 400, "bad request", {},
                            BytesIO(b'{"error":{"message":"This model\'s maximum context length is 8192 tokens"}}'))
        return Response()

    monkeypatch.setattr(models, "urlopen", send)
    seen = []

    def complete(request, *, timeout):
        seen.append(request)
        return {"done": True}

    assert models.is_context_overflow("prompt is too long")
    assert not models.is_context_overflow("rate limit reached, too many tokens")
    with WebSession(BigEngine(), name="overflow", app_identity="app",
                    executor=DeepSeekExecutor(model="local", api_key="test"),
                    agent=AgentConfig(max_input_tokens=8192), cache=CacheConfig(mode="off")) as session:
        session.agent.act("Read the rows", params={"note": "x" * 2000})
    assert len(bodies) >= 2


def test_retry_attempt_skips_replay_and_records_again(tmp_path):
    with WebSession(Engine(), name="test", app_identity="app", executor=FunctionExecutor(brain),
                    cache=CacheConfig(directory=tmp_path / "cache", mode="read-write")) as session:
        session.agent.act("Apply input", {"value": "first"})
        session.agent.assert_("Input was applied")
    seen = []

    def executor(request, *, timeout):
        seen.append(request)
        return {"done": True}

    with WebSession(Engine(), name="test", app_identity="app", executor=FunctionExecutor(executor),
                    cache=CacheConfig(directory=tmp_path / "cache", mode="read-write", attempt=1)) as session:
        result = session.agent.act("Apply input", {"value": "first"})
    assert result.model_calls == 1 and len(seen) == 1
    assert result.cache.get("reason") == "retry"


def test_usage_reports_peak_and_cost(monkeypatch):
    from io import BytesIO
    from e2e_web import DeepSeekExecutor, models

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, limit):
            return json.dumps({"choices": [{"message": {"content": '{"done":true}'}}],
                               "usage": {"prompt_tokens": 1000, "completion_tokens": 200,
                                         "prompt_tokens_details": {"cached_tokens": 400}}}).encode()

    monkeypatch.setattr(models, "urlopen", lambda request, *, timeout: Response())
    usage = {}
    executor = DeepSeekExecutor(model="local", api_key="test", price_per_mtok=(1.0, 2.0))
    from e2e_web.models import _model_step
    token = _model_step.set((usage, False, None))
    try:
        executor.complete({"kind": "act"}, timeout=5)
    finally:
        _model_step.reset(token)
    assert usage == {"prompt_tokens": 1000, "completion_tokens": 200, "cached_prompt_tokens": 400,
                     "peak_prompt_tokens": 1000, "estimated_cost_usd": 0.0014}


def test_trim_observation_marks_what_it_cut():
    from e2e_web._utils import trim_observation
    view = dict(id="v", url="/x", tree='- text "' + "t" * 200 + '"', frames=[],
                nodes=[dict(id=f"n{index}", role="text", name="v" * 50) for index in range(50)])
    trimmed = trim_observation(view, 1200)
    assert trimmed["truncated"] is True
    assert 0 < len(trimmed["nodes"]) < 50
    from e2e_web._utils import canonical
    assert len(canonical(trimmed).encode()) <= 1200
    assert trim_observation(view, 100000) is view


def test_a_screenshot_is_charged_as_an_image_not_as_transport_bytes():
    import base64

    from e2e_web.session import _fit_request

    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000d49444154789c6360000002000154a24f5f0000000049454e44ae426082"
    )
    screenshot = base64.b64encode(png).decode() + "A" * 120_000
    observation = dict(id="v", url="https://app.example/form", title="Form", tree="",
                       frames=[], nodes=[dict(id=f"n{index}", role="text", name="row " + "x" * 40)
                                         for index in range(60)])
    request = dict(kind="act", instruction="Read", params={}, observation=observation,
                   screenshot=screenshot)
    fitted = _fit_request(request, 8192)
    # The image is one patch, not 120 KB of base64 prompt: the node table still fits.
    assert len(fitted["observation"]["nodes"]) > 30
    assert fitted["observation"]["truncated"] is True


def test_template_spellings_rebuild_the_form_the_app_used():
    from e2e_web._utils import template

    slots = {"company": "Acme Corp"}
    assert template("Acme Corp", slots) == "{{e2e_unique:company}}"
    assert template("acme-corp", slots) == "{{e2e_unique:company|slug}}"
    assert template("Acme+Corp", slots) == "{{e2e_unique:company|form}}"
    assert template("Acme%20Corp", slots) == "{{e2e_unique:company|uri}}"
    assert template("{{e2e_unique:company|slug}}", slots, expand=True) == "acme-corp"
    assert template("{{e2e_unique:company|uri}}", slots, expand=True) == "Acme%20Corp"
    assert template("{{e2e_unique:company}}", slots, expand=True) == "Acme Corp"

    multiline = {"company": "Acme\nCorp"}
    assert template("Acme Corp", multiline) == "{{e2e_unique:company|flat}}"
    assert template("{{e2e_unique:company|flat}}", multiline, expand=True) == "Acme Corp"


def test_a_unique_value_spelled_as_a_slug_still_collides_with_a_literal():
    from e2e_web._utils import templates_collide

    assert templates_collide({"url": "/companies/acme-corp"}, {"company": "Acme Corp"})
    assert not templates_collide({"url": "/companies/globex-inc"}, {"company": "Acme Corp"})
    assert templates_collide({}, {"a": "Acme Corp", "b": "Acme-Corp"})


class SlugEngine(Engine):
    """Creates a record whose detail route carries the name as a slug."""

    def __init__(self):
        super().__init__()
        self.url = "https://app.example/companies/new"

    def observe(self, *, timeout):
        return {"url": self.url, "tree": f'- textbox "Input": {self.value}\n- status: {self.result}'}

    def perform(self, action, *, timeout):
        self.actions.append(action)
        if action["op"] == "fill":
            self.value = action["value"]
        elif action["op"] == "click":
            slug = "-".join("".join(ch if ch.isalnum() else " " for ch in self.value.lower()).split())
            self.url = f"https://app.example/companies/{slug}"
            self.result = "Applied " + self.value


def test_unique_value_in_a_slugged_route_replays_with_the_next_run_value(tmp_path):
    def create():
        return WebSession(SlugEngine(), name="slug", app_identity="app", executor=FunctionExecutor(brain),
                          cache=CacheConfig(directory=tmp_path / "cache", mode="read-write"))

    with create() as first:
        first.agent.act("Apply unique", {"value": Unique("Acme Corp")})
        first.agent.assert_("Input was applied")
        assert first.engine.url.endswith("/companies/acme-corp")
    with create() as second:
        result = second.agent.act("Apply unique", {"value": Unique("Globex Inc")})
        assert result.cache["mode"] == "replay" and result.model_calls == 0
        assert second.engine.url.endswith("/companies/globex-inc")


def test_model_wire_puts_the_stable_request_before_the_changing_screen():
    from e2e_web.models import _wire_content

    view = dict(id="v", url="https://app.example/form", nodes=[dict(id="n1", role="button", name="Save")])
    first = _wire_content(dict(kind="act", instruction="Save", params={"a": 1}, context="ctx", tools=[],
                               prior_steps=[], history=[], observation=view))
    second = _wire_content(dict(kind="act", instruction="Save", params={"a": 1}, context="ctx", tools=[],
                                prior_steps=[], history=[{"action": {"op": "click"}}], observation=view))
    assert json.loads(first)["observation"] == view
    assert first.index('"instruction"') < first.index('"tools"') < first.index('"history"') < first.index('"observation"')
    # A second turn shares everything before its appended history entry.
    assert second.startswith(first[:first.index('"history"')])


def test_the_http_adapter_sends_the_same_stable_prefix(monkeypatch):
    from io import BytesIO

    from e2e_web import DeepSeekExecutor, models

    bodies = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, limit):
            return json.dumps({"choices": [{"message": {"content": '{"done":true}'}}]}).encode()

    def send(request, *, timeout):
        bodies.append(request.data)
        return Response()

    monkeypatch.setattr(models, "urlopen", send)
    executor = DeepSeekExecutor(model="local", api_key="test")
    view = dict(id="v", url="https://app.example/form", nodes=[])
    executor.complete(dict(kind="act", instruction="Save", params={}, context="ctx", tools=[],
                           prior_steps=[], history=[], observation=view), timeout=5)
    content = json.loads(bodies[0])["messages"][1]["content"]
    assert content.index('"instruction"') < content.index('"history"') < content.index('"observation"')


class ShownCodeEngine(Engine):
    """A screen that shows a one-off code, next to the field it goes into."""

    def observe(self, *, timeout):
        return {"url": self.url,
                "tree": f'- text "CODE-123"\n- textbox "Input": {self.value}\n- status: {self.result}',
                "nodes": [dict(id="code", role="status", name="", text="CODE-123", value=""),
                          dict(id="input", role="textbox", name="Input", value=self.value, editable=True),
                          dict(id="apply", role="button", name="Apply", value=""),
                          dict(id="status", role="status", name="", text=self.result, value="")]}


def test_a_value_read_off_the_screen_is_not_recorded(tmp_path):
    def decide(request, *, timeout):
        if request["history"]:
            return {"done": True, "summary": "entered"}
        return {"action": dict(op="fill", target=target(), value="CODE-123")}

    cache = CacheConfig(directory=tmp_path / "shown", mode="read-write")
    with WebSession(ShownCodeEngine(), name="shown", app_identity="app",
                    executor=FunctionExecutor(decide), cache=cache) as session:
        session.agent.act("Enter the code shown on the screen")
        session.verify(lambda: session.engine.actions)
        assert session.steps[-2].cache.get("derived") in {"whole-node", "minted-token"}
    assert not list((tmp_path / "shown").glob("*.json"))

    # The caller spelling the value out makes it the step's literal input, which
    # replays verbatim.
    literal = CacheConfig(directory=tmp_path / "literal", mode="read-write")
    with WebSession(ShownCodeEngine(), name="literal", app_identity="app",
                    executor=FunctionExecutor(decide), cache=literal) as session:
        session.agent.act("Enter CODE-123 into the field")
        session.verify(lambda: session.engine.actions)
        assert "derived" not in session.steps[-2].cache
    assert len(list((tmp_path / "literal").glob("*.json"))) == 1


def test_an_alert_anchor_matches_by_shape_not_by_its_clock():
    before = view('- status ""')
    recorded = view('- status ""\n- alert "Saved at 10:41:02"')
    entry = recording(before, recorded)
    assert entry["appeared"] == ['- alert "Saved at 10:41:02"']
    assert matches_end(entry, before, view('- status ""\n- alert "Saved at 10:41:09"'))
    assert not matches_end(entry, before, view('- status ""\n- alert "Could not save at 10:41:09"'))


def test_a_too_large_request_gives_up_history_before_the_screen():
    from e2e_web._utils import canonical
    from e2e_web.session import _fit_request

    history = [dict(action={"op": "click"}, result="r" * 2000) for _ in range(20)]
    request = dict(kind="act", instruction="Do", params={}, context="", tools=[],
                   prior_steps=[dict(notice="old")], history=history,
                   observation=dict(id="v", url="https://app.example/form", tree="",
                                    frames=[], nodes=[dict(id=f"n{index}", role="text", name="row") for index in range(40)]))
    fitted = _fit_request(request, 8192)
    assert len(fitted["history"]) < 20
    assert len(canonical(fitted).encode()) <= 8192


def test_a_project_tool_failure_is_feedback_not_a_crash():
    from e2e_web import Tool

    seen = []

    def decide(request, *, timeout):
        seen.append(request)
        if not request["history"]:
            return {"action": {"op": "tool", "name": "boom", "arguments": {}}}
        return {"done": True, "summary": "after tool failure"}

    def boom(arguments):
        raise ValueError("bad argument")

    tool = Tool("boom", "always fails", {"type": "object", "properties": {}, "additionalProperties": False}, boom)
    with WebSession(Engine(), name="tool", app_identity="app", executor=FunctionExecutor(decide),
                    tools=(tool,), cache=CacheConfig(mode="off")) as session:
        result = session.agent.act("Use the tool")
    assert result.summary == "after tool failure"
    error = seen[1]["history"][0]["error"]
    assert error["code"] == "ACTION_FAILED" and "bad argument" in error["message"]


def test_a_dispatched_timeout_is_classified_as_uncertain_commit():
    from e2e_web.browser import BrowserEngine

    class DispatchedTimeout(Exception):
        _log = ["  - performing click action"]

        def __str__(self):
            return "Locator.click: Timeout 20000ms exceeded."

    uncertain = BrowserEngine.translate(DispatchedTimeout(), deadline_passed=True)
    assert uncertain.code == "ACTION_MAY_HAVE_COMMITTED"

    class NotDispatchedTimeout(Exception):
        _log = ["  - waiting for locator(\"#save\")"]

        def __str__(self):
            return "Locator.click: Timeout 20000ms exceeded."

    assert BrowserEngine.translate(NotDispatchedTimeout()).code == "ACTION_FAILED"


def test_an_exploring_judgment_sends_the_read_only_prompt(monkeypatch):
    from io import BytesIO

    from e2e_web import DeepSeekExecutor, models

    systems = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, limit):
            return json.dumps({"choices": [{"message": {"content": '{"passed":false,"reason":"unseen"}'}}]}).encode()

    def send(request, *, timeout):
        systems.append(json.loads(request.data)["messages"][0]["content"])
        return Response()

    monkeypatch.setattr(models, "urlopen", send)
    executor = DeepSeekExecutor(model="local", api_key="test")
    executor.complete(dict(kind="assert", instruction="Check", params={}, observation=dict(nodes=[]),
                           explore=True, history=[]), timeout=5)
    assert "read-only" in systems[0] and "scroll_find" in systems[0]
    assert len(systems[0]) < len(models.RULES) // 2


def test_a_policy_refusal_is_feedback_the_model_can_correct():
    from e2e_web.errors import E2EError

    class DenyOnceEngine(Engine):
        def __init__(self):
            super().__init__()
            self.denied = False

        def perform(self, action, *, timeout):
            if action["op"] == "click" and not self.denied:
                self.denied = True
                raise E2EError("POLICY_DENIED", "upload path is not under an explicit root")
            return super().perform(action, timeout=timeout)

    seen = []

    def decide(request, *, timeout):
        seen.append(request)
        if not request["history"]:
            return {"action": dict(op="click", target=target("button", "Apply"))}
        return {"done": True, "summary": "corrected"}

    with WebSession(DenyOnceEngine(), name="deny", app_identity="app",
                    executor=FunctionExecutor(decide), cache=CacheConfig(mode="off")) as session:
        result = session.agent.act("Apply input")
    assert result.summary == "corrected"
    assert seen[1]["history"][0]["error"]["code"] == "POLICY_DENIED"


def test_a_typed_value_echoing_in_its_own_field_is_not_evidence():
    from e2e_web.cache import delta_evidenced, input_targets

    before = view('- textbox "Input"')
    typed = view('- textbox "Input": Ada')
    appeared, gone = delta(typed, before), delta(before, typed)
    entry = dict(appeared=appeared[0], gone=gone[0])
    targets = input_targets([dict(op="fill", target=target(), value="Ada")])
    assert targets == {("textbox", "Input")}
    assert not delta_evidenced(before, typed, targets)
    # A reaction elsewhere still proves the step did something.
    reacted = view('- textbox "Input": Ada\n- status "Saved"')
    assert delta_evidenced(before, reacted, targets)
    # Without target information the old text-difference behaviour is kept.
    assert delta_evidenced(before, typed)


def test_a_provider_retry_after_extends_the_backoff(monkeypatch):
    from io import BytesIO
    from urllib.error import HTTPError

    from e2e_web import DeepSeekExecutor, models

    delays = []
    attempts = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, limit):
            return json.dumps({"choices": [{"message": {"content": '{"done":true}'}}]}).encode()

    def send(request, *, timeout):
        attempts.append(timeout)
        if len(attempts) == 1:
            raise HTTPError(request.full_url, 429, "slow down", {"Retry-After": "2"}, BytesIO(b""))
        return Response()

    monkeypatch.setattr(models, "urlopen", send)
    monkeypatch.setattr(models.time, "sleep", delays.append)
    assert DeepSeekExecutor(model="local", api_key="test").complete({"kind": "act"}, timeout=6) == {"done": True}
    assert delays and delays[0] >= 2


class OpeningCodeEngine(Engine):
    """A screen that reveals a one-off code after the caller opens a panel."""

    def __init__(self):
        super().__init__()
        self.opened = False

    def observe(self, *, timeout):
        if not self.opened:
            nodes = [dict(id="open", role="button", name="Open", value="")]
            tree = '- button "Open"'
        else:
            nodes = [dict(id="code", role="status", name="", text="CODE-123", value=""),
                     dict(id="input", role="textbox", name="Input", value=self.value, editable=True)]
            tree = f'- text "CODE-123"\n- textbox "Input": {self.value}'
        return {"url": self.url, "tree": tree, "nodes": nodes}

    def perform(self, action, *, timeout):
        self.actions.append(action)
        if action["op"] == "click":
            self.opened = True
        elif action["op"] == "fill":
            self.value = action["value"]


def test_a_recorded_prefix_replays_before_a_run_time_value_gap(tmp_path):
    def record_with(request, *, timeout):
        done = sum(1 for entry in request["history"] if "action" in entry)
        actions = [dict(op="click", target=dict(by="role", value="button", name="Open")),
                   dict(op="fill", target=dict(by="role", value="textbox", name="Input"), value="CODE-123")]
        return {"action": actions[done]} if done < len(actions) else {"done": True, "summary": "entered"}

    cache = CacheConfig(directory=tmp_path / "cache", mode="read-write")
    with WebSession(OpeningCodeEngine(), name="gap", app_identity="app",
                    executor=FunctionExecutor(record_with), cache=cache) as session:
        session.agent.act("Open the panel and enter the code shown")
        session.verify(lambda: session.engine.opened)
        assert session.steps[-2].cache["derived"] == "whole-node"
    entry = json.loads(next((tmp_path / "cache").glob("*.json")).read_text())
    assert [action["op"] for action in entry["actions"]] == ["click", "gap"]
    assert entry["actions"][-1]["reason"] == "whole-node"

    with WebSession(OpeningCodeEngine(), name="gap", app_identity="app",
                    cache=CacheConfig(directory=tmp_path / "cache", strict=True)) as session:
        with pytest.raises(E2EError, match="REPLAY_STALE"):
            session.agent.act("Open the panel and enter the code shown")
        assert session.engine.actions == []

    class RedirectedEngine(OpeningCodeEngine):
        def perform(self, action, *, timeout):
            super().perform(action, timeout=timeout)
            self.url = "https://another.example/form"

    with WebSession(RedirectedEngine(), name="gap", app_identity="app",
                    cache=CacheConfig(directory=tmp_path / "cache", mode="read-only")) as session:
        with pytest.raises(E2EError, match="REPLAY_STALE"):
            session.agent.act("Open the panel and enter the code shown")
        assert len(session.engine.actions) == 1 and session.steps[-1].model_calls == 0

    def continue_with(request, *, timeout):
        done = sum(1 for item in request["history"]
                   if "action" in item and item.get("source") != "replay")
        if done == 0:
            return {"action": dict(op="fill", target=dict(by="role", value="textbox", name="Input"),
                                   value="CODE-999")}
        return {"done": True, "summary": "entered this run's code"}

    with WebSession(OpeningCodeEngine(), name="gap", app_identity="app",
                    executor=FunctionExecutor(continue_with), cache=cache) as session:
        result = session.agent.act("Open the panel and enter the code shown")
        assert result.cache["mode"] == "handoff" and result.cache["reason"] == "gap"
        assert session.engine.opened and session.engine.value == "CODE-999"
        session.verify(lambda: session.engine.value == "CODE-999")
    # The prefix-plus-gap recording is kept; the model supplies the changing value.
    assert json.loads(next((tmp_path / "cache").glob("*.json")).read_text())["actions"][-1]["op"] == "gap"


def test_a_large_screenshot_is_sized_for_the_model():
    import struct
    import zlib

    from e2e_web._utils import png_size
    from e2e_web.image import downscale_png

    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(
            ">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    width, height = 100, 50
    rows = b"".join(b"\x00" + bytes([index % 256, 0, 0]) * width
                    for index in range(height))
    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))
    small = downscale_png(png, 40)
    assert png_size(small[:24]) == (40, 20)
    assert downscale_png(png, 200) is png
    assert downscale_png(b"not a png", 40) == b"not a png"


def test_end_baseline_uses_the_last_arrival_on_the_end_route():
    from e2e_web.cache import end_baseline

    before = view("- status \"\"", "https://app.example/start")
    first = view('- list ""', "https://app.example/list")
    elsewhere = view('- dialog "Details"', "https://app.example/item/1")
    again = view('- list ""\n- listitem "New order"', "https://app.example/list")
    assert end_baseline([first, elsewhere, again], "/list", before) is again
    assert end_baseline([first, elsewhere], "/list", before) is before


def test_a_stateful_model_step_sends_one_conversation_and_screen_updates(monkeypatch):
    from io import BytesIO

    from e2e_web import DeepSeekExecutor, models
    from e2e_web.models import _model_step

    bodies = []
    replies = [{"action": {"op": "click", "target": {"by": "ref", "value": "n1",
                                                      "observation": "s1"}}},
               {"done": True, "summary": "clicked"}]

    def send(request, *, timeout):
        bodies.append(json.loads(request.data))
        content = json.dumps(replies[len(bodies) - 1])
        return BytesIO(json.dumps({"choices": [{"message": {"content": content}}]}).encode())

    monkeypatch.setattr(models, "urlopen", send)
    presenter = ScreenPresenter()
    first = dict(id="s1", url="https://app.example/form", title="Form",
                 nodes=[dict(id="a", role="button", name="Save", value="")])
    second = dict(id="s2", url="https://app.example/form", title="Form",
                  nodes=[dict(id="a", role="button", name="Save", value=""),
                         dict(id="b", role="status", name="Saved", value="")])
    executor = DeepSeekExecutor(model="local", api_key="test")
    token = _model_step.set(({}, True, presenter))
    try:
        executor.complete(dict(kind="act", instruction="Save", params={}, history=[],
                               stateful=True, observation=first), timeout=5)
        executor.complete(dict(kind="act", instruction="Save", params={},
                               history=[dict(action={"op": "click"}, result=None)],
                               stateful=True, observation=second), timeout=5)
    finally:
        _model_step.reset(token)
    assert len(bodies[0]["messages"]) == 2
    assert len(bodies[1]["messages"]) == 4
    assert bodies[1]["messages"][2]["role"] == "assistant"
    first_screen = json.loads(bodies[0]["messages"][1]["content"])["observation"]
    update = json.loads(bodies[1]["messages"][3]["content"])
    assert first_screen["nodes"] and "changes" not in first_screen
    assert "nodes" not in update["observation"] and "changes" in update["observation"]
    assert update["observation"]["changes"]["added"][0]["id"] == "n2"
    assert update["history"][0]["action"]["op"] == "click"
    assert "one conversation" in bodies[1]["messages"][0]["content"]
    assert len(presenter.messages) == 4


def test_ticking_alert_is_neither_an_action_nor_an_unexpected_alert():
    before = view('- alert "Expires in 30 seconds"')
    after = view('- alert "Expires in 28 seconds"')
    assert delta(before, after) == ([], [])
    assert not delta_evidenced(before, after)
    result = view(after["tree"] + '\n- status "Saved"')
    entry = recording(before, result)
    assert entry["appeared"] == ['- status "Saved"']
    assert matches_end(entry, before, result)
    assert not matches_end(entry, before, view(result["tree"] + '\n' + after["tree"]))


def test_stateful_wire_rollover_keeps_aliases_and_sends_pending_history(monkeypatch):
    from io import BytesIO
    from e2e_web import DeepSeekExecutor, models

    presenter, messages = ScreenPresenter(), []
    def provider(request, *, timeout):
        messages.append(json.loads(request.data)["messages"])
        return BytesIO(json.dumps({"choices": [{"message": {"content": '{"done":true}'}}]}).encode())

    monkeypatch.setattr(models, "urlopen", provider)
    model = DeepSeekExecutor(model="test", api_key="test", max_input_tokens=12500)
    token = models._model_step.set(({}, True, presenter))
    try:
        history = []
        for i in range(12):
            nodes = [dict(id="x", role="textbox", name="X", focused=(i == 0)),
                     dict(id="y", role="button", name="Y")]
            model.complete(dict(kind="act", stateful=True, instruction="Read", history=list(history),
                                observation=dict(id=f"s{i}", nodes=nodes, downloads=[{"n": i}])), timeout=2)
            history.append(dict(result=str(i) + "a" * 550))
    finally:
        models._model_step.reset(token)
    update = json.loads(messages[1][-1]["content"])
    assert update["observation"]["changes"]["changed"][0]["unset"] == ["focused"]
    assert update["observation"]["downloads"] == [{"n": 1}]
    assert len(json.loads(messages[2][-1]["content"])["history"]) == 1
    fresh = [json.loads(turn[-1]["content"]) for turn in messages[1:] if len(turn) == 2]
    assert fresh and all(turn["observation"]["nodes"][0]["id"] == "n1" for turn in fresh)
    assert all(sum(len(m["content"].encode()) for m in turn) <= 12500 for turn in messages)


def test_frame_admission_requires_an_app_site_for_external_documents():
    from e2e_web.browser import frame_on_site
    assert frame_on_site("about:srcdoc", "about:blank")
    assert frame_on_site("https://files.shop.co.za", "https://www.shop.co.za")
    assert not frame_on_site("https://other.co.za", "https://shop.co.za")
    assert not frame_on_site("data:text/html,example", "about:blank")
    assert not frame_on_site("https://external.example", "about:blank")


@pytest.mark.parametrize("explore", [False, True])
def test_agent_can_request_a_fresh_screen_without_dispatch(explore):
    seen = []
    def decide(request, *, timeout):
        seen.append(request)
        if not request["history"]:
            return {"action": {"op": "observe"}}
        assert request["history"] == [{"action": {"op": "observe"}}]
        return {"passed": True} if explore else {"done": True}

    with WebSession(Engine(), name="observe", app_identity="app", executor=FunctionExecutor(decide),
                    cache=CacheConfig(mode="off")) as session:
        if explore:
            session.agent.assert_("Check pending result", explore=True)
        else:
            session.agent.act("Check pending result")
        assert session.steps[-1].actions == 0 and session.steps[-1].model_calls == len(seen) == 2


def test_route_keeps_encoded_path_delimiters():
    for encoded, other in [("/docs/a%2Fb", "/docs/a/b"), ("/docs/x%23y", "/docs/x")]:
        assert same_route(route(encoded), encoded)
        assert not same_route(encoded, other)


def test_relative_navigation_preserves_double_slash_path():
    from e2e_web.browser import navigation_url
    from e2e_web.cache import app_relative, origin

    url = "https://app.example//section/detail"
    assert navigation_url(app_relative(url, origin(url)), "https://app.example/") == url


def test_original_agent_config_positional_arguments():
    config = AgentConfig(10, 12, 3, 4, "ctx", "agent", None, None, True)
    assert (config.max_actions, config.max_model_calls, config.context, config.name, config.vision) == (
        3, 4, "ctx", "agent", True)


def test_oversize_fixed_params_fail_before_model_call():
    with WebSession(Engine(), name="budget", app_identity="app", agent=AgentConfig(max_input_tokens=1024),
                    executor=FunctionExecutor(brain), cache=CacheConfig(mode="off")) as session:
        with pytest.raises(E2EError, match="STEP_BUDGET_EXHAUSTED"):
            session.agent.act("Apply input", {"value": "x" * 2000})
        assert session.steps[-1].model_calls == 0


def test_template_collision_with_equal_literal():
    assert templates_collide({"unique": "Acme", "fixed": "Acme"}, {"slot": "Acme"})


def test_overflow_retry_reduces_tree_only_observations():
    from e2e_web.session import _shrink_request

    request = {"observation": {"url": "about:blank", "tree": "line\n" * 3000}}
    smaller = _shrink_request(request)["observation"]
    assert smaller["truncated"]
    assert 0 < len(smaller["tree"]) < len(request["observation"]["tree"])


@pytest.mark.parametrize("order", [("b", "a", "c"), ("a", "d", "b", "c")])
def test_screen_update_preserves_current_dom_order(order):
    presenter = ScreenPresenter()
    presenter.present(dict(id="first", nodes=[dict(id=i) for i in "abc"]))
    current = presenter.present(dict(id="next", nodes=[dict(id=i) for i in order]))
    assert [node["id"] for node in current["nodes"]] == list(order)


def test_clicked_button_state_is_evidence():
    from e2e_web.cache import input_targets

    before = view('- button "Details" [expanded=false]')
    after = view('- button "Details" [expanded=true]')
    actions = [dict(op="click", target=target("button", "Details"))]
    assert delta_evidenced(before, after, input_targets(actions), recorded=recording(before, after))


def test_tab_summary_keeps_active_tab_and_latest_popup():
    from types import SimpleNamespace
    from e2e_web.tabs import Tabs

    class Page:
        url = "about:blank"

        def opener(self):
            return None

        def is_closed(self):
            return False

    pages = [Page() for _ in range(20)]
    pages[0].context = SimpleNamespace(pages=pages)
    tabs = Tabs(pages[0], own_initial=True)
    assert [item["tab"] for item in tabs.summary()["tabs"]] == list(range(1, 21))
    pages.extend([Page(), Page()])
    summary = tabs.summary()
    listed = [item["tab"] for item in summary["tabs"]]
    assert summary["tab_count"] == 22 and len(listed) == 20
    assert 1 in listed and 22 in listed


def test_tool_output_budget_preserves_fitting_text_and_bounds_serialized_objects():
    from e2e_web._utils import canonical

    fitting = "字" * 330
    assert bound_output(fitting, max_lines=400, max_bytes=1024) == fitting
    clipped = bound_output({"details": '"\\' * 3000}, max_lines=400, max_bytes=1024)
    assert clipped["truncated"] and "tool output truncated" in clipped["text"]
    assert len(canonical(clipped).encode()) <= 1024


def test_end_baseline_keeps_distinct_records_on_the_same_route():
    before = view('- text "Start"', "https://app.example/start")
    first = view('- text "Order A"', "https://app.example/orders/123")
    second = view('- text "Order B"', "https://app.example/orders/456")
    assert end_baseline([first, second], second["url"], before) is second
