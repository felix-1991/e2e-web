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
