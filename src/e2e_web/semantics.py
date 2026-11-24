"""Bounded structured observations and main-world closed shadow tracking."""

from pathlib import Path
import time

from .errors import StepTimeout

SEMANTIC_INIT = Path(__file__).with_name("semantic.js").read_text(encoding="utf-8")
DESCRIBE = "el => globalThis[Symbol.for('e2e.web.semantic.v1')].describe(el)"
FRAME_IDENTITY = "el => Object.fromEntries(['id','name','src','title'].map(k=>[k,el.getAttribute(k)||'']))"
VISIBLE = "el => globalThis[Symbol.for('e2e.web.semantic.v1')].visible(el)"
IN_ACTIVE_MODAL = "el => globalThis[Symbol.for('e2e.web.semantic.v1')]?.inActiveModal(el) ?? true"
MODAL_FRAME_INDICES = """frames => {
    const semantics = globalThis[Symbol.for('e2e.web.semantic.v1')];
    const modal = semantics.activeModal();
    return frames.flatMap((el, index) => !modal || semantics.inside(el, modal) ? [index] : []);
}"""
ELEMENT_AT = "point => globalThis[Symbol.for('e2e.web.semantic.v1')].elementAt(point)"
FRAME_POINT = """(el, point) => {
    const rect = el.getBoundingClientRect();
    if (!rect.width || !rect.height || !el.offsetWidth || !el.offsetHeight) return null;
    const x = (point.x - rect.left) * el.offsetWidth / rect.width - el.clientLeft;
    const y = (point.y - rect.top) * el.offsetHeight / rect.height - el.clientTop;
    return x >= 0 && y >= 0 && x < el.clientWidth && y < el.clientHeight ? {x, y} : null;
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
SELECT_OPTIONS = """(el, {choices, batch}) => {
    const select = el.matches('select') ? el : el.closest('label')?.control;
    if (!(select instanceof HTMLSelectElement)) return {problem: 'not-select'};
    if (batch && !select.multiple) return {problem: 'not-multiple'};
    const options = Array.from(select.options);
    const selected = new Set();
    for (const choice of choices) {
        const [field, value] = Object.entries(choice)[0];
        const matches = field === 'index' ? options.slice(value, value + 1) :
            options.filter(option => option[field] === value);
        if (!matches.length) return {problem: 'missing'};
        if (matches.length > 1) return {problem: 'ambiguous'};
        if (matches[0].matches(':disabled')) return {problem: 'disabled'};
        selected.add(matches[0]);
    }
    return {select, options: [...selected]};
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


def scan(scope, *, max_nodes, max_elements, timeout, root=None, replace=True):
    end = time.monotonic() + timeout
    install(scope, timeout=timeout)
    remaining = end - time.monotonic()
    if remaining <= 0:
        raise StepTimeout('STEP_TIMEOUT', 'semantic observation deadline elapsed')
    return scope.locator('html').evaluate(
        '(el, options) => globalThis[Symbol.for("e2e.web.semantic.v1")].scan(options)',
        dict(maxNodes=max_nodes, maxElements=max_elements, root=root, replace=replace), timeout=remaining * 1000)


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
        if name in {'evaluate', 'get_attribute', 'is_enabled', 'is_editable', 'is_visible'}:
            def call(*args, **kwargs):
                kwargs.pop('timeout', None)
                return method(*args, **kwargs)
            return call
        return method
