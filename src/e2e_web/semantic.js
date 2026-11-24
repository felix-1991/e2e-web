(() => {
  const key = Symbol.for('e2e.web.semantic.v1');
  if (globalThis[key]) return;
  const roots = new WeakMap(), ids = new WeakMap(), elements = new Map(), search = new Map();
  const documentId = crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
  let next = 0;
  const original = Element.prototype.attachShadow;
  Element.prototype.attachShadow = function(init) {
    const root = original.call(this, init);
    if (root.mode === 'closed') roots.set(this, root);
    return root;
  };
  const clean = value => String(value ?? '').replace(/\s+/g, ' ').trim().slice(0, 512);
  const buttonInput = type => ['button', 'submit', 'reset', 'image'].includes(type);
  const structure = {
    article:'article', aside:'complementary', details:'group', dialog:'dialog',
    fieldset:'group', form:'form', main:'main', nav:'navigation', section:'group',
    ol:'list', ul:'list', li:'listitem',
    table:'table', thead:'rowgroup', tbody:'rowgroup', tfoot:'rowgroup', tr:'row', td:'cell',
  };
  const parentOf = el => el.parentElement || el.getRootNode()?.host || null;
  const flatParentOf = el => {
    if (el.assignedSlot) return el.assignedSlot;
    const root = roots.get(el.parentElement);
    if (root) {
      for (const slot of root.querySelectorAll('slot')) {
        if (slot.assignedElements().includes(el)) return slot;
      }
    }
    return parentOf(el);
  };
  const inside = (el, ancestor, parent = flatParentOf) => {
    for (let node = el; node; node = parent(node)) if (node === ancestor) return true;
    return false;
  };
  const scrollable = (el, style = getComputedStyle(el)) => {
    let axes = '';
    if (/^(auto|scroll|overlay)$/.test(style.overflowX) && el.scrollWidth > el.clientWidth) axes += 'x';
    if (/^(auto|scroll|overlay)$/.test(style.overflowY) && el.scrollHeight > el.clientHeight) axes += 'y';
    return axes;
  };
  const role = (el, axes) => {
    const explicit = (el.getAttribute('role') || '').trim();
    if (explicit && !/^(none|presentation)(\s|$)/.test(explicit)) return explicit.split(/\s+/)[0].slice(0, 64);
    const tag = el.tagName.toLowerCase();
    if (!explicit) {
      if (tag === 'th') return el.scope === 'row' ? 'rowheader' : 'columnheader';
      if (structure[tag]) return structure[tag];
    }
    if (/^h[1-6]$/.test(tag)) return 'heading';
    if (tag === 'input') {
      if (buttonInput(el.type)) return 'button';
      if (el.list && ['text', 'search', 'email', 'tel', 'url'].includes(el.type)) return 'combobox';
      return ({checkbox:'checkbox',radio:'radio',range:'slider',number:'spinbutton',search:'searchbox',hidden:'none'})[el.type] || 'textbox';
    }
    if (tag === 'select') return el.multiple || el.size > 1 ? 'listbox' : 'combobox';
    const native = ({button:'button',a:el.hasAttribute('href')?'link':'',textarea:'textbox',
      option:'option',img:'img',summary:'button',canvas:'canvas',iframe:'iframe'})[tag];
    if (native && !explicit) return native;
    if (el.isContentEditable && !parentOf(el)?.isContentEditable) return 'textbox';
    if (axes || el.hasAttribute('data-testid') || el.hasAttribute('aria-label') ||
        el.hasAttribute('aria-labelledby') || el.hasAttribute('tabindex') ||
        el.hasAttribute('onclick') || typeof el.onclick === 'function') return 'generic';
    return !el.children.length && clean(el.textContent) ? 'text' : '';
  };
  const labelText = (label, control) => {
    if (!label.contains(control)) return clean(label.textContent);
    const walker = document.createTreeWalker(label, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT,
      node => node === control ? NodeFilter.FILTER_REJECT :
        node.nodeType === Node.TEXT_NODE ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_SKIP);
    let text = '';
    for (let node = walker.nextNode(); node && text.length < 1024; node = walker.nextNode()) text += node.textContent;
    return clean(text);
  };
  const contentRoles = new Set(['button','cell','checkbox','columnheader','gridcell','heading','link',
    'menuitem','menuitemcheckbox','menuitemradio','option','radio','rowheader','switch','tab','tooltip','treeitem']);
  const textRoles = new Set(['caption','code','contentinfo','definition','deletion','emphasis','generic',
    'insertion','list','listitem','mark','none','paragraph','presentation','region','row','rowgroup',
    'section','strong','subscript','superscript','table','term','text','time']);
  const captionTags = {FIELDSET:'legend', FIGURE:'figcaption', TABLE:'caption'};
  const nameFromContent = el => {
    let text = '', visited = 0;
    const append = value => { text += String(value ?? '').slice(0, 1024 - text.length); };
    const walk = (node, depth) => {
      if (++visited > 512 || text.length >= 1024 || depth > 32) return;
      if (node.nodeType === Node.TEXT_NODE) { append(node.textContent); return; }
      if (node.nodeType !== Node.ELEMENT_NODE || node.matches('script,style,template,[inert]')) return;
      const style = getComputedStyle(node);
      if (hidesSubtree(node, style)) return;
      const spaced = node.tagName === 'BR' || !['inline','contents','inline-block','inline-flex','inline-grid'].includes(style.display);
      if (spaced) append(' ');
      const explicit = node.getAttribute('role')?.trim().split(/\s+/)[0];
      const r = explicit || role(node, '');
      const refs = (node.getAttribute('aria-labelledby') || '').split(/\s+/);
      const labelled = refs.map(id => node.getRootNode().getElementById?.(id)?.textContent || '').join(' ');
      const alternative = clean(labelled) || clean(node.getAttribute('aria-label')) ||
        clean(node.getAttribute('alt')) || (node.tagName.toLowerCase() === 'svg' ? clean(node.querySelector('title')?.textContent) : '');
      if (alternative) append(alternative);
      else if (!r || contentRoles.has(r) || textRoles.has(r)) {
        children(node, depth);
      } else {
        const caption = captionTags[node.tagName];
        append((caption && node.querySelector(`:scope > ${caption}`)?.textContent) || node.getAttribute('title'));
      }
      if (spaced) append(' ');
    };
    const children = (node, depth) => {
      const assigned = node.tagName === 'SLOT' ? node.assignedNodes() : null;
      const children = assigned?.length ? assigned : (node.shadowRoot || roots.get(node) || node).childNodes;
      for (const child of children) {
        if (visited >= 512 || text.length >= 1024) break;
        walk(child, depth + 1);
      }
    };
    children(el, 0);
    return clean(text);
  };
  const name = (el, r) => {
    const labelled = el.getAttribute('aria-labelledby');
    if (labelled) {
      const root = el.getRootNode();
      const text = labelled.split(/\s+/).map(id => root.getElementById?.(id)?.textContent || '').join(' ');
      if (clean(text)) return clean(text);
    }
    const label = clean(el.getAttribute('aria-label')) || Array.from(el.labels || [], l => labelText(l, el)).filter(Boolean).join(' ');
    if (label) return clean(label);
    if (el.tagName === 'INPUT' && buttonInput(el.type)) {
      const fallback = {submit:'Submit',reset:'Reset',image:'Submit'}[el.type];
      const caption = el.type === 'image' ? el.getAttribute('alt') : el.value;
      return clean(caption || el.getAttribute('title') || fallback);
    }
    if (el.tagName === 'OPTION') return clean(el.label);
    if (el.tagName === 'FIELDSET') return clean(el.querySelector(':scope > legend')?.textContent);
    if (el.tagName === 'TABLE') return clean(el.caption?.textContent || el.title);
    if (el.tagName === 'TR') {
      return clean(Array.prototype.slice.call(el.cells, 0, 32).map(cell => cell.textContent).join(' '));
    }
    const title = el.getAttribute('title');
    if (['INPUT','TEXTAREA','SELECT','IFRAME'].includes(el.tagName) || el.isContentEditable) {
      return clean(title || el.getAttribute('placeholder'));
    }
    if (['group','form','list','rowgroup','navigation','main','complementary','article','dialog'].includes(r)) {
      return clean(title);
    }
    if (contentRoles.has(r)) return clean(el.getAttribute('alt')) || nameFromContent(el) || clean(title);
    return clean(el.getAttribute('alt') || el.textContent || title) || clean(title);
  };
  const selectOptions = el => {
    const limit = 32;
    const options = new Set(Array.prototype.slice.call(el.selectedOptions, 0, limit));
    for (let i = 0; i < el.options.length && options.size < limit; i++) options.add(el.options[i]);
    return {options: [...options].sort((a, b) => a.index - b.index).map(option => ({
      name: clean(option.label), index: option.index, selected: option.selected, disabled: option.matches(':disabled'),
    })), multiple: el.multiple, options_truncated: el.options.length > options.size};
  };
  const focusedElement = () => {
    let active = document.activeElement;
    while (active) {
      const inner = (active.shadowRoot || roots.get(active))?.activeElement;
      if (!inner) break;
      active = inner;
    }
    return active;
  };
  const modalOrder = new WeakMap(), modalRefs = new WeakMap(), trackedModals = new Set();
  let opening = 0;
  const isModal = el => {
    try { return el.matches('dialog:modal'); } catch { return false; }
  };
  if (typeof HTMLDialogElement !== 'undefined' && typeof HTMLDialogElement.prototype.showModal === 'function') {
    const showModal = HTMLDialogElement.prototype.showModal;
    HTMLDialogElement.prototype.showModal = function(...args) {
      const wasOpen = this.open;
      const result = showModal.apply(this, args);
      if (!wasOpen && isModal(this)) {
        if (!modalOrder.has(this)) {
          modalRefs.set(this, new WeakRef(this));
          this.addEventListener('beforetoggle', event => {
            if (event.newState === 'open') {
              modalOrder.set(this, ++opening);
              trackedModals.add(modalRefs.get(this));
            }
          });
        }
        modalOrder.set(this, ++opening);
        trackedModals.add(modalRefs.get(this));
      }
      return result;
    };
  }
  const activeModal = () => {
    for (let node = focusedElement(); node; node = flatParentOf(node)) {
      if (isModal(node)) return node;
    }
    let open;
    try { open = new Set(document.querySelectorAll('dialog:modal')); } catch { return null; }
    for (const ref of trackedModals) {
      const el = ref.deref();
      if (el && isModal(el)) open.add(el);
      else trackedModals.delete(ref);
    }
    if (open.size < 2) return open.values().next().value || null;
    let newest = null, order = 0;
    for (const el of open) {
      const current = modalOrder.get(el);
      if (current === undefined) return null;
      if (current > order) { newest = el; order = current; }
    }
    return newest;
  };
  const selectionOf = el => {
    if (el.type === 'password' || el.matches('[autocomplete~="one-time-code" i]')) return '';
    if (typeof el.selectionStart === 'number') return clean(el.value.slice(el.selectionStart, el.selectionEnd));
    if (!el.isContentEditable) return '';
    const root = el.getRootNode();
    const selected = root.getSelection?.() || document.getSelection();
    return selected?.anchorNode && selected?.focusNode && el.contains(selected.anchorNode) && el.contains(selected.focusNode)
      ? clean(selected.toString()) : '';
  };
  const describe = (el, axes = scrollable(el), r = role(el, axes)) => {
    const rect = el.getBoundingClientRect();
    const nativeCheck = el.matches('input[type=checkbox],input[type=radio]');
    const ariaChecked = el.getAttribute('aria-checked');
    const disabled = el.matches(':disabled') || el.closest('[aria-disabled]')?.getAttribute('aria-disabled') === 'true';
    const readonly = el.readOnly === true || el.getAttribute('aria-readonly') === 'true';
    const editable = el.isContentEditable || el.matches('textarea') ||
      (el.matches('input') && !['button','submit','reset','image','checkbox','radio','file','range','hidden'].includes(el.type));
    const expanded = el.getAttribute('aria-expanded');
    const pressed = el.getAttribute('aria-pressed');
    const details = el.tagName === 'SUMMARY' && el.parentElement?.tagName === 'DETAILS' ? el.parentElement : null;
    const activeId = el.getAttribute('aria-activedescendant');
    const active = activeId ? el.getRootNode().getElementById?.(activeId) : null;
    const focused = el.getRootNode().activeElement === el && !(el.shadowRoot || roots.get(el))?.activeElement;
    const selection = focused ? selectionOf(el) : '';
    return {role:r,name:name(el, r),test_id:clean(el.getAttribute('data-testid')),
      disabled, readonly, editable:editable && !readonly && !disabled,
      checked:nativeCheck ? el.checked : ariaChecked === 'true',
      indeterminate:nativeCheck ? el.indeterminate : ariaChecked === 'mixed',
      selected:el.selected === true || el.getAttribute('aria-selected') === 'true',
      expanded:expanded === null ? (details ? details.open : null) : expanded === 'true',
      pressed:pressed === 'mixed' ? 'mixed' : (pressed === null ? null : pressed === 'true'),
      focused,
      ...(selection ? {selection} : {}),
      ...(active ? {active_descendant: name(active, active.getAttribute('role'))} : {}),
      ...(el.getAttribute('aria-multiselectable') === 'true' ? {multiselectable:true} : {}),
      scrollable:axes,
      input_type:typeof el.type === 'string' ? el.type : '',
      value:el.type === 'password' || el.matches('[autocomplete~="one-time-code" i]') ? '<masked>' : clean(el.value),
      bounds:{x:rect.x,y:rect.y,width:rect.width,height:rect.height},
      in_viewport:rect.bottom>0 && rect.right>0 && rect.top<innerHeight && rect.left<innerWidth};
  };
  // A form control named "hidden" can shadow the form's DOM property.
  const hidesSubtree = (el, style) => el.getAttribute('aria-hidden') === 'true' || el.hasAttribute('hidden') ||
    style.display === 'none' || ['hidden','collapse'].includes(style.visibility);
  const hiddenAncestor = el => {
    for (let p=el; p; p=parentOf(p)) {
      if (hidesSubtree(p, getComputedStyle(p))) return true;
    }
    return false;
  };
  const inActiveModal = el => {
    const modal = activeModal();
    return !modal || inside(el, modal);
  };
  const elementAt = ({x, y}) => {
    let el = document.elementFromPoint(x, y);
    while (el) {
      const inner = (el.shadowRoot || roots.get(el))?.elementFromPoint(x, y);
      if (!inner || inner === el || !inside(inner, el)) break;
      el = inner;
    }
    return el && inActiveModal(el) ? el : null;
  };
  const visible = el => el.isConnected && el.getClientRects().length > 0 && !hiddenAncestor(el) && inActiveModal(el);
  const matchesContext = (el, context) => {
    const parent = elements.get(context.id)?.deref();
    for (let p = parentOf(el); p; p = parentOf(p)) {
      if (p === parent) {
        return clean(p.getAttribute('data-testid')) === context.test_id && name(p, context.role) === context.name;
      }
    }
    return false;
  };
  const scan = ({maxNodes=200,maxElements=10000,root=null,replace=true}={}) => {
    const lookup = replace ? elements : search;
    lookup.clear();
    const modal = activeModal();
    const start = modal && (!root || inside(modal, root)) ? modal : root || document.documentElement;
    const nodes = [], seen = new Set();
    const stack = start && (!modal || inside(start, modal)) && !hiddenAncestor(parentOf(start)) ? [[start, null, false]] : [];
    let visited=0;
    while(stack.length && visited < maxElements && nodes.length < maxNodes) {
      const [el, parent_id, siblings]=stack.pop(); visited++;
      // Iterate lazily: even a root with millions of children is bounded.
      if (siblings && el.nextElementSibling) stack.push([el.nextElementSibling, parent_id, true]);
      if (seen.has(el)) continue;
      seen.add(el);
      const style = getComputedStyle(el);
      if (hidesSubtree(el, style)) continue;
      let childParent = parent_id;
      if (el.getClientRects().length) {
        const axes = scrollable(el, style);
        const r = role(el, axes);
        if (r) {
          let id=ids.get(el);
          if (!id) {id=`${documentId}:${++next}`; ids.set(el,id);}
          lookup.set(id,new WeakRef(el));
          nodes.push({id,parent_id,...describe(el, axes, r),
            ...(el.tagName === 'SELECT' ? selectOptions(el) : {}),
            ...(el.tagName === 'OPTION' && el.closest('select') ? {native_option:true} : {})});
          childParent = id;
        }
      }
      const shadow=el.shadowRoot || roots.get(el);
      const assigned = modal && el.tagName === 'SLOT' ? el.assignedElements() : [];
      if (assigned.length) {
        for (let index = Math.min(assigned.length, maxElements); index > 0; index--) {
          stack.push([assigned[index - 1], childParent, false]);
        }
      } else if ((!modal || !shadow) && el.firstElementChild) stack.push([el.firstElementChild, childParent, true]);
      if (shadow?.firstElementChild) stack.push([shadow.firstElementChild, childParent, true]);
    }
    return {nodes,truncated:stack.length>0,visited,document:documentId,modal:!!modal,root_id:root ? ids.get(root) || null : null};
  };
  Object.defineProperty(globalThis,key,{value:{
    scan, describe, visible, matchesContext, focusedElement, activeModal, inside, inActiveModal, elementAt,
    resolve:(id, searched) => (searched ? search : elements).get(id)?.deref() || null,
  },configurable:false});
})();
