// Text wire values follow providers/inference.py. SSE is the Studio adapter
// envelope, not a claim that the deferred HTTP handlers implement streaming.
const codepoints = value => [...value].length;
const wireBytes = value => JSON.stringify(value).replace(/[\u007f-\uffff]/g, character => '\\u' + character.charCodeAt(0).toString(16).padStart(4,'0')).length;
const LIMIT = 131072;
const printable = value => !/[\p{C}\p{Z}]/u.test(value.replaceAll(' ',''));
function bounded(value) {
  if (wireBytes(value) > LIMIT) throw Error('Response or request exceeds its byte limit');
  return value;
}
export function requestPayload({session = '', messages = [], text, requested, reply}) {
  if (reply) throw Error('Structured continuation is not implemented by the agent HTTP API.');
  const rows = [...messages.map(({role,text}) => ({role,text})), {role:'user',text}];
  if (!requested || typeof requested.model !== 'string' || !requested.model || codepoints(requested.model) > 200 || !printable(requested.model) || typeof session !== 'string' || codepoints(session) > 200 || !printable(session) || rows.length > 64 || rows.some(x => !['system','user','assistant'].includes(x.role) || typeof x.text !== 'string' || codepoints(x.text) > 32768)) throw Error('Invalid text inference request');
  const row = {selected_llm:requested.model,messages:rows,max_output_tokens:null,cache_policy:null,request_id:session};
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
async function readBody(response, {signal, onChunk = () => {}} = {}) {
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
      if (total > LIMIT) throw Error('Response or request exceeds its byte limit');
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
  let terminal = false;
  function result(value) {
    bounded(value);
    if (!value || typeof value.text !== 'string' || codepoints(value.text) > 65536 || !['completed','length','refused'].includes(value.outcome) || !value.usage || typeof value.usage !== 'object') throw Error('Invalid inference result');
    const allowed = ['text','outcome','usage','actual_model','service_tier','serving_revision','billing'];
    if (Object.keys(value).some(key => !allowed.includes(key))) throw Error('Unsupported inference result fields');
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
  function event(value) {
    if (terminal) throw Error('Event follows terminal response');
    if (!value || typeof value !== 'object') throw Error('Invalid stream event');
    onActivity();
    if (value.kind === 'text' && typeof value.payload?.text === 'string') onText(value.payload.text, false);
    else if (value.kind === 'completed') result(value.payload);
    else if (value.kind === 'error') throw Error(typeof value.message === 'string' ? value.message : 'Agent stream failed');
    else if (value.kind === 'clarification' || value.kind === 'approval') {
      const p = value.payload;
      if (!p || typeof p.id !== 'string' || !p.id || typeof p.question !== 'string' || !Array.isArray(p.options) || p.options.some(x => value.kind === 'clarification' ? typeof x !== 'string' : !x || typeof x.id !== 'string' || typeof x.label !== 'string')) throw Error('Invalid structured question');
      (value.kind === 'clarification' ? onClarification : onApproval)(structuredClone(p));
      terminal = true;
    } else if (value.kind !== 'heartbeat' || !callbacks.heartbeat) throw Error('Unsupported stream event');
  }
  const type = contentType(response);
  if (type === 'application/json') { result(await readJSONResponse(response, {signal})); return; }
  let buffer = '', data = [];
  const dispatch = () => {
    if (data.length) { event(bounded(JSON.parse(data.join('\n')))); data = []; }
  };
  const line = text => {
    if (!text) dispatch();
    else if (text.startsWith('data:')) data.push(text.slice(5).replace(/^ /,''));
    else if (!text.startsWith(':') && !/^(event|id|retry):/.test(text)) throw Error('Invalid SSE field');
  };
  const raw = await readBody(response, {signal, onChunk(chunk) {
    buffer += chunk;
    if (type === 'text/event-stream' && response.ok) {
      let match;
      while ((match = /\r\n|\r(?!$)|\n/.exec(buffer))) { line(buffer.slice(0,match.index)); buffer = buffer.slice(match.index + match[0].length); }
    }
  }});
  checkStatus(response, raw);
  if (type === 'text/event-stream') {
    if (buffer.endsWith('\r')) buffer = buffer.slice(0,-1);
    if (buffer) line(buffer);
    dispatch();
    if (!terminal) throw Error('Stream ended without a terminal response');
  } else throw Error('Unsupported agent response content type');
}

export const supportsContinuation = false;
export async function submitContinuation() {
  throw Error('Structured continuation is not implemented by the agent HTTP API.');
}
