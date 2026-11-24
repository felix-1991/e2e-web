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

    with host.session(name='tabs', app_identity='app', executor=FunctionExecutor(decide),
                      cache=CacheConfig(directory=tmp_path, mode='read-write'),
                      agent=AgentConfig(timeout=8)) as s:
        s.page.context.route('https://tabs.example/**', lambda route: route.fulfill(
            content_type='text/html',
            body=('<button onclick="opener.document.querySelector(\'p\').textContent = \'Confirmed\'">Confirm details</button>'
                  if route.request.url.endswith('/details') else
                  '<button onclick="window.open(\'/details\', \'_blank\')">Open details</button><p role="status"></p>')))
        s.page.goto('https://tabs.example/')
        result = s.agent.act('Open details, confirm and close')
        assert result.actions == 4
        s.expect(s.page.get_by_role('status')).to_have_text('Confirmed')
        assert len(s.page.context.pages) == 1
        with pytest.raises(E2EError, match='last open tab'):
            s.engine.perform(dict(op='close_tab', tab=1), timeout=2)
    assert not list(tmp_path.glob('*.json'))


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
def test_coordinate_action_uses_latest_pixels_and_never_records(host, tmp_path, op, event):
    def decide(request, *, timeout):
        if request['history']:
            return {'done':True}
        node=next(n for n in request['observation']['nodes'] if n['role']=='canvas')
        b=node['bounds']
        return {'action':dict(op=op,x=int(b['x']+20),y=int(b['y']+20),screenshot=request['visual']['id'])}

    with host.session(name='canvas',app_identity='app',executor=FunctionExecutor(decide),
                      cache=CacheConfig(directory=tmp_path/'cache',mode='read-write')) as s:
        s.page.set_content('''<canvas width="200" height="100" style="background:gray"></canvas>
            <p role="status"></p>''')
        s.page.locator('canvas').evaluate('''(el, event) => {
            const status = document.querySelector('p');
            el.addEventListener(event, () => status.textContent = event);
            if (event !== 'click') el.addEventListener('click', () => status.textContent = 'unexpected click');
        }''', event)
        s.agent.act('Interact with the canvas',vision=True)
        s.expect(s.page.get_by_role('status')).to_have_text(event)
    assert not list((tmp_path/'cache').glob('*.json'))


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
        with pytest.raises(E2EError,match='POLICY_DENIED'):
            s.engine.perform(dict(op='click_at',x=visual['width'],y=0,screenshot=visual['id']),timeout=3)
        _, visual=s.engine.capture_vision(timeout=3)
        s.engine.perform(dict(op='type_at',x=30,y=25,value='typed text',screenshot=visual['id']),timeout=3)
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
        action = dict(op=op, x=int(box['x'] + box['width'] / 2), y=int(box['y'] + box['height'] / 2),
                      screenshot=request['visual']['id'], value=value)
        if op == 'type_at':
            action['replace'] = True
        return {'action': action}

    with host.session(name='point-controls', app_identity='app', executor=FunctionExecutor(decide),
                      cache=CacheConfig(directory=tmp_path, mode='read-write')) as s:
        s.page.set_content('''<iframe srcdoc="<input value='old'><select><option>North</option>
            <option>South</option></select><output></output>"></iframe>''')
        frame = s.page.frame_locator('iframe')
        frame.locator('input').evaluate('''el => el.onkeydown = e => {
            if (e.key === 'Enter') document.querySelector('output').value = el.value;
        }''')
        result = s.agent.act('Replace the field, press Enter and select South', vision='on_demand')
        assert result.actions == 3 and result.model_calls == 5
        s.expect(frame.locator('output')).to_have_text('new')
        s.expect(frame.locator('select')).to_have_value('South')
    assert not list(tmp_path.glob('*.json'))


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
        descriptor=ref(request['observation'],'Save')
        if len(calls)==1:
            session.page.locator('button').evaluate('el=>{const n=el.cloneNode(true);el.replaceWith(n)}')
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
def test_scroll_find_is_scoped_to_the_ref_container_in_an_iframe(host, by):
    def decide(request, *, timeout):
        observation = request['observation']
        target = dict(by=by, value='button', name='Virtual item')
        if by == 'semantic':
            target.update(frame='#items-frame', within={'role': 'generic', 'name': 'Results'})
        return {'action': dict(op='scroll_find', target=target, container=ref(observation, 'Results'),
                               direction='right', steps=4)}

    with host.session(name='scoped-scroll', app_identity='app', executor=FunctionExecutor(decide),
                      cache=CacheConfig(mode='off')) as session:
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
        session.agent.act_once('Find the virtual item in Results')
        assert frame.locator('#results').evaluate('el => el.scrollLeft') > 100
        assert frame.locator('#results').get_by_role('button', name='Virtual item').is_visible()
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
