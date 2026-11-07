"""Original viewport capture for an owned, authored UI document.

The default policy masks embedded frames. A selected provider may opt in its
owned application frames after binding their browser identity and admission
contract. This opt-in captures original application pixels; literal credential
checks do not certify arbitrary pixels painted by application scripts. Provider
authentication pages and unknown frames remain outside the capture contract.
"""
from dataclasses import dataclass
from time import monotonic
from urllib.parse import unquote, urljoin, urlsplit
from uuid import uuid4


@dataclass(frozen=True, slots=True)
class OwnedFrameContract:
    """Provider-owned, exact application frame admitted to original pixels."""

    selector: str
    path: str
    sandbox: str


_TRUSTED_ICONS = ['<svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true" focusable="false"><path d="M12 5v14M5 12h14"/></svg>', '<svg viewBox="0 0 24 24" width="23" height="23" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><path d="m9.5 3-.7 2.2-2 .9-2.1-.5-2.5 4.3 1.5 1.7v2.3l-1.5 1.7 2.5 4.3 2.1-.5 2 .9.7 2.2h5l.7-2.2 2-.9 2.1.5 2.5-4.3-1.5-1.7v-2.3l1.5-1.7-2.5-4.3-2.1.5-2-.9-.7-2.2z" transform="translate(0 -1) scale(1 .96)"/><circle cx="12" cy="12" r="3.3"/></svg>']


_PREPARE = r'''({trustedIcons, ownedFrameSelector, contentMaskSelector}) => {
  const saved = new Map();
  const icons = trustedIcons.map(html => new DOMParser().parseFromString(html, 'text/html').body.firstElementChild.outerHTML);
  const buttonIcon = node => {
    if (node.localName !== 'svg' || !node.closest('button[type="button"][aria-label]') ||
        node.getAttribute('aria-hidden') !== 'true') return false;
    const path = node.firstElementChild;
    if (!path || path.localName !== 'path' || path.nextElementSibling ||
        path.childNodes.length || !path.getAttribute('d') || path.getAttribute('d').length > 512 ||
        !/^[MmLlHhVvCcSsQqTtAaZz0-9., +\-]+$/.test(path.getAttribute('d'))) return false;
    const svgAttributes = new Set(['width','height','viewBox','fill','aria-hidden','focusable','xmlns']);
    const pathAttributes = new Set(['d','stroke','stroke-width','stroke-linecap','stroke-linejoin','fill']);
    return [...node.attributes].every(attr => svgAttributes.has(attr.name)) &&
      [...path.attributes].every(attr => pathAttributes.has(attr.name)) &&
      ['width','height'].every(name => /^\d{1,2}$/.test(node.getAttribute(name) || '') &&
        Number(node.getAttribute(name)) <= 48) &&
      ['fill','stroke'].every(name => [node,path].every(element =>
        !element.hasAttribute(name) || ['none','currentColor'].includes(element.getAttribute(name))));
  };
  const authentication = node => node.matches('input,textarea,select,[contenteditable]') && (
    /password|credential|secret|authorization|authentication|login|username|one-time-code|otp|passkey|cc-number|cc-csc|(?:api|access|auth|refresh|bearer)[\s_-]*(?:key|token)/i.test(
      [node.type, node.id, node.name, node.autocomplete, node.getAttribute('aria-label'), node.getAttribute('placeholder'),
       ...[...(node.labels || [])].map(label => label.textContent),
       ...(node.getAttribute('aria-labelledby') || '').split(/\s+/).map(id => document.getElementById(id)?.textContent)].join(' ')) ||
    /^(?:token|key)$/i.test(node.id) || /^(?:token|key)$/i.test(node.name));
  const nodes = [...document.querySelectorAll('*')];
  if (nodes.length > 10000) return {saved, reason:'node_budget'};
  const pseudos = ['::before','::after','::marker','::backdrop','::file-selector-button',
    '::first-letter','::first-line','::placeholder'];
  const urlProperties = [...new Set([
    ...[...getComputedStyle(document.documentElement)].filter(property =>
      !property.startsWith('--') && (
        CSS.supports(property, 'url("about:blank")') ||
        CSS.supports(property, 'url("about:blank"), auto') ||
        CSS.supports(property, 'below 0px url("about:blank")'))),
    'content','background-image','mask-image','border-image-source','list-style-image',
    'filter','backdrop-filter','clip-path','shape-outside','offset-path','cursor',
    'fill','stroke','-webkit-box-reflect','-webkit-mask-box-image-source'
  ])];
  const hasUrl = computed => urlProperties.some(property =>
    /url\s*\(/i.test(computed.getPropertyValue(property)));
  try {
  for (const node of nodes) {
    const backdrop = getComputedStyle(node, '::backdrop');
    if (hasUrl(backdrop) || (backdrop.content && !['none','normal'].includes(backdrop.content)))
      return {saved, urlProperties, reason:'backdrop'};
  }
  for (const node of nodes) {
    const computed = getComputedStyle(node);
    const contentMasked = !!contentMaskSelector && node.matches(contentMaskSelector);
    const pseudoStyles = pseudos.map(pseudo => getComputedStyle(node, pseudo));
    const cssUrl = [computed, ...pseudoStyles].some(hasUrl);
    const effects = ['filter','backdrop-filter','-webkit-box-reflect'].filter(property => {
      const value = computed.getPropertyValue(property);
      return value && value !== 'none';
    });
    const opaque = cssUrl || !!node.shadowRoot ||
      authentication(node) || (node.matches('iframe,frame') ?
        !(ownedFrameSelector && node.matches(ownedFrameSelector)) :
        node.matches('img,picture,canvas,object,embed,video,audio,script,style,template,noscript')) ||
      (node.localName === 'svg' && !icons.includes(node.outerHTML) && !buttonIcon(node));
    if ((opaque || contentMasked) && computed.display === 'contents') throw 'display_contents';
    if (!opaque && !effects.length && !contentMasked) continue;
    const properties = [...new Set([...(opaque ? ['opacity','content-visibility','animation','transition'] : []),
      ...(contentMasked ? ['opacity','clip-path'] : []), ...effects])];
    const originalStyle = node.getAttribute('style');
    const previous = properties.map(property => [property,
      node.style.getPropertyValue(property), node.style.getPropertyPriority(property)]);
    const record = {opaque, previous, originalStyle, preparedStyle:null, overrides:[]};
    saved.set(node, record);
    if (opaque) {
      node.style.setProperty('opacity', '0', 'important');
      node.style.setProperty('content-visibility', 'hidden', 'important');
      node.style.setProperty('animation', 'none', 'important');
      node.style.setProperty('transition', 'none', 'important');
    }
    if (contentMasked) {
      node.style.setProperty('opacity', '0', 'important');
      node.style.setProperty('clip-path', 'inset(0)', 'important');
    }
    for (const effect of effects) node.style.setProperty(effect, 'none', 'important');
    const overrides = properties.map(property => [property,
      node.style.getPropertyValue(property), node.style.getPropertyPriority(property)]);
    record.preparedStyle = node.getAttribute('style');
    record.overrides = overrides;
  }
  return {saved, urlProperties, reason:null};
  } catch (error) {
    for (const [node, record] of saved) {
      if (record.originalStyle === null) node.removeAttribute('style');
      else node.setAttribute('style', record.originalStyle);
    }
    return {saved:new Map(), urlProperties,
      reason:error === 'display_contents' ? 'display_contents' : 'prepare_error'};
  }
}'''


_RESTORE = r'''prepared => {
  for (const [node, record] of prepared.saved) {
    if (node.getAttribute('style') === record.preparedStyle) {
      if (record.originalStyle === null) node.removeAttribute('style');
      else node.setAttribute('style', record.originalStyle);
      continue;
    }
    for (let index=0; index<record.previous.length; index++) {
      const [property, value, priority] = record.previous[index];
      const [, override, overridePriority] = record.overrides[index];
      if (node.style.getPropertyValue(property) !== override ||
          node.style.getPropertyPriority(property) !== overridePriority) continue;
      if (value) node.style.setProperty(property, value, priority);
      else node.style.removeProperty(property);
    }
    if (record.originalStyle === null && !node.style.length) node.removeAttribute('style');
  }
}'''


_INSPECT = r'''({marker, token, verify, trustedIcons, allowFrames, ownedFrameSelector, contentMaskSelector, contentMaskAnchor, contentMaskRequirements, diagnose, prepared}) => {
  const reject = reason => diagnose ? {unavailable:reason} : null;
  const icons = trustedIcons.map(html => new DOMParser().parseFromString(html, 'text/html').body.firstElementChild.outerHTML);
  const buttonIcon = node => {
    if (node.localName !== 'svg' || !node.closest('button[type="button"][aria-label]') ||
        node.getAttribute('aria-hidden') !== 'true') return false;
    const path = node.firstElementChild;
    if (!path || path.localName !== 'path' || path.nextElementSibling ||
        path.childNodes.length || !path.getAttribute('d') || path.getAttribute('d').length > 512 ||
        !/^[MmLlHhVvCcSsQqTtAaZz0-9., +\-]+$/.test(path.getAttribute('d'))) return false;
    const svgAttributes = new Set(['width','height','viewBox','fill','aria-hidden','focusable','xmlns']);
    const pathAttributes = new Set(['d','stroke','stroke-width','stroke-linecap','stroke-linejoin','fill']);
    return [...node.attributes].every(attr => svgAttributes.has(attr.name) ||
        attr.name === marker + '-node') &&
      [...path.attributes].every(attr => pathAttributes.has(attr.name) ||
        attr.name === marker + '-node') &&
      ['width','height'].every(name => /^\d{1,2}$/.test(node.getAttribute(name) || '') &&
        Number(node.getAttribute(name)) <= 48) &&
      ['fill','stroke'].every(name => [node,path].every(element =>
        !element.hasAttribute(name) || ['none','currentColor'].includes(element.getAttribute(name))));
  };
  const authentication = node => node.matches('input,textarea,select,[contenteditable]') && (
    /password|credential|secret|authorization|authentication|login|username|one-time-code|otp|passkey|cc-number|cc-csc|(?:api|access|auth|refresh|bearer)[\s_-]*(?:key|token)/i.test(
      [node.type, node.id, node.name, node.autocomplete, node.getAttribute('aria-label'), node.getAttribute('placeholder'),
       ...[...(node.labels || [])].map(label => label.textContent),
       ...(node.getAttribute('aria-labelledby') || '').split(/\s+/).map(id => document.getElementById(id)?.textContent)].join(' ')) ||
    /^(?:token|key)$/i.test(node.id) || /^(?:token|key)$/i.test(node.name));
  if (window.top !== window || (!allowFrames && document.querySelector('iframe,frame'))) return reject('frame');
  if (document.querySelector('#send:disabled, .message.draft')) return reject('draft');
  if (verify && document.documentElement.getAttribute(marker) !== token) return reject('document_changed');
  document.documentElement.setAttribute(marker, token);
  const found = document.querySelectorAll('*');
  if (found.length > 10000) return reject('node_budget');
  if (contentMaskSelector && (!document.querySelector(contentMaskAnchor) ||
      !document.querySelector(contentMaskSelector)))
    return reject('content_mask_missing');
  if (contentMaskSelector) {
    const maskedRoots = [...document.querySelectorAll(contentMaskSelector)];
    const elevated = [...document.querySelectorAll(':popover-open, :modal')];
    if (document.fullscreenElement) elevated.push(document.fullscreenElement);
    if (elevated.some(node => maskedRoots.some(root => root === node || root.contains(node))))
      return reject('content_top_layer');
  }
  for (const [parentSelector, childSelector] of contentMaskRequirements || []) {
    const parent = document.querySelector(parentSelector);
    if (parent && !parent.querySelector(childSelector)) return reject('content_mask_missing');
  }
  const nodes = [...found], rows = [];
  let characters = 0;
  for (const [index, node] of nodes.entries()) {
    node.setAttribute(marker + '-node', String(index));
    const style = getComputedStyle(node);
    const beforeStyle = getComputedStyle(node, '::before');
    const afterStyle = getComputedStyle(node, '::after');
    // CSS URL images can paint outside the owning element through outset,
    // filters, reflection, and pseudo-elements. A host rectangle is not a
    // sufficient mask, so withhold the complete viewport for any such value.
    const cssUrl = computed => prepared.urlProperties.some(property =>
      /url\s*\(/i.test(computed.getPropertyValue(property)));
    const renderedPseudos = [beforeStyle, afterStyle,
      ...['::marker', '::backdrop', '::file-selector-button', '::first-letter',
          '::first-line', '::placeholder'].map(pseudo => getComputedStyle(node, pseudo))];
    if (cssUrl(getComputedStyle(node, '::backdrop'))) return reject('backdrop');
    const imageStyle = [style, ...renderedPseudos].some(cssUrl);
    if ([style, ...renderedPseudos].some(computed =>
      [computed.filter, computed.backdropFilter, computed.webkitBoxReflect].some(value =>
        value && value !== 'none'))) return reject('css_effect');
    const contentMasked = !!contentMaskSelector && node.matches(contentMaskSelector);
    const opaque = imageStyle || authentication(node) || !!node.shadowRoot ||
      (node.matches('iframe,frame') ?
        !(ownedFrameSelector && node.matches(ownedFrameSelector)) :
        node.matches('img,picture,canvas,object,embed,video,audio,script,style,template,noscript')) ||
      (node.localName === 'svg' && !icons.includes(node.outerHTML.replace(/ data-evidence-[^= ]+="[^"]*"/g, '')) &&
        !buttonIcon(node));
    const isolated = prepared?.saved.get(node);
    if (opaque && (!isolated?.opaque || style.opacity !== '0' ||
        style.contentVisibility !== 'hidden' || style.display === 'contents')) return reject('isolation_verify');
    if (isolated && !node.isConnected) return reject('document_changed');
    if (contentMasked && (style.opacity !== '0' || style.clipPath !== 'inset(0px)' ||
        style.display === 'contents')) return reject('isolation_verify');
    // Each text node belongs to one element. Ancestor textContent/innerText
    // repeats large inert bundles many times, exhausting the fixed budget even
    // when the rendered page is small. The root visible-text walk below still
    // checks secrets split across descendants before pixels are admitted.
    const inert = node.matches('script,style,template,noscript');
    const directText = inert ? '' : [...node.childNodes].filter(child => child.nodeType === Node.TEXT_NODE)
      .map(child => child.nodeValue || '').join('');
    const leafText = inert || directText || node.childElementCount ? '' : (node.innerText || '');
    const texts = [directText, leafText, ...renderedPseudos.map(computed => computed.content || ''),
                   ...(typeof node.value === 'string' ? [node.value] : []),
                   ...[...node.attributes].filter(attr => attr.name !== 'srcdoc')
                     .map(attr => attr.value).filter(Boolean)];
    characters += texts.reduce((sum, text) => sum + text.length, 0);
    if (characters > 1048576) return reject('text_budget');
    rows.push({index, parent: nodes.indexOf(node.parentElement),
      forced: opaque || inert || contentMasked, contentMasked,
      texts});
  }
  const stack = [document.documentElement], visible = [];
  let visited = 0;
  while (stack.length) {
    if (++visited > 20000) return reject('tree_budget');
    const node = stack.pop();
    if (node.nodeType === Node.TEXT_NODE) { visible.push(node.nodeValue || ''); continue; }
    if (node.nodeType !== Node.ELEMENT_NODE) continue;
    const row = rows[Number(node.getAttribute(marker + '-node'))];
    if (row.forced || node.matches('style,script,template,noscript')) continue;
    if (node.matches('input,textarea,select')) { visible.push(node.value || ''); continue; }
    if (node.childNodes.length + visited > 20000) return reject('tree_budget');
    stack.push(...[...node.childNodes].reverse());
  }
  const rendered = visible.join('');
  if (characters + rendered.length > 1048576) return reject('text_budget');
  rows[0].texts.push(rendered);
  return rows;
}'''


async def _closed_author_shadow(session):
    """Audit direct closed roots without serializing masked iframe descendants."""
    document = await session.send('DOM.getDocument', {'depth': 0, 'pierce': False})
    nodes = await session.send('DOM.querySelectorAll', {
        'nodeId': document['root']['nodeId'], 'selector': '*'})
    identifiers = nodes['nodeIds']
    if type(identifiers) is not list or len(identifiers) > 10000:
        raise ValueError('Shadow audit node budget unavailable')
    for identifier in identifiers:
        described = await session.send('DOM.describeNode', {
            'nodeId': identifier, 'depth': 0, 'pierce': True})
        if any(root.get('shadowRootType') == 'closed'
               for root in described['node'].get('shadowRoots', ())):
            return True
    return False


async def _owned_application_frames(session, origin, contract):
    """Bind each selected application iframe to its exact Chromium loader."""
    document = await session.send('DOM.getDocument', {'depth': 0, 'pierce': False})
    found = await session.send('DOM.querySelectorAll', {
        'nodeId': document['root']['nodeId'], 'selector': 'iframe,frame'})
    selected = await session.send('DOM.querySelectorAll', {
        'nodeId': document['root']['nodeId'], 'selector': contract.selector})
    frame_tree = await session.send('Page.getFrameTree')
    top = frame_tree['frameTree']
    if any(child.get('childFrames') for child in top.get('childFrames', ())):
        return None
    children = {child['frame']['id']: child['frame'] for child in top.get('childFrames', ())}
    node_ids = found['nodeIds']
    if (not node_ids or len(node_ids) != len(children) or len(node_ids) > 16
            or set(node_ids) != set(selected['nodeIds'])):
        return None
    expected = urlsplit(origin)
    records = []
    for node_id in node_ids:
        described = await session.send('DOM.describeNode', {
            'nodeId': node_id, 'depth': 0, 'pierce': False})
        node = described['node']
        attrs = dict(zip(node.get('attributes', ())[::2], node.get('attributes', ())[1::2]))
        frame = children.get(node.get('frameId'))
        source = attrs.get('src', '')
        loaded = urlsplit(frame['url']) if frame else None
        resolved = urlsplit(urljoin(origin.rstrip('/') + '/', source))
        if (node.get('localName') != 'iframe'
                or attrs.get('sandbox') != contract.sandbox or 'srcdoc' in attrs
                or not source or frame is None or frame.get('parentId') != top['frame']['id']
                or (loaded.scheme, loaded.netloc) != (expected.scheme, expected.netloc)
                or loaded.path != contract.path
                or loaded.query or loaded.fragment or resolved != loaded):
            return None
        deep = await session.send('DOM.describeNode', {
            'nodeId': node_id, 'depth': -1, 'pierce': True})
        content = deep['node'].get('contentDocument')
        if content is None:
            return None
        stack = [content]
        visited = 0
        while stack:
            item = stack.pop()
            visited += 1
            if (visited > 50000 or any(root.get('shadowRootType') != 'user-agent'
                    for root in item.get('shadowRoots', ()))):
                return None
            stack.extend(item.get('children', ()))
        records.append((node['backendNodeId'], frame['id'], frame['loaderId'], frame['url']))
    return tuple(records)


_FRAME_TEXT = r'''(() => {
  if (self.origin !== 'null' || document.querySelector('iframe,frame')) return null;
  const nodes = document.querySelectorAll('*');
  if (nodes.length > 50000) return null;
  const values = [document.documentElement.textContent || ''];
  let size = values[0].length;
  for (const node of nodes) {
    if (node.matches('input,textarea,select,[contenteditable]')) {
      const hint = [node.type, node.id, node.name, node.autocomplete,
        node.getAttribute('aria-label'), node.getAttribute('placeholder')].join(' ');
      if (/password|credential|secret|authorization|authentication|login|username|one-time-code|otp|passkey|cc-number|cc-csc|(?:api|access|auth|refresh|bearer)[\s_-]*(?:key|token)/i.test(hint))
        return null;
    }
    for (const attr of node.attributes) {
      if (attr.name === 'srcdoc') return null;
      values.push(attr.value); size += attr.value.length;
    }
    if (typeof node.value === 'string') { values.push(node.value); size += node.value.length; }
    for (const pseudo of ['::before','::after','::marker','::backdrop',
                          '::file-selector-button','::first-letter','::first-line',
                          '::placeholder']) {
      const content = getComputedStyle(node, pseudo).content || '';
      values.push(content); size += content.length;
    }
    if (size > 8388608) return null;
  }
  return values.join('');
})()'''


async def _renderer_literals(session, records, boundary):
    """Inspect literal frame content in an isolated world, away from model patches."""
    snapshots = []
    for _, frame_id, _, _ in records:
        world = await session.send('Page.createIsolatedWorld', {
            'frameId': frame_id, 'worldName': 'api-agent-evidence'})
        result = await session.send('Runtime.evaluate', {
            'contextId': world['executionContextId'], 'expression': _FRAME_TEXT,
            'returnByValue': True})
        if result.get('exceptionDetails') or result['result'].get('type') != 'string':
            return None
        literal = result['result']['value']
        decoded = unquote(literal)
        if (boundary.content_export({'text': literal}).get('text') != literal or
                boundary.content_export({'text': decoded}).get('text') != decoded):
            return None
        snapshots.append(literal)
    return tuple(snapshots)


async def capture_studio_viewport(page, boundary, origin, *, path='/', allow_frames=False,
                                  owned_frames=None, content_mask_selector=None,
                                  content_mask_anchor=None,
                                  content_mask_requirements=(),
                                  unavailable=None, stage=None):
    """Return masked original PNG, or None when owned safety is unavailable.

    Authentication controls and unsupported rendering surfaces are hidden and
    masked before capture. When content is withheld, the selected provider supplies
    owned conversation selectors and an anchor; their painted regions are clipped
    and masked while surrounding controls remain visible. Top-layer descendants
    are rejected because they can paint outside the clipped region. With content enabled,
    ordinary form values and exact authored navigation SVGs remain visible.
    An explicit ``owned_frames`` contract admits original
    application frame pixels after frame identity and literal credential checks.
    Browser strings remain transient and are never attachment metadata. The
    caller owns exception isolation.
    """
    def reject(reason):
        if unavailable is not None:
            unavailable(reason)
        return None

    started = monotonic()
    def mark(name):
        if stage is not None:
            stage(name, int((monotonic() - started) * 1000))

    if not origin or (boundary.policy.allow_content is not True and
            (not content_mask_selector or not content_mask_anchor)):
        return reject('policy')
    if content_mask_selector is not None and (type(content_mask_selector) is not str or
            not content_mask_selector or len(content_mask_selector) > 512):
        return reject('policy')
    if content_mask_anchor is not None and (type(content_mask_anchor) is not str or
            not content_mask_anchor or len(content_mask_anchor) > 256):
        return reject('policy')
    if (type(content_mask_requirements) is not tuple or len(content_mask_requirements) > 8 or
            any(type(pair) is not tuple or len(pair) != 2 or
                any(type(selector) is not str or not selector or len(selector) > 256
                    for selector in pair) for pair in content_mask_requirements)):
        return reject('policy')
    if owned_frames is not None and (
            type(owned_frames) is not OwnedFrameContract or not owned_frames.selector
            or not owned_frames.path.startswith('/') or not owned_frames.sandbox):
        return reject('frame')
    expected = urlsplit(origin)
    actual = urlsplit(page.url)
    if (actual.scheme, actual.netloc, actual.path, actual.query, actual.fragment) != (
            expected.scheme, expected.netloc, path, '', ''):
        return reject('url')
    if not allow_frames and len(page.frames) != 1:
        return reject('frame')
    try:
        cdp = await page.context.new_cdp_session(page)
    except Exception:
        return reject('shadow_audit')
    marker, token = 'data-evidence-' + uuid4().hex, uuid4().hex
    prepared = None
    renderer_records = None
    renderer_literals = None
    scripts_disabled = False
    try:
        if owned_frames is not None:
            try:
                await cdp.send('Emulation.setScriptExecutionDisabled', {'value': True})
                scripts_disabled = True
                renderer_records = await _owned_application_frames(cdp, origin, owned_frames)
                mark('frame_admitted')
                if renderer_records is None:
                    return reject('frame')
                renderer_literals = await _renderer_literals(cdp, renderer_records, boundary)
                mark('frame_literals')
                if renderer_literals is None:
                    return reject('frame_content')
            except Exception:
                return reject('frame_inspection')
        try:
            if await _closed_author_shadow(cdp):
                return reject('closed_shadow')
            mark('parent_audit')
        except Exception:
            return reject('shadow_audit')
        prepared = await page.evaluate_handle(_PREPARE, {
            'trustedIcons': _TRUSTED_ICONS,
            'ownedFrameSelector': owned_frames.selector if owned_frames else None,
            'contentMaskSelector': content_mask_selector if boundary.policy.allow_content is not True else None})
        reason = await prepared.evaluate('state => state.reason')
        mark('prepared')
        if reason:
            return reject(reason)
        rows = await page.evaluate(_INSPECT, {'marker': marker, 'token': token,
            'trustedIcons': _TRUSTED_ICONS, 'allowFrames': allow_frames,
            'ownedFrameSelector': owned_frames.selector if owned_frames else None,
            'contentMaskSelector': content_mask_selector if boundary.policy.allow_content is not True else None,
            'contentMaskAnchor': content_mask_anchor if boundary.policy.allow_content is not True else None,
            'contentMaskRequirements': content_mask_requirements if boundary.policy.allow_content is not True else (),
            'diagnose': unavailable is not None, 'prepared': prepared})
        if isinstance(rows, dict) and rows.get('unavailable'):
            return reject(rows['unavailable'])
        mark('inspected')
        if rows is None:
            return reject('inspection')
        if len(rows) > 10000:
            return reject('node_budget')
        # A registered credential in ordinary page content is not safely
        # localized by ancestor pruning: split/repeated secrets can overlap.
        # Keep original pixels unavailable rather than guess at their spans.
        if _sensitive(rows, boundary, exclude_masked_subtrees=boundary.policy.allow_content is not True):
            return reject('registered_secret')
        def inside_content_mask(row):
            index = row['index']
            while index >= 0:
                if rows[index].get('contentMasked', False):
                    return True
                index = rows[index]['parent']
            return False
        masked = {row['index'] for row in rows if row['forced'] and not inside_content_mask(row)}
        locators = [page.locator(f'[{marker}-node="{index}"]') for index in sorted(masked)]
        image = await page.screenshot(type='png', full_page=False, mask=locators,
                                      mask_color='#000000',
                                      animations='allow' if owned_frames else 'disabled')
        mark('screenshot')
        after = await page.evaluate(_INSPECT, {'marker': marker, 'token': token,
            'verify': True, 'trustedIcons': _TRUSTED_ICONS, 'allowFrames': allow_frames,
            'ownedFrameSelector': owned_frames.selector if owned_frames else None,
            'contentMaskSelector': content_mask_selector if boundary.policy.allow_content is not True else None,
            'contentMaskAnchor': content_mask_anchor if boundary.policy.allow_content is not True else None,
            'contentMaskRequirements': content_mask_requirements if boundary.policy.allow_content is not True else (),
            'diagnose': unavailable is not None, 'prepared': prepared})
        mark('verified')
        try:
            closed_shadow = await _closed_author_shadow(cdp)
        except Exception:
            return reject('shadow_audit')
        if closed_shadow:
            return reject('closed_shadow')
        mark('parent_reaudit')
        if owned_frames is not None:
            try:
                if (await _owned_application_frames(cdp, origin, owned_frames) != renderer_records or
                        await _renderer_literals(cdp, renderer_records, boundary) != renderer_literals):
                    return reject('frame_changed')
                mark('frame_rechecked')
            except Exception:
                return reject('frame_inspection')
        result = image if after == rows and page.url == origin.rstrip('/') + path else None
        if result is None:
            reject(after.get('unavailable', 'document_changed') if isinstance(after, dict)
                   else 'document_changed')
    finally:
        try:
            if prepared is not None:
                await prepared.evaluate(_RESTORE)
        finally:
            try:
                if prepared is not None:
                    await prepared.dispose()
                    await page.evaluate('''marker => {
                      for (const node of document.querySelectorAll('[' + marker + '-node]'))
                        node.removeAttribute(marker + '-node');
                      document.documentElement.removeAttribute(marker);
                    }''', marker)
            finally:
                try:
                    if scripts_disabled:
                        await cdp.send('Emulation.setScriptExecutionDisabled', {'value': False})
                finally:
                    await cdp.detach()
                    mark('cleanup')

    # Marker cleanup above awaits the browser. A newly registered credential
    # must still invalidate already captured pixels before they leave this call.
    if owned_frames is not None and any(
            boundary.content_export({'text': literal}).get('text') != literal or
            boundary.content_export({'text': unquote(literal)}).get('text') != unquote(literal)
            for literal in renderer_literals):
        return reject('registered_secret')
    return result if not _sensitive(rows, boundary,
        exclude_masked_subtrees=boundary.policy.allow_content is not True) else reject('registered_secret')


def _sensitive(rows, boundary, *, exclude_masked_subtrees=False):
    generation, snapshot = boundary.snapshot_content()
    seen = set()
    for row in rows:
        if row['forced']:
            continue
        if exclude_masked_subtrees:
            parent = row['parent']
            while parent >= 0:
                if rows[parent]['forced']:
                    break
                parent = rows[parent]['parent']
            if parent >= 0:
                continue
        for text in row['texts']:
            if text in seen:
                continue
            seen.add(text)
            if snapshot._export({'text': text}).get('text') != text:
                return True
    return not boundary.commit_if_current(generation, lambda: None)
