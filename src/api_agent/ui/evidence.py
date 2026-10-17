"""Original viewport capture for the owned, authored Studio document only.

This policy relies on Studio's controlled scripts and styles. It is not a safe
capture policy for arbitrary websites or provider-authentication pages.
"""
from urllib.parse import urlsplit
from uuid import uuid4


_INSPECT = r'''({marker, token, verify}) => {
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
                   ...[...node.attributes].map(attr => attr.value).filter(Boolean)];
    characters += texts.reduce((sum, text) => sum + text.length, 0);
    if (characters > 1048576) return null;
    rows.push({index, parent: nodes.indexOf(node.parentElement),
      forced: node.matches('input,textarea,select,[contenteditable],img,picture,canvas,iframe,frame,object,embed,video,audio,svg') || !!node.shadowRoot || node.localName.includes('-'),
      texts});
  }
  return rows;
}'''


async def capture_studio_viewport(page, boundary, origin):
    """Return masked original PNG, or None when owned safety is unavailable.

    Form values and unsupported rendering surfaces are always masked. Live
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
    rows = await page.evaluate(_INSPECT, {'marker': marker, 'token': token})
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
        after = await page.evaluate(_INSPECT, {'marker': marker, 'token': token, 'verify': True})
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
