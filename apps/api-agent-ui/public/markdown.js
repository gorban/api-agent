import {Marked} from './marked.js';
import DOMPurify from './dompurify.js';

const parser = new Marked({gfm: true, breaks: false, async: false});
const tags = ['p','br','hr','h1','h2','h3','h4','h5','h6','strong','b','em','i','del','s','blockquote','ul','ol','li','pre','code','a','img','table','thead','tbody','tr','th','td','input','kbd','sub','sup','mark','details','summary'];
const safeURL = value => { try { return new URL(value, location.href); } catch { return null; } };
const attributes = ['href','src','alt','title','class','start','align','type','checked','disabled'];

// Parse and sanitize off-document before inserting any response-provided content.
export function renderMarkdown(node, source) {
  try { renderRichMarkdown(node, source); }
  catch {
    // Parser limits must not disable the conversation or execute raw markup.
    node.classList.add('markdown');
    node.textContent = source;
  }
}
function renderRichMarkdown(node, source) {
  const fragment = DOMPurify.sanitize(parser.parse(source), {
    ALLOWED_TAGS: tags, ALLOWED_ATTR: attributes,
    ALLOW_DATA_ATTR: false, ALLOW_ARIA_ATTR: false, RETURN_DOM_FRAGMENT: true
  });
  for (const element of fragment.querySelectorAll('[class]')) {
    const language = element.tagName === 'CODE' && /^language-[\w-]+$/.test(element.className);
    if (!language) element.removeAttribute('class');
  }
  for (const link of fragment.querySelectorAll('a[href]')) {
    const url = safeURL(link.getAttribute('href'));
    if (!url || !['http:', 'https:', 'mailto:'].includes(url.protocol)) link.removeAttribute('href');
    else if (url.origin !== location.origin) { link.target = '_blank'; link.rel = 'noopener noreferrer'; }
  }
  for (const img of fragment.querySelectorAll('img')) {
    const src = img.getAttribute('src') || '';
    const url = safeURL(src);
    // Keep response images within this app; external images remain explicit links.
    if (!url || url.origin !== location.origin || !['http:', 'https:'].includes(url.protocol)) {
      const label = document.createElement('a'); label.textContent = img.alt || 'View image';
      if (url && ['http:', 'https:'].includes(url.protocol)) { label.href = url.href; label.target = '_blank'; label.rel = 'noopener noreferrer'; }
      img.replaceWith(label);
    } else { img.loading = 'lazy'; img.referrerPolicy = 'no-referrer'; }
  }
  for (const input of fragment.querySelectorAll('input')) {
    if (input.type !== 'checkbox') { input.remove(); continue; }
    input.disabled = true;
    input.setAttribute('aria-label', input.closest('li')?.textContent.trim() || 'Task status');
  }
  for (const table of fragment.querySelectorAll('table')) {
    const wrapper = document.createElement('div'); wrapper.className = 'markdown-table';
    wrapper.tabIndex = 0; wrapper.setAttribute('role', 'region'); wrapper.setAttribute('aria-label', 'Response table');
    table.replaceWith(wrapper); wrapper.append(table);
  }
  for (const pre of fragment.querySelectorAll('pre')) {
    pre.tabIndex = 0; pre.setAttribute('role', 'region'); pre.setAttribute('aria-label', 'Code sample');
  }
  node.classList.add('markdown');
  node.replaceChildren(fragment);
}
