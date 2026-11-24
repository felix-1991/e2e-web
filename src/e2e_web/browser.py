"""Web-only engine over a borrowed synchronous Playwright Page."""

from contextlib import contextmanager, ExitStack
from pathlib import Path
from dataclasses import asdict
import hashlib
import json
import uuid
import time
from typing import Any
from urllib.parse import urljoin, urlsplit

from .actions import TAB_OPS, VISUAL_OPS, press_sequence, validate_action, validate_target
from .config import _ObservationSettings
from .errors import E2EError, StepTimeout
from .tabs import Tabs
from .types import Secret, ObservationConfig
from . import semantics
from ._utils import canonical


def navigation_url(value: str, base_url: str | None = None) -> str:
    """Apply the agent navigation policy before handing a URL to Playwright."""
    try:
        if not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError()
        original = urlsplit(value)
        if original.scheme in {"http", "https"} and not original.hostname:
            raise ValueError()
        url = urljoin(base_url or "", value)
        parts = urlsplit(url)
        if url == "about:blank":
            return url
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ValueError()
        parts.port  # Validate malformed ports as well.
        return url
    except ValueError:
        raise E2EError("POLICY_DENIED", "navigation requires a valid http/https URL or exact about:blank") from None


class BrowserEngine:
    """Do not own or change the caller's Page/context default timeout."""

    name = "playwright-web-v1"

    def __init__(self, page: Any, *, base_url: str | None = None, upload_roots: tuple[Path, ...] = (),
                 observation: ObservationConfig | None = None, own_initial: bool = False):
        self._tabs = Tabs(page, own_initial=own_initial)
        self.base_url = base_url or page.url
        if observation is None:
            observation = ObservationConfig(**_ObservationSettings.resolve().model_dump())
        self.observation = observation
        limits = asdict(self.observation)
        for key, value in limits.items():
            low, high = (4096, 1048576) if key == 'max_bytes' else (1, 100000)
            if type(value) is not int or not low <= value <= high:
                raise E2EError('INVALID_ARGUMENT', f'invalid observation limit {key}')
        if self.observation.max_depth > 8 or self.observation.max_frames > 100:
            raise E2EError('INVALID_ARGUMENT', 'observation depth is at most 8 and frames at most 100')
        self._observation_id = None
        self._nodes = {}
        self._scopes = {}
        self._visual = None
        self.upload_roots = tuple(Path(p).resolve() for p in upload_roots)

    @property
    def page(self):
        return self._tabs.page

    @property
    def current_url(self) -> str:
        return self.page.url

    @property
    def active_tab(self) -> int:
        self._tabs.refresh()
        return self._tabs.active

    def resolve_navigation(self, value: str) -> str:
        return navigation_url(value, self.base_url)

    @staticmethod
    def _semantic_matches(nodes, target):
        matches = [node for node in nodes.values() if node['role'] == target['value']
                   and node['name'] == target['name']
                   and (not target.get('test_id') or node['test_id'] == target['test_id'])]
        within = target.get('within')
        if within is not None:
            parents = [node['id'] for node in nodes.values()
                       if all(node[key] == value for key, value in within.items())]
            if len(parents) != 1:
                return []
            matches = [node for node in matches
                       if any(parent['id'] == parents[0] for parent in semantics.ancestors(node, nodes))]
        return matches

    def target(self, descriptor: dict[str, Any], *, timeout: float = 3, within=None):
        """Compile a replayable descriptor into a native Playwright Locator."""
        end = time.monotonic() + timeout
        target = validate_target(descriptor)
        if target['by'] == 'ref':
            if target['observation'] != self._observation_id or target['value'] not in self._nodes:
                raise E2EError('NODE_STALE', 'reference is not in the latest observation')
            node = self._nodes[target['value']]
            scope = self._scopes[tuple(node['frame'])]
            parent = self.page
            for selector in node['frame']:
                if not parent.locator(selector).evaluate(semantics.IN_ACTIVE_MODAL, timeout=self._remaining_ms(end)):
                    raise E2EError('NODE_STALE', 'observed iframe is now behind a modal dialog')
                parent = parent.frame_locator(selector)
            element = semantics.resolve(scope, node['id'], timeout=timeout)
            if element is None or not element.evaluate(semantics.VISIBLE):
                raise E2EError('NODE_STALE', 'observed node is detached or hidden')
            current = element.evaluate(semantics.DESCRIBE)
            if any(current[k] != node[k] for k in ('role', 'name', 'test_id', 'input_type')):
                raise E2EError('NODE_STALE', 'observed node identity changed')
            context = next((parent for parent in semantics.ancestors(node, self._nodes)
                            if parent['role'] in {'row', 'listitem'}), None)
            if context and not element.evaluate(semantics.MATCHES_CONTEXT,
                                                {key: context[key] for key in ('id', 'role', 'name', 'test_id')}):
                raise E2EError('NODE_STALE', 'observed row or list item changed')
            return element
        scope = self.page
        frames = target.get("frame", [])
        for index, selector in enumerate([frames] if isinstance(frames, str) else frames):
            if not scope.locator(selector).evaluate(semantics.IN_ACTIVE_MODAL, timeout=self._remaining_ms(end)):
                raise E2EError('NODE_STALE', 'target iframe is behind a modal dialog')
            if 'frame_identity' in target:
                identity = scope.locator(selector).evaluate(semantics.FRAME_IDENTITY, timeout=timeout*1000)
                if identity != target['frame_identity'][index]:
                    raise E2EError('NODE_STALE', 'recorded iframe identity changed')
                duplicates = scope.locator('iframe').evaluate_all("(frames, identity) => frames.filter(el => ['id','name','src','title'].every(k => (el.getAttribute(k)||'') === identity[k])).length", identity)
                if duplicates != 1:
                    raise E2EError('LOCATOR_AMBIGUOUS', 'recorded iframe identity is not unique')
            scope = scope.frame_locator(selector)
        if target['by'] == 'semantic':
            result = semantics.scan(scope, max_nodes=self.observation.max_nodes,
                                    max_elements=self.observation.max_elements, timeout=timeout,
                                    root=within, replace=False)
            matches = [node for node in self._semantic_matches({node['id']: node for node in result['nodes']}, target)
                       if node['id'] != result['root_id']]
            if result['truncated']:
                raise E2EError('NODE_STALE', 'relocation scan truncated; cannot prove uniqueness')
            if len(matches) != 1:
                raise E2EError('LOCATOR_AMBIGUOUS' if matches else 'LOCATOR_NOT_FOUND', 'semantic relocation must match one visible node')
            remaining = end - time.monotonic()
            if remaining <= 0:
                raise StepTimeout('STEP_TIMEOUT', 'relocation deadline elapsed')
            element = semantics.resolve(scope, matches[0]['id'], timeout=remaining, searched=True)
            if element is None:
                raise E2EError('NODE_STALE', 'relocated node detached')
            return element
        by, value, exact = target["by"], target["value"], target.get("exact", True)
        if by == "role":
            locator = scope.get_by_role(value, **({"name": target["name"], "exact": exact} if "name" in target else {}))
        elif by == "text":
            locator = scope.get_by_text(value, exact=exact)
        elif by == "label":
            locator = scope.get_by_label(value, exact=exact)
        elif by == "placeholder":
            locator = scope.get_by_placeholder(value, exact=exact)
        elif by == "test_id":
            locator = scope.get_by_test_id(value)
        else:
            locator = scope.locator(value)
        if within is None:
            return locator.nth(target["nth"]) if "nth" in target else locator
        indices = locator.evaluate_all(semantics.DESCENDANTS, within)
        if 'nth' in target:
            indices = indices[target['nth']:target['nth'] + 1]
        if len(indices) != 1:
            raise E2EError('LOCATOR_AMBIGUOUS' if indices else 'LOCATOR_NOT_FOUND',
                           'search target must match one element inside the container')
        return locator.nth(indices[0])

    def observe(self, *, timeout: float) -> dict[str, Any]:
        """Build a bounded semantic subset across light, open and tracked closed DOM."""
        end = time.monotonic() + timeout
        self._observation_id = uuid.uuid4().hex
        self._nodes = {}
        self._scopes = {}
        page = self.page
        frames, all_nodes, queue = [], [], [(page, [], [])]
        visited, truncated = 0, False
        try:
            while queue:
                scope, path, identities = queue.pop(0)
                remaining = end - time.monotonic()
                if remaining <= 0:
                    raise StepTimeout('STEP_TIMEOUT', 'observation deadline elapsed')
                count = self.observation.max_nodes - len(all_nodes)
                elements = self.observation.max_elements - visited
                if count <= 0 or elements <= 0:
                    truncated = True
                    break
                try:
                    result = semantics.scan(scope, max_nodes=count, max_elements=elements, timeout=remaining)
                except Exception:
                    if not path:
                        raise
                    frames.append(dict(selector=' >> '.join(path), frame=path, tree='<frame unavailable>'))
                    continue
                visited += result['visited']
                truncated |= result['truncated']
                nodes = result['nodes']
                self._scopes[tuple(path)] = scope
                for node in nodes:
                    node['frame'] = path
                    node['frame_identity'] = identities
                all_nodes.extend(nodes)
                tree_lines, depths = [], {}
                for node in nodes:
                    depth = depths.get(node['parent_id'], -1) + 1
                    depths[node['id']] = depth
                    if node.pop('native_option', False):
                        continue
                    indent = '  ' * min(depth, 8)
                    line = indent + '- ' + node['role'] + ' ' + json.dumps(node['name'], ensure_ascii=False)
                    if 'options' in node:
                        tree_lines.append(line + ':')
                        for option in node['options']:
                            tree_lines.append(indent + '  - option ' + json.dumps(option['name'], ensure_ascii=False)
                                              + (' [selected]' if option['selected'] else '')
                                              + (' [disabled]' if option['disabled'] else ''))
                        if node['options_truncated']:
                            tree_lines.append(indent + '  … (options truncated)')
                    else:
                        tree_lines.append(line + (': ' + node['value'] if node['value'] else '')
                                          + (' [checked]' if node['checked'] else '')
                                          + (' [selected]' if node['selected'] else ''))
                tree = '\n'.join(tree_lines)
                if not path:
                    main_tree = tree
                else:
                    frames.append(dict(selector=' >> '.join(path), frame=path, tree=tree))
                frame_locators = scope.locator('iframe')
                indices = (frame_locators.evaluate_all(semantics.MODAL_FRAME_INDICES) if result['modal']
                           else range(frame_locators.count()))
                children = len(indices)
                if len(path) >= self.observation.max_depth:
                    truncated |= children > 0
                    continue
                capacity = max(0, self.observation.max_frames - len(frames) - len(queue))
                truncated |= children > capacity
                for index in indices[:capacity]:
                    selector = f'iframe >> nth={index}'
                    identity = scope.locator(selector).evaluate(semantics.FRAME_IDENTITY, timeout=max(1, (end-time.monotonic())*1000))
                    queue.append((scope.frame_locator(selector), [*path, selector], [*identities, identity]))
            title = page.title()
            tabs = self._tabs.summary()
            if page is not self.page:
                raise E2EError('NODE_STALE', 'active tab changed during observation')
            observation = dict(id=self._observation_id, url=page.url[:2048], title=title[:512],
                               viewport=page.viewport_size,
                               tree=locals().get('main_tree', ''), frames=frames, nodes=all_nodes,
                               truncated=truncated, visited=visited, **tabs)
            # Account for the serialized envelope, not just individual text fields.
            while len(canonical(observation).encode()) > self.observation.max_bytes:
                observation['truncated'] = True
                if observation['tree']:
                    observation['tree'] = observation['tree'][:len(observation['tree']) // 2]
                elif observation['frames']:
                    observation['frames'].pop()
                elif observation['nodes']:
                    observation['nodes'].pop()
                elif observation['tabs']:
                    observation['tabs'].pop()
                else:
                    raise E2EError('OBSERVATION_TOO_LARGE', 'observation metadata exceeds byte budget')
            self._nodes = {n['id']: n for n in observation['nodes']}
            self._observation_truncated = observation['truncated']
            return observation
        except E2EError:
            raise
        except Exception as exc:
            raise self.translate(exc) from None

    def record_action(self, action: dict) -> dict | None:
        """Persist semantic anchors instead of document-local references."""
        action = dict(action)
        for key in ('target', 'to'):
            target = action.get(key)
            if target and target['by'] == 'ref':
                if self._observation_truncated:
                    return None
                node = self._nodes[target['value']]
                descriptor = dict(by='semantic', value=node['role'], name=node['name'], test_id=node['test_id'])
                scope = {n['id']: n for n in self._nodes.values() if n['frame'] == node['frame']}
                if len(self._semantic_matches(scope, descriptor)) != 1:
                    for parent in semantics.ancestors(node, scope):
                        if not (parent['name'] or parent['test_id']):
                            continue
                        context_key = 'test_id' if parent['test_id'] else 'name'
                        descriptor['within'] = {'role': parent['role'], context_key: parent[context_key]}
                        if len(self._semantic_matches(scope, descriptor)) == 1:
                            break
                    else:
                        return None
                if node['frame']:
                    descriptor.update(frame=node['frame'], frame_identity=node['frame_identity'])
                action[key] = descriptor
        return action

    def _scroll_find(self, action, *, end):
        target = action['target']
        container = action.get('container')
        page = self.page
        handle = None
        if container is not None:
            frame = container.get('frame', [])
            if container['by'] == 'ref':
                node = self._nodes.get(container['value'])
                frame = node['frame'] if node else []
            frame = [frame] if isinstance(frame, str) else frame
            requested = target.get('frame', frame)
            if frame and 'frame' not in target:
                target = dict(target, frame=frame)
            element = self.target(container, timeout=max(0.001, end - time.monotonic()))
            handle = (element.handle if isinstance(element, semantics.ElementTarget)
                      else element.element_handle(timeout=max(1, (end - time.monotonic()) * 1000)))

        x, y = {'up': (0, -1), 'down': (0, 1), 'left': (-1, 0), 'right': (1, 0)}[action['direction']]
        scroll_scope = page if handle is None else handle
        stopped = False
        try:
            if handle is not None:
                requested = [requested] if isinstance(requested, str) else requested
                if frame != requested:
                    scope = self.page
                    for selector in requested:
                        scope = scope.frame_locator(selector)
                    document = scope.locator('html').element_handle(timeout=max(1, (end - time.monotonic()) * 1000))
                    try:
                        if document.owner_frame() != handle.owner_frame():
                            raise E2EError('INVALID_ARGUMENT', 'search target and container must be in the same frame')
                    finally:
                        document.dispose()
            for attempt in range(action['steps'] + 1):
                if page.is_closed():
                    raise E2EError('NODE_STALE', 'tab closed during scroll search')
                remaining = end - time.monotonic()
                if remaining <= 0:
                    raise StepTimeout('STEP_TIMEOUT', 'scroll search deadline elapsed')
                try:
                    match = self.target(target, timeout=remaining, within=handle)
                    if match.is_visible() and (handle is None or match.evaluate(
                        semantics.CONTAINS, handle, timeout=max(1, (end - time.monotonic()) * 1000)
                    )):
                        match.scroll_into_view_if_needed(timeout=max(1, (end - time.monotonic()) * 1000))
                        return None
                except E2EError as exc:
                    if attempt and exc.code == 'NODE_STALE':
                        raise E2EError('ACTION_FAILED', 'target became stale after scrolling') from None
                    if exc.code != 'LOCATOR_NOT_FOUND':
                        raise
                if stopped or attempt == action['steps']:
                    break
                position = scroll_scope.evaluate(semantics.SCROLL_STATE)
                if handle is None:
                    size = page.viewport_size or page.evaluate('() => ({width: innerWidth, height: innerHeight})')
                    page.mouse.wheel(x * size['width'] * 0.8, y * size['height'] * 0.8)
                else:
                    if not handle.evaluate('el => el.isConnected'):
                        raise E2EError('ACTION_FAILED', 'scroll container was detached during search')
                    handle.evaluate(semantics.SCROLL, dict(x=x, y=y, page=True))
                # A virtual list can extend its scroll range after reaching its old end.
                settle_end = min(end, time.monotonic() + 1)
                while scroll_scope.evaluate(semantics.SCROLL_STATE) == position:
                    remaining = settle_end - time.monotonic()
                    if remaining <= 0:
                        stopped = True
                        break
                    time.sleep(min(0.05, remaining))
            raise E2EError('LOCATOR_NOT_FOUND', 'bounded scroll search did not find target')
        finally:
            if handle is not None:
                handle.dispose()

    @staticmethod
    def _remaining_ms(end):
        remaining = end - time.monotonic()
        if remaining <= 0:
            raise StepTimeout('STEP_TIMEOUT', 'action deadline elapsed')
        return max(1, remaining * 1000)

    @contextmanager
    def _element(self, target, *, end):
        handle = (target.handle if isinstance(target, semantics.ElementTarget)
                  else target.element_handle(timeout=self._remaining_ms(end)))
        try:
            yield handle
        finally:
            handle.dispose()

    def _select(self, target, value, *, end):
        if isinstance(value, str):
            return target.select_option(label=value, timeout=self._remaining_ms(end))
        batch = isinstance(value, list)
        choices = [{'label': option} if isinstance(option, str) else option
                   for option in (value if batch else [value])]
        with self._element(target, end=end) as element, ExitStack() as handles:
            result = element.evaluate_handle(semantics.SELECT_OPTIONS, dict(choices=choices, batch=batch))
            handles.callback(result.dispose)
            problem = result.evaluate('result => result.problem || null')
            if problem:
                code, message = {
                    'not-select': ('ACTION_FAILED', 'select requires a native select element'),
                    'not-multiple': ('ACTION_FAILED', 'a list of options requires a native multiple select'),
                    'missing': ('LOCATOR_NOT_FOUND', 'a requested select option was not found'),
                    'ambiguous': ('LOCATOR_AMBIGUOUS', 'a select option is ambiguous; use its value or index'),
                    'disabled': ('ACTION_FAILED', 'a requested select option is disabled'),
                }[problem]
                raise E2EError(code, message)
            select = result.get_property('select').as_element()
            handles.callback(select.dispose)
            if not choices:
                return select.select_option(label=[], timeout=self._remaining_ms(end))
            options = result.get_property('options')
            handles.callback(options.dispose)
            selected = list(options.get_properties().values())
            for option in selected:
                handles.callback(option.dispose)
            return select.select_option(element=[option.as_element() for option in selected],
                                        timeout=self._remaining_ms(end))

    def _type_focused(self, value, *, replace, end):
        frame = self.page.main_frame
        while frame is not None:
            self._remaining_ms(end)
            handle = frame.evaluate_handle(semantics.FOCUSED_ELEMENT)
            try:
                element = handle.as_element()
                if element is None:
                    break
                frame = element.content_frame()
                if frame is not None:
                    continue
                kind = element.evaluate(semantics.FOCUSED_INPUT)
                if kind is None:
                    break
                if replace:
                    if kind != 'editable':
                        raise E2EError('ACTION_FAILED', 'replace requires a focused editable field')
                    return element.fill(value, timeout=self._remaining_ms(end))
                return element.type(value, timeout=self._remaining_ms(end))
            finally:
                handle.dispose()
        raise E2EError('ACTION_FAILED', 'no writable field or keyboard control has focus')

    def _set_checked(self, target, checked, *, end):
        with self._element(target, end=end) as element:
            state = element.evaluate(semantics.CHECK_STATE)
            current = element.is_checked()
            if current == checked and not state['mixed']:
                return None
            if not checked and state['type'] == 'radio':
                raise E2EError('ACTION_FAILED', 'choose another radio button to change a radio selection')
            if current == checked and state['type'] == 'checkbox':
                raise E2EError('ACTION_FAILED', 'clicking this mixed checkbox would reverse the requested state')
            element.click(timeout=self._remaining_ms(end))
            first = True
            while True:
                self._remaining_ms(end)
                try:
                    state = element.evaluate(semantics.CHECK_STATE)
                    if not state['connected']:
                        if first:
                            return None
                        raise E2EError('ACTION_FAILED', 'checkable element was replaced before reaching the requested state')
                    if not state['mixed'] and element.is_checked() == checked:
                        return None
                except Exception as exc:
                    detail = str(exc).lower()
                    if first and ('not attached to the dom' in detail or 'execution context was destroyed' in detail):
                        return None
                    raise
                first = False
                time.sleep(min(0.05, self._remaining_ms(end) / 1000))

    def perform(self, action: dict[str, Any], *, timeout: float) -> Any:
        """Dispatch only the validated portable action grammar."""
        if not isinstance(action, dict) or action.get('op') != 'press' or 'times' not in action:
            return self._perform(action, timeout=timeout)
        end = time.monotonic() + timeout
        for press in press_sequence(validate_action(action)):
            self._perform(press, timeout=self._remaining_ms(end) / 1000)

    @contextmanager
    def _input_target(self, descriptor, *, timeout):
        """Inspect and pin the same element that will receive the input."""
        end = time.monotonic() + timeout
        with ExitStack() as stack:
            try:
                target = self.target(descriptor, timeout=timeout)
                element = stack.enter_context(self._element(target, end=end))
                if not element.evaluate(semantics.IN_ACTIVE_MODAL):
                    raise E2EError('NODE_STALE', 'input target is behind a modal dialog')
                one_time_code = element.evaluate('el => el.matches(\'[autocomplete~="one-time-code" i]\')')
            except E2EError:
                raise
            except Exception as exc:
                raise self.translate(exc) from None
            yield semantics.ElementTarget(element), one_time_code

    def _perform(self, action: dict[str, Any], *, timeout: float, input_target=None) -> Any:
        action = validate_action(action)
        end = time.monotonic() + timeout
        op, ms = action["op"], max(1, timeout * 1000)
        try:
            if op in VISUAL_OPS:
                return self._perform_visual(action, timeout=timeout)
            if op == 'scroll_find':
                return self._scroll_find(action, end=end)
            if op == "navigate":
                url = self.resolve_navigation(action["url"])
                response = self.page.goto(url, wait_until="domcontentloaded", timeout=ms)
                return {"url": self.page.url, "status": response.status if response else None}
            if op in {"back", "forward", "reload"}:
                method = {"back": "go_back", "forward": "go_forward", "reload": "reload"}[op]
                response = getattr(self.page, method)(wait_until="domcontentloaded", timeout=ms)
                return {"url": self.page.url, "status": response.status if response else None}
            if op == 'resize_viewport':
                self._visual = self._observation_id = None
                self._remaining_ms(end)
                size = dict(width=action['width'], height=action['height'])
                self.page.set_viewport_size(size)
                return size
            if op in TAB_OPS:
                self._remaining_ms(end)
                if op == 'switch_tab':
                    self._tabs.switch(action['tab'])
                else:
                    self._tabs.close(action['tab'])
                self._visual = self._observation_id = None
                return self._tabs.summary()
            if op == "scroll" and "target" not in action:
                return self.page.mouse.wheel(action["x"], action["y"])
            if op == "wait":
                if action["seconds"] >= timeout:
                    raise StepTimeout("STEP_TIMEOUT", "requested wait exceeds remaining timeout")
                self.page.wait_for_timeout(action["seconds"] * 1000)
                return None
            if op in {"tool", "fill_secret"}:
                raise E2EError("POLICY_DENIED", "harness must resolve tools and secrets")
            if op == 'type' and 'target' not in action:
                return self._type_focused(action['value'], replace=action.get('replace', False), end=end)
            if op == 'press' and 'target' not in action:
                self._remaining_ms(end)
                return self.page.keyboard.press(action['value'])
            target = self.target(action["target"], timeout=timeout) if input_target is None else input_target
            remaining = end - time.monotonic()
            if remaining <= 0:
                raise StepTimeout('STEP_TIMEOUT', 'action deadline elapsed')
            ms = remaining * 1000
            if op == 'scroll':
                return target.evaluate(semantics.SCROLL, dict(x=action['x'], y=action['y'], page=False), timeout=ms)
            if op == 'scroll_to':
                return target.scroll_into_view_if_needed(timeout=ms)
            if op == "upload":
                paths = [Path(p).expanduser().resolve() for p in action["value"]]
                if any(not p.is_file() or not any(p.is_relative_to(root) for root in self.upload_roots) for p in paths):
                    raise E2EError("POLICY_DENIED", "upload path is not a file under an explicit upload root")
                files = [str(p) for p in paths]
                if target.evaluate("""el => {
                    const input = el.closest('label')?.control || el;
                    return input.matches('input[type=file]');
                }""", timeout=ms):
                    return target.set_input_files(files, timeout=max(1, (end - time.monotonic()) * 1000))
                with self.page.expect_file_chooser(timeout=max(1, (end - time.monotonic()) * 1000)) as chooser:
                    target.click(timeout=max(1, (end - time.monotonic()) * 1000))
                return chooser.value.set_files(files, timeout=max(1, (end - time.monotonic()) * 1000))
            if op == "drag":
                destination = self.target(action['to'], timeout=remaining)
                if hasattr(target, 'drag_to') and action['to']['by'] not in {'ref', 'semantic'}:
                    return target.drag_to(destination, timeout=ms)
                target.scroll_into_view_if_needed(timeout=ms)
                destination.scroll_into_view_if_needed(timeout=ms)
                start, finish = target.bounding_box(), destination.bounding_box()
                if not start or not finish:
                    raise E2EError('NODE_STALE', 'drag target is no longer visible')
                self.page.mouse.move(start['x']+start['width']/2, start['y']+start['height']/2)
                self.page.mouse.down()
                try:
                    self.page.mouse.move(finish['x']+finish['width']/2, finish['y']+finish['height']/2, steps=10)
                finally:
                    self.page.mouse.up()
                return None
            if op == "select":
                return self._select(target, action['value'], end=end)
            if op in {'check', 'uncheck'}:
                return self._set_checked(target, op == 'check', end=end)
            if op in {"fill", "type", "press"}:
                method = {"fill": "fill", "type": "press_sequentially", "press": "press"}[op]
                return getattr(target, method)(action["value"], timeout=ms)
            if op in {'click', 'double_click', 'right_click', 'long_press'}:
                method = target.dblclick if op == 'double_click' else target.click
                options = {}
                if 'modifiers' in action:
                    options['modifiers'] = action['modifiers']
                if op == 'right_click':
                    options['button'] = 'right'
                if op == 'long_press':
                    if ms <= 500:
                        raise StepTimeout('STEP_TIMEOUT', 'remaining timeout is too short for a long press')
                    options['delay'] = 500
                return method(timeout=ms, **options)
            return getattr(target, op)(timeout=ms)
        except E2EError:
            raise
        except Exception as exc:
            raise self.translate(exc) from None

    def screenshot(self, *, timeout: float) -> bytes:
        """Capture pixels only when the harness has allowed secret-safe vision."""
        return self.page.screenshot(timeout=max(1, timeout * 1000), scale='css')

    def capture_vision(self, *, timeout: float):
        """Issue a one-use screenshot token with CSS-pixel coordinates."""
        end = time.monotonic() + timeout
        data = self.screenshot(timeout=timeout)
        # Initial canvas compositing can settle after the first capture.
        for _ in range(2):
            remaining = end - time.monotonic()
            if remaining <= 0:
                raise StepTimeout('STEP_TIMEOUT', 'screenshot deadline elapsed')
            next_image = self.screenshot(timeout=remaining)
            same = next_image == data
            data = next_image
            if same:
                break
        size = self.page.evaluate('({width:innerWidth,height:innerHeight})')
        self._visual = dict(id=uuid.uuid4().hex, digest=hashlib.sha256(data).hexdigest(),
                            url=self.page.url, **size)
        return data, {k: self._visual[k] for k in ('id', 'width', 'height')}

    @contextmanager
    def _at_point(self, x, y, *, end):
        page = self.page
        frame, point = page.main_frame, dict(x=x, y=y)
        with ExitStack() as stack:
            for _ in range(self.observation.max_depth + 1):
                semantics.install(frame, timeout=self._remaining_ms(end) / 1000)
                handle = frame.evaluate_handle(semantics.ELEMENT_AT, point)
                stack.callback(handle.dispose)
                element = handle.as_element()
                child = element.content_frame() if element is not None else None
                if child is None:
                    yield element
                    return
                point = element.evaluate(semantics.FRAME_POINT, point)
                if point is None:
                    break
                frame = child
            yield None

    def _perform_visual(self, action, *, timeout):
        end = time.monotonic() + timeout
        visual = self._visual
        self._visual = None
        if not visual or action['screenshot'] != visual['id'] or self.page.url != visual['url']:
            raise E2EError('NODE_STALE', 'coordinate action requires the latest screenshot')
        for x, y in [(action['x'], action['y'])] + ([(action['to_x'], action['to_y'])] if action['op'] == 'drag_at' else []):
            if x >= visual['width'] or y >= visual['height']:
                raise E2EError('POLICY_DENIED', 'coordinate is outside the captured viewport')
        current = self.screenshot(timeout=timeout)
        if hashlib.sha256(current).hexdigest() != visual['digest']:
            raise E2EError('NODE_STALE', 'screen changed since screenshot; observe again')
        self._remaining_ms(end)
        op = action['op']
        if op in {'select_at', 'press_at'} or (op == 'type_at' and action.get('replace')):
            with self._at_point(action['x'], action['y'], end=end) as element:
                if op == 'select_at':
                    if element is None or not element.evaluate("el => el.matches('select')"):
                        raise E2EError('LOCATOR_NOT_FOUND', 'no native select at screenshot point')
                    return self._select(semantics.ElementTarget(element), action['value'], end=end)
                if op == 'type_at':
                    if element is None or element.evaluate(semantics.FOCUSED_INPUT) != 'editable':
                        raise E2EError('LOCATOR_NOT_FOUND', 'no editable input at screenshot point')
                    return element.fill(action['value'], timeout=self._remaining_ms(end))
                if element is None:
                    raise E2EError('LOCATOR_NOT_FOUND', 'no interactive area at screenshot point')
                if element.evaluate('el => el.tabIndex >= 0 || el.isContentEditable'):
                    return element.press(action['value'], timeout=self._remaining_ms(end))
            self._remaining_ms(end)
            self.page.mouse.click(action['x'], action['y'])
            self._remaining_ms(end)
            return self.page.keyboard.press(action['value'])
        if op == 'hover_at':
            self.page.mouse.move(action['x'], action['y'])
        elif op == 'drag_at':
            self.page.mouse.move(action['x'], action['y'])
            self.page.mouse.down()
            try:
                self.page.mouse.move(action['to_x'], action['to_y'], steps=10)
            finally:
                self.page.mouse.up()
        else:
            self.page.mouse.click(action['x'], action['y'])
            if op == 'type_at':
                self.page.keyboard.insert_text(action['value'])

    def fill_secret(self, descriptor: dict[str, Any], secret: Secret, *, timeout: float) -> None:
        """Authorize the current sink before resolving a secret into a field."""
        end = time.monotonic() + timeout

        def remaining():
            seconds = end - time.monotonic()
            if seconds <= 0:
                raise StepTimeout("STEP_TIMEOUT", "secret fill deadline elapsed")
            return seconds * 1000

        try:
            target = self.target(descriptor, timeout=timeout)
            editable_kind = target.evaluate("""el => el.isContentEditable || el.tagName === 'TEXTAREA' ||
                (el.tagName === 'INPUT' && !['button','submit','reset','checkbox','radio','file',
                 'image','range','color','hidden'].includes(el.type))""", timeout=remaining())
            if not editable_kind or not target.is_enabled(timeout=remaining()) or not target.is_editable(timeout=remaining()):
                raise E2EError("POLICY_DENIED", "secret target must be enabled and editable")
            if secret.purpose == "password" and target.get_attribute("type", timeout=remaining()) != "password":
                raise E2EError("POLICY_DENIED", "password secrets require a password field")
            target.fill(secret.value, timeout=remaining())
        except E2EError:
            raise
        except Exception as exc:
            raise self.translate(exc) from None

    @staticmethod
    def translate(exc: Exception) -> E2EError:
        """Classify browser failures without persisting potentially sensitive messages."""
        if "strict mode violation" in str(exc):
            return E2EError("LOCATOR_AMBIGUOUS", "locator matched multiple elements")
        if "Timeout" in type(exc).__name__:
            return StepTimeout("STEP_TIMEOUT", "browser operation timed out")
        return E2EError("ACTION_FAILED", "browser operation failed")
