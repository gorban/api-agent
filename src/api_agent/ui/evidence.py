"""Original viewport capture for the owned, authored Studio document only.

This policy relies on Studio's controlled scripts and styles. It is not a safe
capture policy for arbitrary websites or provider-authentication pages.
"""
from urllib.parse import urlsplit
from uuid import uuid4


_TRUSTED_ICONS = ['<svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true" focusable="false"><path d="M12 5v14M5 12h14"/></svg>', '<svg viewBox="0 0 24 24" width="23" height="23" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><path d="m9.5 3-.7 2.2-2 .9-2.1-.5-2.5 4.3 1.5 1.7v2.3l-1.5 1.7 2.5 4.3 2.1-.5 2 .9.7 2.2h5l.7-2.2 2-.9 2.1.5 2.5-4.3-1.5-1.7v-2.3l1.5-1.7-2.5-4.3-2.1.5-2-.9-.7-2.2z" transform="translate(0 -1) scale(1 .96)"/><circle cx="12" cy="12" r="3.3"/></svg>']


_INSPECT = r'''({marker, token, verify, trustedIcons}) => {
  const icons = trustedIcons.map(html => new DOMParser().parseFromString(html, 'text/html').body.firstElementChild.outerHTML);
  const authentication = node => node.matches('input,textarea,select,[contenteditable]') && (
    /password|credential|secret|authorization|authentication|login|username|one-time-code|otp|passkey|cc-number|cc-csc|(?:api|access|auth|refresh|bearer)[\s_-]*(?:key|token)/i.test(
      [node.type, node.id, node.name, node.autocomplete, node.getAttribute('aria-label'), node.getAttribute('placeholder'),
       ...[...(node.labels || [])].map(label => label.textContent),
       ...(node.getAttribute('aria-labelledby') || '').split(/\s+/).map(id => document.getElementById(id)?.textContent)].join(' ')) ||
    /^(?:token|key)$/i.test(node.id) || /^(?:token|key)$/i.test(node.name));
  if (window.top !== window || document.querySelector('iframe,frame')) return null;
  if (document.querySelector('#send:disabled, .message.draft')) return null;
  if (verify && document.documentElement.getAttribute(marker) !== token) return null;
  document.documentElement.setAttribute(marker, token);
  const found = document.querySelectorAll('*');
  if (found.length > 10000) return null;
  const nodes = [...found], rows = [];
  let characters = 0;
  for (const [index, node] of nodes.entries()) {
    node.setAttribute(marker + '-node', String(index));
    const style = getComputedStyle(node);
    const before = getComputedStyle(node, '::before').content;
    const after = getComputedStyle(node, '::after').content;
    const texts = [node.textContent || '', node.innerText || '', before, after,
                   ...(typeof node.value === 'string' ? [node.value] : []),
                   ...[...node.attributes].map(attr => attr.value).filter(Boolean)];
    characters += texts.reduce((sum, text) => sum + text.length, 0);
    if (characters > 1048576) return null;
    rows.push({index, parent: nodes.indexOf(node.parentElement),
      forced: authentication(node) || node.matches('img,picture,canvas,iframe,frame,object,embed,video,audio') || (node.localName === 'svg' && !icons.includes(node.outerHTML.replace(/ data-evidence-[^= ]+="[^"]*"/g, ''))) || !!node.shadowRoot || node.localName.includes('-'),
      texts});
  }
  const stack = [document.documentElement], visible = [];
  let visited = 0;
  while (stack.length) {
    if (++visited > 20000) return null;
    const node = stack.pop();
    if (node.nodeType === Node.TEXT_NODE) { visible.push(node.nodeValue || ''); continue; }
    if (node.nodeType !== Node.ELEMENT_NODE) continue;
    const row = rows[Number(node.getAttribute(marker + '-node'))];
    if (row.forced) continue;
    if (node.matches('input,textarea,select')) { visible.push(node.value || ''); continue; }
    if (node.childNodes.length + visited > 20000) return null;
    stack.push(...[...node.childNodes].reverse());
  }
  const rendered = visible.join('');
  if (characters + rendered.length > 1048576) return null;
  rows[0].texts.push(rendered);
  return rows;
}'''


async def capture_studio_viewport(page, boundary, origin):
    """Return masked original PNG, or None when owned safety is unavailable.

    Authentication controls and unsupported rendering surfaces are masked.
    Ordinary form values and the exact authored navigation SVGs remain visible. Live
    boundary comparisons find registered secrets and credential-shaped text,
    including strings split across child nodes. Browser strings remain transient
    and are never attachment metadata. The caller owns exception isolation.
    """
    if boundary.policy.allow_content is not True or not origin:
        return None
    expected = urlsplit(origin)
    actual = urlsplit(page.url)
    if (actual.scheme, actual.netloc, actual.path, actual.query, actual.fragment) != (
            expected.scheme, expected.netloc, '/', '', ''):
        return None
    if len(page.frames) != 1:
        return None
    marker, token = 'data-evidence-' + uuid4().hex, uuid4().hex
    rows = await page.evaluate(_INSPECT, {'marker': marker, 'token': token, 'trustedIcons': _TRUSTED_ICONS})
    try:
        if rows is None:
            return None
        if len(rows) > 10000:
            return None
        # A registered credential in ordinary page content is not safely
        # localized by ancestor pruning: split/repeated secrets can overlap.
        # Keep original pixels unavailable rather than guess at their spans.
        if _sensitive(rows, boundary):
            return None
        masked = {row['index'] for row in rows if row['forced']}
        locators = [page.locator(f'[{marker}-node="{index}"]') for index in sorted(masked)]
        image = await page.screenshot(type='png', full_page=False, mask=locators,
                                      mask_color='#000000', animations='disabled')
        after = await page.evaluate(_INSPECT, {'marker': marker, 'token': token, 'verify': True, 'trustedIcons': _TRUSTED_ICONS})
        result = image if after == rows and page.url == origin.rstrip('/') + '/' else None
    finally:
        await page.evaluate('''marker => {
          for (const node of document.querySelectorAll('[' + marker + '-node]'))
            node.removeAttribute(marker + '-node');
          document.documentElement.removeAttribute(marker);
        }''', marker)

    # Marker cleanup above awaits the browser. A newly registered credential
    # must still invalidate already captured pixels before they leave this call.
    return result if not _sensitive(rows, boundary) else None


def _sensitive(rows, boundary):
    return any(boundary.content_export({'text': text}).get('text') != text
               for row in rows if not row['forced'] for text in row['texts'])
