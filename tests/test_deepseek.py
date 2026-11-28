"""Exercise DeepSeek-compatible wire format without external model charges."""

import json

import pytest

from e2e_web import DeepSeekExecutor, E2EError
from e2e_web import models


def test_deepseek_json_options_auth_and_reasoning_response(monkeypatch):
    seen=[]
    class Response:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def read(self,limit):
            return json.dumps({'choices':[{'finish_reason':'stop','message':{
                'content':'{"done":true,"summary":"ready"}', 'reasoning_content':'private reasoning'}}]}).encode()
    def send(request,*,timeout):
        seen.append((request,timeout))
        return Response()
    monkeypatch.setattr(models,'urlopen',send)
    executor=DeepSeekExecutor(model='deepseek-flash',api_key='fake-test-key',thinking=False,
                              request_options={'max_tokens':2048})
    assert executor.complete({'kind':'act','observation':{}},timeout=12)['done'] is True
    request,timeout=seen[0]
    body=json.loads(request.data)
    assert request.full_url=='https://api.deepseek.com/chat/completions'
    assert request.get_header('Authorization')=='Bearer fake-test-key'
    assert body['response_format']=={'type':'json_object'}
    assert body['thinking']=={'type':'disabled'} and body['max_tokens']==2048 and timeout==12
    assert body['model']=='deepseek-flash' and 'JSON' in body['messages'][0]['content']
    with pytest.raises(E2EError,match='does not accept images'):
        executor.complete({'kind':'act','screenshot':'base64'},timeout=12)
    assert len(seen)==1
    executor=DeepSeekExecutor(model='vision-model',api_key='fake-test-key',supports_vision=True,
                              base_url='https://gateway.example/v1/')
    executor.complete({'kind':'act','screenshot':'base64','visual':{'id':'image'}},timeout=12)
    request,_=seen[1]
    assert request.full_url=='https://gateway.example/v1/chat/completions'
    content=json.loads(request.data)['messages'][1]['content']
    assert content[1]['image_url']['url']=='data:image/png;base64,base64'


def test_model_options_cannot_replace_harness_messages():
    with pytest.raises(E2EError,match='cannot override'):
        DeepSeekExecutor(model='model',api_key='fake',request_options={'messages':[]})


@pytest.mark.parametrize(("kind", "explore", "expected"), [
    ("assert", False, '"passed"'), ("extract", False, "schema"), ("assert", True, "scroll_find"),
])
def test_wire_rules_match_decision_kind(monkeypatch, kind, explore, expected):
    from io import BytesIO

    def send(request, *, timeout):
        system = json.loads(request.data)["messages"][0]["content"]
        assert expected in system and "untrusted" in system
        if not explore:
            assert "click_at" not in system and len(system) < len(models.RULES) // 5
        return BytesIO(b'{"choices":[{"message":{"content":"{}"}}]}')

    monkeypatch.setattr(models, "urlopen", send)
    DeepSeekExecutor(model="local", api_key="fake").complete(
        {"kind": kind, "explore": explore, "observation": {}}, timeout=2)


def test_compact_geometry_and_select_options_preserve_evidence():
    bounds = dict(x=-1.5, y=2, width=30, height=15)
    options = [dict(index=i, name=str(i), disabled=i == 1, selected=i == 0) for i in range(32)]
    view = {"nodes": [dict(id="control", bounds=bounds, options=options)]}
    compact = models._model_observation(view, {"control": "n1"})
    node = compact["nodes"][0]
    assert node["bounds"] == [-1.5, 2, 30, 15]
    assert node["options"][:3] == [dict(name="0", selected=True),
                                   dict(name="1", disabled=True), dict(name="2")]
    options[-1]["index"] = 93
    assert models._model_observation(view, {"control": "n1"})["nodes"][0]["options"][-1]["index"] == 93
    assert view["nodes"][0]["bounds"] == bounds and "disabled" in options[0]
    assert len(models.canonical(compact)) < 0.7 * len(models.canonical(view))


def test_partial_native_projection_keeps_frame_failure_and_truncation():
    view = dict(truncated=True, tree='- button "Apply"', nodes=[dict(id='a', name='Apply')],
                frames=[dict(frame=['iframe'], tree='<frame unavailable>')])
    compact = models._model_observation(view, {'a': 'n1'})
    assert 'tree' not in compact and compact['truncated']
    assert compact['frames'] == view['frames'] and compact['nodes'][0]['name'] == 'Apply'


def test_model_nodes_share_frame_paths_and_keep_paths_for_clipped_frame_metadata():
    path = ['iframe >> nth=0', 'iframe >> nth=3']
    nodes = [dict(id=str(i), frame=path) for i in range(20)]
    nodes.append(dict(id='other', frame=['iframe >> nth=1']))
    view = dict(nodes=nodes, frames=[dict(frame=path, tree='')], truncated=True)
    compact = models._model_observation(view, {n['id']: n['id'] for n in nodes})
    for record in compact['nodes'][:-1]:
        assert 'frame' not in record
        assert compact['frames'][record['frame_index']]['frame'] == path
    assert compact['nodes'][-1] == nodes[-1]
    assert len(models.canonical(compact)) < len(models.canonical(view))


def test_input_budget_clips_wire_observation_and_checks_fixed_cost(monkeypatch):
    from io import BytesIO

    sent = []
    def send(request, *, timeout):
        sent.append(json.loads(request.data)["messages"])
        return BytesIO(b'{"choices":[{"message":{"content":"{}"}}]}')

    monkeypatch.setattr(models, 'urlopen', send)
    executor = DeepSeekExecutor(model='local', api_key='fake', max_input_tokens=3400)
    view = dict(id='view', nodes=[dict(id=str(i), name='字段' * 50) for i in range(30)])
    request = dict(kind='assert', observation=view)
    token = models._model_step.set(({}, True))
    try:
        executor.complete(request, timeout=1)
        model_view = json.loads(sent[0][1]['content'])['observation']
        assert model_view['truncated'] and 0 < len(model_view['nodes']) < len(view['nodes'])
        assert 'truncated' not in view and len(view['nodes']) == 30
        assert sum(len(m['content'].encode()) for m in sent[0]) <= 3400
        with pytest.raises(E2EError, match='STEP_BUDGET_EXHAUSTED'):
            executor.complete(dict(request, instruction='字' * 2000), timeout=1)
        assert len(sent) == 1
    finally:
        models._model_step.reset(token)


def test_input_budget_accounts_for_screenshot_dimensions_before_transport(monkeypatch):
    import base64
    executor = DeepSeekExecutor(model='local', api_key='fake', supports_vision=True, max_input_tokens=4000)
    header = b'\x89PNG\r\n\x1a\n\0\0\0\rIHDR' + (2800).to_bytes(4, 'big') * 2
    monkeypatch.setattr(models, 'urlopen', lambda *args, **kwargs: pytest.fail('over-budget request sent'))
    with pytest.raises(E2EError, match='STEP_BUDGET_EXHAUSTED'):
        executor.complete(dict(kind='assert', screenshot=base64.b64encode(header).decode()), timeout=1)


@pytest.mark.parametrize("choice", [None, {"message": None}])
def test_invalid_provider_envelope_reports_model_output_invalid(monkeypatch, choice):
    executor = DeepSeekExecutor(model="local", api_key="fake")
    monkeypatch.setattr(executor, "_post", lambda *args: json.dumps({"choices": [choice]}).encode())
    with pytest.raises(E2EError, match="MODEL_OUTPUT_INVALID"):
        executor.complete({"kind": "act"}, timeout=1)


def test_invalid_optional_usage_does_not_discard_a_valid_decision(monkeypatch):
    executor = DeepSeekExecutor(model="local", api_key="fake", price_per_mtok=(0.5, 1.0))
    response = {"choices": [{"message": {"content": '{"done":true}'}}],
                "usage": {"prompt_tokens": "unknown", "completion_tokens": 100}}
    monkeypatch.setattr(executor, "_post", lambda *args: json.dumps(response).encode())
    metrics = {}
    token = models._model_step.set((metrics, False))
    try:
        assert executor.complete({"kind": "act"}, timeout=1) == {"done": True}
        assert metrics["estimated_cost_usd"] == 0.0001
    finally:
        models._model_step.reset(token)
