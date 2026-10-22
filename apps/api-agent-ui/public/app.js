import {renderMarkdown} from './markdown.js';
import {requestPayload, readResponse, readJSONResponse, supportsContinuation, submitContinuation} from './transport.js';
import {normalizeCatalog, resolveSelection, modelGroups, reasoningEfforts} from './catalog.js';
import {beginReply, takeReply, customAnswer} from './reply.js';
const $ = id => document.getElementById(id);
const REQUEST_TIMEOUT_MS = 150000;
const KEY = 'api-agent-studio-v2';
const defaults = () => ({model: '', version: '', reasoning: ''});
const fresh = () => ({session: crypto.randomUUID(), messages: [], draft: '', reply: null, settings: defaults()});
let state = fresh(), active = null, catalog = [], catalogStatus = '', apiOrigin, apiHeaders = Object.freeze({}), settingsOpener;
const field = value => typeof value === 'string' && value.length < 1000;
const questionValid = q => !q || (field(q.id) && typeof q.question === 'string' && ['clarification', 'approval'].includes(q.kind) && Array.isArray(q.options) && q.options.every(x => q.kind === 'clarification' ? typeof x === 'string' : x && field(x.id) && typeof x.label === 'string'));
const settingsValid = x => x && ['model', 'version', 'reasoning'].every(k => field(x[k])) && (x.family === undefined || (field(x.family) && [...x.family].length <= 200 && !/[\p{C}\p{Z}]/u.test(x.family.replaceAll(' ','')) && !(x.family && x.model)));
const snapshotValid = x => settingsValid(x) && !!x.model;
const valid = x => x && field(x.session) && typeof x.draft === 'string' && settingsValid(x.settings) && Array.isArray(x.messages) && x.messages.every(m => m && field(m.id) && ['user', 'assistant'].includes(m.role) && typeof m.text === 'string' && settingsValid(m.requested) && (!m.selectionSnapshot || snapshotValid(m.selectionSnapshot)) && (!m.requested.family || !!m.selectionSnapshot) && questionValid(m.question));
function storageFailure() { $('storage-note').textContent = 'Not saved in this browser'; $('storage-warning').textContent = 'Browser storage is unavailable. Your conversation is kept until this page closes.'; $('storage-warning').hidden = false; }
try {
  const raw = localStorage.getItem(KEY);
  if (raw) {
    const loaded = JSON.parse(raw);
    if (!valid(loaded)) throw Error('invalid');
    state = loaded;
    for (const m of state.messages) if (m.pending) { m.pending = false; m.error = 'The response was interrupted when this page closed. You can retry.'; }
  }
} catch { storageFailure(); }
if (state.reply && (!state.messages.some(m => m.id === state.reply.messageId && m.question && !m.resolved) || typeof state.reply.savedDraft !== 'string')) state.reply = null;
let previousChat = null;
try { const raw = localStorage.getItem(KEY + ':previous'); if (raw) { const prior = JSON.parse(raw); if (valid(prior) && !state.messages.length) previousChat = prior; } } catch {}
function savePrevious() { try { if (previousChat) localStorage.setItem(KEY + ':previous', JSON.stringify(previousChat)); else localStorage.removeItem(KEY + ':previous'); } catch {} }
function save() { try { localStorage.setItem(KEY, JSON.stringify(state)); } catch { storageFailure(); } }
function announce(text) { $('status').textContent = text; }
function el(tag, cls, text) { const n = document.createElement(tag); if (cls) n.className = cls; if (text !== undefined) n.textContent = text; return n; }
function clearPromptError() { $('prompt-error').hidden = true; $('prompt-error').textContent = ''; $('prompt').removeAttribute('aria-invalid'); }
function rejectPrompt(text) { $('prompt-error').textContent = text; $('prompt-error').hidden = false; $('prompt').setAttribute('aria-invalid', 'true'); announce(text); focusComposer(); }
function focusComposer() { $('prompt').focus({preventScroll: true}); }
function scrollLatest() { const c = $('conversation-scroll'); c.scrollTop = c.scrollHeight; }
function follow() { if ($('follow-latest').checked) scrollLatest(); }
function responseDetails(m) {
  const details = el('details', 'response-metadata');
  details.append(el('summary', '', 'Response metadata'));
  const grid = el('dl', 'metadata-grid');
  const display = (value, fallback) => typeof value === 'string' && value ? value : fallback;
  for (const [key, value] of [
    ['Requested model', display(m.requested.family || m.requested.model, 'Provider default')],
    ['Resolved catalog model', display(m.selectionSnapshot?.model || m.requested.model, 'Not recorded')],
    ['Reported model', display(m.actual?.actual_model || m.actual?.model, 'Not reported')],
    ['Requested version', display(m.requested.version, 'Provider default')],
    ['Reported version', display(m.actual?.version, 'Not reported')],
    ['Requested reasoning', display(m.requested.reasoning, 'Provider default')],
    ['Reported reasoning', display(m.actual?.reasoning, 'Not reported')],
    ['Reported serving revision', display(m.actual?.serving_revision, 'Not reported')],
    ['Reported service tier', display(m.actual?.service_tier, 'Not reported')],
    ['Status', m.pending ? 'Waiting for response' : m.error ? 'Incomplete' : 'Complete']
  ]) { const row = el('div'); row.append(el('dt', '', key), el('dd', '', value)); grid.append(row); }
  details.append(grid); return details;
}
function questionControls(m) {
  const box = el('fieldset', 'response-question');
  box.append(el('legend', '', m.question.question));
  for (const option of m.question.options) {
    const label = typeof option === 'string' ? option : option.label;
    const button = el('button', 'suggestion', label); button.type = 'button';
    button.addEventListener('click', () => {
      if (active) return;
      if (m.question.kind === 'approval') submitReply(label, option.id);
      else { state.draft = label; $('prompt').value = label; save(); focusComposer(); }
    });
    box.append(button);
  }
  if (m.question.kind === 'clarification') {
    const custom = el('button', 'suggestion', 'Choose your own answer below…'); custom.type = 'button';
    custom.addEventListener('click', () => {
      if (active) return;
      state.draft = customAnswer($('prompt').value, m.question.options);
      $('prompt').value = state.draft; save(); focusComposer();
    });
    box.append(custom);
  }
  return box;
}
function submitReply(text, optionId) {
  if (!supportsContinuation) {
    announce('This backend does not support question continuations. Your answer and earlier draft are preserved.');
    return;
  }
  const message = state.messages.find(m => m.id === state.reply?.messageId);
  if (!message || !resolveSelection(catalog, message.selectionSnapshot || message.requested).valid) { announce('The question’s model selection is no longer available.'); return; }
  const reply = takeReply(state, text, optionId);
  if (!reply) { announce('Choose an explicit approval option.'); return; }
  $('prompt').value = state.draft;
  run(text, {...message.requested}, null, reply);
}
function render() {
  $('reply-context').hidden = !state.reply;
  $('reply-context').textContent = state.reply ? 'Answering api-agent’s question. Your earlier draft will return after you send.' : '';
  const c = $('conversation'), focused = document.activeElement;
  const focusedId = focused.closest?.('.message')?.dataset.messageId;
  const wasSummary = focused.tagName === 'SUMMARY';
  const old = new Map([...c.children].map(n => [n.dataset.messageId, n]));
  $('undo-new-chat').hidden = !previousChat || !!state.messages.length;
  $('welcome').hidden = !!state.messages.length;
  $('main').classList.toggle('has-messages', !!state.messages.length);
  $('follow-controls').hidden = !state.messages.length;
  const nodes = state.messages.map(m => {
    const signature = JSON.stringify(m), previous = old.get(m.id);
    if (previous?.dataset.signature === signature) return previous;
    const article = el('article', `message ${m.role}${m.pending ? ' draft' : ''}`);
    article.dataset.messageId = m.id; article.dataset.signature = signature;
    const head = el('h2', 'message-head');
    if (m.role === 'assistant') head.append(el('span', 'avatar', '{·}'));
    head.append(el('span', '', m.role === 'user' ? 'You' : 'api-agent'));
    const body = el('div', 'message-body');
    if (m.role === 'assistant') renderMarkdown(body, m.text);
    else body.textContent = m.text;
    article.append(head, body);
    if (m.error) {
      article.append(el('p', 'error-note', m.error));
      const retry = el('button', 'suggestion', 'Retry response'); retry.type = 'button';
      retry.disabled = !!active;
      retry.addEventListener('click', () => { focusComposer(); run(m.request, {...m.requested}, m); });
      article.append(retry);
    }
    if (m.role === 'assistant') {
      const details = responseDetails(m);
      details.open = !!previous?.querySelector('details')?.open;
      article.append(details);
      if (m.question && !m.resolved && !m.pending && !m.error && state.reply?.messageId === m.id) article.append(questionControls(m));
    }
    return article;
  });
  for (const n of [...c.children]) if (!nodes.includes(n)) n.remove();
  nodes.forEach((n, i) => { if (c.children[i] !== n) c.insertBefore(n, c.children[i] || null); });
  if (!focused.isConnected && wasSummary) nodes.find(n => n.dataset.messageId === focusedId)?.querySelector('summary')?.focus({preventScroll: true});
  c.querySelectorAll('.suggestion').forEach(b => b.disabled = !!active);
  $('send').disabled = !!active; $('new-chat').disabled = !!active;
  if (!active && document.activeElement === $('stop')) focusComposer();
  $('stop').hidden = !active;
}
async function run(text, requested, retryMessage, reply) {
  if (active) return;
  reply = retryMessage ? retryMessage.requestReply : reply;
  if (reply && !supportsContinuation) { announce('This backend no longer supports this question continuation. The original reply is preserved for retry.'); return; }
  const selection = resolveSelection(catalog, retryMessage?.selectionSnapshot || requested);
  if (!selection.valid) { announce(selection.error); return; }
  previousChat = null; savePrevious(); clearPromptError();
  const history = retryMessage ? state.messages.slice(0, Math.max(0, state.messages.indexOf(retryMessage) - 1)) : state.messages;
  const messages = history.filter(m => !m.error && !m.pending).map(({role, text}) => ({role, text}));
  const controller = new AbortController();
  const request = {controller, sessionId: crypto.randomUUID(), heartbeat: null, stopped: false}; active = request;
  const snapshot = {...requested, family: '', model: selection.selection.selected_llm, version: selection.selection.version || requested.version};
  if (!retryMessage) state.messages.push({id: crypto.randomUUID(), role: 'user', text, requested: {...requested}, selectionSnapshot: {...snapshot}});
  const m = retryMessage || {id: crypto.randomUUID(), role: 'assistant', request: text, requested: {...requested}};
  if (!retryMessage) {
    m.requestReply = reply ? structuredClone(reply) : null;
    m.selectionSnapshot = {...snapshot};
  }
  Object.assign(m, {text: '', actual: {}, error: '', pending: true});
  if (!retryMessage) state.messages.push(m);
  render(); follow(); save();
  const messageBody = [...$('conversation').children].find(node => node.dataset.messageId === m.id).querySelector('.message-body');
  announce('Waiting for api-agent. You can stop at any time.');

  const timer = setTimeout(() => { request.failure = 'The agent took too long to respond. Please try again.'; stopRequest(request); }, REQUEST_TIMEOUT_MS);
  try {
    await configuration;
    if (controller.signal.aborted) throw new DOMException('Aborted', 'AbortError');
    request.dispatched = true;
    const response = reply ? await submitContinuation({session: request.sessionId, text, messages, requested: {...m.selectionSnapshot}, reply: structuredClone(m.requestReply)}, {signal: controller.signal}) : await fetch(apiOrigin + '/app/agent/chat/stream', {
      method: 'POST', headers: {'Content-Type': 'application/json', Accept: 'text/event-stream, application/json', ...apiHeaders},
      body: JSON.stringify(requestPayload({session: request.sessionId, text, messages, requested: {...requested, model: selection.selection.selected_llm}, reply})), signal: controller.signal
    });
    await readResponse(response, {
      sessionId: request.sessionId,
      onSession(session) {
        request.heartbeat = setInterval(() => {
          void controlRequest('heartbeat', request).catch(error => { request.failure = error.message; controller.abort(); });
        }, session.heartbeat_interval_ms);
      },
      onText(text, replace) {
        m.text = replace ? text : m.text + text;
        renderMarkdown(messageBody, m.text);
        follow(); save();
      },
      onMetadata(metadata) { for (const key of ['model', 'version', 'reasoning', 'actual_model', 'service_tier', 'serving_revision']) if (field(metadata[key]) && !Object.hasOwn(m.actual, key)) m.actual[key] = metadata[key]; },
      onClarification(question) { m.question = {...question, kind: 'clarification'}; },
      onApproval(question) { m.question = {...question, kind: 'approval'}; },
      signal: controller.signal
    });
    announce('Response complete.');
  } catch (error) {
    m.error = request.failure ? request.failure : request.stopped ? 'Response stopped. You can retry when you’re ready.' : error instanceof SyntaxError ? 'The agent returned an unreadable response. Please try again.' : error instanceof TypeError ? 'Couldn’t connect to api-agent. Please try again when the agent is available.' : error.name === 'AbortError' ? 'The connection was interrupted. Please try again.' : error.message;
    if (!request.stopped && request.dispatched) void controlRequest('cancel', request).catch(() => {});
    announce(m.error);
  } finally {
    clearTimeout(timer); clearInterval(request.heartbeat); m.pending = false; active = null;
    if (beginReply(state, m)) $('prompt').value = state.draft;
    render(); follow(); save();
  }
}
function fillSelect(id, entries, selected, placeholder = 'Provider default') {
  const select = $(id); select.replaceChildren();
  if (id !== 'model') select.add(new Option(placeholder, ''));
  for (const item of entries) {
    const option = new Option(item.name || item.id, item.id);
    option.disabled = !!item.disabled;
    select.add(option);
  }
  if (selected && !entries.some(x => x.id === selected)) select.add(new Option(selected + ' (saved selection)', selected));
  select.value = selected;
}
function syncSettings() {
  const groups = modelGroups(catalog);
  const concrete = state.settings.model || (!state.settings.family && catalog.defaultSelection);
  const group = groups.find(item => state.settings.family ? item.family === state.settings.family : item.rows.some(row => row.selected_llm === concrete));
  const models = groups.map(item => ({id: item.value, name: item.name}));
  let selectedModel = group?.value || state.settings.model || '';
  if (!group && state.settings.family) {
    selectedModel = JSON.stringify(['missing-family', state.settings.family]);
    while (catalog.some(row => row.selected_llm === selectedModel)) selectedModel = '~' + selectedModel;
    models.push({id: selectedModel, name: state.settings.family + ' (saved family)', disabled: true});
  }
  fillSelect('model', models, selectedModel);
  const entries = group?.rows || [];
  const selection = resolveSelection(catalog, state.settings);
  const grouped = group?.family !== null && group !== undefined;
  const versions = grouped ? entries.map(row => ({id: row.selected_llm, name: (row.version || 'Unknown version') + (entries.filter(other => other.version === row.version).length > 1 || !row.version ? ' — ' + row.selected_llm : '')})) : entries.filter(row => row.version).map(row => ({id: row.version}));
  const unresolvedVersion = grouped && state.settings.version && (!selection.selection || selection.selection.version !== state.settings.version);
  fillSelect('version', versions, unresolvedVersion ? '' : grouped ? (state.settings.model ? state.settings.model : state.settings.version && selection.selection.selected_llm) || '' : state.settings.version, unresolvedVersion ? 'Unresolved version: ' + state.settings.version : 'Provider default');
  const availableEfforts = reasoningEfforts(catalog, state.settings);
  fillSelect('reasoning', availableEfforts.map(id => ({id})), state.settings.reasoning);
  $('reasoning').disabled = availableEfforts.length === 0;
  const label = selection.selection?.display_name || group?.name || state.settings.family || state.settings.model || 'Provider default';
  $('backend-default').textContent = state.settings.model || state.settings.family ? 'Use backend default' : 'Keep selected model';
  $('backend-default').disabled = !$('model').value;
  $('model-short-name').textContent = label + (state.settings.reasoning ? ': ' + state.settings.reasoning : '');
  $('model-status').textContent = catalogStatus || (selection.valid ? '' : selection.error);
}
const configSignal = AbortSignal.timeout(5000);
const configuration = fetch('/config.json', {signal:configSignal}).then(r => readJSONResponse(r, {signal:configSignal})).then(c => {
  if (!c || typeof c !== 'object' || Array.isArray(c) || Object.keys(c).some(key => !['apiOrigin', 'apiHeaders'].includes(key)) || typeof c.apiOrigin !== 'string') throw Error('Invalid API configuration.');
  const url = new URL(c.apiOrigin);
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.pathname !== '/' || url.search || url.hash) throw Error('Invalid API configuration.');
  if (c.apiHeaders !== undefined) {
    const headers = c.apiHeaders;
    if (!headers || typeof headers !== 'object' || Array.isArray(headers) || Object.keys(headers).length !== 1 || !Object.hasOwn(headers, 'X-API-Agent-Session') || typeof headers['X-API-Agent-Session'] !== 'string' || !/^[A-Za-z0-9_-]{32,128}$/.test(headers['X-API-Agent-Session'])) throw Error('Invalid API configuration headers.');
    apiHeaders = Object.freeze({...headers});
  }
  apiOrigin = url.origin;
});
// Attach rejection handling immediately; sending still receives the configuration error.
configuration.catch(() => { catalogStatus = 'The API configuration could not be loaded. Reload the page after checking the Studio host.'; $('model-status').textContent = catalogStatus; });
let modelsLoading = false;
async function loadModels() {
  if (modelsLoading) return;
  modelsLoading = true;
  let response;
  const status = $('model-status');
  const retry = $('retry-models');
  status.dataset.catalogState = 'loading';
  catalogStatus = 'Loading model choices…';
  status.textContent = catalogStatus;
  retry.hidden = true;
  const slow = setTimeout(() => {
    catalogStatus = 'The model provider is still getting ready…';
    status.textContent = catalogStatus;
  }, 5000);
  try {
    await configuration;
    response = await fetch(apiOrigin + '/app/agent/models', {headers: apiHeaders, signal: AbortSignal.timeout(125000)});
    const data = await readJSONResponse(response);
    catalog = normalizeCatalog(data);
    catalogStatus = catalog.length ? '' : 'No model choices are available. Retry after checking the provider.';
    syncSettings();
    retry.hidden = catalog.length > 0;
  } catch {
    catalogStatus = !apiOrigin ? 'The API configuration could not be loaded. Reload the page after checking the Studio host.' : response && !response.ok ? `Model list unavailable (HTTP ${response.status}). Retry after checking the provider.` : 'Model list unavailable. Retry after checking the provider.';
    status.textContent = catalogStatus;
    retry.hidden = !apiOrigin;
  }
  finally { clearTimeout(slow); status.dataset.catalogState = 'settled'; modelsLoading = false; }
}
$('retry-models').addEventListener('click', loadModels);
$('backend-default').addEventListener('click', () => {
  if (state.settings.model || state.settings.family) state.settings = defaults();
  else { const selected = resolveSelection(catalog, state.settings).selection; if (selected) state.settings.model = selected.selected_llm; }
  syncSettings(); save();
});
$('settings-nav').addEventListener('click', () => { settingsOpener = document.activeElement; $('settings').showModal(); });
$('close-settings').addEventListener('click', () => $('settings').close());
let backdropStart = false;
function outsideDialog(event) { const r = $('settings').getBoundingClientRect(); return event.clientX < r.left || event.clientX > r.right || event.clientY < r.top || event.clientY > r.bottom; }
$('settings').addEventListener('pointerdown', e => { backdropStart = outsideDialog(e); });
$('settings').addEventListener('click', e => { if (backdropStart && outsideDialog(e)) $('settings').close(); backdropStart = false; });
$('settings').addEventListener('close', () => settingsOpener?.focus({preventScroll: true}));
for (const id of ['model', 'version', 'reasoning']) $(id).addEventListener('change', () => {
  const groups = modelGroups(catalog);
  const current = groups.find(group => group.value === $('model').value);
  if (!current) { syncSettings(); return; }
  if (id === 'model') {
    state.settings = current.family === null ? {model: current.rows[0].selected_llm, version: '', reasoning: ''} : {model: '', family: current.family, version: '', reasoning: ''};
  } else if (id === 'version' && current.family !== null) {
    const row = current.rows.find(item => item.selected_llm === $('version').value);
    const reasoning = state.settings.reasoning;
    state.settings = row ? {model: row.selected_llm, version: row.version || '', reasoning} : {model: '', family: current.family, version: '', reasoning};
    if (!reasoningEfforts(catalog, state.settings).includes(reasoning)) state.settings.reasoning = '';
  } else {
    state.settings[id] = $(id).value;
    if (id !== 'reasoning') state.settings.reasoning = '';
  }
  syncSettings(); save();
});
$('prompt').value = state.draft;
$('prompt').addEventListener('input', () => { clearPromptError(); state.draft = $('prompt').value; save(); });
$('composer').addEventListener('submit', e => {
  e.preventDefault(); if (active) return;
  const text = $('prompt').value.trim();
  if (!text) { rejectPrompt('Write a message to get started.'); return; }
  if (state.reply) { submitReply(text); return; }
  const selection = resolveSelection(catalog, state.settings);
  if (!selection.valid) { rejectPrompt(selection.error); return; }
  state.draft = ''; $('prompt').value = ''; focusComposer(); run(text, {...state.settings});
});
$('prompt').addEventListener('keydown', e => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); $('composer').requestSubmit(); } });
async function controlRequest(action, request, keepalive = false) {
  const response = await fetch(apiOrigin + '/app/agent/chat/' + action, {
    method: 'POST', headers: {'Content-Type': 'application/json', ...apiHeaders},
    body: JSON.stringify({session_id: request.sessionId}), keepalive,
    signal: AbortSignal.timeout(5000)
  });
  const value = await readJSONResponse(response);
  if (!value || value.session_id !== request.sessionId || !['running','completed','cancelled','failed','cleanup_pending','unknown'].includes(value.status) || Object.keys(value).sort().join(',') !== 'session_id,status') throw Error('Invalid agent lifecycle response');
  if (action === 'heartbeat' && !['running','completed','failed'].includes(value.status)) throw Error('The agent request is no longer running. Please retry.');
}
function stopRequest(request, keepalive = false) {
  request.stopped = true; clearInterval(request.heartbeat);
  if (apiOrigin && request.dispatched) void controlRequest('cancel', request, keepalive).catch(() => {});
  request.controller.abort();
}
$('stop').addEventListener('click', () => { if (active) { stopRequest(active); focusComposer(); } });
window.addEventListener('pagehide', () => { if (active) stopRequest(active, true); });
$('new-chat').addEventListener('click', () => { if (active) return; clearPromptError(); const settings = state.settings; if (state.messages.length || state.draft) previousChat = structuredClone(state); savePrevious(); state = fresh(); state.settings = settings; $('prompt').value = ''; render(); $('conversation-scroll').scrollTop = 0; save(); focusComposer(); announce(previousChat ? 'A new conversation is ready. Undo new chat is available until you send a message.' : 'A new conversation is ready.'); });
document.querySelectorAll('[data-prompt]').forEach(b => b.addEventListener('click', () => { clearPromptError(); $('prompt').value = b.dataset.prompt; state.draft = b.dataset.prompt; save(); focusComposer(); }));
$('follow-latest').addEventListener('change', () => { if ($('follow-latest').checked) scrollLatest(); });
$('latest-response').addEventListener('click', () => { $('follow-latest').checked = true; scrollLatest(); });
const pauseFollow = () => { $('follow-latest').checked = false; };
$('conversation-scroll').addEventListener('wheel', e => { if (e.deltaY < 0) pauseFollow(); }, {passive: true});
$('conversation-scroll').addEventListener('touchstart', pauseFollow, {passive: true});
$('conversation-scroll').addEventListener('pointerdown', pauseFollow);
$('conversation-scroll').addEventListener('keydown', e => { if (['ArrowUp', 'PageUp', 'Home'].includes(e.key)) pauseFollow(); });
$('copyright-notice').textContent = `Copyright David Bruck © 2026–${new Date().getFullYear()}`;
syncSettings(); render(); loadModels();

$('undo-new-chat').addEventListener('click', () => {
  if (active || !previousChat || state.messages.length) return;
  clearPromptError(); const draft = state.draft;
  state = previousChat; previousChat = null;
  if (draft) state.draft = draft;
  savePrevious(); $('prompt').value = state.draft; render(); save(); follow(); focusComposer();
  announce('Previous conversation restored.');
});
