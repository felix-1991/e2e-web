"""Web-only engine over a borrowed synchronous Playwright Page."""

from contextlib import contextmanager, ExitStack
from weakref import WeakSet
from pathlib import Path
from dataclasses import asdict
import hashlib
import json
import re
import uuid
import time
from typing import Any
from urllib.parse import urljoin, urlsplit

from .actions import TAB_OPS, VISUAL_OPS, press_sequence, validate_action, validate_target
from .config import _ObservationSettings
from .errors import E2EError, ReplayStale, StepTimeout
from .image import MAX_SCREENSHOT_LONG_SIDE, downscale_png
from .samples import SAMPLE_PREFIX, resolve_sample
from .tabs import Tabs
from .types import Secret, ObservationConfig
from . import semantics
from ._utils import canonical, digest, png_size


# Playwright logs these lines only once an input is being dispatched: a
# failure that reaches them may already have committed the action.
_POST_DISPATCH = re.compile(
    r"^\s*- (?:performing \w+ action|[\w ]+ action done|waiting for scheduled navigations to finish)\s*$",
    re.MULTILINE | re.IGNORECASE,
)


def _may_have_committed(exc: Exception) -> bool:
    """Whether a failed action's call log shows that its input was dispatched."""
    log = getattr(exc, "_log", None)
    if log is None:
        log = getattr(exc, "log", None)
    lines = [str(line) for line in log] if isinstance(log, (list, tuple)) else []
    return bool(_POST_DISPATCH.search("\n".join([str(exc), *lines])))


_MINTED_ID = re.compile(
    r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
    r'|(?<![a-z0-9])(?=[0-9a-f]*[a-f])(?=[0-9a-f]*[0-9])[0-9a-f]{8,}(?![a-z0-9])'
    r'|(?<![a-z0-9])1[5-9][0-9]{8}(?:[0-9]{3})?(?![a-z0-9])',
    re.IGNORECASE | re.ASCII,
)


def _test_id_shape(value):
    # Keep the literal pieces, including separators. A literal marker in a test
    # id cannot collide with a generated piece.
    return tuple(_MINTED_ID.split(value)) if _MINTED_ID.search(value) else None


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


def _site(host: str) -> str:
    """A host's registrable domain, approximate when the suffix list is unknown."""
    host = host.lower().rstrip(".")
    if not host or re.fullmatch(r"[0-9.]+", host) or ":" in host:
        return host
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    if len(labels[-1]) == 2 and len(labels[-2]) <= 3:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def frame_on_site(frame_url: str, app_url: str) -> bool:
    """Whether a child document belongs to the app's own site.

    Third-party frames (ads, trackers, embeds) are not the agent's to read or
    act on, and a stalled ad frame must not tax the capture. A document that
    inherits its parent's origin (`about:blank`, `srcdoc`) is the app's own;
    a `data:` document has an opaque origin and is not.
    """
    if not frame_url or frame_url in {"about:blank", "about:srcdoc"}:
        return True
    try:
        app, frame = urlsplit(app_url), urlsplit(frame_url)
        return bool(app.scheme in {"http", "https"} and app.hostname
                    and frame.scheme in {"http", "https"} and frame.hostname
                    and _site(frame.hostname) == _site(app.hostname))
    except ValueError:
        return False


class BrowserEngine:
    """Do not own or change the caller's Page/context default timeout."""

    name = "playwright-web-v1"

    def __init__(self, page: Any, *, base_url: str | None = None,
                 upload_roots: tuple[Path, ...] | None = None,
                 observation: ObservationConfig | None = None, own_initial: bool = False,
                 test_id_attribute: str = "data-testid"):
        self.test_id_attribute = test_id_attribute
        self._dialog_policy = {'action': 'dismiss'}
        self._dialogs: list[dict[str, Any]] = []
        self._downloads: list[dict[str, Any]] = []
        self._dialog_pages = WeakSet()
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
        self._recorded_pixels = None
        self.masked_vision = False
        # No explicit roots means the project the tests run in, as upstream:
        # hidden files and paths outside the root stay refused.
        self.upload_roots = (tuple(Path(p).resolve() for p in upload_roots)
                             if upload_roots is not None else (Path.cwd().resolve(),))

        self._tabs = Tabs(page, own_initial=own_initial, on_page=self._install_dialogs)

    def detach_page(self, page) -> None:
        if page in self._dialog_pages:
            page.remove_listener('dialog', self._on_dialog)
            page.remove_listener('download', self._on_download)
            self._dialog_pages.discard(page)

    def detach_events(self) -> None:
        for page in tuple(self._dialog_pages):
            self.detach_page(page)

    def _install_dialogs(self, page) -> None:
        """Answer every dialog on this session's own pages instead of auto-dismissing it."""
        if page is None or page in self._dialog_pages:
            return
        self._dialog_pages.add(page)
        page.on('dialog', self._on_dialog)
        page.on('download', self._on_download)

    def _on_download(self, download) -> None:
        """Report a download the app started, so a click that exported a file is visible.

        Only what is known at once is read: asking for the failure waits for the
        transfer, which would block the page's event loop.
        """
        self._downloads.append(dict(suggested_filename=(download.suggested_filename or '')[:256],
                                    url=(download.url or '')[:512]))
        del self._downloads[:-5]

    def _on_dialog(self, dialog) -> None:
        policy = self._dialog_policy
        record = dict(type=dialog.type, message=(dialog.message or '')[:512], action=policy['action'])
        try:
            if record['action'] == 'accept':
                dialog.accept(policy.get('text') if dialog.type == 'prompt' else None)
            else:
                dialog.dismiss()
        except Exception:
            record['action'] = 'unanswered'
        self._dialogs.append(record)
        del self._dialogs[:-5]

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
        base = self.base_url
        # A session opened on about:blank gets its app origin from the first
        # page it navigates to, so a recording's path resolves against that.
        if not base or urlsplit(base).scheme not in {"http", "https"}:
            base = self.page.url
        return navigation_url(value, base)

    @staticmethod
    def _semantic_matches(nodes, target, *, shape_only=False, relocating=False):
        """Match a recorded descriptor, strictest tier first.

        A test id is the strongest discriminator, but apps mint or churn one, so a
        node the remaining fields still name has not moved. Ambiguity is left to the
        caller, which accepts only one match or records where the target stood.
        """
        matches = [node for node in nodes.values() if node['role'] == target['value']
                   and node['name'] == target['name']
                   and all(node.get(k) == target[k] for k in ('href', 'level', 'input_type', 'description') if k in target)
                   # Text names a control only when nothing else does, so a counter or a
                   # row value that moved with the data cannot strand the target.
                   and ('text' not in target or target.get('name') or target.get('test_id')
                        or node.get('text') == target['text'])]
        within = target.get('within')
        if within is not None:
            parents = [node['id'] for node in nodes.values()
                       if all(node[key] == value for key, value in within.items())]
            if len(parents) > 1 and relocating:
                raise E2EError("LOCATOR_AMBIGUOUS", "semantic container is not unique")
            if len(parents) != 1:
                return []
            matches = [node for node in matches
                       if any(parent['id'] == parents[0] for parent in semantics.ancestors(node, nodes))]
        test_id = target.get('test_id')
        if not test_id:
            return matches
        exact = [node for node in matches if node['test_id'] == test_id]
        if not shape_only and (exact or not target.get('dynamic_test_id')):
            return exact
        shape = _test_id_shape(test_id)
        return [node for node in matches if shape is not None and _test_id_shape(node['test_id']) == shape]

    @staticmethod
    def _positioned(matches, target):
        """A recorded place resolves twins only when the count still reads the same.

        One twin where the recording counted two is as likely the other one, and a
        third twin is a screen the recording never saw, so neither is resolved.
        """
        position = target.get('position')
        if not position:
            return matches
        if len(matches) != position['of']:
            return []
        return [matches[position['index']]]

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
                if not parent.locator(selector).evaluate(semantics.VISIBLE, timeout=self._remaining_ms(end)):
                    raise E2EError('NODE_STALE', 'observed iframe is hidden, inert or behind a modal dialog')
                parent = parent.frame_locator(selector)
            element = semantics.resolve(scope, node['id'], timeout=timeout)
            if element is None or not element.evaluate(semantics.VISIBLE):
                raise E2EError('NODE_STALE', 'observed node is detached or hidden')
            current = element.evaluate(semantics.DESCRIBE)
            if any(current.get(k) != node.get(k) for k in ('role', 'name', 'test_id', 'input_type', 'href', 'level', 'description')):
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
            if target['by'] == 'semantic':
                semantics.install(scope, timeout=self._remaining_ms(end) / 1000)
                if not scope.locator(selector).evaluate(semantics.VISIBLE, timeout=self._remaining_ms(end)):
                    raise E2EError('NODE_STALE', 'semantic target iframe is hidden or behind a modal dialog')
            elif not scope.locator(selector).evaluate(semantics.IN_ACTIVE_MODAL, timeout=self._remaining_ms(end)):
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
            matches = [node for node in self._semantic_matches({node['id']: node for node in result['nodes']}, target, relocating=True)
                       if node['id'] != result['root_id']]
            if result['truncated']:
                raise E2EError('NODE_STALE', 'relocation scan truncated; cannot prove uniqueness')
            matches = self._positioned(matches, target)
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
        frames, all_nodes, queue = [], [], [(page, [], [], None)]
        visited, truncated = 0, False
        try:
            while queue:
                scope, path, identities, space = queue.pop(0)
                remaining = end - time.monotonic()
                if remaining <= 0:
                    raise StepTimeout('STEP_TIMEOUT', 'observation deadline elapsed')
                count = self.observation.max_nodes - len(all_nodes)
                elements = self.observation.max_elements - visited
                if count <= 0 or elements <= 0:
                    truncated = True
                    break
                try:
                    result = semantics.scan(scope, max_nodes=count, max_elements=elements, timeout=remaining, space=space)
                except Exception:
                    if not path:
                        raise
                    # A frame nobody could read cannot prove a control disappeared.
                    frames.append(dict(selector=' >> '.join(path), frame=path, tree='<frame unavailable>'))
                    truncated = True
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
                    if node.get('description'):
                        line += ' description=' + json.dumps(node['description'], ensure_ascii=False)
                    if node.get('text'):
                        line += ' text=' + json.dumps(node['text'], ensure_ascii=False)
                    if 'level' in node:
                        line += f" [level={node['level']}]"
                    for state in ('expanded', 'pressed'):
                        value = node[state]
                        if value is not None:
                            line += f' [{state}={str(value).lower()}]'
                    for state in ('disabled', 'readonly', 'indeterminate', 'secure'):
                        if node.get(state):
                            line += f' [{state}]'
                    if node.get('purpose'):
                        line += ' [purpose=' + node['purpose'] + ']'
                    for flag in ('aria-required', 'aria-invalid'):
                        if node.get('attributes', {}).get(flag) == 'true':
                            line += ' [' + flag[5:] + ']'
                    if node.get('value_text'):
                        line += ' [value_text=' + json.dumps(node['value_text'], ensure_ascii=False) + ']'
                    if node.get('message'):
                        line += ' [message=' + json.dumps(node['message'], ensure_ascii=False) + ']'
                    if 'options' in node:
                        tree_lines.append(line + ':')
                        for option in node['options']:
                            tree_lines.append(indent + '  - option ' + json.dumps(option['name'], ensure_ascii=False)
                                              + (' [selected]' if option['selected'] else '')
                                              + (' [disabled]' if option['disabled'] else '')
                                              + (' [group=' + json.dumps(option['group'], ensure_ascii=False) + ']'
                                                 if 'group' in option else ''))
                        if node['options_truncated']:
                            tree_lines.append(indent + '  … (options truncated)')
                    else:
                        value = node['value']
                        if value and value.splitlines() != [value]:
                            value = json.dumps(value, ensure_ascii=True)
                        tree_lines.append(line + (': ' + value if value else '')
                                          + (' [checked]' if node['checked'] else '')
                                          + (' [selected]' if node['selected'] else ''))
                tree = '\n'.join(tree_lines)
                if not path:
                    main_tree = tree
                else:
                    frames.append(dict(selector=' >> '.join(path), frame=path, tree=tree))
                frame_locators = scope.locator('iframe')
                indices = frame_locators.evaluate_all(semantics.VISIBLE_FRAME_INDICES)
                capacity = max(0, self.observation.max_frames - len(frames) - len(queue))
                for index in indices:
                    selector = f'iframe >> nth={index}'
                    element = scope.locator(selector).element_handle(timeout=self._remaining_ms(end))
                    try:
                        child_frame = element.content_frame() if element is not None else None
                        child_url = child_frame.url if child_frame is not None else ''
                    finally:
                        if element is not None:
                            element.dispose()
                    if child_frame is None or not frame_on_site(child_url, page.url):
                        # A third-party frame stays a boundary node; its absence is
                        # not truncation, because the agent never reads it.
                        continue
                    if len(path) >= self.observation.max_depth or capacity == 0:
                        truncated = True
                        break
                    capacity -= 1
                    info = scope.locator(selector).evaluate(semantics.FRAME_INFO, space, timeout=self._remaining_ms(end))
                    queue.append((scope.frame_locator(selector), [*path, selector], [*identities, info["identity"]], info["space"]))
            title = page.title()
            tabs = self._tabs.summary()
            if page is not self.page:
                raise E2EError('NODE_STALE', 'active tab changed during observation')
            dialogs, self._dialogs = self._dialogs, []
            downloads, self._downloads = self._downloads, []
            observation = dict(id=self._observation_id, url=page.url[:2048], title=title[:512],
                               viewport=page.viewport_size,
                               **({'dialogs': dialogs} if dialogs else {}),
                               **({'downloads': downloads} if downloads else {}),
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
        if action['op'] in VISUAL_OPS:
            if self._recorded_pixels is None:
                return None
            action['screenshot'] = self._recorded_pixels
            return action
        for key in ('target', 'to', 'container'):
            target = action.get(key)
            if target and target['by'] == 'ref':
                if self._observation_truncated:
                    return None
                node = self._nodes[target['value']]
                descriptor = dict(by='semantic', value=node['role'], name=node['name'], test_id=node['test_id'])
                for field in ('href', 'level', 'input_type'):
                    if node.get(field):
                        descriptor[field] = node[field]
                if node.get('text') and not (node['name'] or node['test_id']):
                    descriptor['text'] = node['text']
                if node['role'] == 'box' and not (node['name'] or node['test_id']):
                    return None
                scope = {n['id']: n for n in self._nodes.values() if n['frame'] == node['frame']}
                if node.get('description') and len(self._semantic_matches(scope, descriptor)) > 1:
                    descriptor['description'] = node['description']
                if len(self._semantic_matches(scope, descriptor)) != 1:
                    for parent in list(semantics.ancestors(node, scope))[:4]:
                        if not (parent['name'] or parent['test_id']):
                            continue
                        context_key = 'test_id' if parent['test_id'] else 'name'
                        descriptor['within'] = {'role': parent['role'], context_key: parent[context_key]}
                        if len(self._semantic_matches(scope, descriptor)) == 1:
                            break
                    else:
                        descriptor.pop('within', None)
                        # One identical "Set up" button per card must still record; its place
                        # among the twins is what tells it from its neighbours on replay.
                        twins = [match['id'] for match in self._semantic_matches(scope, descriptor)]
                        if not 2 <= len(twins) <= 1000 or node['id'] not in twins:
                            return None
                        descriptor['position'] = {'index': twins.index(node['id']), 'of': len(twins)}
                if _test_id_shape(node['test_id']):
                    dynamic = dict(descriptor)
                    if 'within' not in dynamic:
                        container = next((parent for parent in semantics.ancestors(node, scope)
                                          if parent['name'] or parent['test_id']), None)
                        if container:
                            field = 'test_id' if container['test_id'] else 'name'
                            dynamic['within'] = {'role': container['role'], field: container[field]}
                    if len(self._semantic_matches(scope, dynamic, shape_only=True)) == 1:
                        descriptor = dict(dynamic, dynamic_test_id=True)
                if node['frame']:
                    descriptor.update(frame=node['frame'], frame_identity=node['frame_identity'])
                action[key] = descriptor
        return action

    def _resolve_upload_files(self, paths):
        files = []
        for path in paths:
            if isinstance(path, str) and path.startswith(SAMPLE_PREFIX):
                sample = resolve_sample(path)
                if sample is None:
                    raise E2EError("POLICY_DENIED", "unknown built-in upload sample")
                files.append(sample)
                continue
            file = Path(path).expanduser().resolve()
            root = next((root for root in self.upload_roots if file.is_relative_to(root)), None)
            if not file.is_file() or root is None:
                raise E2EError("POLICY_DENIED", "upload path is not a file under an allowed upload root")
            relative = file.relative_to(root)
            if any(part.startswith(".") for part in relative.parts if part not in {".", ".."}):
                raise E2EError("POLICY_DENIED", "upload refuses hidden files and directories")
            files.append(file)
        return [str(file) for file in files]

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

    def _scroll_direction(self, action, *, end):
        """Scroll by whole screens in one direction, at the viewport or a container."""
        axes = {"up": (-1, 0), "down": (1, 0), "left": (0, -1), "right": (0, 1)}
        sign_y, sign_x = axes[action["direction"]]
        times = action.get("times", 1)
        container = None
        if "target" in action:
            container = self.target(action["target"], timeout=self._remaining_ms(end) / 1000)
        try:
            for attempt in range(times):
                self._remaining_ms(end)
                if container is not None:
                    if not container.evaluate(semantics.SCROLL_NEAREST, dict(x=sign_x, y=sign_y, page=True),
                                              timeout=self._remaining_ms(end)):
                        raise E2EError("ACTION_FAILED", "the target and its ancestors do not scroll")
                else:
                    size = self.page.viewport_size or self.page.evaluate(
                        '() => ({width: innerWidth, height: innerHeight})')
                    self.page.mouse.wheel(sign_x * size["width"] * 0.8, sign_y * size["height"] * 0.8)
                if attempt + 1 < times:
                    # Lazy lists and sticky headers use the pause to render.
                    time.sleep(min(0.1, self._remaining_ms(end) / 1000))
        finally:
            if isinstance(container, semantics.ElementTarget):
                container.handle.dispose()
        return None

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
        batch = isinstance(value, list)
        choices = [{'label': option} if isinstance(option, str) else option
                   for option in (value if batch else [value])]
        with ExitStack() as handles:
            if isinstance(target, semantics.ElementTarget):
                handles.callback(target.handle.dispose)
            while True:
                ms = self._remaining_ms(end)
                if not target.evaluate("() => !!globalThis[Symbol.for('e2e.web.semantic.v1')]", timeout=ms):
                    target.evaluate('(el) => {' + semantics.SEMANTIC_INIT + '}', timeout=self._remaining_ms(end))
                result = target.evaluate_handle(semantics.SELECT_OPTIONS, dict(choices=choices, batch=batch),
                                                timeout=self._remaining_ms(end))
                problem = result.evaluate('result => result.problem || null')
                if not isinstance(value, str) or problem != 'missing':
                    break
                result.dispose()
                time.sleep(min(0.05, self._remaining_ms(end) / 1000))
            handles.callback(result.dispose)
            if problem:
                code, message = {
                    'not-select': ('ACTION_FAILED', 'select requires a native select element'),
                    'not-multiple': ('ACTION_FAILED', 'a list of options requires a native multiple select'),
                    'missing': ('LOCATOR_NOT_FOUND', 'a requested select option was not found'),
                    'hidden': ('LOCATOR_NOT_FOUND', 'a requested select option is hidden'),
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
            with ExitStack() as handles:
                receiver = element
                position = None
                if state['type'] and element.is_enabled():
                    stand_in = element.evaluate_handle(semantics.CHECK_LABEL)
                    handles.callback(stand_in.dispose)
                    if stand_in.evaluate('result => !!result'):
                        receiver = stand_in.get_property('label').as_element()
                        handles.callback(receiver.dispose)
                        position = stand_in.evaluate('result => result.position')
                receiver.click(position=position, timeout=self._remaining_ms(end))
            first = True
            hold_until = min(end, time.monotonic() + 3)
            while True:
                if time.monotonic() >= hold_until:
                    if time.monotonic() >= end:
                        raise StepTimeout('STEP_TIMEOUT', 'action deadline elapsed while waiting for the checked state')
                    raise E2EError('ACTION_FAILED', 'the checked state did not change; '
                                    'choose another radio in the group or use a different control')
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
    def _input_target(self, descriptor, *, timeout, resolved=None):
        """Inspect and pin the same element that will receive the input."""
        end = time.monotonic() + timeout
        with ExitStack() as stack:
            try:
                target = self.target(descriptor, timeout=timeout) if resolved is None else resolved
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
            if op == "scroll" and "direction" in action:
                return self._scroll_direction(action, end=end)
            if op == "scroll" and "target" not in action:
                return self.page.mouse.wheel(action["x"], action["y"])
            if op == "dialog":
                self._dialog_policy = {'action': action['action']}
                if 'text' in action:
                    self._dialog_policy['text'] = action['text']
                return None
            if op == "observe":
                return self.observe(timeout=timeout)
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
                # A wrapper around the real scroller is a common layout; scroll the element
                # that actually scrolls instead of silently doing nothing.
                if target.evaluate(semantics.SCROLL_NEAREST, dict(x=action['x'], y=action['y']), timeout=ms):
                    return None
                raise E2EError('ACTION_FAILED', 'the target and its ancestors do not scroll')
            if op == 'scroll_to':
                return target.scroll_into_view_if_needed(timeout=ms)
            if op == "upload":
                files = self._resolve_upload_files(action["value"])
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
                # Hover the source first, so a control covered by an overlay is
                # refused as not actionable instead of dragging from the wrong pixel.
                target.hover(timeout=ms)
                start, finish = target.bounding_box(), destination.bounding_box()
                if not start or not finish:
                    raise E2EError('NODE_STALE', 'drag target is no longer visible')
                viewport = self.page.viewport_size or self.page.evaluate('() => ({width: innerWidth, height: innerHeight})')
                points = [(start['x']+start['width']/2, start['y']+start['height']/2),
                          (finish['x']+finish['width']/2, finish['y']+finish['height']/2)]
                if any(not (0 <= x < viewport['width'] and 0 <= y < viewport['height']) for x, y in points):
                    raise E2EError('ACTION_FAILED', 'drag source and destination cannot be shown in one viewport')
                self.page.mouse.move(*points[0])
                self.page.mouse.down()
                try:
                    self.page.mouse.move(*points[1], steps=10)
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
            if op == 'click' and (target.count() if hasattr(target, 'count') else 1):
                # Only for an element that is already there: probing a missing target
                # would spend this action's budget waiting for it twice.
                stand_in = target.evaluate_handle(semantics.CHECK_LABEL, timeout=ms)
                try:
                    if stand_in.evaluate('result => !!result'):
                        label = stand_in.get_property('label').as_element()
                        try:
                            return label.click(position=stand_in.evaluate('result => result.position'),
                                               timeout=ms, **({'modifiers': action['modifiers']}
                                                              if 'modifiers' in action else {}))
                        finally:
                            label.dispose()
                finally:
                    stand_in.dispose()
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
            raise self.translate(exc, deadline_passed=time.monotonic() >= end) from None

    def screenshot(self, *, timeout: float) -> bytes:
        """Capture pixels only when the harness has allowed secret-safe vision."""
        page = self.page
        selector = 'e2e_private=fields' if self.masked_vision else 'input[type=password i],[autocomplete~=current-password i],[autocomplete~=new-password i],[autocomplete~="one-time-code" i]'
        return page.screenshot(timeout=max(1, timeout * 1000), scale='css',
                               # Chromium's auto focus ring can alternate corner pixels on repaint.
                               # Keep a visible outline in captures, below ordinary page styles.
                               style='@layer e2e_capture { :where(input,textarea,select):where(:focus-visible) { outline-style: solid } }',
                               mask=[frame.locator(selector) for frame in page.frames], mask_color='#000000')

    def _vision_image(self, *, timeout: float) -> bytes:
        """The screenshot a model receives: masked, then sized for the provider."""
        return downscale_png(self.screenshot(timeout=timeout), MAX_SCREENSHOT_LONG_SIDE)

    def capture_vision(self, *, timeout: float):
        """Issue a one-use screenshot token with image-pixel coordinates."""
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
        data = downscale_png(data, MAX_SCREENSHOT_LONG_SIDE)
        width, height = png_size(data[:24])
        size = self.page.evaluate('({width:innerWidth,height:innerHeight})')
        scale = width / size['width'] if size.get('width') else 1
        self._visual = dict(id=uuid.uuid4().hex, digest=hashlib.sha256(data).hexdigest(),
                            url=self.page.url, frame_urls=[frame.url for frame in self.page.frames],
                            width=width, height=height, css_width=size['width'], css_height=size['height'],
                            scale=scale)
        return data, {k: self._visual[k] for k in ('id', 'width', 'height', 'scale')}

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

    def _pixel_signature(self, visual):
        return digest([visual['url'], visual['frame_urls'], visual['width'], visual['height'], visual['digest']])

    def current_pixel_signature(self, *, timeout):
        self.capture_vision(timeout=timeout)
        return self._pixel_signature(self._visual)

    def replay_pixels(self, action, *, timeout):
        _, token = self.capture_vision(timeout=timeout)
        if self._pixel_signature(self._visual) != action['screenshot']:
            self._visual = None
            raise ReplayStale('REPLAY_STALE', 'recorded viewport pixels or URL changed')
        return dict(action, screenshot=token['id'])

    def _perform_visual(self, action, *, timeout, replay=False):
        end = time.monotonic() + timeout
        visual = self._visual
        self._visual = None
        if not visual or action['screenshot'] != visual['id'] or self.page.url != visual['url'] or [frame.url for frame in self.page.frames] != visual['frame_urls']:
            raise E2EError('NODE_STALE', 'coordinate action requires the latest screenshot')
        # A model reading an image can be a pixel off at an edge; the point is
        # clamped onto the captured image rather than failing the whole step.
        def clamp(x, y):
            return (min(max(0, x), max(0, visual['width'] - 1)),
                    min(max(0, y), max(0, visual['height'] - 1)))
        x, y = clamp(action['x'], action['y'])
        to_x, to_y = clamp(action['to_x'], action['to_y']) if action['op'] == 'drag_at' else (0, 0)
        current = self._vision_image(timeout=timeout)
        if hashlib.sha256(current).hexdigest() != visual['digest']:
            raise E2EError('NODE_STALE', 'screen changed since screenshot; observe again')
        self._remaining_ms(end)
        op = action['op']
        scale = visual.get('scale') or 1
        css_x, css_y = x / scale, y / scale
        css_to_x, css_to_y = to_x / scale, to_y / scale
        self._recorded_pixels = self._pixel_signature(visual)
        if op in {'type_at', 'press_at'}:
            with self._at_point(css_x, css_y, end=end) as element:
                if element is None or not element.evaluate("""el => el.tabIndex >= 0 &&
                    !el.matches('input[type=password i],[autocomplete~=current-password i],[autocomplete~=new-password i],[autocomplete~="one-time-code" i]')"""):
                    self._recorded_pixels = None
                    if replay:
                        raise ReplayStale('REPLAY_STALE', 'recorded input point is no longer a replayable field')
        if op in {'select_at', 'press_at'} or (op == 'type_at' and action.get('replace')):
            with self._at_point(css_x, css_y, end=end) as element:
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
            self.page.mouse.click(css_x, css_y)
            self._remaining_ms(end)
            return self.page.keyboard.press(action['value'])
        if op == 'hover_at':
            self.page.mouse.move(css_x, css_y)
        elif op == 'drag_at':
            self.page.mouse.move(css_x, css_y)
            self.page.mouse.down()
            try:
                self.page.mouse.move(css_to_x, css_to_y, steps=10)
            finally:
                self.page.mouse.up()
        else:
            self.page.mouse.click(css_x, css_y)
            if op == 'type_at':
                # Clicking a blank area or a drawn canvas focuses nothing; refuse
                # rather than reporting a typed value that never reached a field.
                self._type_focused(action['value'], replace=False, end=end)

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
            if secret.purpose == "password" and target.evaluate(semantics.FIELD_PURPOSE) != "password":
                raise E2EError("POLICY_DENIED", "password secrets require a password field")
            target.fill(secret.value, timeout=remaining())
        except E2EError:
            raise
        except Exception as exc:
            raise self.translate(exc) from None

    @staticmethod
    def translate(exc: Exception, *, deadline_passed: bool = False) -> E2EError:
        """Classify browser failures without persisting potentially sensitive messages.

        A timeout the caller could not control is a step timeout; anything else is an
        action the model can retry differently, so it must not end the step.
        """
        detail = str(exc)[:400]
        if "strict mode violation" in detail:
            if _may_have_committed(exc):
                return E2EError("ACTION_MAY_HAVE_COMMITTED",
                                "the locator became ambiguous after its input was dispatched; it may have committed")
            return E2EError("LOCATOR_AMBIGUOUS", "locator matched multiple elements")
        if re.search(r"not attached|detached|Execution context was destroyed|Target closed|frame was detached",
                     detail, re.IGNORECASE):
            return E2EError("NODE_STALE", "the observed element is gone")
        actionable = re.search(r"intercepts pointer events|not visible|not stable|not enabled|not editable|"
                               r"outside of the viewport|not a checkbox|did not change|Cannot (?:type|select|focus)",
                               detail, re.IGNORECASE)
        if "Timeout" in type(exc).__name__ or "Timeout" in detail:
            if _may_have_committed(exc):
                return E2EError("ACTION_MAY_HAVE_COMMITTED",
                                "the action timed out after its input was dispatched; it may have committed")
            if deadline_passed:
                return StepTimeout("STEP_TIMEOUT", "browser operation timed out")
            return E2EError("ACTION_FAILED", "element did not become actionable"
                            + (f": {actionable.group(0)}" if actionable else ""))
        return E2EError("ACTION_FAILED", detail or "browser operation failed")
