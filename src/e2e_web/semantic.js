(testIdAttribute => {
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
  const passwordSelector = 'input[type=password i],[autocomplete~=current-password i],[autocomplete~=new-password i]';
  const secureSelector = passwordSelector + ',[autocomplete~=one-time-code i]';
  const buttonInput = type => ['button', 'submit', 'reset', 'image'].includes(type);
  const structure = {
    progress:'progressbar', meter:'meter', output:'status', article:'article', aside:'complementary', details:'group', dialog:'dialog',
    address:'group', optgroup:'group', fieldset:'group', main:'main', nav:'navigation',
    ol:'list', ul:'list', menu:'list', li:'listitem', figure:'figure', hr:'separator',
    p:'paragraph', blockquote:'blockquote', pre:'code', code:'code', em:'emphasis',
    strong:'strong', del:'deletion', ins:'insertion', sub:'subscript', sup:'superscript',
    mark:'mark', time:'time', cite:'blockquote', q:'blockquote', s:'deletion',
    dl:'list', dt:'term', dd:'definition',
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
  const concreteRoles = new Set([
    'alert', 'alertdialog', 'application', 'article', 'banner', 'blockquote', 'button', 'caption',
    'cell', 'checkbox', 'code', 'columnheader', 'combobox', 'complementary', 'contentinfo', 'definition',
    'deletion', 'dialog', 'directory', 'document', 'emphasis', 'feed', 'figure', 'form',
    'generic', 'grid', 'gridcell', 'group', 'heading', 'img', 'insertion', 'link',
    'list', 'listbox', 'listitem', 'log', 'main', 'mark', 'marquee', 'math',
    'meter', 'menu', 'menubar', 'menuitem', 'menuitemcheckbox', 'menuitemradio', 'navigation', 'none',
    'note', 'option', 'paragraph', 'presentation', 'progressbar', 'radio', 'radiogroup', 'region',
    'row', 'rowgroup', 'rowheader', 'scrollbar', 'search', 'searchbox', 'separator', 'slider',
    'spinbutton', 'status', 'strong', 'subscript', 'superscript', 'switch', 'tab', 'table',
    'tablist', 'tabpanel', 'term', 'textbox', 'time', 'timer', 'toolbar', 'tooltip',
    'tree', 'treegrid', 'treeitem',
  ]);
  const explicitRole = el => (el.getAttribute('role') || '').split(/\s+/).find(token => concreteRoles.has(token));
  const paintedBox = el => {
    if (el.children.length || el.shadowRoot || roots.has(el) || el.hasAttribute('role') || clean(el.textContent)) return false;
    const rect = el.getBoundingClientRect();
    if (rect.width < 12 || rect.height < 12) return false;
    const style = getComputedStyle(el);
    return style.backgroundImage !== 'none' ||
      !['transparent', 'rgba(0, 0, 0, 0)'].includes(style.backgroundColor) && !/, 0\)$/.test(style.backgroundColor) ||
      parseFloat(style.outlineWidth) > 0 && style.outlineStyle !== 'none' ||
      ['Top','Right','Bottom','Left'].some(side => parseFloat(style[`border${side}Width`]) > 0 &&
        !['none','hidden'].includes(style[`border${side}Style`]));
  };
  const role = (el, axes) => {
    let explicit = explicitRole(el);
    if ((explicit === 'none' || explicit === 'presentation') &&
        el.matches('button:enabled,input:enabled:not([type=hidden i]),select:enabled,textarea:enabled,a[href],[tabindex],[aria-label],[aria-labelledby]')) {
      explicit = undefined;
    }
    if (explicit && explicit !== 'none' && explicit !== 'presentation') return explicit;
    const tag = el.tagName.toLowerCase();
    if (!explicit && el.isContentEditable && !parentOf(el)?.isContentEditable &&
        !el.matches('svg, math, canvas, video, audio, iframe, img, hr, br, wbr, area, a, button, input, textarea, select')) return 'textbox';
    if (!explicit) {
      if (tag === 'svg') return 'img';
      if (tag === 'header' || tag === 'footer') {
        let scoped = false;
        for (let p = flatParentOf(el); p && !scoped; p = flatParentOf(p)) {
          scoped = p.matches('article,aside,main,nav,section') ||
            ['article','complementary','main','navigation','region'].includes(explicitRole(p));
        }
        if (!scoped) return tag === 'header' ? 'banner' : 'contentinfo';
      }
      if (tag === 'th') {
        // HTML-AAM: the scope attribute decides; without it, a header whose
        // neighbour is data is the row's, and a header-only row is a column's.
        const scope = (el.getAttribute('scope') || '').toLowerCase();
        if (scope === 'row' || scope === 'rowgroup') return 'rowheader';
        if (scope === 'col' || scope === 'colgroup') return 'columnheader';
        const previous = el.previousElementSibling, next = el.nextElementSibling;
        if (!previous && !next) {
          const row = el.parentElement;
          const table = row && row.tagName === 'TR' ? row.closest('table') : null;
          return table && table.rows.length <= 1 ? '' : 'columnheader';
        }
        if (previous?.tagName === 'TH' && next?.tagName === 'TH') return 'columnheader';
        const hasDataNeighbor = [previous, next].some(cell => cell?.tagName === 'TD' &&
          (clean(cell.textContent) !== '' || cell.children.length > 0));
        return hasDataNeighbor ? 'rowheader' : 'columnheader';
      }
      if (tag === 'td') {
        const table = el.closest('table');
        return table && ['grid', 'treegrid'].includes(explicitRole(table)) ? 'gridcell' : 'cell';
      }
      if (tag === 'section' && (el.hasAttribute('aria-label') || el.hasAttribute('aria-labelledby') || el.title)) return 'region';
      // A form is a landmark only when named; an unnamed one is a plain container.
      if (tag === 'form' && (el.hasAttribute('aria-label') || el.hasAttribute('aria-labelledby') || el.title)) return 'form';
      if (structure[tag]) return structure[tag];
    }
    if (/^h[1-6]$/.test(tag)) return 'heading';
    if (tag === 'input') {
      if (buttonInput(el.type) || el.type === 'file') return 'button';
      if (el.list && ['text', 'search', 'email', 'tel', 'url'].includes(el.type)) return 'combobox';
      return ({checkbox:'checkbox',radio:'radio',range:'slider',number:'spinbutton',search:'searchbox',hidden:'none'})[el.type] || 'textbox';
    }
    if (tag === 'select') return el.multiple || el.size > 1 ? 'listbox' : 'combobox';
    const native = ({button:'button',a:el.hasAttribute('href')?'link':'',textarea:'textbox',
      option:'option',img:'img',summary:'button',canvas:'canvas',iframe:'iframe'})[tag];
    if (native && !explicit) {
      // An unnamed decorative image is presentation: listing it would spend a
      // node on something no person reads or aims at.
      if (tag === 'img' && el.getAttribute('alt') === '' && !el.getAttribute('title') &&
          !el.hasAttribute('aria-label') && !el.hasAttribute('aria-labelledby') &&
          !el.hasAttribute('aria-describedby') && !el.hasAttribute('tabindex') &&
          !el.hasAttribute(testIdAttribute)) return '';
      return native;
    }
    if (axes || el.hasAttribute(testIdAttribute) || el.hasAttribute('aria-label') ||
        el.hasAttribute('aria-labelledby') || el.hasAttribute('tabindex') ||
        el.hasAttribute('onclick') || typeof el.onclick === 'function') return 'generic';
    if (!el.children.length && clean(el.textContent)) return 'text';
    // A container whose own text reads inline with its children is a line of text,
    // not a control: its name would otherwise be an entire subtree.
    if (directTextOf(el) !== '') return 'text';
    return paintedBox(el) ? 'box' : '';
  };
  const contentRoles = new Set(['button','cell','checkbox','columnheader','gridcell','heading','link',
    'menuitem','menuitemcheckbox','menuitemradio','option','radio','rowheader','switch','tab','tooltip','treeitem']);
  const textRoles = new Set(['caption','code','contentinfo','definition','deletion','emphasis','generic',
    'insertion','list','listitem','mark','none','paragraph','presentation','region','row','rowgroup',
    'section','strong','subscript','superscript','table','term','text','time']);
  const captionTags = {FIELDSET:'legend', FIGURE:'figcaption', TABLE:'caption'};
  const contentTextOf = (el, value) => {
    const tokens = [];
    const token = /\s*("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|[-\w]+\((?:[^()"']|"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')*\)|\/|[-\w]+)/gy;
    let match;
    let end = 0;
    while ((match = token.exec(value)) !== null) {
      tokens.push(match[1]);
      end = token.lastIndex;
    }
    if (value.slice(end).trim() !== '') return null;
    let text = '';
    for (const part of tokens.slice(tokens.lastIndexOf('/') + 1)) {
      const attribute = /^attr\(\s*([-\w]+)\s*\)$/.exec(part);
      if (attribute !== null) text += el.getAttribute(attribute[1]) ?? '';
      else if (part.startsWith('"') || part.startsWith("'")) {
        text += part.slice(1, -1).replace(/\\([0-9a-fA-F]{1,6})\s?|\\(.)/g, (_all, hex, char) =>
          hex === undefined ? char : String.fromCodePoint(Number.parseInt(hex, 16)));
      } else return null;
    }
    return text;
  };

  /**
   * What an element's `::before` or `::after` adds to a name (accname 2F.ii):
   * its content's text, spaced like a block when the pseudo element is not
   * inline; empty when it shows nothing.
   */
  const generatedContentOf = (el, pseudo) => {
    const style = el.ownerDocument.defaultView?.getComputedStyle(el, pseudo);
    if (style === undefined || style.display === 'none' || style.visibility === 'hidden') return '';
    const value = style.content;
    if (value === '' || value === 'none' || value === 'normal') return '';
    const text = contentTextOf(el, value);
    if (text === null) return '';
    return style.display === 'inline' ? text : ` ${text} `;
  };

  const ICON_GLYPHS = () => /\p{Co}/gu;
  /** Text with private-use icon glyphs read as gaps, so an icon font cannot name a control. */
  const withoutGlyphs = text => ICON_GLYPHS().test(text)
    ? text.replace(ICON_GLYPHS(), ' ').replace(/\s+/g, ' ').trim() : text;
  // A reference contributes what the target is *named*, not everything it contains:
  // textContent would pull in aria-hidden decoration and miss an inner <img alt>.
  const referenceIds = value => [...new Set((value || '').trim().split(/\s+/).filter(Boolean))];
  const referencedText = (el, id, seen) => {
    const ref = el.getRootNode().getElementById?.(id);
    if (!ref || seen.has(ref)) return '';
    seen.add(ref);
    const own = clean(ref.getAttribute('aria-label')) || clean(ref.getAttribute('alt')) ||
      (ref.tagName.toLowerCase() === 'svg' ? clean(ref.querySelector(':scope > title')?.textContent) : '');
    if (own) return own;
    const embedded = embeddedValueOf(ref, explicitRole(ref) || role(ref, ''));
    if (embedded !== null) return embedded;
    return nameFromContent(ref, false, {
      inReference: true, seen,
      hiddenAllowed: hiddenAncestor(ref) || getComputedStyle(ref).visibility !== 'visible',
    }) || clean(ref.getAttribute('title'));
  };
  const referenceTextOf = (el, ids) => {
    const seen = new Set();
    return clean(referenceIds(ids).map(id => referencedText(el, id, seen)).join(' '));
  };
  const closedDetails = el => el.tagName === 'DETAILS' && !el.open;
  const firstSummary = el => el.querySelector(':scope > summary');
  const skipsContent = style => style.contentVisibility === 'hidden';
  const nameRangeRoles = new Set(['slider', 'spinbutton', 'progressbar', 'meter', 'scrollbar']);
  /** What a control shows as its value, when a name is assembled around it. */
  const embeddedValueOf = (el, r) => {
    if (el.matches(secureSelector)) return '';
    if (r === 'textbox' || r === 'searchbox') {
      return el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' ? clean(el.value) : clean(el.textContent);
    }
    if (r === 'combobox' || r === 'listbox') {
      if (el.tagName === 'SELECT') {
        const chosen = Array.from(el.selectedOptions || []);
        if (chosen.length) return clean(chosen.map(option => option.label).join(' '));
        return el.options?.length ? clean(el.options[0].label) : '';
      }
      const owned = (owner, selector) => {
        const matches = new Set(owner.querySelectorAll(selector));
        for (const id of referenceIds(owner.getAttribute('aria-owns'))) {
          const target = owner.getRootNode().getElementById?.(id);
          if (!target || target === owner) continue;
          if (target.matches(selector)) matches.add(target);
          for (const child of target.querySelectorAll(selector)) matches.add(child);
        }
        return [...matches];
      };
      const list = r === 'listbox' ? el : owned(el, '[role~=listbox],select').find(
        node => role(node, '') === 'listbox');
      const selected = list ? owned(list, '[aria-selected="true"]').filter(
        node => role(node, '') === 'option') : [];
      if (!selected.length) return el.tagName === 'INPUT' ? clean(el.value) : '';
      return clean(selected.map(option => clean(option.getAttribute('aria-label')) ||
        nameFromContent(option, true)).join(' '));
    }
    if (nameRangeRoles.has(r)) {
      return clean(el.getAttribute('aria-valuetext') ?? el.getAttribute('aria-valuenow') ?? el.value ?? '');
    }
    if (r === 'menu') return '';
    return null;
  };
  const nameFromContent = (el, renderedTextOnly = false, {inReference, hiddenAllowed, exclude, seen = new Set()} = {}) => {
    let text = '', visited = 0;
    const append = value => { text += String(value ?? '').slice(0, 1024 - text.length); };
    const walk = (node, depth) => {
      if (node === exclude || ++visited > 512 || text.length >= 1024 || depth > 32) return;
      if (node.nodeType === Node.TEXT_NODE) {
        const parent = node.parentElement || node.getRootNode().host;
        if (parent && (hiddenAllowed || getComputedStyle(parent).visibility === 'visible')) append(node.textContent);
        return;
      }
      if (node.nodeType !== Node.ELEMENT_NODE || node.matches('script,style,template,[inert],' + secureSelector)) return;
      if (seen.has(node)) return;
      seen.add(node);
      const style = getComputedStyle(node);
      if (!hiddenAllowed && hidesSubtree(node, style)) return;
      if (!hiddenAllowed && style.visibility !== 'visible') { children(node, depth); return; }
      const spaced = node.tagName === 'BR' || !['inline','contents','inline-block','inline-flex','inline-grid'].includes(style.display);
      if (spaced) append(' ');
      if (renderedTextOnly) {
        children(node, depth);
        if (spaced) append(' ');
        return;
      }
      const r = explicitRole(node) || role(node, '');
      const refs = !inReference ? referenceIds(node.getAttribute('aria-labelledby')) : [];
      const labelled = refs.map(id => referencedText(node, id, seen)).join(' ');
      const alternative = clean(labelled) || clean(node.getAttribute('aria-label')) ||
        clean(node.getAttribute('alt')) || (node.tagName.toLowerCase() === 'svg' ? clean(node.querySelector('title')?.textContent) : '');
      const embedded = alternative ? null : embeddedValueOf(node, r);
      if (alternative) append(alternative);
      else if (embedded !== null) append(embedded);
      else if (!r || contentRoles.has(r) || textRoles.has(r)) {
        children(node, depth);
      } else {
        const caption = captionTags[node.tagName];
        append((caption && node.querySelector(`:scope > ${caption}`)?.textContent) || node.getAttribute('title'));
      }
      if (spaced) append(' ');
    };
    const children = (node, depth) => {
      if (!renderedTextOnly) append(generatedContentOf(node, '::before'));
      if (skipsContent(getComputedStyle(node))) return;
      if (closedDetails(node)) {
        const summary = firstSummary(node);
        if (summary) walk(summary, depth + 1);
        return;
      }
      const assigned = node.tagName === 'SLOT' ? node.assignedNodes() : null;
      const children = assigned?.length ? assigned : (node.shadowRoot || roots.get(node) || node).childNodes;
      for (const child of children) {
        if (visited >= 512 || text.length >= 1024) break;
        walk(child, depth + 1);
      }
      if (!renderedTextOnly) append(generatedContentOf(node, '::after'));
    };
    children(el, 0);
    return clean(text);
  };
  // Only these roles take their name from their content. Every other role would
  // otherwise be named by an entire subtree, which drifts on any text edit.
  const nameFromContentRoles = new Set([...contentRoles, 'listitem', 'status', 'alert']);
  const LINE_BREAKING = new Set(['iframe','img','input','select','textarea','br','video','canvas','audio','object','embed']);
  const directTextOf = el => {
    if (skipsContent(getComputedStyle(el))) return '';
    let text = '';
    for (const node of el.childNodes) if (node.nodeType === 3) text += node.textContent;
    return text.replace(/\s+/g, ' ').trim();
  };
  const inlineLevel = style => /^(inline|ruby)/.test(style.display) || style.display === 'contents';
  const lineRunOf = (el, depth = 0) => {
    if (depth > 8 || skipsContent(getComputedStyle(el))) return '';
    let text = '';
    const children = el.shadowRoot || roots.get(el) || el;
    for (const node of children.childNodes) {
      if (node.nodeType === 3) { text += node.textContent; continue; }
      if (node.nodeType !== 1) continue;
      const style = getComputedStyle(node);
      if (hidesSubtree(node, style) || style.visibility !== 'visible') { text += ' '; continue; }
      if (LINE_BREAKING.has(node.tagName.toLowerCase())) { text += ' '; continue; }
      text += inlineLevel(style) ? lineRunOf(node, depth + 1) : ' ';
    }
    return text;
  };
  // Text that reads inline with the element's other content, as a sentence does.
  const lineTextOf = el => {
    if (el.matches('input, textarea, select, iframe, img, svg, canvas, video, audio')) return '';
    if (el.matches(secureSelector)) return '';
    if (directTextOf(el) === '') return '';
    return clean(lineRunOf(el)).slice(0, 512);
  };
  const name = (el, r) => {
    const labelled = el.getAttribute('aria-labelledby');
    if (labelled) {
      const text = referenceTextOf(el, labelled);
      if (clean(text)) return clean(text);
    }
    const label = clean(el.getAttribute('aria-label')) || Array.from(el.labels || [], l => nameFromContent(l, false, {exclude: el, inReference: true})).filter(Boolean).join(' ');
    if (label) return clean(label);
    if (el.tagName === 'INPUT' && buttonInput(el.type)) {
      const fallback = {submit:'Submit',reset:'Reset',image:'Submit'}[el.type];
      const caption = el.type === 'image' ? el.getAttribute('alt') : el.value;
      return clean(caption || el.getAttribute('title') || fallback);
    }
    if (el.tagName.toLowerCase() === 'svg') return clean(el.querySelector(':scope > title')?.textContent || el.getAttribute('title'));
    if (el.tagName === 'OPTGROUP') return clean(el.label);
    if (el.tagName === 'OPTION') return clean(el.label);
    if (el.tagName === 'FIELDSET') return clean(el.querySelector(':scope > legend')?.textContent);
    if (el.tagName === 'FIGURE') return clean(el.querySelector(':scope > figcaption')?.textContent || el.title);
    if (el.tagName === 'TABLE') return clean(el.caption?.textContent || el.title);
    if (el.tagName === 'TR') {
      // Read cells in DOM order, with the same subtree exclusions as the observation.
      return nameFromContent(el, true);
    }
    const title = el.getAttribute('title');
    if (['textbox','searchbox'].includes(r) || ['INPUT','TEXTAREA','SELECT','IFRAME'].includes(el.tagName) || el.isContentEditable) {
      const hint = clean(title) || clean(el.getAttribute('placeholder')) || clean(el.getAttribute('aria-placeholder'));
      if (hint || !el.isContentEditable) return hint;
      const hostHint = clean(el.getAttribute('data-placeholder'));
      if (hostHint) return hostHint;
      const block = el.querySelector('[data-placeholder]');
      return block && !clean(block.textContent) ? clean(block.getAttribute('data-placeholder')) : '';
    }
    if (['banner','contentinfo','group','form','region','separator','list','rowgroup','navigation','main','complementary','article','dialog'].includes(r)) {
      return clean(title);
    }
    if (nameFromContentRoles.has(r)) return clean(el.getAttribute('alt')) || nameFromContent(el) || clean(title);
    return clean(el.getAttribute('alt')) || clean(title);
  };
  const offeredOption = option => {
    for (let p = option; p && p.tagName !== 'SELECT'; p = p.parentElement) {
      if (hidesSubtree(p, getComputedStyle(p))) return false;
    }
    return true;
  };
  const selectOptions = el => {
    const limit = 32;
    const options = new Set();
    const scanLimit = 512;
    for (const option of Array.prototype.slice.call(el.selectedOptions, 0, limit)) {
      if (offeredOption(option)) options.add(option);
    }
    let examined = 0, truncated = el.selectedOptions.length > limit;
    while (examined < Math.min(el.options.length, scanLimit)) {
      const option = el.options[examined++];
      if (!offeredOption(option) || options.has(option)) continue;
      if (options.size === limit) { truncated = true; break; }
      options.add(option);
    }
    return {options: [...options].sort((a, b) => a.index - b.index).map(option => ({
      name: clean(option.label), index: option.index,
      ...(option.parentElement?.tagName === 'OPTGROUP' ? {group: clean(option.parentElement.label)} : {}),
      selected: option.selected, disabled: option.matches(':disabled'),
    })), multiple: el.multiple, options_truncated: truncated || examined < el.options.length};
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
  const collectModals = (root, found) => {
    for (const el of root.querySelectorAll('*')) {
      if (isModal(el)) found.add(el);
      const shadow = el.shadowRoot || roots.get(el);
      if (shadow) collectModals(shadow, found);
    }
    return found;
  };
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
    // A dialog in a shadow root is hidden from document.querySelector; walk
    // roots only when a dialog exists at all, so ordinary pages pay nothing.
    let hasDialog = false;
    try { hasDialog = document.querySelector('dialog') !== null; } catch { hasDialog = false; }
    if (hasDialog) collectModals(document, open);
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
    if (el.matches(secureSelector)) return '';
    if (typeof el.selectionStart === 'number') return clean(el.value.slice(el.selectionStart, el.selectionEnd));
    if (!el.isContentEditable) return '';
    const root = el.getRootNode();
    const selected = root.getSelection?.() || document.getSelection();
    return selected?.anchorNode && selected?.focusNode && el.contains(selected.anchorNode) && el.contains(selected.focusNode)
      ? clean(selected.toString()) : '';
  };
  const linkDestination = el => {
    if (!el.matches('a[href],area[href]')) return '';
    try {
      const url = new URL(el.getAttribute('href'), el.baseURI);
      return ['http:', 'https:'].includes(url.protocol) ? clean(url.origin + url.pathname) : '';
    } catch { return ''; }
  };
  const disabledRoles = new Set(['application','button','checkbox','columnheader','combobox','grid',
    'gridcell','group','link','listbox','menu','menubar','menuitem','menuitemcheckbox','menuitemradio',
    'option','radio','radiogroup','row','rowheader','scrollbar','searchbox','separator','slider','spinbutton',
    'switch','tab','tablist','textbox','toolbar','tree','treegrid','treeitem']);
  const disabledOf = (el, role) => {
    if (el.matches(':disabled')) return true;
    for (let p = el; p; p = flatParentOf(p)) {
      const attr = p.getAttribute('aria-disabled')?.toLowerCase();
      if (attr === 'true' || attr === 'false') return attr === 'true' && (p === el || disabledRoles.has(role));
    }
    return false;
  };
  const rangeRoles = new Set(['slider','spinbutton','progressbar','meter','scrollbar']);
  const describe = (el, axes = scrollable(el), r = role(el, axes)) => {
    const rect = el.getBoundingClientRect();
    const nativeCheck = el.matches('input[type=checkbox],input[type=radio]');
    const ariaChecked = el.getAttribute('aria-checked');
    const disabled = disabledOf(el, r);
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
    const masked = el.matches(secureSelector);
    let rawValue = el.value;
    let valueText = '';
    if (rangeRoles.has(r) && !masked) {
      const nativeValue = el.tagName === 'PROGRESS' && !el.hasAttribute('value') ? '' : rawValue;
      rawValue = el.getAttribute('aria-valuenow') ?? nativeValue;
      valueText = clean(el.getAttribute('aria-valuetext'));
    }
    const editingHost =  el.isContentEditable && !parentOf(el)?.isContentEditable;
    if (!masked && editingHost) {
      const rendered = el.innerText.replace(/\uFEFF/g, '');
      rawValue = rendered === '\n' && !el.textContent.trim() ? '' : rendered;
    }
    const value = masked ? '<masked>' : (editingHost ? rawValue.slice(0, 512) : clean(rawValue));
    const href = r === 'link' ? linkDestination(el) : '';
    const rawName = name(el, r);
    const glyphless = rawName === null ? null : withoutGlyphs(rawName);
    // A name made only of icon glyphs says nothing: the title is the real label.
    const accessibleName = glyphless === rawName || glyphless !== '' ? glyphless : (clean(el.getAttribute('title')) || '');
    const level = Number(el.getAttribute('aria-level'));
    const headingLevel = Number.isSafeInteger(level) && level > 0 ? level :
      (/^H[1-6]$/.test(el.tagName) ? Number(el.tagName[1]) : 2);
    const purpose = masked ? (el.matches(passwordSelector) ? 'password' : 'one-time-code')
      : el.matches('[autocomplete~=username i]') ? 'username' : '';
    const attributes = {};
    for (const attribute of el.attributes) {
      if (!attribute.name.startsWith('aria-') &&
          !['type', 'autocomplete', 'required', 'placeholder', 'min', 'max', 'step'].includes(attribute.name)) continue;
      if (masked && attribute.name === 'value') continue;
      attributes[attribute.name] = clean(attribute.value);
    }
    const namedNotification = ['alert', 'status'].includes(r) &&
      (el.hasAttribute('aria-label') || el.hasAttribute('aria-labelledby') || el.labels?.length);
    const description = referenceTextOf(el, el.getAttribute('aria-describedby')) || clean(el.getAttribute('aria-description'));
    const message = namedNotification ? nameFromContent(el, true) : '';
    const lineText = lineTextOf(el);
    return {role:r,name:accessibleName,
      ...(description && description !== accessibleName ? {description} : {}),
      ...(r === 'heading' ? {level:headingLevel} : {}),
      ...(message && message !== accessibleName ? {message} : {}),
      test_id:clean(el.getAttribute(testIdAttribute)),
      text:lineText && lineText !== accessibleName ? lineText : '',
      ...(href ? {href} : {}),
      disabled, readonly, editable:editable && !readonly && !disabled,
      checked:nativeCheck ? el.checked : ariaChecked === 'true',
      indeterminate:nativeCheck ? el.indeterminate : ariaChecked === 'mixed',
      selected:el.tagName === 'OPTION' ? el.selected : el.getAttribute('aria-selected')?.toLowerCase() === 'true',
      expanded:expanded === null ? (details ? details.open : null) : expanded === 'true',
      pressed:pressed === 'mixed' ? 'mixed' : (pressed === null ? null : pressed === 'true'),
      focused,
      secure:masked,
      ...(purpose ? {purpose} : {}),
      ...(Object.keys(attributes).length ? {attributes} : {}),
      ...(selection ? {selection} : {}),
      ...(active ? {active_descendant: name(active, active.getAttribute('role'))} : {}),
      ...(el.getAttribute('aria-multiselectable') === 'true' ? {multiselectable:true} : {}),
      scrollable:axes,
      input_type:typeof el.type === 'string' ? el.type : '',
      value,
      ...(valueText ? {value_text:valueText} : {}),
      ...(editable && !masked && typeof rawValue === 'string' && value === rawValue && (editingHost || rawValue === el.value) ? {value_exact:true} : {}),
      bounds:{x:rect.x,y:rect.y,width:rect.width,height:rect.height},
      in_viewport:rect.bottom>0 && rect.right>0 && rect.top<innerHeight && rect.left<innerWidth};
  };
  // A form control named "hidden" can shadow the form's DOM property.
  const hidesSubtree = (el, style) => el.getAttribute('aria-hidden')?.toLowerCase() === 'true' || el.hasAttribute('hidden') ||
    style.display === 'none' || el.hasAttribute('inert') || style.getPropertyValue('interactivity') === 'inert';
  const hiddenAncestor = el => {
    for (let p=el, child=null; p; child=p, p=flatParentOf(p)) {
      const style = getComputedStyle(p);
      if (hidesSubtree(p, style) || (child && (skipsContent(style) ||
          (closedDetails(p) && child !== firstSummary(p))))) return true;
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
  const boxHasArea = el => {
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  };
  const textLaysOut = node => {
    const range = node.ownerDocument.createRange();
    range.selectNode(node);
    const rect = range.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  };
  // A display:contents element has no box of its own; it is shown when some
  // child or text it carries is drawn.
  const contentsShow = el => {
    const visibility = getComputedStyle(el).visibility;
    for (let child = el.firstChild; child; child = child.nextSibling) {
      if (child.nodeType === 1 && visible(child)) return true;
      if (child.nodeType === 3 && visibility === 'visible' && textLaysOut(child)) return true;
    }
    return false;
  };
  // A checkbox or radio hidden only by having no box, under a shown label of
  // its own that draws it. Chrome keeps it in the tree, and the label is what
  // a click reaches.
  const INTERACTIVE = 'a,button,input,select,textarea,summary,[contenteditable],[role="button"],[role="link"]';
  const labelShows = (el, style) => {
    if (!el.matches('input[type=checkbox],input[type=radio]')) return false;
    if (style.visibility !== 'visible' || skipsContent(style)) return false;
    return Array.from(el.labels || []).some(label => {
      if (getComputedStyle(label).display === 'contents') return false;
      if (!visible(label) || label.matches(INTERACTIVE)) return false;
      return !Array.from(label.querySelectorAll(INTERACTIVE)).some(inner => inner !== el);
    });
  };
  const visible = el => {
    if (!el.isConnected || !inActiveModal(el) || hiddenAncestor(el)) return false;
    const style = getComputedStyle(el);
    if (style.display === 'contents') return style.visibility === 'visible' && contentsShow(el);
    return style.visibility === 'visible' && (boxHasArea(el) || labelShows(el, style));
  };
  const matchesContext = (el, context) => {
    const parent = elements.get(context.id)?.deref();
    for (let p = flatParentOf(el); p; p = flatParentOf(p)) {
      if (p === parent) {
        return clean(p.getAttribute(testIdAttribute)) === context.test_id && name(p, context.role) === context.name;
      }
    }
    return false;
  };
  // Child document coordinates start at the content box, inside border and padding.
  const frameBox = el => {
    const rect = el.getBoundingClientRect(), style = getComputedStyle(el);
    const sx = rect.width / el.offsetWidth, sy = rect.height / el.offsetHeight;
    const left = parseFloat(style.paddingLeft), top = parseFloat(style.paddingTop);
    return {x:rect.x + (el.clientLeft + left) * sx, y:rect.y + (el.clientTop + top) * sy, sx, sy,
      width:(el.clientWidth - left - parseFloat(style.paddingRight)) * sx,
      height:(el.clientHeight - top - parseFloat(style.paddingBottom)) * sy};
  };
  const frameSpace = (el, parent) => {
    const box = frameBox(el);
    parent ??= {x:0, y:0, sx:1, sy:1, left:0, top:0, right:innerWidth, bottom:innerHeight};
    const x = parent.x + box.x * parent.sx, y = parent.y + box.y * parent.sy;
    return {x, y, sx:box.sx * parent.sx, sy:box.sy * parent.sy,
      left:Math.max(parent.left, x), top:Math.max(parent.top, y),
      right:Math.min(parent.right, x + box.width * parent.sx),
      bottom:Math.min(parent.bottom, y + box.height * parent.sy)};
  };
  const projectNode = (node, space) => {
    const b = node.bounds;
    const x = space.x + b.x * space.sx, y = space.y + b.y * space.sy;
    const width = b.width * space.sx, height = b.height * space.sy;
    const left = Math.max(space.left, x), top = Math.max(space.top, y);
    const right = Math.min(space.right, x + width), bottom = Math.min(space.bottom, y + height);
    node.in_viewport = right > left && bottom > top;
    node.bounds = node.in_viewport ? {x:left, y:top, width:right-left, height:bottom-top} : {x, y, width, height};
  };
  const scan = ({maxNodes=200,maxElements=10000,root=null,replace=true,space=null}={}) => {
    const lookup = replace ? elements : search;
    lookup.clear();
    const modal = activeModal();
    const start = modal && (!root || inside(modal, root)) ? modal : root || document.documentElement;
    const nodes = [], seen = new Set();
    const stack = start && (!modal || inside(start, modal)) && !hiddenAncestor(start) ? [[start, null, false]] : [];
    let visited=0, omittedSlots=false;
    while(stack.length && visited < maxElements && nodes.length < maxNodes) {
      const [el, parent_id, siblings]=stack.pop(); visited++;
      // Iterate lazily: even a root with millions of children is bounded.
      if (siblings && el.nextElementSibling) stack.push([el.nextElementSibling, parent_id, true]);
      if (seen.has(el)) continue;
      seen.add(el);
      const style = getComputedStyle(el);
      if (hidesSubtree(el, style)) continue;
      let childParent = parent_id;
      // A display:contents container has no box of its own but its label and test id
      // are what the app addresses it by, so it is listed while its content shows.
      const contentsOnly = style.display === 'contents' &&
        !!(el.hasAttribute(testIdAttribute) || el.hasAttribute('aria-label') || el.hasAttribute('aria-labelledby') || clean(el.textContent));
      const shown = style.display === 'contents'
        ? contentsOnly && contentsShow(el)
        : boxHasArea(el) || labelShows(el, style);
      if (style.visibility === 'visible' && shown) {
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
      if (skipsContent(style)) continue;
      if (closedDetails(el)) {
        const summary = firstSummary(el);
        if (summary) stack.push([summary, childParent, false]);
        continue;
      }
      const shadow=el.shadowRoot || roots.get(el);
      const children = el.tagName === 'SLOT' ? el.assignedElements() : [];
      if (children.length || (el.tagName === 'SLOT' && el.assignedNodes().length)) {
        omittedSlots ||= children.length > maxElements;
        for (let index = Math.min(children.length, maxElements); index > 0; index--) {
          stack.push([children[index - 1], childParent, false]);
        }
      } else if (!shadow && el.firstElementChild) stack.push([el.firstElementChild, childParent, true]);
      if (shadow?.firstElementChild) stack.push([shadow.firstElementChild, childParent, true]);
    }
    if (space) for (const node of nodes) projectNode(node, space);
    return {nodes,truncated:omittedSlots || stack.length>0,visited,document:documentId,modal:!!modal,root_id:root ? ids.get(root) || null : null};
  };
  const secureFields = root => {
    const fields = [], scopes = [root];
    while (scopes.length) {
      const scope = scopes.pop();
      fields.push(...scope.querySelectorAll(secureSelector));
      for (const el of scope.querySelectorAll('*')) {
        const shadow = el.shadowRoot || roots.get(el);
        if (shadow) scopes.push(shadow);
      }
    }
    return fields;
  };
  Object.defineProperty(globalThis,key,{value:{
    secureFields, offeredOption,
    scan, describe, visible, matchesContext, focusedElement, activeModal, inside, inActiveModal, elementAt, frameSpace, frameBox,
    resolve:(id, searched) => (searched ? search : elements).get(id)?.deref() || null,
  },configurable:false});
})
