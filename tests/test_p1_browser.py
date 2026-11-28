"""P1 browser contracts: live references, replay relocation, pixels and closed DOM."""

import json
import os

import pytest

from e2e_web import AgentConfig, CacheConfig, E2EError, FunctionExecutor, ObservationConfig, ReplayStale, Secret, Web
from e2e_web._utils import canonical

pytestmark = [pytest.mark.browser, pytest.mark.skipif(os.getenv('E2E_WEB_BROWSER_TESTS') != '1', reason='enable browser tests')]


@pytest.fixture
def host():
    with Web() as host:
        yield host


def ref(observation, name):
    node = next(n for n in observation['nodes'] if n['name'] == name)
    return dict(by='ref', value=node['id'], observation=observation['id'])


def latest_model_request(messages):
    """The newest decision payload, with stateful screen updates applied."""
    nodes, request = {}, None
    for message in messages:
        if message.get('role') != 'user' or not isinstance(message.get('content'), str):
            continue
        try:
            payload = json.loads(message['content'])
        except ValueError:
            continue
        if not isinstance(payload, dict) or 'observation' not in payload:
            continue
        observation = payload['observation']
        changes = observation.get('changes')
        if 'nodes' in observation:
            nodes = {node['id']: node for node in observation['nodes']}
        elif changes is not None:
            for node in changes.get('added', []):
                nodes[node['id']] = node
            for item in changes.get('changed', []):
                nodes.setdefault(item['id'], {'id': item['id']}).update(item.get('now') or {})
                for key in item.get("unset", []):
                    nodes[item["id"]].pop(key, None)
            for node_id in changes.get('removed', []):
                nodes.pop(node_id, None)
        prior = request or {}
        history = prior.get("history", []) + payload.get("history", [])
        request = {**prior, **payload, "history": history,
                   "observation": dict(observation, nodes=list(nodes.values()))}
    return request


def test_modal_excludes_background_frames_and_rejects_old_references(host):
    with host.session(name='modal', app_identity='app', cache=CacheConfig(mode='off')) as s:
        s.page.set_content('''<button>Background</button><iframe srcdoc="<button>Background frame</button>"></iframe>
            <dialog id="top"><button>Top action</button><iframe srcdoc="<button>Modal frame</button>"></iframe></dialog>
            <dialog id="lower"><button>Lower action</button></dialog>''')
        s.page.frame_locator('iframe').first.get_by_role('button').wait_for()
        old = s.observe()
        s.page.evaluate('''() => {
            document.querySelector('#lower').showModal();
            document.querySelector('#top').showModal();
            document.activeElement.blur();
        }''')
        with pytest.raises(E2EError, match='NODE_STALE'):
            s.engine.perform(dict(op='click', target=ref(old, 'Background frame')), timeout=2)
        observed = s.observe()
        names = {node['name'] for node in observed['nodes']}
        assert {'Top action', 'Modal frame'} <= names
        assert not names & {'Background', 'Background frame', 'Lower action'}
        s.page.locator('#top').evaluate('el => el.close()')
        assert 'Lower action' in s.observe()['tree']


def test_content_name_uses_visible_label_and_skips_status_and_nested_group(host):
    with host.session(name='names', app_identity='app', cache=CacheConfig(mode='off')) as s:
        s.page.set_content('''<div role="tablist"><button role="tab"><span>Inbox</span>
            <span role="status">3</span><span hidden>Hidden</span></button></div>
            <div role="tree"><div role="treeitem">src<div role="group"><div role="treeitem">Nested</div></div></div></div>
            <button aria-labelledby="override">fallback</button><span id="override" hidden>Named override</span>''')
        observation = s.observe()
        names = {(node['role'], node['name']) for node in observation['nodes']}
        assert {('tab', 'Inbox'), ('status', '3'), ('treeitem', 'src'), ('treeitem', 'Nested'),
                ('button', 'Named override')} <= names
        s.page.get_by_role('tab', name='Inbox', exact=True).click()


def test_agent_switches_popup_and_closes_it_within_one_step(host, tmp_path):
    old_targets = []

    def decide(request, *, timeout):
        observed = request['observation']
        index = len(request['history'])
        if index == 0:
            assert observed['active_tab'] == 1
            old_targets.append(ref(observed, 'Open details'))
            return {'action': dict(op='click', target=old_targets[0])}
        if index == 1:
            assert observed['active_tab'] == 1 and observed['tab_count'] == 2
            return {'action': dict(op='switch_tab', tab=2)}
        if index == 2:
            assert observed['active_tab'] == 2
            with pytest.raises(E2EError, match='NODE_STALE'):
                s.engine.perform(dict(op='click', target=old_targets[0]), timeout=2)
            return {'action': dict(op='click', target=ref(observed, 'Confirm details'))}
        if index == 3:
            return {'action': dict(op='close_tab', tab=2)}
        assert observed['active_tab'] == 1 and observed['tab_count'] == 1
        return {'done': True}

    for replay, changed in [(False, False), (True, False), (True, True)]:
        with host.session(name='tabs', app_identity='app', executor=FunctionExecutor(decide),
                          cache=CacheConfig(directory=tmp_path, mode='read-only' if replay else 'read-write', strict=replay),
                          agent=AgentConfig(timeout=8)) as s:
            s.page.context.route('https://tabs.example/**', lambda route: route.fulfill(
                content_type='text/html',
                body=('<button onclick="opener.document.querySelector(\'p\').textContent = \'Confirmed\'">Confirm details</button>'
                      if route.request.url.endswith('/details') else
                      '<button onclick="window.open(\'/details\', \'_blank\')">Open details</button><p role="status"></p>')))
            s.page.goto('https://tabs.example/')
            if changed:
                s.page.get_by_role('button').evaluate("el => el.onclick = () => window.open('/other')")
                with pytest.raises(ReplayStale, match='tab identity'):
                    s.agent.act('Open details, confirm and close')
                assert s.engine.active_tab == 1
                assert s.page.get_by_role('status').text_content() == ''
                continue
            result = s.agent.act('Open details, confirm and close')
            assert result.actions == 4
            assert result.model_calls == (0 if replay else 5)
            s.expect(s.page.get_by_role('status')).to_have_text('Confirmed')
            assert len(s.page.context.pages) == 1
            with pytest.raises(E2EError, match='last open tab'):
                s.engine.perform(dict(op='close_tab', tab=1), timeout=2)



def test_agent_on_borrowed_page_tracks_only_new_descendants(host):
    with host.session(name='owner', app_identity='app', cache=CacheConfig(mode='off')) as owned:
        original = owned.page
        other = original.context.new_page()
        original.set_content('<button onclick="window.open(\'about:blank\')">Open</button>')
        with Web.attach(original, name='borrow', app_identity='app', cache=CacheConfig(mode='off')) as s:
            with original.expect_popup() as event:
                original.get_by_role('button', name='Open').click()
            popup = event.value
            assert [tab['tab'] for tab in s.observe()['tabs']] == [1, 2]
            s.engine.perform({'op': 'switch_tab', 'tab': 2}, timeout=2)
            assert s.page is popup
            with pytest.raises(E2EError, match='caller owns'):
                s.engine.perform({'op': 'close_tab', 'tab': 1}, timeout=2)
        assert not any(page.is_closed() for page in (original, other, popup))


def test_closed_roots_refs_and_secret_authorization(host):
    def decide(request, *, timeout):
        if not request['history']:
            return {'action': dict(op='fill_secret', target=ref(request['observation'], 'Password'), secret='password')}
        return {'done': True}

    with host.session(name='closed', app_identity='app', executor=FunctionExecutor(decide), cache=CacheConfig(mode='off')) as s:
        s.page.set_content('''<div id="outer"></div><script>
            const outer = document.querySelector('#outer').attachShadow({mode:'closed'});
            outer.innerHTML='<div id="inner"></div>';
            const inner=outer.querySelector('#inner').attachShadow({mode:'closed'});
            inner.innerHTML='<label>Password<input type="password"></label><button>Save</button>';
            inner.querySelector('button').onclick=()=>document.querySelector('#outer').setAttribute('saved','true');
            window.secretInput = inner.querySelector('input');
        </script>''')
        assert s.page.locator('#outer').evaluate('el=>el.shadowRoot') is None
        s.agent.act('Fill closed password', {'password': Secret('password', 'private-closed-pass', purpose='password')})
        assert s.page.evaluate('window.secretInput.value') == 'private-closed-pass'
        observation = s.observe()
        assert 'private-closed-pass' not in json.dumps(observation)
        s.engine.perform(dict(op='click', target=ref(observation, 'Save')), timeout=3)
        s.expect(s.page.locator('#outer')).to_have_attribute('saved', 'true')


def test_stable_ids_but_stale_observation_and_detached_node_are_rejected(host):
    with host.session(name='refs', app_identity='app', cache=CacheConfig(mode='off')) as s:
        s.page.set_content('<button>Save</button>')
        first, second = s.observe(), s.observe()
        assert first['nodes'][0]['id'] == second['nodes'][0]['id']
        assert first['id'] != second['id']
        with pytest.raises(E2EError, match='NODE_STALE'):
            s.engine.perform(dict(op='click', target=ref(first, 'Save')), timeout=3)
        s.page.locator('button').evaluate('el => el.outerHTML="<button>Save</button>"')
        with pytest.raises(E2EError, match='NODE_STALE'):
            s.engine.perform(dict(op='click', target=ref(second, 'Save')), timeout=3)


def test_ref_recording_relocates_after_dom_reorder_and_refuses_ambiguity(host, tmp_path):
    calls=[]
    def decide(request, *, timeout):
        calls.append(request)
        return {'action':dict(op='click', target=ref(request['observation'], 'Save'))} if not request['history'] else {'done':True}

    options=dict(name='relocation', app_identity='app', executor=FunctionExecutor(decide),
                 cache=CacheConfig(directory=tmp_path/'cache', mode='read-write'))
    for markup in ('<div><button id="old">Save</button></div>', '<p>New unrelated content</p><section><button id="new">Save</button></section>'):
        with host.session(**options) as s:
            s.page.set_content(markup + '<p role="status"></p><script>document.querySelector("button").onclick=()=>document.querySelector("[role=status]").textContent="Saved"</script>')
            result=s.agent.act('Save changes')
            s.expect(s.page.get_by_role('status')).to_have_text('Saved')
    assert result.model_calls == 0 and len(calls) == 2
    trace=json.loads(next((tmp_path/'cache').glob('*.json')).read_text())
    assert trace['actions'][0]['target']['by'] == 'semantic'
    with host.session(**options) as s:
        s.page.set_content('<button>Save</button><button>Save</button>')
        with pytest.raises(E2EError, match='REPLAY_STALE'):
            s.agent.act('Save changes')


@pytest.mark.parametrize('op', ['fill', 'type', 'press'])
def test_one_time_code_input_is_masked_and_never_recorded(host, tmp_path, op):
    events = []

    def decide(request, *, timeout):
        if not request['history']:
            return {'action': dict(op=op, target=ref(request['observation'], 'Code'), value='4')}
        assert request['history'][0]['action']['value'] == '<masked>'
        code = next(node for node in request['observation']['nodes'] if node['name'] == 'Code')
        assert code['value'] == '<masked>'
        return {'done': True}

    with host.session(name='otp', app_identity='app', executor=FunctionExecutor(decide),
                      agent=AgentConfig(on_before_action=events.append, on_action=events.append),
                      cache=CacheConfig(directory=tmp_path, mode='read-write')) as s:
        s.page.set_content('''<label>Code <input autocomplete="section-login one-time-code"></label>
            <p role="status"></p><script>
            document.querySelector('input').oninput = () => document.querySelector('p').textContent = 'Entered';
            </script>''')
        s.agent.act('Enter the test code')
        s.expect(s.page.get_by_role('status')).to_have_text('Entered')
        s.expect(s.page.get_by_label('Code')).to_have_value('4')

    assert [event['action']['value'] for event in events] == ['<masked>', '<masked>']
    assert not list(tmp_path.glob('*.json'))


def test_replay_refuses_cached_text_when_target_becomes_one_time_code(host, tmp_path):
    def decide(request, *, timeout):
        if request['history']:
            return {'done': True}
        return {'action': dict(op='fill', target=ref(request['observation'], 'Code'), value='123456')}

    cache = CacheConfig(directory=tmp_path, mode='read-write')
    with host.session(name='otp', app_identity='app', executor=FunctionExecutor(decide), cache=cache) as s:
        s.page.set_content('<label>Code <input></label>')
        s.agent.act('Enter the test code')
        s.expect(s.page.get_by_label('Code')).to_have_value('123456')
    assert len(list(tmp_path.glob('*.json'))) == 1

    strict = CacheConfig(directory=tmp_path, strict=True)
    with host.session(name='otp', app_identity='app', cache=strict) as s:
        s.page.set_content('<label>Code <input autocomplete="one-time-code"></label>')
        with pytest.raises(ReplayStale, match='one-time-code'):
            s.agent.act('Enter the test code')
        assert s.page.get_by_label('Code').input_value() == ''


def test_observation_limits_and_offscreen_scroll_to(host):
    limits=ObservationConfig(max_nodes=12,max_elements=80,max_bytes=4096)
    with host.session(name='budget', app_identity='app', observation=limits, cache=CacheConfig(mode='off')) as s:
        s.page.set_content(''.join(f'<button>{i} '+('x'*500)+'</button>' for i in range(100)))
        observation=s.observe()
        assert len(canonical(observation).encode()) <= 4096
        assert len(observation['nodes']) <= 12 and observation['visited'] <= 80 and observation['truncated']
    with host.session(name='scroll', app_identity='app', cache=CacheConfig(mode='off')) as s:
        s.page.set_content('<div style="height:2500px"></div><button>Bottom</button>')
        observation=s.observe()
        assert next(n for n in observation['nodes'] if n['name']=='Bottom')['in_viewport'] is False
        s.engine.perform(dict(op='scroll_to', target=ref(observation, 'Bottom')), timeout=3)
        assert s.page.evaluate('scrollY') > 1000


def test_virtualized_scroll_search(host):
    with host.session(name='virtual', app_identity='app', cache=CacheConfig(mode='off')) as s:
        s.page.set_content('''<div style="height:3000px"></div><script>
          addEventListener('scroll',()=>{if(scrollY>400 && !document.querySelector('button')){
            const b=document.createElement('button'); b.textContent='Loaded row'; document.body.append(b);
          }});
        </script>''')
        s.engine.perform(dict(op='scroll_find', target=dict(by='semantic',value='button',name='Loaded row'), direction='down',steps=5),timeout=4)
        assert s.page.get_by_role('button',name='Loaded row').is_visible()


def test_scroll_find_waits_for_a_list_that_appends_at_its_end(host):
    with host.session(name='append-list', app_identity='app', cache=CacheConfig(mode='off')) as s:
        s.page.set_content('''<div id="results" aria-label="Results" style="height:100px;overflow-y:scroll">
            <div style="height:260px"></div></div><script>
            const results = document.querySelector('#results');
            results.addEventListener('scroll', () => {
                if (results.scrollTop < results.scrollHeight - results.clientHeight) return;
                setTimeout(() => results.insertAdjacentHTML('beforeend', '<button>Appended row</button>'), 300);
            });
            </script>''')
        s.engine.perform(dict(op='scroll_find', target=dict(by='role', value='button', name='Appended row'),
                              container=dict(by='css', value='#results'), direction='down', steps=30), timeout=4)
        assert s.page.get_by_role('button', name='Appended row').is_visible()


@pytest.mark.parametrize(('op', 'event'), [('click_at', 'click'), ('hover_at', 'pointerenter')])
def test_coordinate_action_replays_only_on_matching_pixels(host, tmp_path, op, event):
    def decide(request, *, timeout):
        if request['history']:
            return {'done':True}
        node=next(n for n in request['observation']['nodes'] if n['role']=='canvas')
        b=node['bounds']
        return {'action':dict(op=op,x=int(b['x']+20),y=int(b['y']+20),screenshot=request['visual']['id'])}

    for replay, changed in [(False, False), (True, False), (True, True)]:
        with host.session(name='canvas',app_identity='app',executor=FunctionExecutor(decide),
                          cache=CacheConfig(directory=tmp_path/'cache',mode='read-only' if replay else 'read-write', strict=replay)) as s:
            s.page.set_content('''<canvas width="200" height="100" style="background:gray"></canvas>
                <p role="status"></p>''')
            s.page.locator('canvas').evaluate('''(el, event) => {
                const status = document.querySelector('p');
                el.addEventListener(event, () => status.textContent = event);
                if (event !== 'click') el.addEventListener('click', () => status.textContent = 'unexpected click');
            }''', event)
            if changed:
                s.page.locator('canvas').evaluate("el => el.style.background='red'")
                with pytest.raises(ReplayStale, match='pixels'):
                    s.agent.act('Interact with the canvas', vision=True)
                assert s.page.get_by_role('status').text_content() == ''
                continue
            result = s.agent.act('Interact with the canvas',vision=True)
            assert result.model_calls == (0 if replay else 2)
            s.expect(s.page.get_by_role('status')).to_have_text(event)



def test_agent_resize_viewport_updates_responsive_observation_and_invalidates_pixels(host, tmp_path):
    narrow = {'width': 390, 'height': 640}
    wide = {'width': 1000, 'height': 640}

    def decide(request, *, timeout):
        if not request['history']:
            assert request['observation']['viewport'] == wide
            assert 'Desktop menu' in request['observation']['tree']
            return {'action': dict(op='resize_viewport', **narrow)}
        assert request['history'][0]['result'] == narrow
        assert request['observation']['viewport'] == narrow
        assert 'Mobile menu' in request['observation']['tree']
        assert 'Desktop menu' not in request['observation']['tree']
        return {'done': True}

    with host.session(name='resize', app_identity='app', executor=FunctionExecutor(decide),
                      context_options={'viewport': wide},
                      cache=CacheConfig(directory=tmp_path, mode='read-write')) as s:
        s.page.set_content('''<style>
            #mobile { display: none }
            @media (max-width: 500px) { #mobile { display: initial } #desktop { display: none } }
            </style><button id="desktop">Desktop menu</button><button id="mobile">Mobile menu</button>''')
        _, visual = s.engine.capture_vision(timeout=3)
        result = s.agent.act('Check the menu at a narrow viewport')
        assert result.actions == 1
        with pytest.raises(E2EError, match='NODE_STALE'):
            s.engine.perform(dict(op='hover_at', x=15, y=15, screenshot=visual['id']), timeout=3)
        s.expect(s.page.get_by_role('button', name='Mobile menu')).to_be_visible()
    assert not list(tmp_path.glob('*.json'))


def test_visual_staleness_bounds_drag_and_type(host):
    with host.session(name='pixels',app_identity='app',cache=CacheConfig(mode='off')) as s:
        s.page.set_content('<input aria-label="Text" style="position:absolute;left:10px;top:10px;width:200px;height:30px"><p>Initial</p>')
        _, visual=s.engine.capture_vision(timeout=3)
        s.page.locator('p').evaluate('el=>el.textContent="Changed"')
        with pytest.raises(E2EError,match='NODE_STALE'):
            s.engine.perform(dict(op='hover_at',x=20,y=20,screenshot=visual['id']),timeout=3)
        _, visual=s.engine.capture_vision(timeout=3)
        # A point one pixel past the captured edge is clamped onto the image,
        # as upstream does, instead of failing the whole step.
        s.engine.perform(dict(op='click_at',x=visual['width'],y=0,screenshot=visual['id']),timeout=3)
        _, visual=s.engine.capture_vision(timeout=3)
        box = s.page.get_by_label('Text').bounding_box()
        scale = visual['scale']
        s.engine.perform(dict(op='type_at', x=int((box['x'] + box['width'] / 2) * scale),
                              y=int((box['y'] + box['height'] / 2) * scale),
                              value='typed text', screenshot=visual['id']), timeout=3)
        s.expect(s.page.get_by_label('Text')).to_have_value('typed text')
        s.page.evaluate('window.drags=0; addEventListener("pointermove",e=>{if(e.buttons) window.drags++})')
        _, visual=s.engine.capture_vision(timeout=3)
        s.engine.perform(dict(op='drag_at',x=300,y=150,to_x=400,to_y=200,screenshot=visual['id']),timeout=3)
        assert s.page.evaluate('window.drags') > 0


def test_on_demand_screenshot_supports_coordinate_controls_in_iframe(host, tmp_path):
    def decide(request, *, timeout):
        index = len(request['history'])
        if index == 0:
            assert request['vision_mode'] == 'on_demand' and 'screenshot' not in request
            return {'action': {'op': 'screenshot'}}
        assert request['screenshot'] and request['visual']['id']
        if index == 4:
            return {'done': True}
        op, locator, value = [('type_at', 'input', 'new'), ('press_at', 'input', 'Enter'),
                              ('select_at', 'select', 'South')][index - 1]
        box = s.page.frame_locator('iframe').locator(locator).bounding_box()
        scale = request['visual']['scale']
        action = dict(op=op, x=int((box['x'] + box['width'] / 2) * scale),
                      y=int((box['y'] + box['height'] / 2) * scale),
                      screenshot=request['visual']['id'], value=value)
        if op == 'type_at':
            action['replace'] = True
        return {'action': action}

    for replay in (False, True):
        with host.session(name='point-controls', app_identity='app', executor=FunctionExecutor(decide),
                          cache=CacheConfig(directory=tmp_path, mode='read-only' if replay else 'read-write', strict=replay)) as s:
            s.page.set_content('''<iframe srcdoc="<input value='old'><select><option>North</option>
                <option>South</option></select><output></output>"></iframe>''')
            frame = s.page.frame_locator('iframe')
            frame.locator('input').evaluate('''el => el.onkeydown = e => {
                if (e.key === 'Enter') document.querySelector('output').value = el.value;
            }''')
            result = s.agent.act('Replace the field, press Enter and select South', vision='on_demand')
            assert result.actions == 3 and result.model_calls == (0 if replay else 5)
            s.expect(frame.locator('output')).to_have_text('new')
            s.expect(frame.locator('select')).to_have_value('South')



def test_on_demand_screenshot_refuses_registered_secret(host):
    model = FunctionExecutor(lambda request, **_: {'action': {'op': 'screenshot'}})
    with host.session(name='private', app_identity='app', executor=model, cache=CacheConfig(mode='off'),
                      agent=AgentConfig(vision='on_demand')) as s:
        with pytest.raises(E2EError, match='POLICY_DENIED'):
            s.agent.act('Request a screenshot', {'password': Secret('password', 'not-for-the-model')})
        assert s.steps[-1].actions == 0


def test_exploring_assertion_combines_viewports_and_refuses_mutation(host):
    def decide(request, *, timeout):
        assert request['kind'] == 'assert' and request['explore'] and 'prior_steps' not in request
        observed = request['observation']
        visible = {node['name'] for node in observed['nodes'] if node['in_viewport']}
        if not request['history']:
            assert 'Start' in visible and 'Finish' not in visible
            return {'action': dict(op='scroll_to', target=ref(observed, 'Finish'))}
        assert 'Finish' in visible and 'Start' not in visible
        assert any(node['name'] == 'Start' for node in request['history'][0]['visible_nodes'])
        return {'passed': True, 'reason': 'Observed both sections'}

    with host.session(name='explore', app_identity='app', executor=FunctionExecutor(decide),
                      cache=CacheConfig(mode='off')) as s:
        s.page.set_content('<h1>Start</h1><div style="height:2100px"></div><button>Finish</button>')
        s.agent.assert_('Start and Finish both exist', explore=True, max_actions=1)
        assert s.steps[-1].actions == 1
        s.executor = FunctionExecutor(lambda request, **_: {'action': dict(op='click', target=ref(request['observation'], 'Finish'))})
        with pytest.raises(E2EError, match='POLICY_DENIED'):
            s.agent.assert_('Check the finish control', explore=True)
        assert s.steps[-1].actions == 0


def test_agent_observes_delayed_action_effect_before_next_decision(host):
    def decide(request, *, timeout):
        if not request['history']:
            return {'action': dict(op='click', target=ref(request['observation'], 'Save'))}
        assert 'Saved asynchronously' in request['observation']['tree']
        return {'done': True}

    with host.session(name='settle', app_identity='app', executor=FunctionExecutor(decide), cache=CacheConfig(mode='off')) as s:
        s.page.set_content('''<button onclick="setTimeout(() => document.querySelector('p').textContent = 'Saved asynchronously', 160)">Save</button>
            <p role="status"></p>''')
        s.agent.act('Save and confirm')
        assert s.steps[-1].model_calls == 2


def test_act_remembers_confirm_target_after_picker_closes(host):
    def decide(request, *, timeout):
        observed = request['observation']
        if not request['history']:
            return {'action': dict(op='click', target=ref(observed, 'Apply'))}
        assert request['history'][-1]['observed_target'] == {'role': 'button', 'name': 'Apply'}
        assert all(node['name'] != 'Apply' for node in observed['nodes'])
        assert any(node['role'] == 'link' and node['name'] == 'Item 42' for node in observed['nodes'])
        return {'done': True}

    with host.session(name='picker', app_identity='app', executor=FunctionExecutor(decide),
                      cache=CacheConfig(mode='off')) as session:
        session.page.set_content('''<div><label><input type="checkbox" checked>Item 42</label>
            <button onclick="this.parentElement.remove(); document.querySelector('a').hidden=false">Apply</button>
            </div><a hidden href="#details">Item 42</a>''')
        assert session.agent.act('Apply the selection and stop on the results').actions == 1


def test_repeated_keys_follow_focus_and_text_selection_is_secret_safe(host):
    def decide(request, *, timeout):
        if not request['history']:
            return {'action': dict(op='press', target=ref(request['observation'], 'First'), value='Tab', times=2)}
        return {'done': True}

    with host.session(name='keyboard', app_identity='app', executor=FunctionExecutor(decide),
                      cache=CacheConfig(mode='off')) as s:
        s.page.set_content('<input aria-label="First"><input aria-label="Second"><input aria-label="Third" value="selection">')
        with pytest.raises(E2EError, match='STEP_BUDGET_EXHAUSTED'):
            s.agent.act('Move keyboard focus', max_actions=1)
        assert s.steps[-1].actions == 0
        result = s.agent.act('Move keyboard focus', max_actions=2)
        assert result.actions == 2
        s.expect(s.page.get_by_label('Third')).to_be_focused()
        s.page.get_by_label('Third').evaluate('el => el.setSelectionRange(0, 3)')
        assert any(node.get('selection') == 'sel' for node in s.observe()['nodes'])
        s.agent.act('Register a private test value', {'secret': Secret('secret', 'selection')}, max_actions=2)
        s.page.get_by_label('Third').evaluate('el => el.setSelectionRange(0, 3)')
        assert all('selection' not in node for node in s.observe()['nodes'])


def test_agent_refreshes_stale_ref_without_repeating_dispatched_action(host):
    calls=[]
    session=None
    def decide(request, *, timeout):
        calls.append(request)
        if any('action' in event for event in request['history']):
            return {'done':True}
        if len(calls)==1:
            descriptor=ref(request['observation'],'Save')
            session.page.locator('button').evaluate("el=>{el.textContent='Save again'}")
        else:
            descriptor = ref(request['observation'], 'Save again')
        return {'action':dict(op='click',target=descriptor)}
    with host.session(name='stale-refresh',app_identity='app',executor=FunctionExecutor(decide),cache=CacheConfig(mode='off')) as session:
        session.page.set_content('<button onclick="document.querySelector(\'p\').textContent=\'Saved\'">Save</button><p role="status"></p>')
        session.agent.act('Save once')
        session.expect(session.page.get_by_role('status')).to_have_text('Saved')
        assert calls[1]['history'][0]['dispatched'] is False
        assert len(calls)==3


def test_reference_inside_iframe_and_coordinate_requires_current_vision(host):
    with host.session(name='frame-reference',app_identity='app',cache=CacheConfig(mode='off')) as s:
        s.page.set_content('<iframe srcdoc="<button>Inside</button>"></iframe>')
        s.page.frame_locator('iframe').get_by_role('button').wait_for()
        observation=s.observe()
        s.engine.perform(dict(op='click',target=ref(observation,'Inside')),timeout=3)
        _,visual=s.engine.capture_vision(timeout=3)
        s.executor=FunctionExecutor(lambda request,**kw: {'action':dict(op='click_at',x=1,y=1,screenshot=visual['id'])})
        with pytest.raises(E2EError,match='POLICY_DENIED'):
            s.agent.act('Coordinates without vision')


def test_iframe_relocation_checks_frame_identity(host):
    with host.session(name='frame-identity',app_identity='app',cache=CacheConfig(mode='off')) as s:
        s.page.set_content('<iframe id="first" srcdoc="<button>Save</button>"></iframe><iframe id="second" srcdoc="<button>Save</button>"></iframe>')
        s.page.frame_locator('#second').get_by_role('button').wait_for()
        observation=s.observe()
        first=next(n for n in observation['nodes'] if n['name']=='Save')
        action=s.engine.record_action(dict(op='click',target=dict(by='ref',value=first['id'],observation=observation['id'])))
        assert action['target']['frame_identity'][0]['id']=='first'
        s.page.locator('#second').evaluate('el=>document.body.prepend(el)')
        with pytest.raises(E2EError,match='NODE_STALE'):
            s.engine.perform(action,timeout=3)


def test_closed_tracking_precedes_caller_bootstrap_script(host):
    bootstrap="window.bootHost=document.createElement('div');window.bootHost.attachShadow({mode:'closed'}).innerHTML='<button>Boot button</button>';"
    with host.session(name='bootstrap',app_identity='app',init_scripts=(bootstrap,),cache=CacheConfig(mode='off')) as s:
        s.page.set_content('<body></body>')
        s.page.evaluate('document.body.append(window.bootHost)')
        assert any(n['name']=='Boot button' for n in s.observe()['nodes'])


def test_semantic_tree_discovers_icon_and_exposes_control_states(host):
    def decide(request, *, timeout):
        observation = request['observation']
        icon = next(node for node in observation['nodes'] if node['test_id'] == 'toolbar-icon')
        return {'action': dict(op='click', target=dict(by='ref', value=icon['id'], observation=observation['id']))}

    with host.session(name='semantics', app_identity='app', executor=FunctionExecutor(decide),
                      cache=CacheConfig(mode='off')) as session:
        page = session.page
        page.set_content("""
            <div data-testid="toolbar-icon" onclick="this.dataset.clicked='yes'">
              <svg width="24" height="24"></svg>
            </div>
            <button aria-expanded="true" aria-pressed="false">Filters</button>
            <div role="checkbox" aria-checked="mixed">Select all</div>
            <label>Read only <input readonly></label>
            <label>Native check <input type="checkbox" checked aria-checked="false"></label>
            <script>document.querySelector('[type=checkbox]').indeterminate = true;</script>
        """)
        nodes = {node['name']: node for node in session.observe()['nodes']}
        assert nodes['Filters']['expanded'] is True
        assert nodes['Filters']['pressed'] is False
        assert nodes['Select all']['indeterminate'] is True
        assert nodes['Native check']['checked'] and nodes['Native check']['indeterminate']
        assert nodes['Read only']['readonly'] and not nodes['Read only']['editable']
        session.agent.act_once('Use the toolbar icon')
        session.expect(page.get_by_test_id('toolbar-icon')).to_have_attribute('data-clicked', 'yes')


def test_ref_uses_row_context_and_replays_after_reordering(host, tmp_path):
    def decide(request, *, timeout):
        observation = request['observation']
        nodes = {node['id']: node for node in observation['nodes']}
        row = next(node for node in nodes.values() if node['role'] == 'row' and node['name'] == 'Lin Edit')
        edit = next(node for node in nodes.values() if node['role'] == 'button'
                    and nodes[node['parent_id']]['parent_id'] == row['id'])
        return {'action': dict(op='click', target=dict(by='ref', value=edit['id'], observation=observation['id']))}

    def markup(names):
        rows = ''.join(
            f'<tr><td>{name}</td><td><button onclick="document.querySelector(\'p\').textContent='
            'this.closest(\'tr\').cells[0].textContent">Edit</button></td></tr>'
            for name in names
        )
        return f'<table aria-label="Accounts">{rows}</table><p role="status"></p>'

    options = dict(name='row-context', app_identity='app', executor=FunctionExecutor(decide))
    with host.session(**options, cache=CacheConfig(directory=tmp_path, mode='read-write')) as session:
        session.page.set_content(markup(['Ada', 'Lin']))
        session.agent.act_once('Edit Lin')
        session.expect(session.page.get_by_role('status')).to_have_text('Lin')
    recorded = json.loads(next(tmp_path.glob('*.json')).read_text())['actions'][0]
    assert recorded['target']['within'] == {'role': 'row', 'name': 'Lin Edit'}

    with host.session(**options, cache=CacheConfig(directory=tmp_path, strict=True)) as session:
        session.page.set_content(markup(['Lin', 'Ada']))
        assert session.agent.act_once('Edit Lin').model_calls == 0
        session.expect(session.page.get_by_role('status')).to_have_text('Lin')

        session.page.set_content(markup(['Lin', 'Ada']))
        observation = session.observe()
        action = decide({'observation': observation}, timeout=3)['action']
        session.page.get_by_role('cell', name='Lin', exact=True).evaluate("el => el.textContent = 'Recycled'")
        with pytest.raises(E2EError, match='NODE_STALE'):
            session.engine.perform(action, timeout=3)
        assert session.page.get_by_role('status').text_content() == ''


@pytest.mark.parametrize('by', ['role', 'semantic'])
def test_scroll_find_is_scoped_to_the_ref_container_in_an_iframe(host, by, tmp_path):
    def decide(request, *, timeout):
        observation = request['observation']
        target = dict(by=by, value='button', name='Virtual item')
        if by == 'semantic':
            target.update(frame='#items-frame', within={'role': 'generic', 'name': 'Results'})
        return {'action': dict(op='scroll_find', target=target, container=ref(observation, 'Results'),
                               direction='right', steps=4)}

    for mode, replay in [('read-write', False), ('read-only', True)]:
        with host.session(name='scoped-scroll', app_identity='app', executor=FunctionExecutor(decide),
                          cache=CacheConfig(directory=tmp_path, mode=mode, strict=replay)) as session:
            page = session.page
            page.set_content('<iframe id="items-frame" style="width:500px;height:250px"></iframe>')
            frame = page.locator('iframe').element_handle().content_frame()
            frame.set_content("""
                <button>Virtual item</button>
                <div id="results" aria-label="Results" style="width:220px;overflow-x:auto">
                  <div style="width:700px;height:28px;position:relative"></div>
                </div>
                <script>
                  const results = document.querySelector('#results');
                  results.addEventListener('scroll', () => {
                    if (results.scrollLeft > 100 && !results.querySelector('button')) {
                      results.firstElementChild.innerHTML =
                        '<button style="position:absolute;left:240px">Virtual item</button>';
                    }
                  });
                </script>
            """)
            container = next(node for node in session.observe()['nodes'] if node['name'] == 'Results')
            assert container['scrollable'] == 'x'
            result = session.agent.act_once('Find the virtual item in Results')
            assert result.model_calls == (0 if replay else 1)
            assert frame.locator('#results').evaluate('el => el.scrollLeft') > 100
            session.expect(frame.locator('#results').get_by_role('button', name='Virtual item')).to_be_visible()
            observation = session.observe()
            session.engine.perform(dict(op='scroll', target=ref(observation, 'Results'), x=-700, y=0), timeout=3)
            assert frame.locator('#results').evaluate('el => el.scrollLeft') == 0


def test_targeted_mouse_actions_and_file_chooser_upload(host, tmp_path):
    actions = [
        dict(op='right_click', label='Mouse', modifiers=['Shift']),
        dict(op='double_click', label='Mouse', modifiers=['Alt']),
        dict(op='long_press', label='Mouse'),
    ]
    path = tmp_path / 'upload.txt'
    path.write_text('upload')
    actions.append(dict(op='upload', label='Choose file', value=[str(path)]))
    by_instruction = {action['op']: action for action in actions}

    def decide(request, *, timeout):
        action = by_instruction[request['instruction']].copy()
        action['target'] = ref(request['observation'], action.pop('label'))
        return {'action': action}

    with host.session(name='node-actions', app_identity='app', executor=FunctionExecutor(decide),
                      upload_roots=(tmp_path,), cache=CacheConfig(mode='off')) as session:
        session.page.set_content("""
            <button>Mouse</button><input type="file" hidden>
            <button onclick="document.querySelector('input').click()">Choose file</button>
            <script>
              window.events = [];
              const mouse = document.querySelector('button');
              let down;
              mouse.oncontextmenu = e => {e.preventDefault(); events.push(['right', e.shiftKey]);};
              mouse.ondblclick = e => events.push(['double', e.altKey]);
              mouse.onmousedown = e => { if (e.button === 0) down = performance.now(); };
              mouse.onmouseup = e => { if (e.button === 0 && performance.now() - down > 400) events.push(['hold']); };
            </script>
        """)
        for instruction in by_instruction:
            session.agent.act_once(instruction)
        assert session.page.evaluate('events') == [['right', True], ['double', True], ['hold']]
        assert session.page.locator('input').evaluate('el => el.files[0].name') == 'upload.txt'


def test_native_multi_select_sets_a_complete_batch_atomically(host):
    def decide(request, *, timeout):
        return {'action': dict(op='select', target=ref(request['observation'], request['params']['field']),
                               value=request['params']['choices'])}

    with host.session(name='multi-select', app_identity='app', executor=FunctionExecutor(decide),
                      cache=CacheConfig(mode='off')) as session:
        page = session.page
        page.set_content("""
            <label>Languages <select multiple>
              <option value="en" selected>English</option>
              <option value="fr">French</option>
              <option value="dup0">Other</option>
              <option value="dup1">Other</option>
              <optgroup disabled label="Unavailable"><option value="off">Disabled</option></optgroup>
            </select></label>
            <label>Region <select><option value="North">West</option><option value="S">North</option></select></label>
            <script>
              window.changes = 0;
              document.querySelector('select').onchange = () => changes++;
            </script>
        """)

        def select(choices, field='Languages'):
            session.agent.act_once('Choose the requested options', {'field': field, 'choices': choices})

        languages = page.get_by_label('Languages')
        node = next(node for node in session.observe()['nodes'] if node['name'] == 'Languages')
        assert node['multiple'] and [option['index'] for option in node['options']] == list(range(5))
        select(['French', {'value': 'dup0'}, {'index': 3}])
        session.expect(languages).to_have_values(['fr', 'dup0', 'dup1'])
        assert page.evaluate('changes') == 1

        for invalid, code in [
            (['English', 'Missing'], 'LOCATOR_NOT_FOUND'),
            (['English', 'Other'], 'LOCATOR_AMBIGUOUS'),
            (['English', 'Disabled'], 'ACTION_FAILED'),
        ]:
            with pytest.raises(E2EError, match=code):
                select(invalid)
            session.expect(languages).to_have_values(['fr', 'dup0', 'dup1'])
            assert page.evaluate('changes') == 1

        select([])
        session.expect(languages).to_have_values([])
        select('North', 'Region')
        session.expect(page.get_by_label('Region')).to_have_value('S')
        select({'value': 'North'}, 'Region')
        session.expect(page.get_by_label('Region')).to_have_value('North')
        with pytest.raises(E2EError, match='ACTION_FAILED'):
            select(['North'], 'Region')
        session.expect(page.get_by_label('Region')).to_have_value('North')


def test_keyboard_follows_focus_into_a_shadow_combobox_inside_iframe(host, tmp_path):
    def decide(request, *, timeout):
        observation = request['observation']
        index = len(request['history'])
        if index == 0:
            return {'action': dict(op='click', target=ref(observation, 'City'))}
        if index == 1:
            return {'action': dict(op='type', value='Pa', replace=True)}
        if index == 2:
            return {'action': dict(op='type', value='r')}
        if index == 3:
            return {'action': dict(op='press', value='ArrowDown')}
        if index == 4:
            combo = next(node for node in observation['nodes'] if node['name'] == 'City')
            assert combo['focused'] and combo['active_descendant'] == 'Paris'
            return {'action': dict(op='press', value='Enter')}
        return {'done': True}

    with host.session(name='focused-combo', app_identity='app', executor=FunctionExecutor(decide),
                      cache=CacheConfig(directory=tmp_path, mode='read-write')) as session:
        session.page.set_content('<input aria-label="Outside" value="outside"><iframe></iframe>')
        frame = session.page.locator('iframe').element_handle().content_frame()
        frame.set_content('<div id="widget"></div><output></output><input aria-label="Read only" readonly>')
        session.observe()
        frame.evaluate("""() => {
            const root = document.querySelector('#widget').attachShadow({mode:'closed'});
            root.innerHTML = `<input aria-label="City" role="combobox" value="old">
                <div role="listbox"><div role="option" id="paris">Paris</div></div>`;
            const combo = root.querySelector('input');
            combo.onkeydown = event => {
                if (combo.value !== 'Par') return;
                if (event.key === 'ArrowDown') combo.setAttribute('aria-activedescendant', 'paris');
                if (event.key === 'Enter' && combo.getAttribute('aria-activedescendant') === 'paris') {
                    document.querySelector('output').value = 'Paris';
                }
            };
        }""")
        result = session.agent.act('Choose Paris from the city combobox')
        assert result.actions == 5
        session.expect(frame.locator('output')).to_have_text('Paris')
        session.expect(session.page.get_by_label('Outside')).to_have_value('outside')

        frame.get_by_label('Read only').focus()
        with pytest.raises(E2EError, match='ACTION_FAILED'):
            session.engine.perform(dict(op='type', value='ignored'), timeout=2)
        session.expect(frame.get_by_label('Read only')).to_have_value('')
    assert not list(tmp_path.glob('*.json'))


def test_delayed_switch_state_is_polled_after_one_click(host):
    def decide(request, *, timeout):
        return {'action': dict(op=request['instruction'], target=ref(request['observation'], 'Notifications'))}

    with host.session(name='async-switch', app_identity='app', executor=FunctionExecutor(decide),
                      cache=CacheConfig(mode='off')) as session:
        page = session.page
        page.set_content("""
            <button role="switch" aria-checked="false">Notifications</button>
            <script>
              window.clicks = 0;
              document.querySelector('button').onclick = event => {
                clicks++;
                const toggle = event.currentTarget;
                setTimeout(() => toggle.setAttribute('aria-checked', toggle.getAttribute('aria-checked') !== 'true'), 160);
              };
            </script>
        """)
        switch = page.get_by_role('switch')
        session.agent.act_once('check')
        assert switch.get_attribute('aria-checked') == 'true'
        session.agent.act_once('check')
        assert page.evaluate('clicks') == 1
        session.agent.act_once('uncheck')
        assert switch.get_attribute('aria-checked') == 'false'
        assert page.evaluate('clicks') == 2


@pytest.mark.parametrize("keep_repeating", [False, True])
def test_compact_model_fill_waits_for_candidates_and_counts_tokens(host, monkeypatch, keep_repeating):
    from io import BytesIO
    from e2e_web import DeepSeekExecutor, models

    views = []

    def provider(request, *, timeout):
        messages = json.loads(request.data)["messages"]
        sent = json.loads([message for message in messages if message.get("role") == "user"][-1]["content"])["observation"]
        views.append(sent)
        payload = latest_model_request(messages)
        view = payload["observation"]
        assert "tree" not in view and "tree" not in sent
        field = next(node for node in view["nodes"] if node.get("name") == "Query")
        assert field["id"].startswith("n") and "frame_identity" not in field
        if len(views) > 1:
            assert payload["history"][0]["action"]["target"]["value"] == field["id"]
        if len(views) == 3 and not keep_repeating:
            assert payload["history"][-1]["dispatched"] is False
            assert any(node.get("name") == "Candidate" for node in view["nodes"])
            answer = {"done": True, "summary": "Candidate visible"}
        else:
            answer = {"action": {"op": "fill", "target": {"by": "ref", "value": field["id"],
                                                          "observation": view["id"]}, "value": "abc"}}
        return BytesIO(json.dumps({"choices": [{"message": {"content": json.dumps(answer)}}],
                                   "usage": {"prompt_tokens": 10, "completion_tokens": 2,
                                             "prompt_cache_hit_tokens": 3}}).encode())

    monkeypatch.setattr(models, "urlopen", provider)
    model = DeepSeekExecutor(model="local", api_key="test")
    with host.session(name="search", app_identity="test", executor=model, cache=CacheConfig(mode="off")) as session:
        session.page.set_content("""<input aria-label="Query" oninput="setTimeout(
            () => document.querySelector('button').hidden=false, 120)"><button hidden>Candidate</button>""")
        assert "tree" in session.observe()
        if keep_repeating:
            with pytest.raises(E2EError, match="STEP_NO_CONCLUSION"):
                session.agent.act("Find candidate")
        else:
            session.agent.act("Find candidate")
        step = session.steps[-1]
        assert step.actions == 1 and step.model_calls == len(views) == 3
        assert step.model_usage == {"prompt_tokens": 30, "completion_tokens": 6, "cached_prompt_tokens": 9,
                                    "peak_prompt_tokens": 10,
                                    "observation_bytes": sum(len(canonical(view).encode()) for view in views)}
        assert session.total_usage["prompt_tokens"] == 30


def test_table_row_summary_uses_visible_cell_text(host):
    with host.session(name='visible-cells', app_identity='test') as session:
        session.page.set_content('''<table><thead><tr><th>Chain</th><th hidden>Internal</th><th>Status</th></tr></thead>
          <tbody><tr><td>ID-42<span hidden>wrong-id</span></td><td hidden>secret-column</td>
          <td>Ready<span style="visibility:hidden">old-state</span></td></tr></tbody></table>''')
        nodes = session.observe()['nodes']
        rows = [n for n in nodes if n['role'] == 'row']
        assert [n['name'] for n in rows] == ['Chain Status', 'ID-42 Ready']
        assert [[n['name'] for n in nodes if n['parent_id'] == row['id']] for row in rows] == [
            ['Chain', 'Status'], ['ID-42', 'Ready']]


def test_visibility_override_and_inert_ref_revalidation(host):
    with host.session(name='visible-targets', app_identity='test') as session:
        session.page.set_content('''<section style="visibility:hidden"><button>Hidden</button>
            <button style="visibility:visible" onclick="this.dataset.clicked=1">Override</button></section>
            <section inert><button>Inert</button></section><button id="live">Live</button>''')
        view = session.observe()
        assert {n['name'] for n in view['nodes'] if n['role'] == 'button'} == {'Override', 'Live'}
        session.engine.perform(dict(op='click', target=ref(view, 'Override')), timeout=1)
        assert session.page.locator('[data-clicked]').count() == 1
        session.page.locator('#live').evaluate('el => el.inert = true')
        with pytest.raises(E2EError, match='NODE_STALE'):
            session.engine.perform(dict(op='click', target=ref(view, 'Live')), timeout=1)


def test_nested_frame_bounds_follow_top_viewport(host):
    with host.session(name='frame-placement', app_identity='test') as session:
        page = session.page
        page.set_viewport_size({'width': 500, 'height': 400})
        page.set_content('''<iframe id="outer" style="position:absolute;left:40px;top:420px;
            border:4px solid;padding:6px;width:200px;height:120px"></iframe>''')
        outer = page.frames[1]
        outer.set_content('''<iframe style="position:absolute;left:20px;top:10px;border:2px solid;
            width:100px;height:80px"></iframe>''')
        page.frames[2].set_content('''<button style="position:absolute;left:5px;top:6px;
            width:110px;height:30px">Nested</button>''')

        def observed():
            return next(n for n in session.observe()['nodes'] if n['name'] == 'Nested')

        node = observed()
        assert len(node['frame']) == 2 and not node['in_viewport']
        assert node['bounds']['x'] == 77 and node['bounds']['y'] == 448
        page.locator('#outer').evaluate("el => el.style.top='360px'")
        node = observed()
        assert node['in_viewport']
        assert node['bounds'] == dict(x=77, y=388, width=95, height=12)
        page.locator('#outer').evaluate("el => el.style.top='40px'")
        assert observed()['bounds'] == dict(x=77, y=68, width=95, height=30)


def test_agent_observes_native_roles_editing_hosts_and_generated_names(host):
    with host.session(name='roles', app_identity='app', cache=CacheConfig(mode='off')) as session:
        session.page.set_content('''
            <style>#publish::before {content: "Publish"} .suffix::after {content: attr(data-label)}</style>
            <button id="publish"><span class="suffix" data-label=" now"></span></button>
            <button role="unknown switch" aria-checked="false">Power</button>
            <input type="file" aria-label="Attach"><progress aria-label="Import" value="4" max="10"></progress>
            <output>Saved</output><article contenteditable aria-label="Editor">Draft</article>
            <button role="unknown">Fallback</button>''')
        nodes = {node['name']: node for node in session.observe()['nodes']}
        for name, expected in [('Publish now', 'button'), ('Power', 'switch'), ('Attach', 'button'),
                               ('Import', 'progressbar'), ('Saved', 'status'), ('Editor', 'textbox'),
                               ('Fallback', 'button')]:
            assert nodes[name]['role'] == expected
        assert nodes['Editor']['editable']
        assert nodes['Attach']['input_type'] == 'file'


@pytest.mark.parametrize('state,end_value', [('pressed', 'true'), ('pressed', 'mixed'), ('expanded', 'true')])
def test_replay_requires_recorded_control_state_even_when_saved_appears(host, tmp_path, state, end_value):
    def decide(request, *, timeout):
        return {'action': {'op': 'click', 'target': ref(request['observation'], 'Pin')}}

    def markup(change):
        update = f"this.setAttribute('aria-{state}', '{end_value}');" if change else ''
        return (f'<button aria-{state}="false" onclick="{update} document.querySelector(\'output\').textContent='
                "\'Saved\'\">Pin</button><output></output>")

    options = dict(name='state', app_identity='app', executor=FunctionExecutor(decide))
    with host.session(**options, cache=CacheConfig(directory=tmp_path, mode='read-write')) as session:
        session.page.set_content(markup(True))
        session.agent.act_once('Pin the item')
        session.expect(session.page.get_by_role('button')).to_have_attribute(f'aria-{state}', end_value)

    for change in (True, False):
        with host.session(**options, cache=CacheConfig(directory=tmp_path, mode='read-only', strict=True)) as session:
            session.page.set_content(markup(change))
            if change:
                assert session.agent.act_once('Pin the item').model_calls == 0
            else:
                with pytest.raises(ReplayStale, match='end-state'):
                    session.agent.act_once('Pin the item')
                assert session.page.get_by_role('status').text_content() == 'Saved'


def test_upload_replay_uses_current_params_and_roots(host, tmp_path):
    from e2e_web import Unique

    page = """
        <button onclick="this.textContent='Ready'">Prepare</button>
        <input type="file" multiple aria-label="Attachments">
    """

    def decide(request, *, timeout):
        observation = request['observation']
        if not request['history']:
            return {'action': dict(op='click', target=ref(observation, 'Prepare'))}
        if len(request['history']) == 1:
            return {'action': dict(op='upload', target=ref(observation, 'Attachments'),
                                   value=request['params']['attachments'])}
        return {'done': True}

    def params(folder):
        return {'attachments': [Unique(str(folder / filename)) for filename in ('report.txt', 'notes.txt')]}

    options = dict(name='upload-replay', app_identity='app', executor=FunctionExecutor(decide))
    first_dir, second_dir = tmp_path / 'first', tmp_path / 'second'
    for folder in (first_dir, second_dir):
        folder.mkdir()
        for filename in ('report.txt', 'notes.txt'):
            (folder / filename).write_text(folder.name + filename)
    cache_dir = tmp_path / 'cache'
    for folder, replay in [(first_dir, False), (second_dir, True)]:
        cache = CacheConfig(directory=cache_dir, strict=replay, mode='read-only' if replay else 'read-write')
        with host.session(**options, upload_roots=(folder,), cache=cache) as s:
            s.page.set_content(page)
            result = s.agent.act('Attach the reports', params=params(folder))
            assert result.model_calls == (0 if replay else 3)
            s.verify(lambda: s.page.locator('input').evaluate(
                'el => Promise.all(Array.from(el.files, file => file.text()))'
            ) == [folder.name + 'report.txt', folder.name + 'notes.txt'])

    cache = CacheConfig(directory=cache_dir, strict=True)
    with host.session(**options, upload_roots=(first_dir,), cache=cache) as s:
        s.page.set_content(page)
        with pytest.raises(E2EError, match='POLICY_DENIED'):
            s.agent.act('Attach the reports', params=params(second_dir))
        assert s.page.get_by_role('button', name='Prepare', exact=True).count() == 1
        assert s.page.locator('input').evaluate('el => el.files.length') == 0

    trace_path = next(cache_dir.glob('*.json'))
    trace = json.loads(trace_path.read_text())
    trace['actions'][1]['value'][1] = str(first_dir / 'notes.txt')
    trace_path.write_text(json.dumps(trace))
    with host.session(**options, upload_roots=(first_dir, second_dir), cache=cache) as s:
        s.page.set_content(page)
        with pytest.raises(ReplayStale, match="params"):
            s.agent.act('Attach the reports', params=params(second_dir))
        assert s.page.get_by_role('button', name='Prepare', exact=True).count() == 1
        assert s.page.locator('input').evaluate('el => el.files.length') == 0


def test_replay_generated_test_id_prefers_exact_and_requires_unique_shape(host, tmp_path):
    old = 'save-1234abcd'
    fresh = 'save-cafe5678'

    def markup(ids):
        buttons = ''.join(f'<button data-testid="{identifier}" onclick="this.dataset.used=1;'
                          f'document.querySelector(\'output\').textContent=\'Saved\'">Save</button>'
                          for identifier in ids)
        return buttons + '<output></output>'

    def decide(request, *, timeout):
        return {'action': dict(op='click', target=ref(request['observation'], 'Save'))}

    options = dict(name='generated-id', app_identity='app', executor=FunctionExecutor(decide))
    with host.session(**options, cache=CacheConfig(directory=tmp_path, mode='read-write')) as session:
        session.page.set_content(markup([old]))
        session.agent.act_once('Save')
        session.expect(session.page.get_by_role('status')).to_have_text('Saved')

    for ids, expected in [([fresh], fresh), ([fresh, old], old),
                          ([fresh, 'save-5678cafe'], None), (['archive-cafe5678'], None)]:
        with host.session(**options, cache=CacheConfig(directory=tmp_path, strict=True)) as session:
            session.page.set_content(markup(ids))
            if expected:
                assert session.agent.act_once('Save').model_calls == 0
                assert session.page.locator('[data-used]').get_attribute('data-testid') == expected
            else:
                with pytest.raises(ReplayStale):
                    session.agent.act_once('Save')
                assert session.page.locator('[data-used]').count() == 0

    # A fresh recording with two shape twins cannot learn a unique shape.
    options['name'] = 'generated-id-twins'
    with host.session(**options, cache=CacheConfig(directory=tmp_path, mode='read-write')) as session:
        session.page.set_content(markup([old, fresh]))
        session.agent.act_once('Save')
        session.expect(session.page.get_by_role('status')).to_have_text('Saved')
    with host.session(**options, cache=CacheConfig(directory=tmp_path, strict=True)) as session:
        session.page.set_content(markup([fresh]))
        with pytest.raises(ReplayStale):
            session.agent.act_once('Save')
        assert session.page.locator('[data-used]').count() == 0


def test_replay_missing_second_target_hands_off_without_repeating_prefix(host, tmp_path):
    def markup(second):
        return ('<button onclick="this.dataset.count=Number(this.dataset.count || 0)+1">Prepare</button>'
                f'<button onclick="document.querySelector(\'output\').textContent=\'Saved\'">{second}</button>'
                '<output></output>')

    def decide(request, *, timeout):
        view = request['observation']
        if any(node['name'] == 'Saved' for node in view['nodes']):
            return {'done': True}
        history = request['history']
        if not history:
            name = 'Prepare'
        elif history[0].get('source') == 'replay':
            assert history[0]['action']['target']['name'] == 'Prepare'
            assert history[1]['dispatched'] is False
            assert history[1]['action']['target']['name'] == 'Save'
            name = 'Apply'
        else:
            name = 'Save'
        return {'action': dict(op='click', target=ref(view, name))}

    options = dict(name='mid-replay', app_identity='app', executor=FunctionExecutor(decide))
    for strict, label in [(False, 'Save'), (True, 'Apply'), (False, 'Apply'), (True, 'Save')]:
        with host.session(**options, cache=CacheConfig(directory=tmp_path, mode='read-write', strict=strict)) as session:
            session.page.set_content(markup(label))
            if strict and label == 'Apply':
                with pytest.raises(ReplayStale):
                    session.agent.act('Prepare and save', max_actions=2)
                assert session.page.get_by_role('status').text_content() == ''
            else:
                result = session.agent.act('Prepare and save', max_actions=2)
                session.expect(session.page.get_by_role('status')).to_have_text('Saved')
                if strict:
                    assert result.model_calls == 0
                if label == 'Apply':
                    assert result.cache['reason'] == 'target-not-found'
                    assert result.actions == 2 and result.model_calls == 2
            assert session.page.get_by_role('button', name='Prepare').get_attribute('data-count') == '1'


def test_semantic_timeout_falls_back_to_masked_pixels(host, monkeypatch):
    from e2e_web import StepTimeout

    def decide(request, *, timeout):
        assert request['observation']['tree_unavailable']
        assert request['visual']['width'] > 0 and request['screenshot']
        return {'passed': True, 'reason': 'viewport available'}

    with host.session(name='pixel-fallback', app_identity='app', executor=FunctionExecutor(decide)) as s:
        s.page.set_content('<input autocomplete="one-time-code" value="1234"><button>Ready</button>')
        original = s.engine.screenshot(timeout=3)
        s.page.locator('input').fill('9876')
        s.page.locator('input').blur()
        assert s.engine.screenshot(timeout=3) == original
        s.page.get_by_role('button').evaluate("el => el.textContent='Changed'")
        assert s.engine.screenshot(timeout=3) != original

        def timed_out(*, timeout):
            raise StepTimeout('STEP_TIMEOUT', 'semantic capture timed out')

        monkeypatch.setattr(s.engine, 'observe', timed_out)
        s.agent.assert_('Read the viewport', vision=True)
        with pytest.raises(StepTimeout):
            s.agent.assert_('Read the tree', vision=False)


def test_control_values_roles_and_replay_state(host):
    from e2e_web.cache import delta

    with host.session(name='form-states', app_identity='app', cache=CacheConfig(mode='off')) as s:
        s.page.set_content("""
            <article contenteditable aria-label="Draft"><p>Initial</p></article>
            <table role="treegrid"><tr><th scope="rowgroup">Order</th><td>Total</td></tr></table>
            <select multiple aria-label="Users"><option aria-selected="true">User</option></select>
            <input type="checkbox" aria-label="All"><input aria-label="Code">
        """)
        before = s.observe()
        assert {('rowheader', 'Order'), ('gridcell', 'Total')} <= {(n['role'], n['name']) for n in before['nodes']}
        assert not next(n for n in before['nodes'] if n['role'] == 'option')['selected']
        s.engine.perform(dict(op='fill', target=ref(before, 'Draft'), value='Updated  draft\nNext'), timeout=2)
        s.page.evaluate("""() => {
            document.querySelector('[type=checkbox]').indeterminate = true;
            document.querySelector('[aria-label=Code]').readOnly = true;
            document.querySelector('select').disabled = true;
        }""")
        after = s.observe()
        draft = next(n for n in after['nodes'] if n['name'] == 'Draft')
        rendered = s.page.locator('article[contenteditable]').inner_text()
        assert rendered.startswith('Updated') and '\n' in rendered and rendered.endswith('Next')
        assert draft['value'] == rendered and draft['value_exact']
        added, _ = delta(before, after)
        for marker in ('[readonly]', '[disabled]', '[indeterminate]'):
            assert any(marker in line for line in added)


def test_check_uses_associated_label_under_styled_span(host):
    with host.session(name='styled-check', app_identity='app', cache=CacheConfig(mode='off')) as s:
        s.page.set_content("""
            <style>label {position:relative;display:inline-block;padding:12px}
            input {position:absolute;inset:0;width:100%;height:100%;margin:0}
            span {position:absolute;inset:0;background:white}</style>
            <label><input type="checkbox" aria-label="Notifications"><span>Notifications</span></label>
        """)
        for operation in ('check', 'uncheck'):
            s.engine.perform(dict(op=operation, target=ref(s.observe(), 'Notifications')), timeout=2)
            assert s.page.locator('input').is_checked() == (operation == 'check')


def test_replay_rejects_selection_change_on_another_control(host, tmp_path):
    def choose(request, *, timeout):
        return {'action': dict(op='click', target=ref(request['observation'], 'Apply'))}

    for replay in (False, True):
        cache = CacheConfig(directory=tmp_path, mode='read-only' if replay else 'read-write', strict=replay)
        with host.session(name='select-owner', app_identity='app', executor=FunctionExecutor(choose), cache=cache) as s:
            options = '<option>A</option><option>B</option>'
            s.page.set_content(f"""
                <select aria-label="Source">{options}</select><select aria-label="Destination">{options}</select>
                <button onclick="document.querySelectorAll('select')[{int(replay)}].selectedIndex=1">Apply</button>
            """)
            if replay:
                with pytest.raises(ReplayStale, match='end-state'):
                    s.agent.act_once('Apply the source selection')
                assert s.page.get_by_label('Destination').input_value() == 'B'
            else:
                s.agent.act_once('Apply the source selection')
                s.expect(s.page.get_by_label('Source')).to_have_value('B')


def test_aria_range_keyboard_changes_rendered_value(host):
    with host.session(name="range", app_identity="app") as session:
        session.page.set_content("""<div role="slider" aria-label="Speed" tabindex="0"
          style="width:120px;height:20px" aria-valuenow="0" onkeydown="if(event.key==='ArrowRight') {
            this.setAttribute('aria-valuenow','1'); this.setAttribute('aria-valuetext','Fast') }"></div>
          <progress aria-label="Loading"></progress><progress aria-label="Finished" value="0"></progress>""")
        before = session.observe()
        values = {n["name"]: n["value"] for n in before["nodes"]}
        assert values["Speed"] == "0" and values["Loading"] == "" and values["Finished"] == "0"
        session.engine.perform(dict(op="press", target=ref(before, "Speed"), value="ArrowRight"), timeout=2)
        assert '- slider "Speed" [value_text="Fast"]: 1' in session.observe()["tree"]


def test_strict_replay_checks_which_custom_listbox_changed(host, tmp_path):
    for redirected in (False, True):
        with host.session(name="pick", app_identity="app", cache=CacheConfig(
                directory=tmp_path, mode="read-only" if redirected else "read-write", strict=redirected),
                executor=FunctionExecutor(lambda request, **kw: {"action": {
                    "op": "click", "target": {"by": "test_id", "value": "source-choice"}}})) as session:
            session.page.set_content("""<div role="listbox" aria-label="Source">
              <button role="option" data-testid="source-choice">Default</button></div>
              <div role="listbox" aria-label="Destination"><button role="option">Default</button></div>""")
            options = session.page.get_by_role("option")
            options.first.evaluate("""(el, redirected) => {
                el.onclick = () => document.querySelectorAll('[role=option]')[Number(redirected)]
                    .setAttribute('aria-selected','true');
            }""", redirected)
            if redirected:
                with pytest.raises(ReplayStale, match="end-state delta"):
                    session.agent.act_once("Select the default source")
                assert session.steps[-1].model_calls == 0
            else:
                session.agent.act_once("Select the default source")
                session.expect(options.first).to_have_attribute("aria-selected", "true")


def test_icon_labels_presentational_controls_and_editor_placeholders(host):
    with host.session(name='control-names', app_identity='app') as session:
        session.page.set_content('''<img id="icon" alt="Commit"><button role="presentation"
            aria-labelledby="icon" onclick="this.dataset.used=1"></button>
            <div contenteditable aria-placeholder="Search records"></div>
            <div contenteditable><p data-placeholder="Draft body"><br></p></div>''')
        view = session.observe()
        nodes = {(node['role'], node['name']): node for node in view['nodes']}
        button = nodes['button', 'Commit']
        session.engine.perform(dict(op='click', target=dict(by='ref', value=button['id'], observation=view['id'])), timeout=2)
        assert session.page.locator('button').get_attribute('data-used') == '1'
        assert nodes['textbox', 'Search records']['editable'] and nodes['textbox', 'Draft body']['editable']


def test_same_name_links_expose_destinations_without_query_or_credentials(host):
    with host.session(name='links', app_identity='app') as session:
        session.page.set_content('''<base href="https://docs.example/guide/">
            <a href="setup?secret=one#two">Read</a>
            <a href="https://user:pass@api.example/ref">Read</a>
            <a href="mailto:private@example.com">Email</a>''')
        view = session.observe()
        links = [n for n in view['nodes'] if n['role'] == 'link']
        assert [(n['name'], n.get('href')) for n in links] == [
            ('Read', 'https://docs.example/guide/setup'), ('Read', 'https://api.example/ref'), ('Email', None)]

        action = dict(op='click', target=dict(by='ref', value=links[1]['id'], observation=view['id']))
        recorded = session.engine.record_action(action)
        assert recorded['target']['href'] == 'https://api.example/ref'
        assert session.engine.target(recorded['target']).get_attribute('href') == 'https://user:pass@api.example/ref'
        session.page.locator('a').nth(1).evaluate("el => el.href = '/other'")
        with pytest.raises(E2EError, match='NODE_STALE'):
            session.engine.perform(action, timeout=2)
        with pytest.raises(E2EError, match='LOCATOR_NOT_FOUND'):
            session.engine.target(recorded['target'])


def test_folded_and_skipped_content_excluded_from_refs_and_frames(host):
    with host.session(name='folded', app_identity='app', cache=CacheConfig(mode='off')) as s:
        s.page.set_content('''<section aria-label="Preferences"><details open>
            <p>Introduction</p><summary>Advanced</summary><summary>Other summary</summary>
            <button>Apply</button></details></section>
            <div id="panel"><iframe srcdoc="<button>Frame action</button>"></iframe></div>
            <figure><figcaption>Preview</figcaption></figure><menu><li>Tools</li></menu>
            <div id="widget"><button>Slotted</button></div>
            <script>widget.attachShadow({mode:'open'}).innerHTML =
                '<div role="group" aria-label="Actions"><slot></slot></div>';</script>''')
        s.page.frame_locator('iframe').get_by_role('button').wait_for()
        original = s.observe()
        slot_button = next(n for n in original['nodes'] if n['name'] == 'Slotted')
        assert next(n for n in original['nodes'] if n['id'] == slot_button['parent_id'])['name'] == 'Actions'
        s.page.evaluate("""() => {
            document.querySelector('#widget').shadowRoot.querySelector('div').style.contentVisibility = 'hidden';
            document.querySelector('details').open = false;
            document.querySelector('#panel').style.contentVisibility = 'hidden';
        }""")
        for name in ('Apply', 'Frame action', 'Slotted'):
            with pytest.raises(E2EError, match='NODE_STALE'):
                s.engine.perform(dict(op='click', target=ref(original, name)), timeout=2)
        with pytest.raises(E2EError, match='NODE_STALE'):
            s.engine.target(dict(by='semantic', value='button', name='Frame action', frame=['iframe']))
        folded = s.observe()
        names = {(n['role'], n['name']) for n in folded['nodes']}
        assert {('region', 'Preferences'), ('button', 'Advanced'), ('figure', 'Preview'), ('list', '')} <= names
        assert not folded['frames']
        assert not {'Other summary', 'Apply', 'Introduction', 'Slotted'} & {name for _, name in names}
        s.engine.perform(dict(op='click', target=ref(folded, 'Advanced')), timeout=2)
        assert 'Apply' in s.observe()['tree']


def test_named_alert_body_and_grouped_options_survive_observation_and_replay_checks(host):
    from e2e_web.cache import delta, matches_end, route

    with host.session(name='grouped-options', app_identity='app', cache=CacheConfig(mode='off')) as s:
        s.page.set_content("""<select aria-label="Destination"><option>Choose</option>
            <optgroup label="North"><option value="n">Office</option></optgroup>
            <optgroup label="South"><option value="s">Office</option></optgroup></select>
            <div role="alert" aria-label="Validation">Ready</div>""")
        before = s.observe()
        options = next(node['options'] for node in before['nodes'] if node['role'] == 'combobox')
        assert [option.get('group') for option in options] == [None, 'North', 'South']

        def choose(group):
            s.engine.perform(dict(op='select', target=dict(by='role', value='combobox', name='Destination'),
                                  value=dict(group=group, label='Office')), timeout=2)

        choose('South')
        s.expect(s.page.get_by_label('Destination')).to_have_value('s')
        after = s.observe()
        appeared, gone = delta(before, after)
        entry = dict(end=route(after['url']), appeared=appeared, gone=gone)
        assert matches_end(entry, before, after)
        choose('North')
        assert not matches_end(entry, before, s.observe())
        choose('South')
        s.page.get_by_role('alert').evaluate("el => el.innerHTML = 'Rejected<span hidden>hidden detail</span>'")
        failed = s.observe()
        alert = next(node for node in failed['nodes'] if node['role'] == 'alert')
        assert (alert['name'], alert['message']) == ('Validation', 'Rejected')
        assert not matches_end(entry, before, failed)


def test_grouped_selection_replays_after_groups_reorder(host, tmp_path):
    def choose(request, *, timeout):
        assert not replay, 'strict replay should use the recorded group and label'
        return {'action': dict(op='select', target=ref(request['observation'], 'Destination'),
                               value={'group': 'South', 'label': 'Office'})}

    for replay in (False, True):
        cache = CacheConfig(directory=tmp_path, mode='read-only' if replay else 'read-write', strict=replay)
        with host.session(name='grouped-replay', app_identity='app', cache=cache, executor=FunctionExecutor(choose)) as s:
            groups = ('South', 'North') if replay else ('North', 'South')
            options = ''.join(f'<optgroup label="{group}"><option value="{group}">Office</option></optgroup>'
                              for group in groups)
            s.page.set_content(f'<select aria-label="Destination"><option>Choose</option>{options}</select>')
            s.agent.act_once('Select the South office')
            s.expect(s.page.get_by_label('Destination')).to_have_value('South')


def test_agent_maps_painted_targets_landmarks_and_heading_levels(host):
    with host.session(name='mapped-elements', app_identity='app', cache=CacheConfig(mode='off')) as s:
        s.page.set_content('''<header aria-label="Top">Top</header><article><header>Inner</header></article>
          <h3>Overview</h3><div role="heading" aria-level="5">Details</div>
          <svg width="24" height="24"><title>Inbox icon</title></svg>
          <button draggable="true" ondragstart="event.dataTransfer.setData('text/plain','item')">Move item</button>
          <div id="drop" ondragover="event.preventDefault()" ondrop="this.dataset.received=event.dataTransfer.getData('text/plain')"
               style="border:1px solid;height:40px;width:60px"></div>
          <div style="height:40px;width:60px;background:transparent"></div>''')
        obs = s.observe()
        nodes = obs['nodes']
        assert [n['name'] for n in nodes if n['role'] == 'banner'] == ['Top']
        assert [n['level'] for n in nodes if n['role'] == 'heading'] == [3, 5]
        assert any(n['role'] == 'img' and n['name'] == 'Inbox icon' for n in nodes)
        box, = [n for n in nodes if n['role'] == 'box']
        descriptor = dict(by='ref', value=box['id'], observation=obs['id'])
        assert s.engine.target(descriptor).get_attribute('id') == 'drop'
        drag = dict(op='drag', target=ref(obs, 'Move item'), to=descriptor)
        s.engine.perform(drag, timeout=3)
        assert s.page.locator('#drop').get_attribute('data-received') == 'item'
        assert s.engine.record_action(drag) is None


def test_agent_selects_only_offered_options_with_original_index(host):
    with host.session(name='offered-options', app_identity='app', cache=CacheConfig(mode='off')) as s:
        s.page.set_content('''<select aria-label="Route"><option>Start</option>
            <optgroup hidden label="Archived"><option value="old">Next</option></optgroup>
            <option value="new">Next</option></select>''')
        view = s.observe()
        node = next(n for n in view['nodes'] if n['role'] == 'combobox')
        assert [o['index'] for o in node['options']] == [0, 2]
        assert not node['options_truncated']
        for choice in ('Next', {'index': 2}):
            s.engine.perform(dict(op='select', target=ref(view, 'Route'), value=choice), timeout=2)
            assert s.page.locator('select').input_value() == 'new'
        with pytest.raises(E2EError, match='hidden'):
            s.engine.perform(dict(op='select', target=ref(view, 'Route'), value={'index': 1}), timeout=2)


def test_recorded_ref_checks_input_kind_and_heading_level(host):
    with host.session(name='ref-signature', app_identity='app', cache=CacheConfig(mode='off')) as s:
        s.page.set_content('<input aria-label="Code"><h2>Details</h2><h4>Details</h4>')
        view = s.observe()
        def record(node):
            target = dict(by='ref', value=node['id'], observation=view['id'])
            return s.engine.record_action(dict(op='click', target=target))['target']
        field = record(next(n for n in view['nodes'] if n['name'] == 'Code'))
        heading = record(next(n for n in view['nodes'] if n.get('level') == 4))
        assert s.engine.target(heading).evaluate('el => el.tagName') == 'H4'
        s.page.locator('input').evaluate("el => el.type = 'password'")
        with pytest.raises(E2EError, match='LOCATOR_NOT_FOUND'):
            s.engine.target(field)


def test_shadow_controls_inherit_aria_disabled_with_local_override(host):
    with host.session(name='disabled-shadow', app_identity='app', cache=CacheConfig(mode='off')) as s:
        s.page.set_content('<div id="widget" aria-disabled="true"></div>')
        s.page.evaluate("""() => document.querySelector('#widget').attachShadow({mode:'open'}).innerHTML =
            '<button>Inherited</button><div aria-disabled="false"><button>Allowed</button></div>'""")
        nodes = {n['name']: n for n in s.observe()['nodes'] if n['role'] == 'button'}
        assert nodes['Inherited']['disabled'] and not nodes['Allowed']['disabled']


def test_mixed_inline_text_is_read_and_containers_are_not_named_by_their_content(host):
    with host.session(name='line-text', app_identity='app', cache=CacheConfig(mode='off')) as s:
        s.page.set_content('''<p id="terms">By continuing you agree to our <a href="/terms">Terms</a>.</p>
            <div role="tabpanel"><p>Panel text</p></div>
            <div data-testid="wrap"><span>Wrapped</span></div>''')
        observation = s.observe()
        nodes = observation['nodes']
        terms = next(node for node in nodes if node['role'] == 'paragraph')
        assert terms['text'] == 'By continuing you agree to our Terms.'
        assert next(node for node in nodes if node['role'] == 'tabpanel')['name'] == ''
        assert next(node for node in nodes if node['test_id'] == 'wrap')['name'] == ''
        assert '- paragraph "" text=' in observation['tree']


def test_a_checkbox_covered_by_its_own_label_is_clicked_through_the_label(host):
    with host.session(name='covered', app_identity='app', cache=CacheConfig(mode='off')) as s:
        s.page.set_content('''<label class="row"><input type="checkbox" style="position:absolute;opacity:0;width:0;height:0">
            <span style="display:block;padding:20px">Remember me</span></label>
            <p role="status"></p><script>
            document.querySelector('input').onchange = () => document.querySelector('p').textContent =
              document.querySelector('input').checked ? 'On' : 'Off';</script>''')
        for state in ('Remember me',):
            assert s.engine.perform(dict(op='click', target=dict(by='role', value='checkbox', name=state)), timeout=2) is None
        s.expect(s.page.locator('input')).to_be_checked()
        s.expect(s.page.get_by_role('status')).to_have_text('On')


def test_scrolling_a_wrapper_reaches_the_element_that_scrolls(host):
    with host.session(name='scroll-wraper', app_identity='app', cache=CacheConfig(mode='off')) as s:
        s.page.set_content('''<div data-testid="outer" style="height:120px">
            <div id="inner" style="height:120px;overflow:auto">
              <div style="height:1200px">Tall</div></div></div>''')
        target = dict(by='test_id', value='outer')
        s.engine.perform(dict(op='scroll', target=target, x=0, y=240), timeout=2)
        assert s.page.locator('#inner').evaluate('el => el.scrollTop') == 240
        s.page.set_content('<div data-testid="flat" style="height:40px">no scroller</div>')
        with pytest.raises(E2EError, match='ACTION_FAILED'):
            s.engine.perform(dict(op='scroll', target=dict(by='test_id', value='flat'), x=0, y=240), timeout=2)
        with pytest.raises(E2EError, match='ACTION_FAILED'):
            s.engine.perform(dict(op='scroll', target=dict(by='test_id', value='flat'), direction='down'), timeout=2)


def test_an_unchanged_check_is_an_action_failure_not_a_step_timeout(host):
    with host.session(name='stuck-check', app_identity='app', cache=CacheConfig(mode='off')) as s:
        s.page.set_content('<div role="checkbox" aria-checked="false" tabindex="0">Blocked</div>')
        with pytest.raises(E2EError) as error:
            s.engine.perform(dict(op='check', target=dict(by='role', value='checkbox', name='Blocked')), timeout=8)
        assert error.value.code == 'ACTION_FAILED'


def test_a_failed_action_is_reported_to_the_model_instead_of_ending_the_step(host):
    seen = []

    def decide(request, *, timeout):
        seen.append(request)
        index = len(request['history'])
        if index == 0:
            return {'action': dict(op='click', target=dict(by='role', value='button', name='Missing'))}
        assert request['history'][0]['dispatched'] is False
        assert request['history'][0]['error']['code'] in {'LOCATOR_NOT_FOUND', 'ACTION_FAILED'}
        if index == 1:
            return {'action': dict(op='click', target=dict(by='role', value='button', name='Present'))}
        return {'done': True}

    with host.session(name='recover', app_identity='app', executor=FunctionExecutor(decide),
                      agent=AgentConfig(timeout=12, action_timeout=3),
                      cache=CacheConfig(mode='off')) as s:
        s.page.set_content('<button onclick="document.querySelector(\'p\').textContent=\'Done\'">Present</button>'
                           '<p role="status"></p>')
        result = s.agent.act('Click the prepared control')
        # The failed attempt spent its action budget, then the model chose another control.
        assert result.actions == 2 and result.model_calls == 3
        s.expect(s.page.get_by_role('status')).to_have_text('Done')


def test_twin_controls_are_recorded_by_place_and_replayed_in_the_same_place(host, tmp_path):
    def markup(count):
        cards = ''.join(f'<div class="card"><button data-card="{index}">Set up</button></div>' for index in range(count))
        return cards + '<p role="status"></p><script>document.querySelectorAll("button").forEach(' \
                       'el => el.onclick = () => { document.querySelector("p").textContent = "Picked " + el.dataset.card; });</script>'

    def decide(request, *, timeout):
        return {'action': dict(op='click', target=ref(request['observation'], 'Set up'))}

    options = dict(name='twins', app_identity='app', executor=FunctionExecutor(decide))
    with host.session(**options, cache=CacheConfig(directory=tmp_path, mode='read-write')) as s:
        s.page.set_content(markup(3))
        s.agent.act_once('Pick the first card')
        s.expect(s.page.get_by_role('status')).to_have_text('Picked 0')
    entry = json.loads(next(p for p in tmp_path.glob('*.json')).read_text())
    assert entry['actions'][0]['target']['position'] == {'index': 0, 'of': 3}

    with host.session(**options, cache=CacheConfig(directory=tmp_path, strict=True)) as s:
        s.page.set_content(markup(3))
        assert s.agent.act_once('Pick the first card').model_calls == 0
        s.expect(s.page.get_by_role('status')).to_have_text('Picked 0')

    # A different number of twins is a screen the recording never saw.
    with host.session(**options, cache=CacheConfig(directory=tmp_path, strict=True)) as s:
        s.page.set_content(markup(2))
        with pytest.raises(ReplayStale):
            s.agent.act_once('Pick the first card')


def test_native_dialogs_are_reported_and_answered_by_policy(host):
    with host.session(name='dialogs', app_identity='app', cache=CacheConfig(mode='off')) as s:
        s.page.set_content('''<button id="confirm" onclick="document.querySelector('p').textContent =
              confirm('Delete the draft?') ? 'Deleted' : 'Kept'">Delete</button>
            <button id="prompt" onclick="document.querySelector('p').textContent = prompt('Name?') || 'none'">Name</button>
            <p role="status"></p>''')
        # A dialog nothing handled is dismissed by the browser, which cancels the action.
        s.engine.perform(dict(op='click', target=dict(by='role', value='button', name='Delete')), timeout=2)
        observed = s.observe()
        assert observed['dialogs'] == [{'type': 'confirm', 'message': 'Delete the draft?', 'action': 'dismiss'}]
        s.expect(s.page.get_by_role('status')).to_have_text('Kept')
        assert not s.observe().get('dialogs')

        s.engine.perform(dict(op='dialog', action='accept'), timeout=2)
        s.engine.perform(dict(op='click', target=dict(by='role', value='button', name='Delete')), timeout=2)
        s.expect(s.page.get_by_role('status')).to_have_text('Deleted')

        s.engine.perform(dict(op='dialog', action='accept', text='Ada'), timeout=2)
        s.engine.perform(dict(op='click', target=dict(by='role', value='button', name='Name')), timeout=2)
        s.expect(s.page.get_by_role('status')).to_have_text('Ada')
        assert [dialog['type'] for dialog in s.observe()['dialogs']] == ['confirm', 'prompt']


def test_a_secret_purpose_follows_the_autocomplete_contract(host):
    with host.session(name='purpose', app_identity='app', cache=CacheConfig(mode='off')) as s:
        s.page.set_content('<input type="text" autocomplete="current-password" aria-required="true">'
                           '<input type="password">')
        nodes = s.observe()['nodes']
        current = nodes[0]
        assert current['purpose'] == 'password' and current['secure'] is True
        assert current['attributes']['aria-required'] == 'true'
        assert nodes[1]['secure'] is True and nodes[1]['value'] == '<masked>'
        assert '[required]' in s.observe()['tree']


def test_a_popup_opened_without_an_opener_is_still_part_of_the_session(host):
    with host.session(name='noopener', app_identity='app', cache=CacheConfig(mode='off')) as s:
        s.page.context.route('https://noopener.example/**',
                             lambda route: route.fulfill(content_type='text/html',
                                                         body='<h1>Details</h1><button>Confirm</button>'))
        s.page.goto('https://noopener.example/')
        s.page.set_content('<button onclick="window.open(\'https://noopener.example/details\', '
                           '\'_blank\', \'noopener\')">Open</button>')
        s.page.get_by_role('button').click()
        s.page.wait_for_timeout(500)
        tabs = s.observe()
        assert tabs['tab_count'] == 2
        s.engine.perform(dict(op='switch_tab', tab=2), timeout=2)
        assert s.page.url.endswith('/details')


def test_bounds_reach_the_model_as_whole_css_pixels(host):
    from e2e_web import models
    captured = []

    def provider(request, *, timeout):
        from io import BytesIO
        payload = json.loads(json.loads(request.data)["messages"][1]["content"])
        captured.append(payload["observation"])
        return BytesIO(json.dumps({"choices": [{"message": {"content": json.dumps({"done": True})}}],
                                   "usage": {"prompt_tokens": 1, "completion_tokens": 1}}).encode())

    monkey = pytest.MonkeyPatch()
    monkey.setattr(models, "urlopen", provider)
    try:
        from e2e_web import DeepSeekExecutor
        with host.session(name="bounds", app_identity="app", executor=DeepSeekExecutor(model="local", api_key="test"),
                          cache=CacheConfig(mode="off")) as s:
            s.page.set_content('<button style="margin-left:13.37px">Save</button>')
            s.agent.act("Look")
    finally:
        monkey.undo()
    node = next(node for node in captured[0]["nodes"] if node.get("role") == "button")
    # Whole pixels travel as integers and a fractional position keeps its precision.
    assert all(round(value, 2) == value for value in node["bounds"])
    assert any(isinstance(value, float) for value in node["bounds"])


def test_labelledby_reference_reads_the_target_name_not_its_raw_text(host):
    with host.session(name='labelledby', app_identity='app', cache=CacheConfig(mode='off')) as s:
        s.page.set_content('''<button aria-labelledby="wrap">x</button>
            <span id="wrap"><img alt="Search"><span aria-hidden="true">decoration</span></span>''')
        view = s.observe()
        button = next(node for node in view['nodes'] if node['role'] == 'button')
        assert button['name'] == 'Search'


def test_a_name_assembled_around_a_control_uses_its_value(host):
    with host.session(name='embedded', app_identity='app', cache=CacheConfig(mode='off')) as s:
        s.page.set_content('<div role="button">Flash <span role="spinbutton" aria-valuenow="3"></span> times</div>')
        view = s.observe()
        button = next(node for node in view['nodes'] if node['role'] == 'button')
        assert button['name'] == 'Flash 3 times'


def test_an_icon_font_glyph_name_falls_back_to_the_title(host):
    with host.session(name='glyph', app_identity='app', cache=CacheConfig(mode='off')) as s:
        s.page.set_content('<button title="Delete"><i>\uf090</i></button>'
                           '<button aria-label="Save">\uf0c7 Save</button>')
        view = s.observe()
        names = {(node['role'], node['name']) for node in view['nodes']}
        assert ('button', 'Delete') in names and ('button', 'Save') in names


def test_a_display_contents_container_keeps_its_test_id(host):
    with host.session(name='contents', app_identity='app', cache=CacheConfig(mode='off')) as s:
        s.page.set_content('<div data-testid="form" style="display:contents"><input aria-label="Email"></div>')
        view = s.observe()
        container = next(node for node in view['nodes'] if node['test_id'] == 'form')
        assert container['role'] == 'generic'
        assert any(node['name'] == 'Email' for node in view['nodes'])


def test_a_download_the_app_starts_is_reported(host):
    with host.session(name='downloads', app_identity='app', cache=CacheConfig(mode='off')) as s:
        s.page.context.route('https://files.example/**',
                             lambda route: route.fulfill(status=200, headers={'Content-Disposition': 'attachment; filename=orders.csv'},
                                                         content_type='text/csv', body='id,total\n1,10\n'))
        s.page.set_content('<button onclick="window.location.href=\'https://files.example/export\'">Export</button>')
        with s.page.expect_download() as downloaded:
            s.page.get_by_role('button').click()
        assert downloaded.value.suggested_filename == 'orders.csv'
        assert s.observe()['downloads'][0]['suggested_filename'] == 'orders.csv'


def test_borrowed_dialog_policy_expires_on_close(host):
    with host.session(name="owner", app_identity="app") as owner:
        page = owner.page.context.new_page()
        with Web.attach(page, name="temporary", app_identity="app") as session:
            session.engine.perform({"op": "dialog", "action": "accept"}, timeout=2)
            assert page.evaluate("confirm('during session')")
        assert page.evaluate("confirm('after session')") is False


def test_hidden_reference_cycle_and_native_label_content(host):
    with host.session(name="labels", app_identity="app") as session:
        session.page.set_content("""
            <label><img alt="Account"><span hidden>ignore</span><input id="user"></label>
            <button aria-labelledby="caption">Fallback</button>
            <span id="caption" hidden><span aria-labelledby="caption" hidden>Archive</span></span>
            <input type="text" autocomplete="section-login current-password" value="private-sample">
        """)
        view = session.observe()
        assert any(n["name"] == "Archive" and n["role"] == "button" for n in view["nodes"])
        assert any(n["name"] == "Account" and n["role"] == "textbox" for n in view["nodes"])
        assert any(n.get("purpose") == "password" and n["secure"] for n in view["nodes"])
        assert "private-sample" not in canonical(view)
        assert session.page.locator("e2e_private=fields").count() == 1


def test_custom_test_id_attribute_in_frame():
    with Web(test_id_attribute="data-qa") as host, host.session(name="custom-id", app_identity="app") as s:
        s.page.set_content('''<iframe srcdoc="<button data-testid='wrong' data-qa='save'
            onclick='this.textContent=1'>Save</button>"></iframe>''')
        button = s.page.frame_locator("iframe").get_by_test_id("save")
        button.wait_for()
        node = next(n for n in s.observe()["nodes"] if n["name"] == "Save")
        assert node["test_id"] == "save"
        s.engine.perform({"op": "click", "target": {"by": "test_id", "value": "save", "frame": node["frame"]}}, timeout=2)
        assert button.text_content() == "1"


def test_dialog_only_click_reaches_next_decision(host):
    def decide(request, *, timeout):
        if not request["history"]:
            return {"action": {"op": "click", "target": {"by": "role", "value": "button", "name": "Check"}}}
        assert request["observation"]["dialogs"] == [{"type": "alert", "message": "Ready", "action": "dismiss"}]
        return {"done": True}

    with host.session(name="dialog-only", app_identity="app", cache=CacheConfig(mode="off"),
                      executor=FunctionExecutor(decide)) as s:
        s.page.set_content('<button onclick="alert(\'Ready\')">Check</button>')
        s.page.get_by_role("button").focus()
        s.agent.act("Check and read the alert")


def test_a_replay_that_ran_nothing_rerecords_a_complete_flow(host, tmp_path):
    page_html = '''<label>Name <input></label><button>Save</button><p role="status"></p><script>
        document.querySelector('button').onclick = () => {
            document.querySelector('p').textContent = 'Saved ' + document.querySelector('input').value;
        };</script>'''

    def decide(request, *, timeout):
        done = sum(1 for entry in request['history'] if 'action' in entry)
        actions = [dict(op='fill', target=dict(by='role', value='textbox', name='Name'), value='Ada'),
                   dict(op='click', target=dict(by='role', value='button', name='Save'))]
        return {'action': actions[done]} if done < len(actions) else {'done': True, 'summary': 'saved'}

    cache = CacheConfig(directory=tmp_path, mode='read-write')
    with host.session(name='rerecord', app_identity='app', executor=FunctionExecutor(decide), cache=cache) as s:
        s.page.set_content(page_html)
        s.agent.act('Fill the name and save')
        s.expect(s.page.get_by_role('status')).to_have_text('Saved Ada')
    path = next(tmp_path.glob('*.json'))
    entry = json.loads(path.read_text())
    entry['actions'][0]['target']['name'] = 'Missing'
    path.write_text(json.dumps(entry))

    with host.session(name='rerecord', app_identity='app', executor=FunctionExecutor(decide),
                      agent=AgentConfig(action_timeout=2), cache=cache) as s:
        s.page.set_content(page_html)
        result = s.agent.act('Fill the name and save')
        assert result.cache['mode'] == 'handoff' and result.cache['replay_prefix'] == 0
        s.expect(s.page.get_by_role('status')).to_have_text('Saved Ada')

    with host.session(name='rerecord', app_identity='app', executor=FunctionExecutor(decide), cache=cache) as s:
        s.page.set_content(page_html)
        result = s.agent.act('Fill the name and save')
        assert result.cache['mode'] == 'replay' and result.model_calls == 0
        s.expect(s.page.get_by_role('status')).to_have_text('Saved Ada')


def test_a_rerendered_control_is_relocated_without_another_model_turn(host):
    calls = []
    session = None

    def decide(request, *, timeout):
        calls.append(request)
        if len(calls) == 1:
            session.page.evaluate("""() => {
                const old = document.querySelector('button');
                old.replaceWith(old.cloneNode(true));
            }""")
            node = next(item for item in request['observation']['nodes'] if item['name'] == 'Save')
            return {'action': dict(op='click', target=dict(by='ref', value=node['id'],
                                                          observation=request['observation']['id']))}
        return {'done': True, 'summary': 'saved'}

    with host.session(name='relocate-live', app_identity='app', executor=FunctionExecutor(decide),
                      cache=CacheConfig(mode='off')) as session:
        session.page.set_content('<button onclick="window.clicks=(window.clicks||0)+1">Save</button>')
        result = session.agent.act('Save the form')
        assert session.page.evaluate('window.clicks') == 1
        assert len(calls) == 2 and result.actions == 1
        assert calls[1]["history"][0]["action"]["op"] == "click"


def test_third_party_frames_stay_boundaries_in_observation(host):
    def handle(route):
        url = route.request.url
        if url == "http://app.example/":
            body = ('<iframe src="http://third.example/ad"></iframe>'
                    '<iframe src="http://app.example/panel"></iframe>')
        elif url.endswith("/ad"):
            body = '<button>Buy now</button>'
        else:
            body = '<button>Panel action</button>'
        route.fulfill(status=200, content_type="text/html", body=body)

    with host.session(name="frames", app_identity="app", cache=CacheConfig(mode="off"),
                      observation=ObservationConfig(max_frames=1)) as s:
        s.page.route("**/*", handle)
        s.page.goto("http://app.example/")
        view = s.observe()
        tree = "\n".join(frame["tree"] for frame in view["frames"])
        assert "Panel action" in tree
        assert "Buy now" not in tree
        assert len(view["frames"]) == 1
        assert not view["truncated"]


def test_th_scope_and_neighbouring_data_choose_the_header_axis(host):
    with host.session(name='th', app_identity='app', cache=CacheConfig(mode='off')) as s:
        s.page.set_content('''<table>
            <tr><th>Name</th><th scope="col">Role</th><th scope="row">Ada</th><td>Engineer</td></tr>
            <tr><td>Bob</td><th>Builder</th><td>Engineer</td><td></td></tr>
        </table>''')
        view = s.observe()
        roles = [node['role'] for node in view['nodes'] if node['role'] in {'rowheader', 'columnheader'}]
        # Name and Role head columns; scope=row and the th between data cells head rows.
        assert roles.count('rowheader') == 2 and roles.count('columnheader') == 2


def test_directional_scroll_moves_the_viewport_or_a_named_container(host):
    with host.session(name='scroll-direction', app_identity='app', cache=CacheConfig(mode='off')) as s:
        s.page.set_content('''<div style="height:3000px"></div>
            <div id="box" style="height:100px;width:200px;overflow:auto">
              <div style="height:1000px">inner</div></div>''')
        s.engine.perform(dict(op='scroll', direction='down'), timeout=2)
        s.page.wait_for_timeout(100)
        assert s.page.evaluate('window.scrollY') > 0
        s.engine.perform(dict(op='scroll', direction='up', times=2), timeout=2)
        s.page.wait_for_timeout(100)
        assert s.page.evaluate('window.scrollY') == 0
        s.engine.perform(dict(op='scroll', direction='down', target=dict(by='css', value='#box')), timeout=2)
        s.page.wait_for_timeout(100)
        assert s.page.evaluate("document.querySelector('#box').scrollTop") > 0


def test_a_browser_recording_caches_the_prefix_before_a_run_time_value(host, tmp_path):
    html = '''<button>Open</button><p role="status"></p><label>Code <input></label><script>
        document.querySelector('button').onclick = () => {
            document.querySelector('p').textContent = 'CODE-123';
        };</script>'''

    def record(request, *, timeout):
        done = sum(1 for entry in request['history'] if 'action' in entry)
        actions = [dict(op='click', target=dict(by='role', value='button', name='Open')),
                   dict(op='fill', target=dict(by='role', value='textbox', name='Code'), value='CODE-123')]
        return {'action': actions[done]} if done < len(actions) else {'done': True, 'summary': 'entered'}

    cache = CacheConfig(directory=tmp_path, mode='read-write')
    with host.session(name='gap', app_identity='app', executor=FunctionExecutor(record), cache=cache) as s:
        s.page.set_content(html)
        s.agent.act('Open and enter the code shown')
        s.expect(s.page.get_by_label('Code')).to_have_value('CODE-123')
    entry = json.loads(next(tmp_path.glob('*.json')).read_text())
    assert [action['op'] for action in entry['actions']] == ['click', 'gap']

    def continue_with(request, *, timeout):
        done = sum(1 for item in request['history'] if 'action' in item and item.get('source') != 'replay')
        if done == 0:
            return {'action': dict(op='fill', target=dict(by='role', value='textbox', name='Code'),
                                   value='CODE-999')}
        return {'done': True, 'summary': 'entered this run code'}

    with host.session(name='gap', app_identity='app', executor=FunctionExecutor(continue_with), cache=cache) as s:
        s.page.set_content(html)
        result = s.agent.act('Open and enter the code shown')
        assert result.cache['mode'] == 'handoff' and result.cache['reason'] == 'gap'
        assert s.page.evaluate("document.querySelector('p').textContent") == 'CODE-123'
        s.expect(s.page.get_by_label('Code')).to_have_value('CODE-999')


def test_uploads_default_to_the_project_root_and_refuse_hidden_paths(host, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'report.txt').write_text('report')
    (tmp_path / '.secret').write_text('secret')
    outside = tmp_path.parent / 'outside-upload.txt'
    outside.write_text('outside')
    try:
        with host.session(name='upload-default', app_identity='app', cache=CacheConfig(mode='off')) as s:
            s.page.set_content('<input type="file">')
            target = dict(by='css', value='input')
            s.engine.perform(dict(op='upload', target=target, value=[str(tmp_path / 'report.txt')]), timeout=3)
            assert s.page.locator('input').evaluate('el => el.files[0].name') == 'report.txt'
            with pytest.raises(E2EError, match='hidden'):
                s.engine.perform(dict(op='upload', target=target, value=[str(tmp_path / '.secret')]), timeout=3)
            with pytest.raises(E2EError, match='allowed upload root'):
                s.engine.perform(dict(op='upload', target=target, value=[str(outside)]), timeout=3)
    finally:
        outside.unlink(missing_ok=True)


def test_builtin_upload_samples_need_no_project_file(host):
    with host.session(name='samples', app_identity='app', cache=CacheConfig(mode='off')) as s:
        s.page.set_content('<input type="file">')
        target = dict(by='css', value='input')
        s.engine.perform(dict(op='upload', target=target, value=['sample:photo.png']), timeout=3)
        assert s.page.locator('input').evaluate('el => el.files[0].name') == 'photo.png'
        with pytest.raises(E2EError, match='unknown built-in'):
            s.engine.perform(dict(op='upload', target=target, value=['sample:missing.png']), timeout=3)


def test_owned_selection_labels_and_descriptions_reach_agent_refs(host):
    with host.session(name="owned", app_identity="app") as s:
        s.page.set_content("""
          <button aria-labelledby="prefix combo combo" aria-describedby="help">Apply</button>
          <span hidden id="prefix">Choose</span><input role="combobox" id="combo" aria-owns="list">
          <ul role="listbox" id="list" aria-owns="second"><li role="option" aria-selected="true">A</li></ul>
          <div role="option" aria-selected="true" id="second" aria-label="B">icon</div>
          <span hidden id="help">Applies both selections</span>""")
        view = s.observe()
        button = next(n for n in view["nodes"] if n["role"] == "button")
        assert button["name"] == "Choose A B"
        assert button["description"] == "Applies both selections"
        s.engine.perform(dict(op="click", target=ref(view, button["name"])), timeout=2)
        assert s.page.locator("button").evaluate("el => el === document.activeElement")


def test_pure_canvas_change_replays_without_a_semantic_delta(host, tmp_path):
    def decide(request, *, timeout):
        if request["history"]:
            return {"done": True}
        return {"action": dict(op="click_at", x=18, y=18, screenshot=request["visual"]["id"])}

    for replay in (False, True):
        with host.session(name="paint", app_identity="app", executor=FunctionExecutor(decide),
                          cache=CacheConfig(directory=tmp_path, strict=replay)) as s:
            s.page.set_content("""<canvas width="180" height="100" onclick="
                this.getContext('2d').fillRect(0,0,180,100)"></canvas>""")
            result = s.agent.act("Paint canvas", vision=True)
            assert result.model_calls == (0 if replay else 2)
            s.verify(lambda: s.page.locator("canvas").evaluate(
                "el => el.getContext('2d').getImageData(10,10,1,1).data[3] === 255"))


def test_described_twins_relocate_after_order_changes(host):
    with host.session(name="twins", app_identity="app") as session:
        session.page.set_content("""
            <button aria-description="personal">Remove</button>
            <button aria-description="work">Remove</button>""")
        view = session.observe()
        node = next(n for n in view["nodes"] if n.get("description") == "work")
        action = session.engine.record_action(dict(op="click", target=dict(
            by="ref", value=node["id"], observation=view["id"])))
        assert action["target"]["description"] == "work" and "position" not in action["target"]
        session.page.evaluate("document.body.prepend(document.querySelector('button:last-of-type'))")
        session.engine.perform(action, timeout=2)
        assert session.page.evaluate("document.activeElement.getAttribute('aria-description')") == "work"


def test_navigation_recording_follows_a_new_deployment(host, tmp_path):
    def decide(request, *, timeout):
        return {"action": {"op": "navigate", "url": "https://first.example/done"}}

    for domain, strict in [("first.example", False), ("second.example", True)]:
        base = f"https://{domain}"
        with host.session(name="portable-nav", app_identity="app", base_url=base,
                          executor=FunctionExecutor(decide),
                          cache=CacheConfig(mode="read-write", directory=tmp_path, strict=strict)) as session:
            session.page.route("**/*", lambda route: route.fulfill(
                content_type="text/html", body=f"<h1>{route.request.url.rsplit('/', 1)[-1]}</h1>"))
            session.page.goto(base + "/start")
            result = session.agent.act_once("Open done")
            session.expect(session.page.get_by_role("heading")).to_have_text("done")
            assert session.page.url == base + "/done"
            assert result.model_calls == (0 if strict else 1)


def test_first_model_observation_includes_pending_dialog(host):
    seen = []
    def decide(request, *, timeout):
        seen.append(request["observation"])
        return {"done": True}

    with host.session(name="dialog-event", app_identity="app", executor=FunctionExecutor(decide),
                      cache=CacheConfig(mode="off")) as session:
        session.page.evaluate("alert('Saved')")
        session.agent.act("Read the dialog")
        assert seen[0]["dialogs"][0]["message"] == "Saved"


def test_cross_route_recording_uses_the_arrival_screen_for_its_anchors(host, tmp_path):
    def decide(request, *, timeout):
        actions = [dict(op='navigate', url='/form'),
                   dict(op='click', target=dict(by='role', value='button', name='Save'))]
        history = request['history']
        return {'action': actions[len(history)]} if len(history) < len(actions) else {'done': True}

    def serve(route):
        body = '<h1>Home</h1>' if route.request.url.endswith('/home') else (
            '<h1>Form</h1><button onclick="document.querySelector(\'p\').textContent=\'Saved\'">Save</button>'
            '<p role="status"></p>')
        route.fulfill(content_type='text/html', body=body)

    for strict in (False, True):
        with host.session(name='cross-route', app_identity='app', base_url='https://app.example',
                          executor=FunctionExecutor(decide),
                          cache=CacheConfig(directory=tmp_path, mode='read-write', strict=strict)) as session:
            session.page.route('**/*', serve)
            session.page.goto('https://app.example/home')
            result = session.agent.act('Open the form and save', timeout=4)
            session.expect(session.page.get_by_role('status')).to_have_text('Saved')
            assert result.model_calls == (0 if strict else 3)


def test_relative_navigation_replays_the_recorded_destination(host, tmp_path):
    def decide(request, *, timeout):
        return {"action": {"op": "navigate", "url": "next"}}

    for directory, strict in [("first", False), ("other", True)]:
        with host.session(name="path-relative", app_identity="app", executor=FunctionExecutor(decide),
                          cache=CacheConfig(directory=tmp_path, mode="read-write", strict=strict)) as s:
            s.page.route("**/*", lambda route: route.fulfill(content_type="text/html", body="<h1>Ready</h1>"))
            s.page.goto(f"https://app.example/{directory}/start")
            result = s.agent.act_once("Open next", timeout=4)
            s.verify(lambda: s.page.url == "https://app.example/first/next")
            assert result.model_calls == (0 if strict else 1)
