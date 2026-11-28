"""Bounded structured observations and main-world closed shadow tracking."""

import json
from pathlib import Path
import time

from .errors import StepTimeout

_SEMANTIC_READER = Path(__file__).with_name("semantic.js").read_text(encoding="utf-8").strip()


def init_script(test_id_attribute: str) -> str:
    return f"{_SEMANTIC_READER}({json.dumps(test_id_attribute)});"


SEMANTIC_INIT = init_script("data-testid")
DESCRIBE = "el => globalThis[Symbol.for('e2e.web.semantic.v1')].describe(el)"
FRAME_IDENTITY = "el => Object.fromEntries(['id','name','src','title'].map(k=>[k,el.getAttribute(k)||'']))"
VISIBLE = "el => globalThis[Symbol.for('e2e.web.semantic.v1')].visible(el)"
IN_ACTIVE_MODAL = "el => globalThis[Symbol.for('e2e.web.semantic.v1')]?.inActiveModal(el) ?? true"
VISIBLE_FRAME_INDICES = """frames => {
    const semantics = globalThis[Symbol.for('e2e.web.semantic.v1')];
    return frames.flatMap((el, index) => semantics.visible(el) && el.offsetWidth && el.offsetHeight ? [index] : []);
}"""
FRAME_INFO = f"""(el, parent) => ({{identity:({FRAME_IDENTITY})(el),
    space:globalThis[Symbol.for('e2e.web.semantic.v1')].frameSpace(el, parent)}})"""
ELEMENT_AT = "point => globalThis[Symbol.for('e2e.web.semantic.v1')].elementAt(point)"
FRAME_POINT = """(el, point) => {
    const box = globalThis[Symbol.for('e2e.web.semantic.v1')].frameBox(el);
    const x = point.x - box.x, y = point.y - box.y;
    return x >= 0 && y >= 0 && x < box.width && y < box.height ? {x:x/box.sx, y:y/box.sy} : null;
}"""
MATCHES_CONTEXT = "(el, context) => globalThis[Symbol.for('e2e.web.semantic.v1')].matchesContext(el, context)"
FOCUSED_ELEMENT = """() => globalThis[Symbol.for('e2e.web.semantic.v1')]?.focusedElement?.() ??
    document.activeElement"""
FOCUSED_INPUT = """el => {
    if (!el.isConnected || el.matches(':disabled') ||
        el.closest('[aria-disabled="true"], [aria-readonly="true"], [inert]')) return null;
    if (el instanceof HTMLInputElement) {
        return !el.readOnly && ['text','search','tel','url','email','password','number'].includes(el.type)
            ? 'editable' : null;
    }
    if (el instanceof HTMLTextAreaElement) return el.readOnly ? null : 'editable';
    if (el.isContentEditable) return 'editable';
    return el.hasAttribute('tabindex') && !el.matches('html,body,a,button,select,iframe,frame')
        ? 'keyboard' : null;
}"""
CONTAINS = """(node, container) => {
    for (let p = node.parentElement || node.getRootNode().host; p;
         p = p.parentElement || p.getRootNode().host) {
        if (p === container) return true;
    }
    return false;
}"""
SCROLL_STATE = """element => {
    const el = element || document.scrollingElement;
    return [el.scrollLeft, el.scrollTop, el.scrollWidth, el.scrollHeight];
}"""
DESCENDANTS = f"(nodes, container) => nodes.flatMap((node, index) => ({CONTAINS})(node, container) ? [index] : [])"
SCROLL = """(el, {x, y, page}) => {
    const style = getComputedStyle(el);
    const acceptsScroll = overflow => /^(auto|scroll|overlay)$/.test(overflow);
    const dx = acceptsScroll(style.overflowX) ? x : 0;
    const dy = acceptsScroll(style.overflowY) ? y : 0;
    el.scrollBy({
        left: page ? dx * Math.max(1, Math.floor(el.clientWidth * 0.8)) : dx,
        top: page ? dy * Math.max(1, Math.floor(el.clientHeight * 0.8)) : dy,
        behavior: 'instant',
    });
}"""
SELECT_OPTIONS = r"""(el, {choices, batch}) => {
    const select = el.matches('select') ? el : el.closest('label')?.control;
    if (!(select instanceof HTMLSelectElement)) return {problem: 'not-select'};
    if (batch && !select.multiple) return {problem: 'not-multiple'};
    const options = Array.from(select.options);
    const selected = new Set();
    for (const choice of choices) {
        const field = 'label' in choice ? 'label' : Object.keys(choice)[0];
        const value = choice[field];
        const matches = field === 'index' ? options.slice(value, value + 1) :
            options.filter(option => option[field] === value && (!('group' in choice) ||
                (option.parentElement?.tagName === 'OPTGROUP' &&
                 option.parentElement.label.trim().replace(/\s+/g, ' ') === choice.group)));
        const offered = matches.filter(globalThis[Symbol.for('e2e.web.semantic.v1')].offeredOption);
        if (!offered.length && matches.length) return {problem: 'hidden'};
        if (!matches.length) return {problem: 'missing'};
        if (offered.length > 1) return {problem: 'ambiguous'};
        if (offered[0].matches(':disabled')) return {problem: 'disabled'};
        selected.add(offered[0]);
    }
    return {select, options: [...selected]};
}"""
SCROLL_NEAREST = """(el, {x, y, page = false}) => {
    const scrolls = (node, overflow, size, client) => /^(auto|scroll|overlay)$/.test(overflow) &&
        node[size] > node[client];
    const box = el.getBoundingClientRect();
    const root = el.getRootNode();
    // Start where the pointer would land: the deepest element under the centre can be
    // the element that actually scrolls, inside a wrapper that does not.
    let start = typeof root.elementFromPoint === 'function'
        ? root.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2) : null;
    if (!start || !el.contains(start)) start = el;
    for (let node = start; node && node !== document.body && node !== document.documentElement;
         node = node.parentElement || node.getRootNode().host) {
        const style = getComputedStyle(node);
        const vertical = y && scrolls(node, style.overflowY, 'scrollHeight', 'clientHeight');
        const horizontal = x && scrolls(node, style.overflowX, 'scrollWidth', 'clientWidth');
        if (!vertical && !horizontal) continue;
        node.scrollBy({
            left: horizontal ? x * (page ? Math.max(1, Math.floor(node.clientWidth * 0.8)) : 1) : 0,
            top: vertical ? y * (page ? Math.max(1, Math.floor(node.clientHeight * 0.8)) : 1) : 0,
            behavior: 'instant',
        });
        return true;
    }
    return false;
}"""
CHECK_LABEL = """input => {
    if (!input.matches('input[type=checkbox],input[type=radio]') || input.matches(':disabled, [aria-disabled="true"]')) return null;
    const root = input.getRootNode();
    const box = input.getBoundingClientRect();
    const hasBox = box.width > 0 && box.height > 0;
    for (const label of input.labels) {
        label.scrollIntoView({block:'nearest', inline:'nearest'});
        const rect = (hasBox ? input : label).getBoundingClientRect();
        const x = rect.x + rect.width / 2, y = rect.y + rect.height / 2;
        const hit = root.elementFromPoint(x, y);
        if (!hit || !label.contains(hit)) continue;
        const interactive = hit.closest('input,button,a,select,textarea,summary,[contenteditable],[role="button"],[role="link"]');
        if (interactive && label.contains(interactive)) continue;
        const bounds = label.getBoundingClientRect();
        if (x < bounds.left || y < bounds.top || x >= bounds.right || y >= bounds.bottom) continue;
        return {label, position:{x:x-bounds.left-label.clientLeft, y:y-bounds.top-label.clientTop}};
    }
    return null;
}"""
FIELD_PURPOSE = """el => {
    const autocomplete = (el.getAttribute('autocomplete') || '').toLowerCase();
    if (el.type === 'password') return 'password';
    if (el.matches('[autocomplete~=current-password i],[autocomplete~=new-password i]')) return 'password';
    return autocomplete || 'none';
}"""
CHECK_STATE = """el => {
    const control = el.matches('input') ? el : el.closest('label')?.control || el;
    const native = control.matches('input[type=checkbox],input[type=radio]');
    return {
        connected: el.isConnected,
        type: native ? control.type : null,
        mixed: native ? control.indeterminate : el.getAttribute('aria-checked') === 'mixed',
    };
}"""


def install(scope, *, timeout):
    """Also install into a borrowed document; existing closed roots stay inaccessible."""
    scope.locator('html').evaluate('(el) => {' + SEMANTIC_INIT + '}', timeout=timeout * 1000)


def scan(scope, *, max_nodes, max_elements, timeout, root=None, replace=True, space=None):
    end = time.monotonic() + timeout
    install(scope, timeout=timeout)
    remaining = end - time.monotonic()
    if remaining <= 0:
        raise StepTimeout('STEP_TIMEOUT', 'semantic observation deadline elapsed')
    return scope.locator('html').evaluate(
        '(el, options) => globalThis[Symbol.for("e2e.web.semantic.v1")].scan(options)',
        dict(maxNodes=max_nodes, maxElements=max_elements, root=root, replace=replace, space=space), timeout=remaining * 1000)


def ancestors(node, nodes):
    """Walk the listed parents of a node in its document."""
    while parent := nodes.get(node.get('parent_id')):
        yield parent
        node = parent


def resolve(scope, node_id, *, timeout, searched=False):
    handle = scope.locator('html').evaluate_handle(
        '(el, {id, searched}) => globalThis[Symbol.for("e2e.web.semantic.v1")]?.resolve(id, searched) || null',
        dict(id=node_id, searched=searched), timeout=timeout * 1000).as_element()
    return ElementTarget(handle) if handle else None


class ElementTarget:
    """Expose Locator-compatible calls on a resolved closed-root ElementHandle."""

    def __init__(self, handle):
        self.handle = handle

    def __getattr__(self, name):
        method = getattr(self.handle, 'type' if name == 'press_sequentially' else name)
        if name in {'evaluate', 'evaluate_handle', 'get_attribute', 'is_enabled', 'is_editable', 'is_visible'}:
            def call(*args, **kwargs):
                kwargs.pop('timeout', None)
                return method(*args, **kwargs)
            return call
        return method
