"""Original viewport capture for an owned, authored UI document.

The default policy masks embedded frames. A selected provider may opt in its
owned application frames after binding their browser identity and admission
contract. This opt-in captures original application pixels; literal credential
checks do not certify arbitrary pixels painted by application scripts. Provider
authentication pages and unknown frames remain outside the capture contract.
"""
import asyncio
import base64
import json
import math
import re
from dataclasses import dataclass
from functools import wraps
from time import monotonic
from urllib.parse import unquote, urljoin, urlsplit
from uuid import uuid4

from api_agent.providers.control import finish_cleanup
from api_agent.providers.progress import bind_timeout_budget, component_timeout, report_timeout


@dataclass(frozen=True, slots=True)
class OwnedFrameContract:
    """Provider-owned, exact application frame admitted to original pixels."""

    selector: str
    path: str
    sandbox: str


@dataclass(frozen=True, slots=True)
class OwnedChromeContract:
    """Exact provider-authored chrome; its source owns nested UI and static assets.

    Original source-owned SVG/static image pixels are admitted after literal
    credential checks, like owned application frames. This does not certify
    arbitrary information encoded into image or vector pixels.
    """

    selector: str
    shadow_selectors: tuple[str, ...] = ()
    svg_selector: str | None = None
    image_selector: str | None = None
    image_sources: tuple[str, ...] = ()
    image_source_properties: tuple[str, ...] = ()


def _valid_owned_chrome(chrome):
    if type(chrome) is not OwnedChromeContract:
        return False
    for value, optional in ((chrome.selector, False), (chrome.svg_selector, True),
                            (chrome.image_selector, True)):
        if optional and value is None:
            continue
        if type(value) is not str or not value or len(value) > 256:
            return False
    for values, count, length in ((chrome.shadow_selectors, 16, 256),
                                  (chrome.image_sources, 8, 65536),
                                  (chrome.image_source_properties, 8, 256)):
        if type(values) is not tuple or len(values) > count:
            return False
        if any(type(value) is not str or not value or len(value) > length for value in values):
            return False
    return True


_TRUSTED_ICONS = ['<svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true" focusable="false"><path d="M12 5v14M5 12h14"/></svg>', '<svg viewBox="0 0 24 24" width="23" height="23" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><path d="m9.5 3-.7 2.2-2 .9-2.1-.5-2.5 4.3 1.5 1.7v2.3l-1.5 1.7 2.5 4.3 2.1-.5 2 .9.7 2.2h5l.7-2.2 2-.9 2.1.5 2.5-4.3-1.5-1.7v-2.3l1.5-1.7-2.5-4.3-2.1.5-2-.9-.7-2.2z" transform="translate(0 -1) scale(1 .96)"/><circle cx="12" cy="12" r="3.3"/></svg>']


_PREPARE = r'''({trustedIcons, ownedFrameSelector, ownedShadowSelector, chromeOwned, chromeConfig, contentMaskSelector, preserveContentFocus=false}) => {
  const saved = new Map(), masks = new Map();
  const ownedHosts = new Set(ownedShadowSelector ? document.querySelectorAll(ownedShadowSelector) : []);
  const chromeRoots = chromeOwned ? [...ownedHosts] : [];
  if (chromeOwned) chromeConfig.imageSources = [...chromeConfig.imageSources,
    ...chromeRoots.flatMap(root => chromeConfig.imageSourceProperties.map(path =>
      path.split('.').reduce((value, key) => value?.[key], root)))
      .filter(value => typeof value === 'string' && value.length <= 65536)];
  const composedParent = node => node.assignedSlot || node.parentElement || node.getRootNode().host || null;
  const composedContains = (root, node) => {
    for (let current = node; current; current = composedParent(current))
      if (current === root) return true;
    return false;
  };
  const insideChrome = node => chromeRoots.some(root => composedContains(root, node));
  const staticImage = node => {
    if (node.localName !== 'img' || !insideChrome(node) || !chromeConfig.imageSelector ||
        !node.matches(chromeConfig.imageSelector) || !node.complete ||
        !node.naturalWidth || node.hasAttribute('srcset')) return false;
    try {
      const source = new URL(node.currentSrc || node.src, location.href);
      return chromeConfig.imageSources.some(value => new URL(value, location.href).href === source.href);
    } catch { return false; }
  };

  const maskBox = node => {
    const box = node.getBoundingClientRect();
    let left = Math.max(0, box.left), top = Math.max(0, box.top);
    let right = Math.min(innerWidth, box.right), bottom = Math.min(innerHeight, box.bottom);
    for (let parent = composedParent(node); parent; parent = composedParent(parent)) {
      const style = getComputedStyle(parent), clip = parent.getBoundingClientRect();
      const scaleX = parent.offsetWidth ? clip.width / parent.offsetWidth : 1;
      const scaleY = parent.offsetHeight ? clip.height / parent.offsetHeight : 1;
      if (style.clipPath !== 'none') {
        // Computed inset offsets use the ancestor border box. Rounded corners
        // share this conservative rectangle; unsupported shapes stay withheld.
        const inset = /^inset\(([^()]*)\)$/.exec(style.clipPath);
        if (!inset || inset[1].length > 512) throw 'content_clip';
        const parts = inset[1].trim().split(/\s+round\s+/);
        const length = /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(px|%)$/;
        const offsets = parts[0].trim().split(/\s+/);
        if (parts.length > 2 || offsets.length > 4 || !offsets.every(value => length.test(value)))
          throw 'content_clip';
        if (parts.length === 2) {
          const radii = parts[1].split('/').map(part => part.trim().split(/\s+/));
          if (radii.length > 2 || radii.some(values => values.length > 4 ||
              !values.every(value => length.test(value) && parseFloat(value) >= 0))) throw 'content_clip';
        }
        const [a,b=a,c=a,d=b] = offsets;
        const resolve = (value, extent, scale) => parseFloat(value) *
          (value.endsWith('%') ? extent / 100 : scale);
        left = Math.max(left, clip.left + resolve(d, clip.width, scaleX));
        right = Math.min(right, clip.right - resolve(b, clip.width, scaleX));
        top = Math.max(top, clip.top + resolve(a, clip.height, scaleY));
        bottom = Math.min(bottom, clip.bottom - resolve(c, clip.height, scaleY));
      }
      const paintClip = /\b(paint|content|strict)\b/.test(style.contain);
      if (paintClip || /^(hidden|clip|scroll|auto)$/.test(style.overflowX)) {
        const edge = clip.left + parent.clientLeft * scaleX;
        left = Math.max(left, edge); right = Math.min(right, edge + parent.clientWidth * scaleX);
      }
      if (paintClip || /^(hidden|clip|scroll|auto)$/.test(style.overflowY)) {
        const edge = clip.top + parent.clientTop * scaleY;
        top = Math.max(top, edge); bottom = Math.min(bottom, edge + parent.clientHeight * scaleY);
      }
    }
    return [left, top, Math.max(0, right-left), Math.max(0, bottom-top)];
  };

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
       ...(node.getAttribute('aria-labelledby') || '').split(/\s+/).map(id => node.getRootNode().getElementById?.(id)?.textContent)].join(' ')) ||
    /^(?:token|key)$/i.test(node.id) || /^(?:token|key)$/i.test(node.name));
  const nodes = [], scopes = [document];
  while (scopes.length && nodes.length <= 10000) {
    for (const node of scopes.pop().querySelectorAll('*')) {
      nodes.push(node);
      if (node.shadowRoot && (ownedHosts.has(node) || (insideChrome(node) && chromeConfig.shadowSelectors.some(selector => node.matches(selector))))) {
        ownedHosts.add(node); scopes.push(node.shadowRoot);
      }
      if (nodes.length > 10000) break;
    }
  }
  if (nodes.length > 10000) return {saved, reason:'node_budget'};
  const contentRoots = nodes.filter(node => contentMaskSelector && node.matches(contentMaskSelector));
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
  let restoreFocus = () => {};
  try {
  // Read every admission decision before writing styles: reactive selectors
  // must never turn an originally ordinary surface into an excluded subtree.
  const plans = new Map();
  for (const node of nodes) {
    const computed = getComputedStyle(node);
    const contentMasked = !!contentMaskSelector && node.matches(contentMaskSelector);
    const pseudoStyles = pseudos.map(pseudo => getComputedStyle(node, pseudo));
    const cssUrl = [computed, ...pseudoStyles].some(hasUrl);
    const effects = ['filter','backdrop-filter','-webkit-box-reflect'].filter(property => {
      const value = computed.getPropertyValue(property);
      return value && value !== 'none';
    });
    const opaque = cssUrl || (!!node.shadowRoot && !ownedHosts.has(node)) ||
      authentication(node) || (node.matches('iframe,frame') ?
        !(ownedFrameSelector && node.matches(ownedFrameSelector)) :
        (node.matches('img,picture,canvas,object,embed,video,audio,script,style,template,noscript') && !staticImage(node))) ||
      (node.localName === 'svg' && !(insideChrome(node) && chromeConfig.svgSelector &&
        node.matches(chromeConfig.svgSelector) && !node.querySelector('foreignObject,image,filter,script')) && !icons.includes(node.outerHTML) && !buttonIcon(node));
    const size = (axis, edges) => Math.max(0, (parseFloat(computed[axis]) || 0) -
      (computed.boxSizing === 'border-box' ? edges.reduce((sum, edge) =>
        sum + (parseFloat(computed[edge]) || 0), 0) : 0));
    plans.set(node, {contentMasked, effects, opaque, display:computed.display,
      intrinsic:size('width', ['paddingLeft','paddingRight','borderLeftWidth','borderRightWidth']) + 'px ' +
        size('height', ['paddingTop','paddingBottom','borderTopWidth','borderBottomWidth']) + 'px'});
  }
  const isolatedRoots = nodes.filter(node => plans.get(node).opaque || plans.get(node).contentMasked);
  const paintProtected = new Set(nodes.filter(node => isolatedRoots.some(root => composedContains(root, node))));
  const geometryProtected = new Set(nodes.filter(node => isolatedRoots.some(root => root !== node && composedContains(root, node))));
  const layout = nodes.filter(node => !geometryProtected.has(node))
    .map(node => [node, node.getBoundingClientRect().toJSON()]);
  // Snapshot original paint before any inline override or capture-induced blur.
  // Shared property names keep the bounded value snapshots compact. Pseudos
  // matter too: authored selectors can react without changing box geometry.
  const paintProperties = [...getComputedStyle(document.documentElement)]
    .filter(property => !property.startsWith('--'));
  if (paintProperties.length > 1024) return {saved, urlProperties, reason:'paint_budget'};
  const paintScopes = [null, ...pseudos], paint = [];
  const ownAnimations = node => node.getAnimations({subtree:true})
    .filter(animation => animation.effect?.target === node);
  const effectSignature = animation => JSON.stringify({
    keyframes:animation.effect?.getKeyframes(), timing:animation.effect?.getTiming()});
  let paintBytes = 0;
  for (const node of nodes) {
    if (paintProtected.has(node)) continue;
    let nodeBytes = 0;
    const values = paintScopes.map(pseudo => {
      const computed = getComputedStyle(node, pseudo);
      return paintProperties.map(property => {
        const value = computed.getPropertyValue(property);
        nodeBytes += value.length * 2;
        return value;
      });
    });
    const animations = ownAnimations(node);
    if (animations.length > 1024) return {saved, urlProperties, reason:'paint_budget'};
    const effects = animations.map(animation => {
      const signature = effectSignature(animation);
      nodeBytes += signature.length * 2;
      return [animation, animation.effect, signature];
    });
    paintBytes += nodeBytes;
    if (nodeBytes > 131072 || paintBytes > 67108864)
      return {saved, urlProperties, reason:'paint_budget'};
    paint.push([node, values, effects]);
  }
  for (const node of nodes) {
    const backdrop = getComputedStyle(node, '::backdrop');
    if (hasUrl(backdrop) || (backdrop.content && !['none','normal'].includes(backdrop.content)))
      return {saved, urlProperties, reason:'backdrop'};
  }
  // Size containment on a top-layer element can crash the renderer, and a
  // content root there escapes the clipping its mask relies on.
  const topLayered = node => node.matches(':popover-open, :modal, :fullscreen') ||
    getComputedStyle(node).overlay === 'auto';
  if (isolatedRoots.some(node => topLayered(node)))
    return {saved, urlProperties, reason:'content_top_layer'};
  // Skipping a subtree may blur its focused control. Recover only that
  // capture-induced loss; any newer focus or user interaction takes priority.
  let focused = document.activeElement;
  while (focused?.shadowRoot?.activeElement) focused = focused.shadowRoot.activeElement;
  let focusHidden = false, interrupted = false;
  const interfere = () => { interrupted = true; };
  const events = ['focusin','pointerdown','keydown'];
  for (const event of events) document.addEventListener(event, interfere, true);
  const containsFocus = node => {
    for (let candidate = focused; candidate; candidate = candidate.getRootNode()?.host)
      if (composedContains(node, candidate)) return true;
    return false;
  };
  restoreFocus = () => {
    for (const event of events) document.removeEventListener(event, interfere, true);
    if (!focusHidden || interrupted || !focused?.isConnected ||
        ![null,document.body,document.documentElement].includes(document.activeElement)) return;
    // Chromium focus preserves the current input selection. Focus handlers may
    // update that selection, and their newer range remains authoritative.
    focused.focus({preventScroll:true});
  };
  for (const node of nodes) {
    const {contentMasked, effects, opaque, display, intrinsic} = plans.get(node);
    // A paused page cannot open new top-layer content, and inspection rejects any
    // already open under an isolated root, so opacity alone hides it there. Size
    // containment stays for live pages only: it can move authored layout.
    if ((opaque || contentMasked) && !preserveContentFocus) focusHidden ||= containsFocus(node);
    if ((opaque || contentMasked) && display === 'contents') throw 'display_contents';
    if (!opaque && !effects.length && !contentMasked) continue;
    const containment = preserveContentFocus ? [] : ['content-visibility','contain-intrinsic-size'];
    const properties = [...new Set([...(opaque ? ['opacity', ...containment, 'animation','transition'] : []),
      ...(contentMasked ? ['opacity','clip-path', ...containment] : []), ...effects])];
    const originalStyle = node.getAttribute('style');
    const previous = properties.map(property => [property,
      node.style.getPropertyValue(property), node.style.getPropertyPriority(property)]);
    const record = {opaque, previous, originalStyle, preparedStyle:null, overrides:[]};
    saved.set(node, record);
    if ((opaque || contentMasked) && !preserveContentFocus) {
      node.style.setProperty('contain-intrinsic-size', intrinsic, 'important');
    }
    if (opaque) {
      node.style.setProperty('opacity', '0', 'important');
      if (!preserveContentFocus) node.style.setProperty('content-visibility', 'hidden', 'important');
      node.style.setProperty('animation', 'none', 'important');
      node.style.setProperty('transition', 'none', 'important');
    }
    if (contentMasked) {
      if (!preserveContentFocus) node.style.setProperty('content-visibility', 'hidden', 'important');
      node.style.setProperty('opacity', '0', 'important');
      node.style.setProperty('clip-path', 'inset(0)', 'important');
    }
    for (const effect of effects) node.style.setProperty(effect, 'none', 'important');
    const overrides = properties.map(property => [property,
      node.style.getPropertyValue(property), node.style.getPropertyPriority(property)]);
    record.preparedStyle = node.getAttribute('style');
    record.overrides = overrides;
  }
  // Admit every native keyframe, not only the phase painted during inspection.
  // Native keyframes resolve CSS variables; their bounded literal signatures
  // join the ordinary credential boundary below. Unsupported future surfaces
  // remain unavailable even when their current phase is harmless.
  const effectTexts = new Map();
  for (const [node, , effects] of paint) {
    if (paintProtected.has(node)) continue;
    const literals = effects.map(([, , signature]) => signature);
    for (const literal of literals) {
      if (/url\s*\(/i.test(literal)) throw 'css_effect';
      for (const frame of JSON.parse(literal).keyframes || []) {
        for (const [name, value] of Object.entries(frame)) {
          const property = name.replace(/[A-Z]/g, letter => '-' + letter.toLowerCase());
          if (['filter','backdrop-filter','webkit-box-reflect','-webkit-box-reflect'].includes(property) &&
              value && value !== 'none') throw 'css_effect';
        }
      }
    }
    if (literals.length) effectTexts.set(node, literals);
  }
  let layoutShift = null;
  const verifyOriginal = () => {
    // Size containment must not move cards or controls (for example through
    // collapsed margins). Unsupported authored layouts fail closed.
    const shifted = [];
    for (const [node, before] of layout) {
      if (geometryProtected.has(node)) continue;
      if (!node.isConnected) return 'document_changed';
      const after = node.getBoundingClientRect();
      if (['x','y','width','height'].some(key => Math.abs(after[key] - before[key]) > .02))
        shifted.push([node, before, after]);
    }
    if (shifted.length) {
      // Structural identity only: authored tag and test hook, never text or attributes.
      const [node, before, after] = shifted.find(([moved]) => saved.has(moved)) || shifted[0];
      const plan = plans.get(node) || {};
      const box = rect => Object.fromEntries(['x','y','width','height'].map(key => [key, Math.round(rect[key] * 100) / 100]));
      layoutShift = {tag:node.localName, testid:node.getAttribute('data-testid'),
        kind:plan.opaque ? 'opaque' : plan.contentMasked ? 'content' : plan.effects?.length ? 'effect' : 'none',
        shifted:shifted.length, before:box(before), after:box(after)};
      return 'content_layout';
    }
    for (const [node, beforeScopes, beforeEffects] of paint) {
      if (paintProtected.has(node)) continue;
      const animations = ownAnimations(node);
      if (animations.length !== beforeEffects.length || animations.some(animation =>
          !beforeEffects.some(([original, effect, signature]) => original === animation &&
            effect === animation.effect && signature === effectSignature(animation))))
        return 'paint_changed';
      const allowed = new Set(), record = saved.get(node);
      if (record) {
        // Let the browser expand only our planned overrides into their canonical
        // longhands. The detached declaration never enters the authored tree.
        const declaration = document.createElement('div').style;
        for (const [property, value, priority] of record.overrides) {
          declaration.setProperty(property, value, priority); allowed.add(property);
        }
        for (const property of declaration) allowed.add(property);
      }
      for (const [scopeIndex, pseudo] of paintScopes.entries()) {
        const computed = getComputedStyle(node, pseudo);
        for (const [index, property] of paintProperties.entries()) {
          if (scopeIndex === 0 && allowed.has(property)) continue;
          if (computed.getPropertyValue(property) !== beforeScopes[scopeIndex][index])
            return 'paint_changed';
        }
      }
    }
    return null;
  };
  // The first inspection invokes verifyOriginal before any pixels are taken.
  // Avoid repeating the full paint audit here; the baseline and both the
  // pre- and post-screenshot inspections remain authoritative.
  // Keep clipped intersections keyed by original nodes. Capture planning must
  // never add DOM whose presence could activate authored selectors.
  for (const node of contentRoots) masks.set(node, maskBox(node));
  return {saved, masks, ownedHosts, chromeRoots, chromeConfig, restoreFocus, urlProperties, verifyOriginal, layoutShift:() => layoutShift, effectTexts, preserveContentFocus, reason:null, nodeIds:new WeakMap(), nextNodeId:0, marked:new Set()};
  } catch (error) {
    for (const [node, record] of saved) {
      if (record.originalStyle === null) node.removeAttribute('style');
      else node.setAttribute('style', record.originalStyle);
    }
    restoreFocus();
    return {saved:new Map(), urlProperties,
      reason:['display_contents','content_layout','content_clip','paint_changed','document_changed','css_effect'].includes(error) ? error : 'prepare_error'};
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
  for (const node of prepared.marked || []) node.removeAttribute(prepared.marker + '-node');
  prepared.restoreFocus?.();
}'''


_INSPECT = r'''({marker, token, verify, trustedIcons, allowFrames, ownedFrameSelector, ownedShadowSelector, chromeOwned, chromeConfig, contentMaskSelector, contentMaskAnchor, contentMaskRequirements, diagnose, prepared}) => {
  const reject = reason => diagnose ? {unavailable:reason,
    shift:reason === 'content_layout' ? prepared.layoutShift?.() ?? null : null} : null;
  const ownedHosts = prepared.ownedHosts;
  const chromeRoots = prepared.chromeRoots;
  chromeConfig = prepared.chromeConfig;
  const composedParent = node => node.assignedSlot || node.parentElement || node.getRootNode().host || null;
  const composedContains = (root, node) => {
    for (let current = node; current; current = composedParent(current))
      if (current === root) return true;
    return false;
  };
  const insideChrome = node => chromeRoots.some(root => composedContains(root, node));
  const staticImage = node => {
    if (node.localName !== 'img' || !insideChrome(node) || !chromeConfig.imageSelector ||
        !node.matches(chromeConfig.imageSelector) || !node.complete ||
        !node.naturalWidth || node.hasAttribute('srcset')) return false;
    try {
      const source = new URL(node.currentSrc || node.src, location.href);
      return chromeConfig.imageSources.some(value => new URL(value, location.href).href === source.href);
    } catch { return false; }
  };

  const maskBox = node => {
    const box = node.getBoundingClientRect();
    let left = Math.max(0, box.left), top = Math.max(0, box.top);
    let right = Math.min(innerWidth, box.right), bottom = Math.min(innerHeight, box.bottom);
    for (let parent = composedParent(node); parent; parent = composedParent(parent)) {
      const style = getComputedStyle(parent), clip = parent.getBoundingClientRect();
      const scaleX = parent.offsetWidth ? clip.width / parent.offsetWidth : 1;
      const scaleY = parent.offsetHeight ? clip.height / parent.offsetHeight : 1;
      if (style.clipPath !== 'none') {
        // Computed inset offsets use the ancestor border box. Rounded corners
        // share this conservative rectangle; unsupported shapes stay withheld.
        const inset = /^inset\(([^()]*)\)$/.exec(style.clipPath);
        if (!inset || inset[1].length > 512) throw 'content_clip';
        const parts = inset[1].trim().split(/\s+round\s+/);
        const length = /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(px|%)$/;
        const offsets = parts[0].trim().split(/\s+/);
        if (parts.length > 2 || offsets.length > 4 || !offsets.every(value => length.test(value)))
          throw 'content_clip';
        if (parts.length === 2) {
          const radii = parts[1].split('/').map(part => part.trim().split(/\s+/));
          if (radii.length > 2 || radii.some(values => values.length > 4 ||
              !values.every(value => length.test(value) && parseFloat(value) >= 0))) throw 'content_clip';
        }
        const [a,b=a,c=a,d=b] = offsets;
        const resolve = (value, extent, scale) => parseFloat(value) *
          (value.endsWith('%') ? extent / 100 : scale);
        left = Math.max(left, clip.left + resolve(d, clip.width, scaleX));
        right = Math.min(right, clip.right - resolve(b, clip.width, scaleX));
        top = Math.max(top, clip.top + resolve(a, clip.height, scaleY));
        bottom = Math.min(bottom, clip.bottom - resolve(c, clip.height, scaleY));
      }
      const paintClip = /\b(paint|content|strict)\b/.test(style.contain);
      if (paintClip || /^(hidden|clip|scroll|auto)$/.test(style.overflowX)) {
        const edge = clip.left + parent.clientLeft * scaleX;
        left = Math.max(left, edge); right = Math.min(right, edge + parent.clientWidth * scaleX);
      }
      if (paintClip || /^(hidden|clip|scroll|auto)$/.test(style.overflowY)) {
        const edge = clip.top + parent.clientTop * scaleY;
        top = Math.max(top, edge); bottom = Math.min(bottom, edge + parent.clientHeight * scaleY);
      }
    }
    return [left, top, Math.max(0, right-left), Math.max(0, bottom-top)];
  };

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
       ...(node.getAttribute('aria-labelledby') || '').split(/\s+/).map(id => node.getRootNode().getElementById?.(id)?.textContent)].join(' ')) ||
    /^(?:token|key)$/i.test(node.id) || /^(?:token|key)$/i.test(node.name));
  if (window.top !== window || (!allowFrames && document.querySelector('iframe,frame'))) return reject('frame');
  if (document.querySelector('#send:disabled, .message.draft')) return reject('draft');
  if (verify && document.documentElement.getAttribute(marker) !== token) return reject('document_changed');
  document.documentElement.setAttribute(marker, token);
  const found = [], scopes = [document];
  while (scopes.length && found.length <= 10000) {
    for (const node of scopes.pop().querySelectorAll('*')) {
      found.push(node);
      if (node.shadowRoot && (ownedHosts.has(node) || (insideChrome(node) && chromeConfig.shadowSelectors.some(selector => node.matches(selector))))) {
        ownedHosts.add(node); scopes.push(node.shadowRoot);
      }
      if (found.length > 10000) break;
    }
  }
  if (found.length > 10000) return reject('node_budget');
  if (contentMaskSelector && (!found.some(node => node.matches(contentMaskAnchor)) ||
      !found.some(node => node.matches(contentMaskSelector))))
    return reject('content_mask_missing');
  // Top-layer content paints outside every ancestor's opacity, and content-visibility
  // does not apply to inline or table hosts. Audit every open shadow root, owned or
  // not (closed roots are rejected earlier), rather than trusting CSS to hide it.
  // Only the browser sets overlay: a closing element keeps it through its exit
  // transition, and a customizable select's open picker has it too.
  const inTopLayer = node => node.matches(':popover-open, :modal, :fullscreen') ||
    getComputedStyle(node).overlay === 'auto' ||
    (node.localName === 'select' && getComputedStyle(node, '::picker(select)').overlay === 'auto');
  const topLayer = [];
  let topLayerVisited = 0;
  const collectTopLayer = root => {
    for (const node of root.querySelectorAll('*')) {
      if (++topLayerVisited > 20000) return false;
      if (inTopLayer(node)) topLayer.push(node);
      if (node.shadowRoot && !collectTopLayer(node.shadowRoot)) return false;
    }
    return true;
  };
  if (!collectTopLayer(document)) return reject('tree_budget');
  // A content root in the top layer escapes the clipping its mask relies on.
  // Reject before per-node inspection, which can crash such a renderer.
  const contentRoot = node => !!contentMaskSelector && node.matches(contentMaskSelector);
  const isolatedAbove = node => {
    for (let current = composedParent(node); current; current = composedParent(current))
      if (prepared.saved.get(current)?.opaque || contentRoot(current)) return true;
    return false;
  };
  if (topLayer.some(node => contentRoot(node) || isolatedAbove(node))) return reject('content_top_layer');
  for (const [parentSelector, childSelector] of contentMaskRequirements || []) {
    const children = found.filter(node => node.matches(childSelector));
    for (const parent of found.filter(node => node.matches(parentSelector)))
      if (!children.some(child => composedContains(parent, child))) return reject('content_mask_missing');
  }
  for (const [node, box] of prepared.masks) {
    if (!node.isConnected || JSON.stringify(maskBox(node)) !== JSON.stringify(box))
      return reject('content_mask_geometry');
  }
  const originalReason = prepared.verifyOriginal?.();
  if (originalReason) return reject(originalReason);
  const nodes = [...found], rows = [];
  let characters = 0;
  for (const [index, node] of nodes.entries()) {
    node.setAttribute(marker + '-node', String(index));
    prepared.marker = marker; prepared.marked.add(node);
    if (!prepared.nodeIds.has(node)) prepared.nodeIds.set(node, prepared.nextNodeId++);
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
    const opaque = imageStyle || authentication(node) || (!!node.shadowRoot && !ownedHosts.has(node)) ||
      (node.matches('iframe,frame') ?
        !(ownedFrameSelector && node.matches(ownedFrameSelector)) :
        (node.matches('img,picture,canvas,object,embed,video,audio,script,style,template,noscript') && !staticImage(node))) ||
      (node.localName === 'svg' && !(insideChrome(node) && chromeConfig.svgSelector &&
        node.matches(chromeConfig.svgSelector) && !node.querySelector('foreignObject,image,filter,script')) && !icons.includes(node.outerHTML.replace(/ data-evidence-[^= ]+="[^"]*"/g, '')) &&
        !buttonIcon(node));
    const isolated = prepared?.saved.get(node);
    if (opaque && (!isolated?.opaque || style.opacity !== '0' ||
        (!prepared.preserveContentFocus && style.contentVisibility !== 'hidden') ||
        style.display === 'contents')) return reject('isolation_verify');
    if (isolated && !node.isConnected) return reject('document_changed');
    if (contentMasked && (style.opacity !== '0' || (!prepared.preserveContentFocus && style.contentVisibility !== 'hidden') ||
        style.clipPath !== 'inset(0px)' ||
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
                   ...(prepared.effectTexts?.get(node) || []),
                   ...(typeof node.value === 'string' ? [node.value] : []),
                   ...[...node.attributes].filter(attr => attr.name !== 'srcdoc' &&
                     attr.name !== marker + '-node')
                     .map(attr => attr.value).filter(Boolean)];
    characters += texts.reduce((sum, text) => sum + text.length, 0);
    if (characters > 1048576) return reject('text_budget');
    rows.push({index, parent: nodes.indexOf(composedParent(node)), id:prepared.nodeIds.get(node),
      forced: opaque || inert || contentMasked || prepared.masks.has(node), contentMasked,
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
    let children = node.childNodes;
    if (node.localName === 'slot') {
      const assigned = node.assignedNodes({flatten:true});
      if (assigned.length) children = assigned;
    } else if (node.shadowRoot && ownedHosts.has(node)) {
      children = node.shadowRoot.childNodes;
    }
    stack.push(...[...children].reverse());
  }
  const rendered = visible.join('');
  if (characters + rendered.length > 1048576) return reject('text_budget');
  rows[0].texts.push(rendered);
  return rows;
}'''


class _ShadowDocumentChanged(Exception):
    """An audit's node disappeared before its description was read."""


async def _shadow_node_removed(session, root, identifier):
    # Re-query the native DOM rather than interpreting exception prose. Unknown
    # protocol failures remain closed unless this exact node is now absent.
    try:
        # Keep the original document handle. Calling getDocument again can
        # rebuild frontend node IDs even when the DOM did not change.
        nodes = await session.send('DOM.querySelectorAll', {
            'nodeId': root, 'selector': '*'})
        identifiers = nodes['nodeIds']
        return (type(identifiers) is list and len(identifiers) <= 10000
            and all(type(item) is int and item > 0 for item in identifiers)
            and identifier not in identifiers)
    except Exception:
        return False


async def _closed_author_shadow(session, owned_shadow_selector=None):
    """Audit direct closed roots without serializing masked iframe descendants."""
    document = await session.send('DOM.getDocument', {'depth': 0, 'pierce': False})
    nodes = await session.send('DOM.querySelectorAll', {
        'nodeId': document['root']['nodeId'], 'selector': '*'})
    identifiers = nodes['nodeIds']
    if type(identifiers) is not list or len(identifiers) > 10000:
        raise ValueError('Shadow audit node budget unavailable')
    admitted = set()
    if owned_shadow_selector:
        selected = await session.send('DOM.querySelectorAll', {
            'nodeId': document['root']['nodeId'], 'selector': owned_shadow_selector})
        if len(selected['nodeIds']) > 1:
            raise ValueError('Owned shadow host unavailable')
        admitted = set(selected['nodeIds'])
    for identifier in identifiers:
        try:
            described = await session.send('DOM.describeNode', {
                'nodeId': identifier, 'depth': -1 if identifier in admitted else 0, 'pierce': True})
        except Exception:
            if await _shadow_node_removed(session, document['root']['nodeId'], identifier):
                raise _ShadowDocumentChanged() from None
            raise
        stack = [described['node']]
        visited = 0
        while stack:
            node = stack.pop()
            visited += 1
            if visited > 50000:
                raise ValueError('Shadow audit node budget unavailable')
            if node.get('shadowRootType') == 'closed':
                return True
            stack.extend(node.get('children', ()))
            stack.extend(node.get('shadowRoots', ()))
    return False


async def _owned_application_frames(session, origin, contract, *, include_top=False):
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
    records = tuple(records)
    if include_top:
        frame = top['frame']
        return records, (frame['id'], frame['loaderId'], frame['url'])
    return records


_FRAME_TEXT = r'''(() => {
  if (self.origin !== 'null' || document.querySelector('iframe,frame')) return null;
  const nodes = document.querySelectorAll('*');
  if (nodes.length > 50000) return null;
  const values = [document.documentElement.textContent || ''];
  let size = values[0].length;
  // Author shadow roots are excluded by frame admission. One native document
  // pass includes all element and pseudo effects, including CSSOM-only rules.
  const animations = document.getAnimations();
  if (animations.length > 1024) return null;
  for (const animation of animations) {
    const literal = JSON.stringify({keyframes:animation.effect?.getKeyframes(),
      timing:animation.effect?.getTiming()});
    values.push(literal); size += literal.length;
    if (size > 8388608) return null;
  }
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


class _PausedHandle:
    """Synchronous DevTools evaluation: promise continuations cannot run paused."""

    def __init__(self, session, object_id):
        self.session, self.object_id = session, object_id

    async def evaluate(self, expression):
        result = await self.session.send('Runtime.callFunctionOn', {
            'objectId': self.object_id,
            'functionDeclaration': 'function(){return (' + expression + ')(this)}',
            'returnByValue': True})
        return _runtime_value(result)

    async def dispose(self):
        await self.session.send('Runtime.releaseObject', {'objectId': self.object_id})


def _runtime_value(result):
    if result.get('exceptionDetails'):
        raise ValueError('Paused evidence evaluation failed')
    return result['result'].get('value')


class _PausedPage:
    """Original DOM/compositor access while application task queues are paused.

    Debugger suspension preserves queued one-shot timers and recursive RAF,
    unlike script execution disabling, which drops their callbacks. All
    evaluations here are synchronous and retain the original document objects.
    """

    def __init__(self, page, session):
        self.page, self.session = page, session
        self.prepared = None
        self.capture_top_frame = None
        self.mask_plan = None

    @property
    def url(self):
        return self.page.url

    async def evaluate(self, expression, arguments=None):
        arguments = dict(arguments) if isinstance(arguments, dict) else arguments
        prepared = arguments.pop('prepared', None) if isinstance(arguments, dict) else None
        if prepared is not None:
            result = await self.session.send('Runtime.callFunctionOn', {
                'objectId': prepared.object_id,
                'functionDeclaration': 'function(){return (' + expression + ')(Object.assign(' +
                    json.dumps(arguments) + ',{prepared:this}))}', 'returnByValue': True})
        else:
            result = await self.session.send('Runtime.evaluate', {
                'expression': '(' + expression + ')(' + json.dumps(arguments) + ')',
                'returnByValue': True})
        return _runtime_value(result)

    async def evaluate_handle(self, expression, arguments):
        result = await self.session.send('Runtime.evaluate', {
            'expression': '(' + expression + ')(' + json.dumps(arguments) + ')'})
        if result.get('exceptionDetails') or not result['result'].get('objectId'):
            raise ValueError('Paused evidence preparation failed')
        handle = _PausedHandle(self.session, result['result']['objectId'])
        self.prepared = handle
        return handle

    async def plan_masks(self, prepared, selectors):
        expression = 'prepared => (' + _MASK_PLAN + ')(prepared,' + json.dumps(selectors) + ')'
        self.mask_plan = await prepared.evaluate(expression)
        if type(self.mask_plan) is not dict:
            raise ValueError('Paused evidence mask preparation failed')

    async def screenshot(self, *, mask, **_):
        await self.plan_masks(self.prepared, mask)
        result = await self.session.send('Page.captureScreenshot', {
            'format': 'png', 'fromSurface': True, 'captureBeyondViewport': False})
        return base64.b64decode(result['data'], validate=True)

    async def mask_png(self, image):
        """Apply black rectangles off-DOM, using pristine isolated-world APIs.

        PNG bytes stay in memory. The application never receives the source PNG
        or a canvas node, and arbitrary authored styles cannot affect the mask.
        """
        top = (await self.session.send('Page.getFrameTree'))['frameTree']['frame']
        capture_top_frame = (top['id'], top['loaderId'], top['url'])
        if self.capture_top_frame is not None and capture_top_frame != self.capture_top_frame:
            raise ValueError('Viewport document changed')
        frame_id = top['id']
        world = await self.session.send('Page.createIsolatedWorld', {
            'frameId': frame_id, 'worldName': 'api-agent-evidence-raster'})
        arguments = {'png': base64.b64encode(image).decode('ascii'), 'plan': self.mask_plan}
        result = await self.session.send('Runtime.evaluate', {
            'contextId': world['executionContextId'], 'awaitPromise': True,
            'returnByValue': True,
            'expression': '(' + _MASK_PNG + ')(' + json.dumps(arguments) + ')'})
        masked = _runtime_value(result)
        if type(masked) is not str:
            raise ValueError('Viewport mask unavailable')
        top = (await self.session.send('Page.getFrameTree'))['frameTree']['frame']
        if (top['id'], top['loaderId'], top['url']) != capture_top_frame:
            raise ValueError('Viewport document changed')
        return base64.b64decode(masked, validate=True)


_MASK_PLAN = r'''(prepared, selectors) => {
  if (visualViewport && (visualViewport.scale !== 1 ||
      visualViewport.offsetLeft || visualViewport.offsetTop)) throw 'viewport';
  return {width:innerWidth,height:innerHeight,
    boxes:selectors.flatMap(selector => [...prepared.marked].filter(node =>
      node.matches(selector)).map(node => {
        if (prepared.masks.has(node)) return prepared.masks.get(node);
        const box = node.getBoundingClientRect();
        return [box.x,box.y,box.width,box.height];
      }))};
}'''


_MASK_PNG = r'''async ({png,plan}) => {
  const bytes = Uint8Array.from(atob(png), character => character.charCodeAt(0));
  const bitmap = await createImageBitmap(new Blob([bytes], {type:'image/png'}));
  try {
    const scaleX = bitmap.width / plan.width, scaleY = bitmap.height / plan.height;
    if (!(scaleX > 0) || !(scaleY > 0) || Math.abs(scaleX - scaleY) > .02)
      throw 'viewport_scale';
    const canvas = new OffscreenCanvas(bitmap.width, bitmap.height);
    const context = canvas.getContext('2d');
    context.drawImage(bitmap, 0, 0); context.fillStyle = '#000000';
    for (const [x,y,width,height] of plan.boxes) {
      if (![x,y,width,height].every(Number.isFinite) || width < 0 || height < 0)
        throw 'mask_geometry';
      if (!width || !height) continue;
      const left = Math.floor(x * scaleX), top = Math.floor(y * scaleY);
      const right = Math.ceil((x + width) * scaleX), bottom = Math.ceil((y + height) * scaleY);
      context.fillRect(left, top, right-left, bottom-top);
    }
    const blob = await canvas.convertToBlob({type:'image/png'});
    const output = new Uint8Array(await blob.arrayBuffer());
    let binary = '';
    for (let index=0; index<output.length; index+=32768)
      binary += String.fromCharCode(...output.subarray(index, index+32768));
    return btoa(binary);
  } finally { bitmap.close(); }
}'''


_FOCUSED_CONTENT = r'''selector => {
  let focused = document.activeElement;
  while (focused?.shadowRoot?.activeElement) focused = focused.shadowRoot.activeElement;
  const scopes = [document];
  let count = 0, found = false;
  while (scopes.length) {
    for (const node of scopes.pop().querySelectorAll('*')) {
      if (++count > 10000) throw 'node_budget';
      if (node.shadowRoot) scopes.push(node.shadowRoot);
      if (!node.matches(selector)) continue;
      for (let candidate = focused; candidate;
          candidate = candidate.assignedSlot || candidate.parentElement || candidate.getRootNode().host || null) {
        if (candidate === node) found = true;
      }
    }
  }
  return found;
}'''


class _CaptureSession:
    """One serialized native capture session, owned by the Python Page lifetime."""

    def __init__(self):
        self.lock = asyncio.Lock()
        self.cdp = None

    async def close(self, *_):
        if self.cdp is not None:
            cdp, self.cdp = self.cdp, None
            try:
                await cdp.detach()
            except Exception:
                pass  # Page/context teardown may already have closed the session.


def _serialized_capture(function):
    @wraps(function)
    async def capture(page, *args, **kwargs):
        state = getattr(page, '_api_agent_evidence_session', None)
        if state is None:
            state = _CaptureSession()
            try:
                setattr(page, '_api_agent_evidence_session', state)
            except (AttributeError, TypeError):
                # Policy/contract validation accepts browser-free sentinels.
                return await function(page, *args, **kwargs)
            if callable(getattr(page, 'once', None)):
                page.once('close', state.close)
        async with state.lock:
            return await function(page, *args, **kwargs)
    return capture


CAPTURE_ATTEMPTS = 3
CAPTURE_RETRY_DELAY_SECONDS = 1.0
# Default whole-capture window; enclosing deadlines such as capture_ui_evidence's
# page budget clip it.
CAPTURE_RETRY_WINDOW_SECONDS = 8.0
# Page instability that a later, fully restored attempt can outlast.
_TRANSIENT_REASONS = frozenset({'content_layout', 'paint_changed', 'document_changed',
    'content_mask_geometry', 'frame_changed', 'pause_lost'})
_SHIFT_KINDS = frozenset({'opaque', 'content', 'effect', 'none'})
_STRUCTURAL_NAME = re.compile(r'[A-Za-z0-9_-]+')


def _layout_shift(value, boundary):
    """Admit only the bounded structural description a browser reports."""
    def text(item, limit):
        return item if (type(item) is str and len(item) <= limit and
                        _STRUCTURAL_NAME.fullmatch(item)) else None
    def box(item):
        if type(item) is not dict or set(item) != {'x', 'y', 'width', 'height'}:
            return None
        if any(type(number) not in (int, float) or not math.isfinite(number) for number in item.values()):
            return None
        return {key: float(number) for key, number in item.items()}
    if type(value) is not dict or text(value.get('tag'), 32) is None:
        return None
    shifted = value.get('shifted')
    before, after = box(value.get('before')), box(value.get('after'))
    if (value.get('kind') not in _SHIFT_KINDS or type(shifted) is not int or shifted < 1
            or before is None or after is None):
        return None
    testid = text(value.get('testid'), 64)
    if testid is not None and boundary.content_export({'text': testid}).get('text') != testid:
        testid = None
    return {'tag': value['tag'], 'testid': testid, 'kind': value['kind'],
            'shifted': shifted, 'before': before, 'after': after}


def _final_refusal(failures):
    """An attempt's deciding refusal: any deterministic one, else the newest."""
    return next((failure for failure in failures if failure[0] not in _TRANSIENT_REASONS),
                failures[-1] if failures else ('unavailable', None))


@_serialized_capture
async def capture_studio_viewport(page, boundary, origin, *, unavailable=None, failure_detail=None,
                                  retry_window_seconds=CAPTURE_RETRY_WINDOW_SECONDS, **options):
    """Capture once, redoing the whole capture after transient page instability.

    Each attempt restores the page before returning, so later turns and retries
    see the original document. Up to ``CAPTURE_ATTEMPTS`` run with
    ``CAPTURE_RETRY_DELAY_SECONDS`` between them. An attempt recording any
    deterministic refusal, a layout refusal naming the same moved element twice,
    and a retry projected (from the slowest attempt so far, including the delay)
    to end after ``retry_window_seconds`` stop at once. The window is a timeout
    scope, so enclosing deadlines clip it. An attempt that recorded a refusal
    never returns pixels. Only the final refusal reaches ``unavailable(reason)``,
    and ``failure_detail`` then receives the reason, the completed attempt count
    and, for a layout refusal, the structural description of the element that
    moved. A cancellation or error after a refusal still reports that refusal
    first. Options are those of ``_capture_viewport_once``.
    """
    if (type(retry_window_seconds) not in (int, float) or not math.isfinite(retry_window_seconds)
            or retry_window_seconds <= 0):
        raise ValueError('Invalid capture retry window')
    # The retry window bounds admission of another attempt. Masking the last
    # capture has its own timeout and must retain the caller's deadline even
    # when that retry window has elapsed.
    async with report_timeout('UI viewport evidence', None) as caller_budget:
        async with report_timeout('UI viewport capture', retry_window_seconds) as budget:
            return await _capture_with_retries(page, boundary, origin, budget.remaining,
                                               unavailable, failure_detail, options, caller_budget)


async def _capture_with_retries(page, boundary, origin, window, unavailable, failure_detail, options, caller_budget):
    stage = options.get('stage')
    started = monotonic()
    attempts, reason, shift, longest, failures = 0, None, None, 0.0, []

    def report(interrupted=False):
        if unavailable is not None:
            unavailable(reason)
        if failure_detail is not None:
            failure_detail({'reason': reason, 'attempts': attempts, 'layout_shift': shift,
                            'interrupted': interrupted})

    try:
        while True:
            if attempts and stage is not None:
                stage('retry', int((monotonic() - started) * 1000))
            began = monotonic()
            failures = []
            image = await _capture_viewport_once(page, boundary, origin,
                unavailable=lambda refused, moved: failures.append((refused, moved)),
                caller_budget=caller_budget, **options)
            if image is not None and not failures:
                return image
            attempts += 1
            previous = shift
            reason, shift = _final_refusal(failures)
            now = monotonic()
            longest = max(longest, now - began)
            if (reason not in _TRANSIENT_REASONS or attempts >= CAPTURE_ATTEMPTS
                    or (reason == 'content_layout' and shift is not None and shift == previous)
                    or now - started + CAPTURE_RETRY_DELAY_SECONDS + longest > window):
                break
            failures = []
            await asyncio.sleep(CAPTURE_RETRY_DELAY_SECONDS)
    except BaseException:
        if failures:
            # A refusal the interrupted attempt already recorded is the newest evidence.
            attempts += 1
            reason, shift = _final_refusal(failures)
        if reason is not None:
            try:
                report(interrupted=True)
            except Exception:
                pass  # A failing diagnostic callback must not replace the interruption.
        raise
    report()
    return None


async def _capture_viewport_once(page, boundary, origin, *, unavailable, path='/', allow_frames=False,
                                 owned_frames=None, owned_shadow_selector=None, owned_chrome=None, content_mask_selector=None,
                                 content_mask_anchor=None,
                                 content_mask_requirements=(), stage=None, caller_budget):
    """Return masked original PNG, or None when owned safety is unavailable.

    Authentication controls and unsupported rendering surfaces are hidden before
    compositor capture. Captures apply black rectangle masks to the
    transient PNG in an isolated, off-DOM raster before returning any image. When content is withheld, the selected provider supplies
    owned conversation selectors and an anchor; their painted regions are clipped
    and masked while surrounding controls remain visible. Top-layer descendants
    are rejected because they can paint outside the clipped region. With content enabled,
    ordinary form values and exact authored navigation SVGs remain visible.
    An explicit ``owned_frames`` contract admits original
    application frame pixels after frame identity and literal credential checks.
    Declared content masks also use debugger-paused capture: opacity and clipping
    preserve layout and focus while application callbacks wait. Unpaused surfaces
    retain strong subtree suppression, including transient top-layer painting.
    Native animation time is leased without losing callbacks.
    Browser strings remain transient and are never attachment metadata, except
    a moved element's bounded tag and test id, which the live credential
    boundary screens. The caller owns exception isolation.
    """
    def reject(reason, shift=None):
        unavailable(reason, _layout_shift(shift, boundary))
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
    if owned_chrome is not None:
        if not _valid_owned_chrome(owned_chrome) or owned_shadow_selector is not None:
            return reject('shadow_contract')
        owned_shadow_selector = owned_chrome.selector
    if owned_shadow_selector is not None and (type(owned_shadow_selector) is not str or
            not owned_shadow_selector or len(owned_shadow_selector) > 256):
        return reject('shadow_contract')
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
    state = page._api_agent_evidence_session
    try:
        if state.cdp is None:
            async def create_session():
                state.cdp = await page.context.new_cdp_session(page)
            await finish_cleanup(create_session())
        cdp = state.cdp
    except Exception:
        return reject('shadow_audit')
    raster = _PausedPage(page, cdp)
    marker, token = 'data-evidence-' + uuid4().hex, uuid4().hex
    prepared = None
    result = None
    renderer_records = None
    renderer_literals = None
    debugger_enabled = False
    pause_trigger = None
    pause_listener = None
    resume_listener = None
    pause_lost = False
    paused_capture = False
    own_pause = False
    pause_expression = 'debugger;\n//# sourceURL=api-agent-evidence-pause-' + uuid4().hex
    timeline_rate = None
    timeline_owned = False
    try:
        try:
            timeline_rate = (await cdp.send('Animation.getPlaybackRate'))['playbackRate']
            if type(timeline_rate) not in (int, float) or not math.isfinite(timeline_rate):
                return reject('animation_timeline')
            if timeline_rate != 0:
                timeline_owned = True
                await cdp.send('Animation.setPlaybackRate', {'playbackRate': 0})
        except Exception:
            return reject('animation_timeline')
        needs_pause = owned_frames is not None or (
            boundary.policy.allow_content is not True and bool(content_mask_selector))
        if boundary.policy.allow_content is not True and owned_frames is None:
            try:
                top = (await cdp.send('Page.getFrameTree'))['frameTree']['frame']
                world = await cdp.send('Page.createIsolatedWorld', {
                    'frameId':top['id'], 'worldName':'api-agent-evidence-focus'})
                focused_content = _runtime_value(await cdp.send('Runtime.evaluate', {
                    'expression':'(' + _FOCUSED_CONTENT + ')(' + json.dumps(content_mask_selector) + ')',
                    'contextId':world['executionContextId'], 'returnByValue':True}))
                if type(focused_content) is not bool:
                    return reject('focus_inspection')
                needs_pause = needs_pause or focused_content
            except Exception:
                return reject('focus_inspection')
        if needs_pause:
            async def acquire_pause():
                nonlocal debugger_enabled, pause_listener, resume_listener, pause_trigger, own_pause
                debugger_enabled = True
                paused = asyncio.get_running_loop().create_future()
                def pause_listener(event):
                    paused.set_result(event)
                cdp.once('Debugger.paused', pause_listener)
                await cdp.send('Debugger.enable')
                if paused.done():
                    return False
                def resume_listener(_):
                    nonlocal pause_lost
                    pause_lost = True
                cdp.once('Debugger.resumed', resume_listener)
                pause_trigger = asyncio.create_task(cdp.send('Runtime.evaluate', {
                    'expression': pause_expression}))
                completed, _ = await asyncio.wait(
                    (paused, pause_trigger), return_when=asyncio.FIRST_COMPLETED)
                if pause_trigger in completed:
                    await pause_trigger
                    return False
                packet = paused.result()
                source = await cdp.send('Debugger.getScriptSource', {
                    'scriptId':packet['callFrames'][0]['location']['scriptId']})
                own_pause = source['scriptSource'] == pause_expression
                return own_pause
            try:
                acquired = await finish_cleanup(acquire_pause())
                if not acquired:
                    return reject('frame_inspection')
                if pause_lost or pause_trigger.done():
                    return reject('pause_lost')
                page = raster
                paused_capture = True
            except Exception:
                return reject('frame_inspection')
        if owned_frames is not None:
            try:
                admission = await _owned_application_frames(
                    cdp, origin, owned_frames, include_top=True)
                mark('frame_admitted')
                if admission is None:
                    return reject('frame')
                renderer_records, page.capture_top_frame = admission
                renderer_literals = await _renderer_literals(cdp, renderer_records, boundary)
                mark('frame_literals')
                if renderer_literals is None:
                    return reject('frame_content')
            except Exception:
                return reject('frame_inspection')
        try:
            if await _closed_author_shadow(cdp, owned_shadow_selector):
                return reject('closed_shadow')
            mark('parent_audit')
        except _ShadowDocumentChanged:
            return reject('document_changed')
        except Exception:
            return reject('shadow_audit')
        async def prepare_capture():
            nonlocal prepared
            prepared = await page.evaluate_handle(_PREPARE, {
            'trustedIcons': _TRUSTED_ICONS,
            'preserveContentFocus': paused_capture,
            'ownedFrameSelector': owned_frames.selector if owned_frames else None,
            'ownedShadowSelector': owned_shadow_selector,
            'chromeOwned': owned_chrome is not None,
            'chromeConfig': {'shadowSelectors': owned_chrome.shadow_selectors,
                'svgSelector': owned_chrome.svg_selector, 'imageSelector': owned_chrome.image_selector,
                'imageSources': owned_chrome.image_sources,
                'imageSourceProperties': owned_chrome.image_source_properties} if owned_chrome is not None else None,
            'contentMaskSelector': content_mask_selector if boundary.policy.allow_content is not True else None})
        await finish_cleanup(prepare_capture())
        reason = await prepared.evaluate('state => state.reason')
        mark('prepared')
        if reason:
            return reject(reason)
        rows = await page.evaluate(_INSPECT, {'marker': marker, 'token': token,
            'trustedIcons': _TRUSTED_ICONS, 'allowFrames': allow_frames,
            'ownedFrameSelector': owned_frames.selector if owned_frames else None,
            'ownedShadowSelector': owned_shadow_selector,
            'chromeOwned': owned_chrome is not None,
            'chromeConfig': {'shadowSelectors': owned_chrome.shadow_selectors,
                'svgSelector': owned_chrome.svg_selector, 'imageSelector': owned_chrome.image_selector,
                'imageSources': owned_chrome.image_sources,
                'imageSourceProperties': owned_chrome.image_source_properties} if owned_chrome is not None else None,
            'contentMaskSelector': content_mask_selector if boundary.policy.allow_content is not True else None,
            'contentMaskAnchor': content_mask_anchor if boundary.policy.allow_content is not True else None,
            'contentMaskRequirements': content_mask_requirements if boundary.policy.allow_content is not True else (),
            'diagnose': True, 'prepared': prepared})
        if isinstance(rows, dict) and rows.get('unavailable'):
            return reject(rows['unavailable'], rows.get('shift'))
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
        masked = {row['index'] for row in rows if row.get('contentMasked', False) or
                  (row['forced'] and not inside_content_mask(row))}
        selectors = [f'[{marker}-node="{index}"]' for index in sorted(masked)]
        if not paused_capture and selectors:
            await raster.plan_masks(prepared, selectors)
        if owned_frames is None and selectors:
            top = (await cdp.send('Page.getFrameTree'))['frameTree']['frame']
            raster.capture_top_frame = (top['id'], top['loaderId'], top['url'])
        image = await page.screenshot(type='png', full_page=False,
                                      mask=selectors if paused_capture else [],
                                      mask_color='#000000',
                                      animations='allow', caret='initial')
        mark('screenshot')
        after = await page.evaluate(_INSPECT, {'marker': marker, 'token': token,
            'verify': True, 'trustedIcons': _TRUSTED_ICONS, 'allowFrames': allow_frames,
            'ownedFrameSelector': owned_frames.selector if owned_frames else None,
            'ownedShadowSelector': owned_shadow_selector,
            'chromeOwned': owned_chrome is not None,
            'chromeConfig': {'shadowSelectors': owned_chrome.shadow_selectors,
                'svgSelector': owned_chrome.svg_selector, 'imageSelector': owned_chrome.image_selector,
                'imageSources': owned_chrome.image_sources,
                'imageSourceProperties': owned_chrome.image_source_properties} if owned_chrome is not None else None,
            'contentMaskSelector': content_mask_selector if boundary.policy.allow_content is not True else None,
            'contentMaskAnchor': content_mask_anchor if boundary.policy.allow_content is not True else None,
            'contentMaskRequirements': content_mask_requirements if boundary.policy.allow_content is not True else (),
            'diagnose': True, 'prepared': prepared})
        mark('verified')
        try:
            closed_shadow = await _closed_author_shadow(cdp, owned_shadow_selector)
        except _ShadowDocumentChanged:
            return reject('document_changed')
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
        stable = after == rows
        if boundary.policy.allow_content is not True and isinstance(after, list):
            stable = _outside_mask_snapshot(after) == _outside_mask_snapshot(rows)
        result = image if stable and page.url == origin.rstrip('/') + path else None
        if result is None:
            reject(after.get('unavailable', 'document_changed') if isinstance(after, dict)
                   else 'document_changed', after.get('shift') if isinstance(after, dict) else None)
    finally:
        async def restore_capture():
            nonlocal result, resume_listener
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
                        try:
                            try:
                                if debugger_enabled:
                                    try:
                                        if result is not None and paused_capture and (pause_lost or pause_trigger.done()):
                                            result = reject('pause_lost')
                                        if resume_listener is not None:
                                            cdp.remove_listener('Debugger.resumed', resume_listener)
                                            resume_listener = None
                                        # Only our exact private-script pause may
                                        # be resumed; preserve replacement pauses.
                                        if own_pause and not pause_lost and not pause_trigger.done():
                                            await cdp.send('Debugger.resume')
                                    finally:
                                        await cdp.send('Debugger.disable')
                            finally:
                                if pause_trigger is not None:
                                    # Cancel only our protocol-response waiter.
                                    if not pause_trigger.done():
                                        pause_trigger.cancel()
                                    await asyncio.gather(pause_trigger, return_exceptions=True)
                        finally:
                            if timeline_owned:
                                current_rate = (await cdp.send('Animation.getPlaybackRate'))['playbackRate']
                                if current_rate == 0:
                                    await cdp.send('Animation.setPlaybackRate', {'playbackRate': timeline_rate})
                    finally:
                        if pause_listener is not None:
                            cdp.remove_listener('Debugger.paused', pause_listener)
                        if resume_listener is not None:
                            cdp.remove_listener('Debugger.resumed', resume_listener)
                        # Animation domain teardown resets the native global timeline
                        # to 1, even after a getter-only session. Retain this single
                        # Page-owned session so prior/newer rates survive capture.
                        mark('cleanup')
        await finish_cleanup(restore_capture())
        if result is not None and raster.mask_plan is not None:
            try:
                with bind_timeout_budget(caller_budget):
                    async with component_timeout('Viewport image masking', 5):
                        result = await raster.mask_png(result)
            except Exception:
                result = reject('mask')

    # Marker cleanup above awaits the browser. A newly registered credential
    # must still invalidate already captured pixels before they leave this call.
    if owned_frames is not None and any(
            boundary.content_export({'text': literal}).get('text') != literal or
            boundary.content_export({'text': unquote(literal)}).get('text') != unquote(literal)
            for literal in renderer_literals):
        return reject('registered_secret')
    return result if not _sensitive(rows, boundary,
        exclude_masked_subtrees=boundary.policy.allow_content is not True) else reject('registered_secret')


def _outside_mask_snapshot(rows):
    """Retain exact identity and content outside isolated conversation subtrees."""
    snapshot = []
    for row in rows:
        parent = row['parent']
        hidden = False
        while parent >= 0:
            if rows[parent]['contentMasked']:
                hidden = True
                break
            parent = rows[parent]['parent']
        if not hidden:
            snapshot.append((row['id'], rows[row['parent']]['id'] if row['parent'] >= 0 else -1,
                             row['forced'], row['contentMasked'], row['texts']))
    return snapshot


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
