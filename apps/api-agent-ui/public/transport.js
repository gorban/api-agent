// Bounded application HTTP DTOs; provider wire contracts stay behind the API.
const codepoints = value => [...value].length;
const wireBytes = value => JSON.stringify(value).replace(/[\u007f-\uffff]/g, character => '\\u' + character.charCodeAt(0).toString(16).padStart(4,'0')).length;
const LIMIT = 131072;
const printable = value => !/[\p{C}\p{Z}]/u.test(value.replaceAll(' ',''));
function bounded(value) {
  if (wireBytes(value) > LIMIT) throw Error('Response or request exceeds its byte limit');
  return value;
}
export function requestPayload({session = '', conversation, messages = [], text, requested, reply}) {
  if (reply) throw Error('Structured continuation is not implemented by the agent HTTP API.');
  const rows = [...messages.map(({role,text}) => ({role,content:text})), {role:'user',content:text}];
  if (!requested || typeof requested.model !== 'string' || !requested.model || codepoints(requested.model) > 200 || !printable(requested.model) || typeof session !== 'string' || codepoints(session) > 200 || !printable(session) || rows.length > 64 || rows.some(x => !['system','user','assistant'].includes(x.role) || typeof x.content !== 'string' || codepoints(x.content) > 32768)) throw Error('Invalid text inference request');
  const row = {model:requested.model,messages:rows,session_id:session};
  if (conversation !== undefined) {
    if (typeof conversation !== 'string' || conversation.length !== 36 || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(conversation)) throw Error('Invalid conversation identifier');
    row.conversation_id = conversation;
  }
  if (requested.reasoning !== undefined && requested.reasoning !== '') {
    if (!['off','low','medium','high'].includes(requested.reasoning)) throw Error('Invalid reasoning effort');
    row.reasoning_effort = requested.reasoning;
  }
  return bounded(row);
}
const contentType = response => (response.headers.get('content-type') || '').split(';')[0].trim().toLowerCase();
function checkStatus(response, text) {
  if (!response.ok) throw Error(`Agent HTTP ${response.status}${text ? ': ' + text.slice(0,500) : ''}`);
}
async function readBody(response, {signal, onChunk = () => {}, limit = LIMIT} = {}) {
  const check = () => { if (signal?.aborted) throw new DOMException('Aborted','AbortError'); };
  check();
  if (!response.body) throw Error('Empty agent response');
  const reader = response.body.getReader();
  const abort = () => { void reader.cancel().catch(() => {}); };
  signal?.addEventListener('abort', abort, {once:true});
  const decoder = new TextDecoder('utf-8',{fatal:true});
  let text = '', total = 0;
  try {
    while (true) {
      check(); const {value,done} = await reader.read(); check();
      if (done) break;
      total += value.byteLength;
      if (total > limit) throw Error('Response or request exceeds its byte limit');
      const chunk = decoder.decode(value,{stream:true});
      text += chunk; onChunk(chunk);
    }
    const final = decoder.decode();
    text += final; onChunk(final);
    return text;
  } finally {
    signal?.removeEventListener('abort',abort);
    await reader.cancel().catch(() => {}); reader.releaseLock();
  }
}
export async function readJSONResponse(response, options) {
  const text = await readBody(response, options);
  checkStatus(response, text);
  if (contentType(response) !== 'application/json') throw Error('Unsupported agent response content type');
  return bounded(JSON.parse(text));
}
export async function readResponse(response, callbacks = {}) {
  const {onText = () => {}, onMetadata = () => {}, onClarification = () => {}, onApproval = () => {}, onActivity = () => {}, signal} = callbacks;
  let terminal = false, session = null, phase = 'initial';
  const binding = value => { if (!value || value.session_id !== session) throw Error('Invalid stream session'); };
  const adaptResult = value => {
    if (!value || typeof value !== 'object') throw Error('Invalid inference result');
    if (Object.keys(value).some(key => !['session_id','response_text','outcome','usage','actual_model','service_tier','serving_revision','billing'].includes(key))) throw Error('Unsupported inference result fields');
    const {session_id, response_text, ...rest} = value;
    if (typeof session_id !== 'string' || !session_id || (callbacks.sessionId && callbacks.sessionId !== session_id)) throw Error('Invalid stream session');
    return {...rest,text:response_text};
  };
  function result(value) {
    bounded(value);
    if (!value || typeof value.text !== 'string' || codepoints(value.text) > 65536 || !['completed','length','refused'].includes(value.outcome) || !value.usage || typeof value.usage !== 'object') throw Error('Invalid inference result');
    if (value.billing !== undefined && value.billing !== null) {
      const billing = value.billing;
      if (typeof billing !== 'object' || Array.isArray(billing) || Object.keys(billing).sort().join(',') !== 'generation_id,request_id,usd' || (billing.usd !== null && (typeof billing.usd !== 'number' || !Number.isFinite(billing.usd) || billing.usd < 0 || billing.usd > 1e12)) || [billing.request_id,billing.generation_id].some(id => id !== null && (typeof id !== 'string' || !/^[A-Za-z0-9._:-]{1,200}$/.test(id)))) throw Error('Invalid reported billing evidence');
    }
    const usage = value.usage;
    const required = ['input_tokens','output_tokens','cached_input_tokens','reasoning_tokens'];
    if (Array.isArray(usage) || required.some(k => !(k in usage)) || Object.keys(usage).some(k => ![...required,'cache_write_tokens'].includes(k)) || Object.values(usage).some(v => v !== null && (!Number.isInteger(v) || v < 0 || v > 2147483647))) throw Error('Invalid response usage');
    if ((usage.input_tokens !== null && ((usage.cached_input_tokens ?? 0) + (usage.cache_write_tokens ?? 0) > usage.input_tokens)) || (usage.output_tokens !== null && (usage.reasoning_tokens ?? 0) > usage.output_tokens)) throw Error('Invalid response usage');
    const metadata = {};
    for (const key of ['actual_model','service_tier','serving_revision']) if (value[key] !== undefined && value[key] !== null) {
      if (typeof value[key] !== 'string' || !value[key] || codepoints(value[key]) > 200 || !printable(value[key])) throw Error('Invalid response metadata');
      metadata[key] = value[key];
    }
    if (metadata.actual_model) metadata.model = metadata.actual_model;
    onText(value.text, true);
    onMetadata(metadata); terminal = true;
  }
  function event(name, value) {
    if (terminal) throw Error('Event follows terminal response');
    if (!value || typeof value !== 'object' || Array.isArray(value)) throw Error('Invalid stream event');
    onActivity();
    if (name === 'session') {
      if (phase !== 'initial' || typeof value.session_id !== 'string' || !value.session_id || (callbacks.sessionId && value.session_id !== callbacks.sessionId) || ![value.heartbeat_interval_ms, value.heartbeat_timeout_ms].every(ms => typeof ms === 'number' && Number.isFinite(ms) && ms > 0 && ms <= 2147483647) || value.heartbeat_interval_ms >= value.heartbeat_timeout_ms || Object.keys(value).sort().join(',') !== 'heartbeat_interval_ms,heartbeat_timeout_ms,session_id') throw Error('Invalid stream session');
      session = value.session_id; phase = 'stream'; callbacks.onSession?.(value); return;
    }
    if (name === 'token') {
      if (phase !== 'stream' || typeof value.content !== 'string' || Object.keys(value).join(',') !== 'content') throw Error('Invalid token event');
      onText(value.content, false); return;
    }
    if (name === 'response_complete') {
      binding(value);
      if (phase !== 'stream' || Object.keys(value).join(',') !== 'session_id') throw Error('Invalid response completion');
      phase = 'response_complete'; return;
    }
    if (name === 'result') {
      binding(value);
      if (phase !== 'response_complete') throw Error('Invalid result sequence');
      result(adaptResult(value)); terminal = false; phase = 'result'; return;
    }
    if (name === 'done') {
      binding(value);
      if (phase !== 'result' || Object.keys(value).join(',') !== 'session_id') throw Error('Invalid done event');
      terminal = true; return;
    }
    if (name === 'error' || name === 'cancelled') {
      binding(value);
      throw Error(name === 'cancelled' ? 'Response cancelled.' : typeof value.message === 'string' ? value.message : 'Agent stream failed');
    }
    if (name === 'clarification' || name === 'approval') {
      if (phase !== 'stream') throw Error('Invalid question sequence');
      const p = value;
      if (typeof p.id !== 'string' || !p.id || typeof p.question !== 'string' || !Array.isArray(p.options) || p.options.some(x => name === 'clarification' ? typeof x !== 'string' : !x || typeof x.id !== 'string' || typeof x.label !== 'string')) throw Error('Invalid structured question');
      (name === 'clarification' ? onClarification : onApproval)(structuredClone(p));
      terminal = true; return;
    }
    throw Error('Unsupported stream event');
  }
  const type = contentType(response);
  if (type === 'application/json') { result(adaptResult(await readJSONResponse(response, {signal}))); return; }
  let buffer = '', data = [], eventName = '', frameBytes = 0, frameCount = 0;
  const dispatch = () => {
    if (data.length) { if (++frameCount > 8192) throw Error('Stream exceeds its event limit'); event(eventName, bounded(JSON.parse(data.join('\n')))); }
    data = []; eventName = ''; frameBytes = 0;
  };
  const line = (text, separatorBytes = 0) => {
    frameBytes += new TextEncoder().encode(text).byteLength + separatorBytes;
    if (frameBytes > LIMIT) throw Error('Stream frame exceeds its byte limit');
    if (!text) dispatch();
    else if (text.startsWith('data:')) data.push(text.slice(5).replace(/^ /,''));
    else if (text.startsWith('event:')) { if (eventName) throw Error('Duplicate SSE event'); eventName = text.slice(6).trim(); }
    else if (!text.startsWith(':') && !/^(id|retry):/.test(text)) throw Error('Invalid SSE field');
  };
  const raw = await readBody(response, {signal, limit: type === 'text/event-stream' && response.ok ? 1048576 : LIMIT, onChunk(chunk) {
    buffer += chunk;
    if (type === 'text/event-stream' && response.ok) {
      let match;
      while ((match = /\r\n|\r(?!$)|\n/.exec(buffer))) { line(buffer.slice(0,match.index), match[0].length); buffer = buffer.slice(match.index + match[0].length); }
      if (new TextEncoder().encode(buffer).byteLength + frameBytes > LIMIT) throw Error('Stream frame exceeds its byte limit');
    }
  }});
  checkStatus(response, raw);
  if (type === 'text/event-stream') {
    if (buffer.endsWith('\r')) { line(buffer.slice(0,-1), 1); buffer = ''; }
    if (buffer) line(buffer);
    if (data.length || eventName) throw Error('Incomplete SSE framing');
    if (!terminal) throw Error('Stream ended without a terminal response');
  } else throw Error('Unsupported agent response content type');
}

export const supportsContinuation = false;
export async function submitContinuation() {
  throw Error('Structured continuation is not implemented by the agent HTTP API.');
}
