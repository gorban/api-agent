import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile, writeFile} from 'node:fs/promises';
const {chromium} = await import(process.env.STUDIO_PLAYWRIGHT_MODULE || 'playwright');
import {createStudioServer} from '../../apps/api-agent-ui/server.mjs';
const {server} = createStudioServer({UI_LISTENER_URL:'http://127.0.0.1:9443',AGENT_DJANGO_URL:'http://127.0.0.1:1',API_AGENT_UI_LAUNCH_TOKEN:'synthetic-browser-test-token'});
let host;
const instrumenter = process.env.STUDIO_ISTANBUL_FILE ? (await import(process.env.STUDIO_ISTANBUL_MODULE || 'istanbul-lib-instrument')).createInstrumenter({esModules:true,compact:false,coverageGlobalScope:'globalThis',coverageGlobalScopeFunc:false}) : null;
const instrumentedCoverage=[];
const coverage = [], coveredPages = new WeakSet();
async function startCoverage(page, {instrument = true} = {}) {
  if (instrumenter && instrument) await page.route('**/*.js', async route => {
    const name=new URL(route.request().url()).pathname.slice(1);
    if (!['app.js','appearance.js','markdown.js','reply.js'].includes(name)) return route.continue();
    const response=await route.fetch();
    const body=instrumenter.instrumentSync(await response.text(),new URL(name,root).pathname);
    await fulfill(route,{response,body});
  });
  if (process.env.STUDIO_COVERAGE_FILE) { await page.coverage.startJSCoverage({resetOnNavigation:false}); coveredPages.add(page); }
}
async function reloadPage(page) {
  if (instrumenter) { const value=await page.evaluate(() => window.__coverage__); if(value) instrumentedCoverage.push(value); }
  await page.reload();
}
async function closeBrowser(browser) {
  for (const context of browser.contexts()) for (const page of context.pages()) {
    if (instrumenter) { const value=await page.evaluate(() => window.__coverage__); if(value) instrumentedCoverage.push(value); }
    if (coveredPages.has(page)) coverage.push(...await page.coverage.stopJSCoverage());
  }
  await browser.close();
}
test.before(async () => { await new Promise(resolve => server.listen(0,'127.0.0.1',resolve)); host = 'http://127.0.0.1:'+server.address().port; });
test.after(async () => { if(process.env.STUDIO_ISTANBUL_FILE) await writeFile(process.env.STUDIO_ISTANBUL_FILE,JSON.stringify(instrumentedCoverage)); if (process.env.STUDIO_COVERAGE_FILE) await writeFile(process.env.STUDIO_COVERAGE_FILE,JSON.stringify(coverage)); server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); });
const root = new URL('../../apps/api-agent-ui/public/', import.meta.url);
const model = {selected_llm:'sample',description:'Sample',features:['reasoning_effort'],display_name:'Sample model',version:'v1',family:null,max_output_tokens:null,context_length:null,reasoning_efforts:['off','low']};
function wireModel({selected_llm, display_name, ...metadata}) { return {...metadata,value:selected_llm,label:display_name}; }
function wireResult(value, session_id) { const {text,...metadata}=value; return {...metadata,session_id,response_text:text}; }
const frame=(event,data)=>`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
const sessionFrame=id=>frame('session',{session_id:id,heartbeat_interval_ms:10000,heartbeat_timeout_ms:30000});
const finishFrames=(id,value)=>frame('response_complete',{session_id:id})+frame('result',wireResult(value,id))+frame('done',{session_id:id});
async function fulfill(route, fixture) {
  fixture={...fixture};
  const path=new URL(route.request().url()).pathname;
  if(path==='/app/agent/models' && fixture.json) {
    const envelope=Array.isArray(fixture.json)?{models:fixture.json,default:fixture.json[0]?.selected_llm??null}:fixture.json;
    fixture.json={...envelope,models:envelope.models.map(wireModel),reasoning_default:null};
  }
  if(path==='/app/agent/chat/stream') {
    const id=route.request().postDataJSON().session_id;
    if(fixture.json?.outcome) fixture.json=wireResult(fixture.json,id);
    if(fixture.contentType==='text/event-stream' && fixture.body?.includes('"kind"')) {
      fixture.body=sessionFrame(id)+fixture.body.replace(/data: ([^\n]+)\n\n/g,(_,raw)=>{
        const value=JSON.parse(raw);
        return value.kind==='completed'?finishFrames(id,value.payload):frame(value.kind==='text'?'token':value.kind,value.kind==='text'?{content:value.payload.text}:value.payload);
      });
    }
  }
  return route.fulfill(fixture);
}
test('Studio selection, safe rendering, question editing, and unavailable continuation', async () => {
  const browser = await chromium.launch({headless:true, executablePath:process.env.STUDIO_BROWSER || undefined});
  try {
    const page = await browser.newPage(); await startCoverage(page);
    const errors = []; page.on('pageerror', error => errors.push(error.message));
    let requests = [];
    await page.route('http://127.0.0.1:1/**', async route => {
      const url = new URL(route.request().url());
      if (url.pathname === '/config.json') return fulfill(route,{json:{apiOrigin:'http://localhost'}});
      if (url.pathname === '/app/agent/models') return fulfill(route,{json:[model]});
      if (url.pathname === '/app/agent/chat/stream') {
        requests.push(route.request().postDataJSON());
        return fulfill(route,{contentType:'text/event-stream',body:'data: '+JSON.stringify({kind:'clarification',payload:{id:'q1',question:'What should we make?',options:['A small tool','A plan']}})+'\n\n'});
      }
      return route.continue();
    });
    await page.goto(host);
    await page.waitForFunction(()=>document.querySelector('#model-short-name').textContent==='Sample model');
    await page.locator('#settings-nav').click();
    await page.locator('#model').selectOption('sample');
    assert.equal(await page.locator('#model-short-name').textContent(), 'Sample model');
    assert.deepEqual(await page.locator('#reasoning option').allTextContents(), ['Provider default','off','low']);
    await page.locator('#reasoning').selectOption('off');
    assert.equal(await page.locator('#model-short-name').textContent(), 'Sample model: off');
    await page.locator('#reasoning').selectOption('low');
    assert.equal(await page.locator('#model-short-name').textContent(), 'Sample model: low');
    await page.locator('#reasoning').selectOption('off');
    await page.locator('#close-settings').click();
    await page.locator('#prompt').fill('<b>Make something</b>');
    await page.locator('#prompt').press('Control+Enter');
    await page.locator('legend').waitFor();
    assert.equal(requests[0].model, 'sample');
    assert.equal(requests[0].reasoning_effort, 'off');
    assert.deepEqual(requests[0].messages, [{role:'user',content:'<b>Make something</b>'}]);
    assert.equal(await page.locator('.user .message-body b').count(),0);
    await page.getByRole('button',{name:'A small tool',exact:true}).click();
    assert.equal(await page.locator('#prompt').inputValue(), 'A small tool');
    await page.getByRole('button',{name:'Choose your own answer below…'}).click();
    assert.equal(await page.locator('#prompt').inputValue(), '');
    await page.locator('#prompt').fill('A small tool edited');
    await page.getByRole('button',{name:'Choose your own answer below…'}).click();
    assert.equal(await page.locator('#prompt').inputValue(), 'A small tool edited');
    await page.locator('#send').click();
    assert.match(await page.locator('#status').textContent(), /does not support question continuations/);
    assert.equal(requests.length, 1);
    assert.equal(await page.locator('#prompt').inputValue(), 'A small tool edited');
    await page.evaluate(async () => {
      const {renderMarkdown} = await import('/markdown.js');
      renderMarkdown(document.querySelector('.message-body'), '<script>window.injected=true</script>[bad](javascript:alert(1)) ![remote](https://other.test/pixel)');
    });
    assert.equal(await page.evaluate(() => window.injected), undefined);
    assert.equal(await page.locator('.message-body img').count(), 0);
    assert.equal(await page.locator('.message-body a[href^="javascript:"]').count(), 0);
    assert.deepEqual(errors, []);
  } finally { await closeBrowser(browser); }
});
for (const [name, fixture, expected] of [
  ['HTTP failure', {status:501,body:'Unavailable'}, /HTTP 501/],
  ['malformed response', {contentType:'application/json',body:'{'}, /unreadable response/],
  ['premature stream end', {contentType:'text/event-stream',body:'data: {"kind":"text","payload":{"text":"Partial"}}\n\n'}, /without a terminal/]
]) test(name+' remains incomplete and retryable', async () => {
  const browser = await chromium.launch({headless:true,executablePath:process.env.STUDIO_BROWSER || undefined});
  try {
    const page = await browser.newPage(); await startCoverage(page);
    await page.route('http://127.0.0.1:1/**', async route => {
      const path = new URL(route.request().url()).pathname;
      if (path === '/config.json') return fulfill(route,{json:{apiOrigin:'http://localhost'}});
      if (path === '/app/agent/models') return fulfill(route,{json:[model]});
      if (path === '/app/agent/chat/stream') return fulfill(route,fixture);
      return route.continue();
    });
    await page.goto(host);
    await page.locator('#settings-nav').click();
    await page.locator('#model').selectOption('sample');
    await page.locator('#appearance').selectOption('dark');
    assert.equal(await page.locator('#dark-theme').getAttribute('media'),'all');
    await page.locator('#close-settings').click();
    await page.locator('#prompt').fill('Hello');
    await page.locator('#send').click();
    await page.getByRole('button',{name:'Retry response'}).waitFor();
    assert.match(await page.locator('.error-note').textContent(),expected);
    assert.match(await page.locator('.assistant .metadata-grid').textContent(),/Incomplete/);
    assert.equal(await page.locator('#stop').isVisible(),false);
    await page.locator('#new-chat').click();
    assert.equal(await page.locator('.message').count(),0);
    await page.locator('#undo-new-chat').click();
    assert.equal(await page.locator('.message').count(),2);
  } finally { await closeBrowser(browser); }
});
test('capable continuation adapter restores draft and preserves question identity', async () => {
  const browser = await chromium.launch({headless:true,executablePath:process.env.STUDIO_BROWSER || undefined});
  try {
    const page = await browser.newPage(); await startCoverage(page);
    await page.route('http://127.0.0.1:1/**', async route => {
      const path = new URL(route.request().url()).pathname;
      if (path === '/config.json') return fulfill(route,{json:{apiOrigin:'http://localhost'}});
      if (path === '/app/agent/models') return fulfill(route,{json:[model]});
      if (path === '/app/agent/chat/stream') {
        await new Promise(resolve => setTimeout(resolve,300));
        return fulfill(route,{contentType:'text/event-stream',body:'data: '+JSON.stringify({kind:'clarification',payload:{id:'exact-question',question:'Which approach?',options:['Suggested']}})+'\n\n'});
      }
      return route.continue();
    });
    await page.route('**/transport.js', async route => {
      const response = await route.fetch();
      const body = (await response.text()).replace('export const supportsContinuation = false;', 'export const supportsContinuation = true;').replace(/export async function submitContinuation\(\) \{[\s\S]*?\n\}/, `export async function submitContinuation(input) {
        window.continuationInput = input;
        return new Response(JSON.stringify({session_id:input.session,response_text:'Acknowledged',outcome:'completed',usage:{input_tokens:null,output_tokens:null,cached_input_tokens:null,reasoning_tokens:null}}),{headers:{'content-type':'application/json'}});
      }`);
      await fulfill(route,{response,body});
    });
    await page.goto(host);
    await page.locator('#settings-nav').click();
    await page.locator('#model').selectOption('sample');
    await page.locator('#close-settings').click();
    await page.locator('#prompt').fill('Initial request');
    await page.locator('#send').click();
    await page.locator('#prompt').fill('Earlier unsent draft');
    await page.locator('legend').waitFor();
    assert.equal(await page.locator('#prompt').inputValue(),'');
    await page.getByRole('button',{name:'Suggested',exact:true}).click();
    await page.locator('#prompt').fill('Edited answer');
    await page.locator('#send').click();
    await page.waitForTimeout(100);
    assert.equal(await page.locator('.error-note').allTextContents()+'', '', await page.locator('#status').textContent());
    await page.getByText('Acknowledged',{exact:true}).waitFor();
    assert.deepEqual(await page.evaluate(() => window.continuationInput.reply), {kind:'clarification',id:'exact-question',text:'Edited answer'});
    assert.equal(await page.locator('#prompt').inputValue(),'Earlier unsent draft');
    assert.equal(await page.locator('legend').count(),0);
  } finally { await closeBrowser(browser); }
});
async function openStudio(handler, init) {
  const browser = await chromium.launch({headless:true,executablePath:process.env.STUDIO_BROWSER || undefined});
  const context = await browser.newContext();
  if (init) await context.addInitScript(init);
  const page = await context.newPage(); await startCoverage(page);
  await page.route('http://127.0.0.1:1/**', async route => {
    if (new URL(route.request().url()).pathname === '/app/agent/models') return fulfill(route,{json:{models:[model],default:'sample'}});
    if (new URL(route.request().url()).pathname.match(/\/(heartbeat|cancel)$/)) return fulfill(route,{json:{session_id:route.request().postDataJSON().session_id,status:'running'}});
    return handler(route);
  });
  await page.goto(host);
  await page.waitForFunction(()=>[...document.querySelector('#model').options].some(option=>option.textContent==='Sample model'));
  return {browser,context,page};
}
const result = (text, extra = {}) => ({text,outcome:'completed',usage:{input_tokens:null,output_tokens:null,cached_input_tokens:null,reasoning_tokens:null},...extra});
test('reported metadata stays immutable after settings changes and reload; history and draft persist', async () => {
  const {browser,page} = await openStudio(route => fulfill(route,{json:result('Saved reply',{actual_model:'reported-model',serving_revision:'revision-1',service_tier:'standard'})}));
  try {
    await page.locator('#prompt').fill('Saved request'); await page.locator('#send').click();
    await page.getByText('Saved reply',{exact:true}).waitFor();
    const metadata = await page.locator('.assistant .metadata-grid').textContent();
    assert.match(metadata,/reported-model/); assert.match(metadata,/revision-1/); assert.match(metadata,/Not reported/);
    await page.locator('#prompt').fill('Unsent draft');
    await page.locator('#settings-nav').click(); await page.locator('#model').selectOption('sample');
    await page.locator('#version').selectOption('v1'); await page.locator('#reasoning').selectOption('low');
    await page.locator('#close-settings').click();
    assert.equal(await page.locator('.assistant .metadata-grid').textContent(),metadata);
    await reloadPage(page); await page.getByText('Saved reply',{exact:true}).waitFor();
    assert.equal(await page.locator('#prompt').inputValue(),'Unsent draft');
    assert.equal(await page.locator('.assistant .metadata-grid').textContent(),metadata);
    await page.locator('#new-chat').click(); await page.locator('#prompt').fill('New draft'); await reloadPage(page);
    await page.locator('#undo-new-chat').click();
    assert.equal(await page.locator('#prompt').inputValue(),'New draft');
    assert.equal(await page.locator('.message').count(),2);
  } finally { await closeBrowser(browser); }
});
test('appearance follows system live, explicit overrides, and storage events across tabs', async () => {
  const {browser,context,page} = await openStudio(route => route.abort());
  try {
    await page.emulateMedia({colorScheme:'dark',reducedMotion:'reduce'});
    await page.locator('#settings-nav').click();
    assert.equal(await page.locator('#system-mode').textContent(),'dark');
    assert.equal(await page.locator('#dark-theme').getAttribute('media'),'(prefers-color-scheme: dark)');
    await page.locator('#appearance').selectOption('light');
    assert.equal(await page.locator('#dark-theme').getAttribute('media'),'not all');
    const second = await context.newPage(); await second.goto(host);
    assert.equal(await second.locator('#dark-theme').getAttribute('media'),'not all');
    await page.locator('#appearance').selectOption('dark');
    await second.waitForFunction(() => document.querySelector('#dark-theme').media === 'all');
    await page.locator('#appearance').selectOption('system');
    await second.waitForFunction(() => document.querySelector('#dark-theme').media === '(prefers-color-scheme: dark)');
    await page.emulateMedia({colorScheme:'light'});
    await page.waitForFunction(() => document.querySelector('#system-mode').textContent === 'light');
    await page.locator('#close-settings').click();
    await page.setViewportSize({width:390,height:844});
    assert.ok(await page.locator('#prompt').isVisible());
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  } finally { await closeBrowser(browser); }
});
test('storage failures retain page usability and explain unavailable persistence', async () => {
  const {browser,page} = await openStudio(route => fulfill(route,{json:result('Still works')}), () => {
    Storage.prototype.getItem = () => { throw Error('unavailable'); };
    Storage.prototype.setItem = () => { throw Error('unavailable'); };
  });
  try {
    assert.equal(await page.locator('#storage-warning').isVisible(),true);
    await page.locator('#prompt').fill('Hello'); await page.locator('#send').click();
    await page.getByText('Still works',{exact:true}).waitFor();
    assert.match(await page.locator('#storage-note').textContent(),/Not saved/);
  } finally { await closeBrowser(browser); }
});
for (const status of [400,401,403,429,500,503]) test('HTTP '+status+' never becomes success', async () => {
  const {browser,page} = await openStudio(route => fulfill(route,{status,body:'Synthetic failure'}));
  try {
    await page.locator('#prompt').fill('Hello'); await page.locator('#send').click();
    await page.getByRole('button',{name:'Retry response'}).waitFor();
    assert.match(await page.locator('.error-note').textContent(),new RegExp('HTTP '+status));
    assert.match(await page.locator('.assistant .metadata-grid').textContent(),/Incomplete/);
  } finally { await closeBrowser(browser); }
});
test('Stop preserves new draft and Retry sends original request once', async () => {
  let count = 0, first;
  const {browser,page} = await openStudio(route => {
    count++;
    if (count === 1) { first = route; return new Promise(() => {}); }
    return fulfill(route,{json:result('Retried')});
  });
  try {
    await page.locator('#prompt').fill('Original'); await page.locator('#send').click();
    await page.locator('#stop').waitFor(); await page.locator('#prompt').fill('New unsent draft');
    await page.locator('#stop').click();
    await page.getByRole('button',{name:'Retry response'}).waitFor();
    assert.equal(await page.locator('#prompt').inputValue(),'New unsent draft');
    assert.match(await page.locator('.error-note').textContent(),/stopped/);
    await first.abort().catch(() => {});
    await page.getByRole('button',{name:'Retry response'}).click();
    await page.getByText('Retried',{exact:true}).waitFor();
    assert.equal(await page.locator('.user').count(),1);
    assert.equal(await page.locator('.assistant').count(),1);
    assert.equal(await page.locator('#prompt').inputValue(),'New unsent draft');
    assert.equal(count,2);
  } finally { await closeBrowser(browser); }
});
test('active request survives thirty seconds until explicit Stop', async () => {
  let pending;
  const {browser,page}=await openStudio(route=>{pending=route;return new Promise(()=>{});});
  try {
    await page.clock.install();await page.locator('#prompt').fill('Wait');await page.locator('#send').click();
    await page.locator('#stop').waitFor();await page.clock.fastForward(30001);
    assert.equal(await page.locator('#stop').isVisible(),true);
    await page.locator('#stop').click();await page.getByRole('button',{name:'Retry response'}).waitFor();
    await pending?.abort().catch(()=>{});
  } finally {await closeBrowser(browser);}
});
test('follow latest pauses on user scroll and resumes explicitly; composer remains stationary', async () => {
  const text = Array.from({length:80},(_,i) => 'Paragraph '+i+' with sufficient content.').join('\n\n');
  const {browser,page} = await openStudio(route => fulfill(route,{json:result(text)}));
  try {
    await page.locator('#prompt').fill('Long reply'); await page.locator('#send').click();
    await page.getByText('Paragraph 79 with sufficient content.',{exact:true}).waitFor();
    const before = await page.locator('#composer').boundingBox();
    await page.locator('#conversation-scroll').dispatchEvent('wheel',{deltaY:-120});
    assert.equal(await page.locator('#follow-latest').isChecked(),false);
    await page.locator('#conversation-scroll').evaluate(node => { node.scrollTop=0; });
    const after = await page.locator('#composer').boundingBox();
    assert.equal(after.y,before.y);
    await page.locator('#latest-response').click();
    assert.equal(await page.locator('#follow-latest').isChecked(),true);
    assert.ok(await page.locator('#conversation-scroll').evaluate(node => node.scrollTop > 0));
  } finally { await closeBrowser(browser); }
});
test('interrupted saved response is honest after reload and stale selection blocks requests', async () => {
  let calls=0;
  const {browser,page} = await openStudio(route => { calls++; return fulfill(route,{json:result('Unexpected')}); }, () => {
    const requested={model:'removed-model',version:'old',reasoning:'high'};
    localStorage.setItem('api-agent-studio-v2',JSON.stringify({session:'saved-session',draft:'Preserved draft',settings:requested,messages:[{id:'u',role:'user',text:'Old question',requested},{id:'a',role:'assistant',text:'Partial response',request:'Old question',requested,pending:true}]}));
  });
  try {
    await page.getByRole('button',{name:'Retry response'}).waitFor();
    assert.match(await page.locator('.error-note').textContent(),/interrupted when this page closed/);
    assert.equal(await page.locator('#prompt').inputValue(),'Preserved draft');
    await page.locator('#send').click();
    assert.match(await page.locator('#status').textContent(),/available model/);
    assert.equal(calls,0);
    assert.equal(await page.locator('#prompt').inputValue(),'Preserved draft');
  } finally { await closeBrowser(browser); }
});
test('explicit approval buttons preserve server labels; arbitrary typed approval does not submit', async () => {
  let calls=0;
  const {browser,page} = await openStudio(route => {
    calls++;
    return fulfill(route,{contentType:'text/event-stream',body:'data: '+JSON.stringify({kind:'approval',payload:{id:'approval-1',question:'Apply this plan?',options:[{id:'accept',label:'Apply exact plan'},{id:'decline',label:'Keep current plan'}]}})+'\n\n'});
  });
  try {
    await page.locator('#prompt').fill('Plan please'); await page.locator('#send').click();
    await page.getByRole('button',{name:'Apply exact plan',exact:true}).waitFor();
    assert.equal(await page.getByRole('button',{name:'Keep current plan',exact:true}).count(),1);
    await page.locator('#prompt').fill('yes'); await page.locator('#send').click();
    assert.equal(calls,1);
    assert.equal(await page.locator('#prompt').inputValue(),'yes');
    assert.match(await page.locator('#status').textContent(),/does not support question continuations/);
  } finally { await closeBrowser(browser); }
});
test('provider refusal remains a visible response with unreported model metadata', async () => {
  const {browser,page} = await openStudio(route => fulfill(route,{json:result('I cannot fulfill that request.',{outcome:'refused'})}));
  try {
    await page.locator('#prompt').fill('Request'); await page.locator('#send').click();
    await page.getByText('I cannot fulfill that request.',{exact:true}).waitFor();
    assert.equal(await page.locator('.error-note').count(),0);
    assert.match(await page.locator('.assistant .metadata-grid').textContent(),/Not reported/);
  } finally { await closeBrowser(browser); }
});
for (const kind of ['approval','clarification']) test(kind+' retry retains exact continuation and never uses ordinary chat', async () => {
  let ordinary=0;
  const {browser,page} = await openStudio(route => {
    ordinary++;
    return fulfill(route,{contentType:'text/event-stream',body:'data: '+JSON.stringify({kind,payload:{id:'question-exact',question:'Choose next step',options:kind === 'approval' ? [{id:'option-exact',label:'Proceed exactly'}] : ['Proceed exactly']}})+'\n\n'});
  });
  try {
    await page.route('**/transport.js', async route => {
      const response=await route.fetch();
      const body=(await response.text()).replace('export const supportsContinuation = false;', 'export let supportsContinuation = true; window.setContinuationSupport = value => supportsContinuation = value;').replace(/export async function submitContinuation\(\) \{[\s\S]*?\n\}/, `export async function submitContinuation(input) {
        (window.continuationCalls ||= []).push(structuredClone(input));
        if (window.continuationCalls.length === 1) throw Error('Synthetic continuation failure');
        return new Response(JSON.stringify({...${JSON.stringify(wireResult(result('Continuation retried'),'placeholder'))},session_id:input.session}),{headers:{'content-type':'application/json'}});
      }`);
      await fulfill(route,{response,body});
    });
    await reloadPage(page);
    await page.locator('#prompt').fill('Original request'); await page.locator('#send').click();
    await page.getByRole('button',{name:'Proceed exactly',exact:true}).waitFor();
    await page.getByRole('button',{name:'Proceed exactly',exact:true}).click();
    if (kind === 'clarification') await page.locator('#send').click();
    await page.getByRole('button',{name:'Retry response'}).waitFor();
    await page.locator('#prompt').fill('Preserved next draft');
    await page.locator('#settings-nav').click(); await page.locator('#model').selectOption('sample'); await page.locator('#reasoning').selectOption('low'); await page.locator('#close-settings').click();
    if (kind === 'approval') {
      await page.evaluate(() => window.setContinuationSupport(false));
      await page.getByRole('button',{name:'Retry response'}).click();
      assert.match(await page.locator('#status').textContent(),/no longer supports/);
      assert.equal(await page.evaluate(() => window.continuationCalls.length),1);
      assert.equal(ordinary,1);
      assert.equal(await page.locator('#prompt').inputValue(),'Preserved next draft');
      await page.evaluate(() => window.setContinuationSupport(true));
    }
    await page.getByRole('button',{name:'Retry response'}).click();
    await page.getByText('Continuation retried',{exact:true}).waitFor({timeout:5000});
    const calls=await page.evaluate(() => window.continuationCalls);
    assert.equal(calls.length,2);
    assert.notEqual(calls[1].session,calls[0].session);assert.deepEqual({...calls[1],session:calls[0].session},calls[0]);
    assert.deepEqual(calls[1].reply,{kind,id:'question-exact',text:'Proceed exactly',...(kind === 'approval' ? {option_id:'option-exact'} : {})});
    assert.equal(ordinary,1);
    assert.equal(await page.locator('#prompt').inputValue(),'Preserved next draft');
    assert.equal(await page.locator('.user').count(),2);
  } finally { await closeBrowser(browser); }
});
for (const [field, value, correction, error] of [['version','obsolete','',/version is no longer/],['reasoning','high','off',/reasoning setting is unsupported/]]) test('correcting stale '+field+' clears validation and permits actual send', async () => {
  let calls=0;
  const {browser,page} = await openStudio(route => { calls++; return fulfill(route,{json:result('Valid corrected request')}); });
  try {
    await page.evaluate(({field,value}) => {
      const requested={model:'sample',version:'v1',reasoning:'', [field]:value};
      localStorage.setItem('api-agent-studio-v2',JSON.stringify({session:'saved',draft:'Retained request',settings:requested,messages:[]}));
    },{field,value});
    await reloadPage(page);
    await page.locator('#settings-nav').click();
    await page.waitForFunction(() => document.querySelector('#model option[value="sample"]'));
    assert.match(await page.locator('#model-status').textContent(),error);
    await page.locator('#close-settings').click(); await page.locator('#send').click();
    assert.equal(calls,0); assert.equal(await page.locator('#prompt').inputValue(),'Retained request');
    await page.locator('#settings-nav').click(); await page.locator('#'+field).selectOption(correction);
    assert.equal(await page.locator('#model-status').textContent(),'');
    await page.locator('#close-settings').click(); await page.locator('#send').click();
    await page.getByText('Valid corrected request',{exact:true}).waitFor();
    assert.equal(calls,1);
  } finally { await closeBrowser(browser); }
});
test('settings changes preserve actual catalog network failure', async () => {
  const {browser,page} = await openStudio(route => route.abort());
  try {
    await page.route('**/app/agent/models',route => fulfill(route,{status:503,body:'Unavailable'}));
    await reloadPage(page); await page.locator('#settings-nav').click();
    await page.waitForFunction(() => document.querySelector('#model-status').textContent.includes('Model list unavailable'));
    await page.locator('#model').dispatchEvent('change');
    assert.match(await page.locator('#model-status').textContent(),/Model list unavailable/);
    await page.locator('#version').selectOption('');
    assert.match(await page.locator('#model-status').textContent(),/Model list unavailable/);
  } finally { await closeBrowser(browser); }
});
test('deep valid Markdown falls back to literal text and keeps chat usable', async () => {
  const source='>'.repeat(8000)+' <img src="https://passive.invalid/fallback" onerror="window.fallbackPwned=1"><script>window.fallbackPwned=2</script>';
  let calls=0;
  const {browser,page} = await openStudio(route => fulfill(route,{json:result(++calls === 1 ? source : 'Next response')}));
  try {
    const errors=[], passive=[];page.on('pageerror',error => errors.push(error.message));page.on('request',request => { if(request.url().includes('passive.invalid')) passive.push(request.url()); });
    await page.locator('#prompt').fill('Deep quote'); await page.locator('#send').click();
    await page.waitForFunction(() => document.querySelector('.assistant .message-body')?.textContent.endsWith('</script>'),null,{timeout:5000});
    assert.equal(await page.locator('.assistant .message-body').textContent(),source);
    assert.equal(await page.locator('#send').isEnabled(),true);
    assert.equal(await page.locator('#new-chat').isEnabled(),true);
    assert.equal(await page.locator('#stop').isVisible(),false);
    assert.equal(await page.locator('.assistant .message-body blockquote').count(),0);
    assert.equal(await page.locator('.assistant .message-body img, .assistant .message-body script').count(),0);
    assert.equal(await page.evaluate(() => window.fallbackPwned),undefined);
    assert.deepEqual(passive,[]);
    await page.locator('#prompt').fill('Next'); await page.locator('#send').click();
    await page.getByText('Next response',{exact:true}).waitFor();
    assert.deepEqual(errors,[]);
  } finally { await closeBrowser(browser); }
});
test('Markdown preserves GFM structure and inert controls while constraining URLs and classes', async () => {
  const {browser,page} = await openStudio(route => route.abort());
  try {
    await page.evaluate(async () => {
      const {renderMarkdown}=await import('/markdown.js');
      const node=document.createElement('div');node.id='markdown-fixture';document.body.append(node);
      renderMarkdown(node,'# Heading\n\n**Strong** and *emphasis* and ~~removed~~\n\n- [x] Finished\n- [ ] Remaining\n\n> Quote\n\n```js\nconst value = 1;\n```\n\n| A | B |\n| - | - |\n| 1 | 2 |\n\n[External](https://example.test/) [Mail](mailto:user@example.test) [Unsafe](javascript:alert(1))\n\n<span class="untrusted">Text</span><input type="text"><img src="data:image/png;base64,AA" alt="Omitted image">');
    });
    const node=page.locator('#markdown-fixture');
    assert.equal(await node.locator('h1').textContent(),'Heading');
    assert.equal(await node.locator('strong').textContent(),'Strong');
    assert.equal(await node.locator('em').textContent(),'emphasis');
    assert.equal(await node.locator('del').textContent(),'removed');
    assert.equal(await node.locator('blockquote').count(),1);
    assert.equal(await node.locator('input[type="checkbox"][disabled]').count(),2);
    assert.equal(await node.locator('input[type="text"]').count(),0);
    assert.equal(await node.locator('.markdown-table[role="region"]').count(),1);
    assert.equal(await node.locator('pre[tabindex="0"] code.language-js').count(),1);
    assert.equal(await node.locator('.untrusted').count(),0);
    assert.equal(await node.locator('img').count(),0);
    assert.equal(await node.locator('a[href^="javascript:"]').count(),0);
    assert.equal(await node.locator('a[href="https://example.test/"]').getAttribute('rel'),'noopener noreferrer');
  } finally { await closeBrowser(browser); }
});
test('actual cross-origin HTTP nonce preflights catalog and chat without persistence', async () => {
  const {createServer}=await import('node:http');
  const nonce='synthetic_ephemeral_nonce_0123456789';
  const received=[];let uiOrigin;
  const api=createServer((request,response) => {
    received.push({method:request.method,path:request.url,origin:request.headers.origin,headers:request.headers['access-control-request-headers'],nonce:request.headers['x-api-agent-session']});
    response.setHeader('Access-Control-Allow-Origin',uiOrigin);
    response.setHeader('Vary','Origin');
    if (request.method === 'OPTIONS') {
      const method=request.headers['access-control-request-method'];
      const headers=request.headers['access-control-request-headers'];
      if ((method === 'GET' && headers === 'x-api-agent-session') || (method === 'POST' && headers === 'content-type,x-api-agent-session')) {
        response.setHeader('Access-Control-Allow-Methods',method);
        response.setHeader('Access-Control-Allow-Headers',headers);
        response.writeHead(204);response.end();return;
      }
      response.writeHead(403);response.end();return;
    }
    if (request.headers['x-api-agent-session'] !== nonce) {response.writeHead(403);response.end();return;}
    response.setHeader('Content-Type','application/json');
    if (request.url === '/app/agent/models') response.end(JSON.stringify({models:[wireModel(model)],default:'sample',reasoning_default:null}));
    else {let body='';request.on('data',chunk=>body+=chunk);request.on('end',()=>response.end(JSON.stringify(wireResult(result('Actual CORS response'),JSON.parse(body).session_id))));}
  });
  await new Promise(resolve => api.listen(0,'127.0.0.1',resolve));
  const {server:ui}=createStudioServer({UI_LISTENER_URL:'http://127.0.0.1:9443',AGENT_DJANGO_URL:'http://127.0.0.1:'+api.address().port,API_AGENT_UI_LAUNCH_TOKEN:'synthetic-browser-test-token',API_AGENT_UI_ACCESS_TOKEN:nonce});
  await new Promise(resolve => ui.listen(0,'127.0.0.1',resolve));uiOrigin='http://127.0.0.1:'+ui.address().port;
  const browser=await chromium.launch({headless:true,executablePath:process.env.STUDIO_BROWSER || undefined});
  try {
    const page=await browser.newPage();await startCoverage(page,{instrument:false});await page.goto(uiOrigin);
    await page.locator('#prompt').fill('Real HTTP CORS');await page.locator('#send').click();
    await page.getByText('Actual CORS response',{exact:true}).waitFor({timeout:5000});
    assert.deepEqual(received.filter(x=>x.method==='OPTIONS').map(x=>x.headers),['x-api-agent-session','content-type,x-api-agent-session']);
    assert.ok(received.filter(x=>x.method!=='OPTIONS').every(x=>x.nonce===nonce && x.origin===uiOrigin));
    assert.equal(await page.evaluate(token=>JSON.stringify({...localStorage,...sessionStorage}).includes(token),nonce),false);
    await reloadPage(page);
    assert.equal(await page.evaluate(token=>JSON.stringify({...localStorage,...sessionStorage}).includes(token),nonce),false);
  } finally {
    await closeBrowser(browser);ui.closeAllConnections();api.closeAllConnections();
    await Promise.all([new Promise(resolve=>ui.close(resolve)),new Promise(resolve=>api.close(resolve))]);
  }
});
for (const config of [null, [], {}, {apiOrigin:'file:///invalid'}, {apiOrigin:'http://user:pass@localhost'}, {apiOrigin:'http://localhost/path'}, {apiOrigin:'http://localhost/?token=x'}, {apiOrigin:'http://localhost/#token'}, {apiOrigin:32}, {apiOrigin:'http://localhost',extra:true}, {apiOrigin:'http://localhost',apiHeaders:null}, {apiOrigin:'http://localhost',apiHeaders:[]}, {apiOrigin:'http://localhost',apiHeaders:{}}, {apiOrigin:'http://localhost',apiHeaders:{Authorization:'forbidden'}}, {apiOrigin:'http://localhost',apiHeaders:{'X-API-Agent-Session':'short'}}, {apiOrigin:'http://localhost',apiHeaders:{'X-API-Agent-Session':'a'.repeat(32),Authorization:'forbidden'}}]) test('rejects malformed or unauthorized config '+JSON.stringify(config), async () => {
  const {browser,page}=await openStudio(route=>route.abort());
  try {
    await page.route('**/config.json',route=>fulfill(route,{json:config}));
    await reloadPage(page);await page.locator('#settings-nav').click();
    await page.waitForFunction(()=>document.querySelector('#model-status').textContent.includes('configuration could not be loaded'));
    assert.equal(await page.locator('.message').count(),0);
  } finally {await closeBrowser(browser);}
});
test('Markdown handles malformed links and local images with explicit safe DOM defaults', async () => {
  const {browser,page}=await openStudio(route=>route.abort());
  try {
    await page.evaluate(async()=>{
      const {renderMarkdown}=await import('/markdown.js');const node=document.createElement('div');node.id='markdown-url-fixture';document.body.append(node);
      renderMarkdown(node,'[Local](/local) [Malformed](http://[) [FTP](ftp://example.test/file)\n\n<p class="untrusted">Paragraph</p><code class="unsafe classes">Code</code><img src="/local-image" alt="Local"><img src="http://[" alt="Invalid"><img src="https://remote.invalid/pixel" alt=""><img alt="Missing source"><input type="checkbox">');
    });
    const node=page.locator('#markdown-url-fixture');
    assert.equal(await node.locator('a[href="/local"]').getAttribute('target'),null);
    assert.equal(await node.locator('a[href="http://["],a[href^="ftp:"]').count(),0);
    assert.equal(await node.locator('.untrusted,.unsafe').count(),0);
    assert.equal(await node.locator('img').count(),2);
    assert.equal(await node.locator('img[src="/local-image"]').getAttribute('loading'),'lazy');
    assert.equal(await node.locator('img[src="/local-image"]').getAttribute('referrerpolicy'),'no-referrer');
    assert.equal(await node.getByText('View image',{exact:true}).count(),1);
    assert.equal(await node.locator('input').getAttribute('aria-label'),'Task status');
  } finally {await closeBrowser(browser);}
});
test('appearance storage updates reach original tab and write failures stay visible', async () => {
  const {browser,context,page}=await openStudio(route=>route.abort());
  try {
    const second=await context.newPage();await startCoverage(second);await second.goto(host);
    await page.evaluate(()=>window.addEventListener('storage',event=>{if(event.key==='unrelated-key')window.unrelatedStorageSeen=true;}));
    await second.evaluate(()=>localStorage.setItem('unrelated-key','value'));
    await page.waitForFunction(()=>window.unrelatedStorageSeen);
    await second.evaluate(()=>localStorage.setItem('api-agent-studio-appearance','dark'));
    await page.waitForFunction(()=>document.querySelector('#dark-theme').media==='all');
    await second.evaluate(()=>localStorage.clear());
    await page.waitForFunction(()=>document.querySelector('#dark-theme').media==='(prefers-color-scheme: dark)');
    await page.locator('#settings-nav').click();await page.locator('#appearance').selectOption('light');
    await page.emulateMedia({colorScheme:'dark'});
    await page.waitForFunction(()=>document.querySelector('#system-mode').textContent==='dark');
    assert.equal(await page.locator('#dark-theme').getAttribute('media'),'not all');
    await page.evaluate(()=>{Storage.prototype.setItem=()=>{throw Error('Synthetic write failure');};});
    await page.locator('#appearance').selectOption('dark');
    assert.match(await page.locator('#appearance-status').textContent(),/could not save/);
    assert.equal(await page.locator('#appearance-status').isVisible(),true);
    await page.locator('#appearance').evaluate(select=>{select.value='light';select.dispatchEvent(new Event('change'));select.value='dark';select.dispatchEvent(new Event('change'));});
    await page.waitForFunction(()=>!document.documentElement.hasAttribute('data-appearance-changing'));
    assert.equal(await page.locator('#dark-theme').getAttribute('media'),'all');
  } finally {await closeBrowser(browser);}
});
test('allowlisted in-memory header reaches fixture requests and never enters stored state', async () => {
  const nonce='fixture_session_nonce_0123456789012345', received=[];
  const {browser,page}=await openStudio(route=>{received.push(route.request().headers());return fulfill(route,{json:result('Header fixture response')});});
  try {
    await page.waitForFunction(()=>document.querySelector('#model option[value="sample"]'));
    await page.route('**/config.json',route=>fulfill(route,{json:{apiOrigin:'http://127.0.0.1:1',apiHeaders:{'X-API-Agent-Session':nonce}}}));
    await page.route('**/app/agent/models',route=>{received.push(route.request().headers());return fulfill(route,{json:{models:[model],default:'sample'}});});
    await reloadPage(page);
    await page.locator('#prompt').fill('In-memory nonce');await page.locator('#send').click();
    await page.getByText('Header fixture response',{exact:true}).waitFor();
    assert.equal(received.length,2);
    assert.ok(received.every(headers=>headers['x-api-agent-session']===nonce));
    assert.equal(await page.evaluate(token=>JSON.stringify({...localStorage,...sessionStorage}).includes(token),nonce),false);
  } finally {await closeBrowser(browser);}
});
test('composer validation, starter keyboard send, follow inputs and dialog backdrop preserve intent', async()=>{
  const {browser,page}=await openStudio(route=>fulfill(route,{json:result(Array.from({length:35},(_,i)=>'Line '+i).join('\n\n'))}));
  try{
    await page.locator('#send').click();assert.equal(await page.locator('#prompt').getAttribute('aria-invalid'),'true');
    await page.locator('[data-prompt]').first().click();assert.equal(await page.locator('#prompt').getAttribute('aria-invalid'),null);
    assert.equal(await page.locator('#prompt').inputValue(),'Help me explore an API');
    await page.locator('#prompt').press('Meta+Enter');await page.getByText('Line 34',{exact:true}).waitFor();
    await page.locator('#follow-latest').uncheck();assert.equal(await page.locator('#follow-latest').isChecked(),false);
    await page.locator('#follow-latest').check();assert.equal(await page.locator('#follow-latest').isChecked(),true);
    await page.locator('#follow-latest').uncheck();assert.equal(await page.locator('#follow-latest').isChecked(),false);
    await page.locator('#follow-latest').check();
    await page.locator('#conversation-scroll').dispatchEvent('wheel',{deltaY:10});assert.equal(await page.locator('#follow-latest').isChecked(),true);
    await page.locator('#conversation-scroll').focus();await page.locator('#conversation-scroll').press('ArrowUp');assert.equal(await page.locator('#follow-latest').isChecked(),false);
    await page.locator('#latest-response').click();await page.locator('#conversation-scroll').press('ArrowDown');assert.equal(await page.locator('#follow-latest').isChecked(),true);
    await page.locator('#conversation-scroll').dispatchEvent('touchstart');assert.equal(await page.locator('#follow-latest').isChecked(),false);
    await page.locator('#settings-nav').click();await page.mouse.click(1,1);assert.equal(await page.locator('#settings').isVisible(),false);
    await page.locator('#new-chat').click();await page.locator('#undo-new-chat').click();
    assert.equal(await page.locator('#prompt').inputValue(),'');
    await page.locator('#new-chat').click();await page.locator('#prompt').fill('Draft only');await page.locator('#new-chat').click();await page.locator('#undo-new-chat').click();
    assert.equal(await page.locator('#prompt').inputValue(),'Draft only');
  }finally{await closeBrowser(browser);}
});
test('malformed persisted questions are rejected without rendering or submitting them',async()=>{
  const {browser,page}=await openStudio(route=>route.abort());
  try{
    const questions=[{}, {id:32,question:'Q',kind:'clarification',options:[]},{id:'q',question:32,kind:'clarification',options:[]},{id:'q',question:'Q',kind:'other',options:[]},{id:'q',question:'Q',kind:'clarification',options:null},{id:'q',question:'Q',kind:'clarification',options:[32]},{id:'q',question:'Q',kind:'approval',options:[null]},{id:'q',question:'Q',kind:'approval',options:[{}]},{id:'q',question:'Q',kind:'approval',options:[{id:'option',label:32}]}];
    for(const question of questions){
      await page.evaluate(question=>{const settings={model:'sample',version:'v1',reasoning:''};localStorage.setItem('api-agent-studio-v2',JSON.stringify({session:'saved',draft:'draft',settings,messages:[{id:'a',role:'assistant',text:'unsafe state',requested:settings,question}]}));},question);
      await reloadPage(page);
      assert.equal(await page.locator('.message').count(),0);
      assert.equal(await page.locator('#storage-warning').isVisible(),true);
    }
  }finally{await closeBrowser(browser);}
});
for(const kind of ['clarification','approval'])test('persisted '+kind+' resumes safely and rejects invalid typed approval',async()=>{
  const {browser,page}=await openStudio(route=>route.abort());
  try{
    await page.route('**/transport.js',async route=>{
      const response=await route.fetch();const body=(await response.text()).replace('export const supportsContinuation = false;','export const supportsContinuation = true;').replace(/export async function submitContinuation\(\) \{[\s\S]*?\n\}/,`export async function submitContinuation(input){window.persistedReply=input.reply;return new Response(JSON.stringify({...${JSON.stringify(wireResult(result('Resumed answer'),'placeholder'))},session_id:input.session}),{headers:{'content-type':'application/json'}});}`);
      await fulfill(route,{response,body});
    });
    await page.evaluate(kind=>{const settings={model:'sample',version:'v1',reasoning:''};localStorage.setItem('api-agent-studio-v2',JSON.stringify({session:'saved',draft:'Edited answer',settings,reply:{messageId:'a',savedDraft:'Earlier draft'},messages:[{id:'a',role:'assistant',text:'Question',requested:settings,question:{id:'persisted-q',question:'Your choice?',kind,options:kind==='clarification'?['Suggested']:[{id:'accept',label:'Accept exact'}]}}]}));},kind);
    await reloadPage(page);await page.locator('legend').waitFor();await page.waitForFunction(()=>document.querySelector('#model option[value="sample"]'));
    if(kind==='approval'){
      await page.locator('#send').click();assert.match(await page.locator('#status').textContent(),/explicit approval option/);assert.equal(await page.evaluate(()=>window.persistedReply),undefined);
      await page.getByRole('button',{name:'Accept exact',exact:true}).click();
    }else{await page.getByRole('button',{name:'Choose your own answer below…'}).click();assert.equal(await page.locator('#prompt').inputValue(),'Edited answer');await page.locator('#send').click();}
    await page.getByText('Resumed answer',{exact:true}).waitFor();
    assert.equal(await page.locator('#prompt').inputValue(),'Earlier draft');
    assert.equal((await page.evaluate(()=>window.persistedReply)).id,'persisted-q');
  }finally{await closeBrowser(browser);}
});
test('orphaned persisted reply and corrupt undo cache preserve the current draft',async()=>{
  const {browser,page}=await openStudio(route=>route.abort());
  try{
    for(const reply of [{messageId:'missing',savedDraft:'Earlier'},{messageId:'a',savedDraft:32}]){
      await page.evaluate(reply=>{const settings={model:'sample',version:'v1',reasoning:''};localStorage.setItem('api-agent-studio-v2',JSON.stringify({session:'saved',draft:'Current draft',settings,reply,messages:[{id:'a',role:'assistant',text:'Current conversation',requested:settings,question:{id:'q',question:'Q',kind:'clarification',options:[]}}]}));localStorage.setItem('api-agent-studio-v2:previous','{');},reply);
      await reloadPage(page);assert.equal(await page.locator('#reply-context').isVisible(),false);assert.equal(await page.locator('#prompt').inputValue(),'Current draft');
    }
  }finally{await closeBrowser(browser);}
});
test('system appearance change while the head is loading safely precedes body controls',async()=>{
  const {browser,page}=await openStudio(route=>route.abort(),()=>{
    const original=window.matchMedia.bind(window);
    window.matchMedia=query=>{
      const media=original(query),add=media.addEventListener.bind(media);
      media.addEventListener=(kind,callback,...rest)=>{add(kind,callback,...rest);if(kind==='change')queueMicrotask(()=>media.dispatchEvent(new Event('change')));};
      return media;
    };
  });
  try{
    const errors=[];page.on('pageerror',error=>errors.push(error.message));
    await page.locator('#settings-nav').click();
    assert.equal(await page.locator('#system-mode').textContent(),'light');
    assert.equal(await page.locator('#dark-theme').getAttribute('media'),'(prefers-color-scheme: dark)');
    assert.deepEqual(errors,[]);
  }finally{await closeBrowser(browser);}
});
test('queued retry and toolbar actions cannot duplicate an active request or lose its draft',async()=>{
  const ready=Promise.withResolvers();let calls=0;
  const {browser,page}=await openStudio(route=>{calls++;if(calls===1)return fulfill(route,{status:503,body:'Retry me'});ready.resolve(route);return new Promise(()=>{});});
  try{
    await page.locator('#prompt').fill('Original');await page.locator('#send').click();await page.getByRole('button',{name:'Retry response'}).waitFor();
    const oldRetry=await page.getByRole('button',{name:'Retry response'}).elementHandle();
    await page.getByRole('button',{name:'Retry response'}).click();const pending=await ready.promise;
    await page.locator('#prompt').fill('Next draft');
    await oldRetry.evaluate(node=>node.dispatchEvent(new MouseEvent('click')));
    await page.locator('#prompt').press('Control+Enter');
    await page.locator('#new-chat').dispatchEvent('click');await page.locator('#undo-new-chat').dispatchEvent('click');
    assert.equal(calls,2);assert.equal(await page.locator('#prompt').inputValue(),'Next draft');assert.equal(await page.locator('.user').count(),1);
    await fulfill(pending,{json:result('Queued retry done')});await page.getByText('Queued retry done',{exact:true}).waitFor();
    await page.locator('#stop').dispatchEvent('click');await page.locator('#undo-new-chat').dispatchEvent('click');
    assert.equal(await page.locator('#prompt').inputValue(),'Next draft');assert.equal(await page.locator('.user').count(),1);
  }finally{await closeBrowser(browser);}
});
test('queued old clarification option cannot overwrite the restored draft during continuation',async()=>{
  const {browser,page}=await openStudio(route=>fulfill(route,{contentType:'text/event-stream',body:'data: '+JSON.stringify({kind:'clarification',payload:{id:'q',question:'Choose',options:['Suggested']}})+'\n\n'}));
  try{
    await page.route('**/transport.js',async route=>{const response=await route.fetch();const body=(await response.text()).replace('export const supportsContinuation = false;','export const supportsContinuation = true;').replace(/export async function submitContinuation\(\) \{[\s\S]*?\n\}/,`export async function submitContinuation(input){window.continuationSession=input.session;window.continuationCount=(window.continuationCount||0)+1;return new Promise(resolve=>{window.finishContinuation=()=>resolve(new Response(JSON.stringify({...${JSON.stringify(wireResult(result('Continued'),'placeholder'))},session_id:window.continuationSession}),{headers:{'content-type':'application/json'}}));});}`);await fulfill(route,{response,body});});
    await reloadPage(page);await page.locator('#prompt').fill('Ask');await page.locator('#send').click();await page.getByRole('button',{name:'Suggested',exact:true}).waitFor();
    const oldOption=await page.getByRole('button',{name:'Suggested',exact:true}).elementHandle();
    const oldCustom=await page.getByRole('button',{name:'Choose your own answer below…'}).elementHandle();
    await page.getByRole('button',{name:'Suggested',exact:true}).click();await page.locator('#send').click();
    await page.waitForFunction(()=>window.finishContinuation);await page.locator('#prompt').fill('Suggested');
    await oldCustom.evaluate(node=>node.dispatchEvent(new MouseEvent('click')));
    assert.equal(await page.locator('#prompt').inputValue(),'Suggested');
    await oldOption.evaluate(node=>node.dispatchEvent(new MouseEvent('click')));
    assert.equal(await page.locator('#prompt').inputValue(),'Suggested');assert.equal(await page.evaluate(()=>window.continuationCount),1);
    await page.evaluate(()=>window.finishContinuation());await page.getByText('Continued',{exact:true}).waitFor();
  }finally{await closeBrowser(browser);}
});
test('progressive HTTP responses preserve metadata focus, finish Stop focus, and reject late text after abort',async()=>{
  const {createServer}=await import('node:http');let uiOrigin, ready=Promise.withResolvers();
  const api=createServer((request,response)=>{
    response.setHeader('Access-Control-Allow-Origin',uiOrigin);
    if(request.method==='OPTIONS'){response.setHeader('Access-Control-Allow-Methods','POST');response.setHeader('Access-Control-Allow-Headers','content-type');response.writeHead(204);response.end();return;}
    if(request.url==='/app/agent/models'){response.setHeader('Content-Type','application/json');response.end(JSON.stringify({models:[wireModel(model)],default:'sample',reasoning_default:null}));return;}
    let body='';request.on('data',chunk=>body+=chunk);request.on('end',()=>{const id=JSON.parse(body).session_id;if(request.url.endsWith('/cancel')||request.url.endsWith('/heartbeat')){response.setHeader('Content-Type','application/json');response.end(JSON.stringify({session_id:id,status:'running'}));return;}response.sessionId=id;response.setHeader('Content-Type','text/event-stream');response.write(sessionFrame(id)+frame('token',{content:'Initial draft'}));response.on('error',()=>{});ready.resolve(response);});
  });
  await new Promise(resolve=>api.listen(0,'127.0.0.1',resolve));
  const {server:ui}=createStudioServer({UI_LISTENER_URL:'http://127.0.0.1:9443',AGENT_DJANGO_URL:'http://127.0.0.1:'+api.address().port,API_AGENT_UI_LAUNCH_TOKEN:'synthetic-browser-test-token'});
  await new Promise(resolve=>ui.listen(0,'127.0.0.1',resolve));uiOrigin='http://127.0.0.1:'+ui.address().port;
  const browser=await chromium.launch({headless:true,executablePath:process.env.STUDIO_BROWSER||undefined});
  try{
    const page=await browser.newPage();await startCoverage(page);await page.goto(uiOrigin);await page.waitForFunction(()=>document.querySelector('#model option[value="sample"]'));
    await page.locator('#prompt').fill('First');await page.locator('#send').click();const first=await ready.promise;await page.getByText('Initial draft',{exact:true}).waitFor();
    await page.locator('.assistant summary').press('Enter');assert.equal(await page.locator('.assistant details').getAttribute('open'),'');
    first.end(finishFrames(first.sessionId,result('First final')));await page.getByText('First final',{exact:true}).waitFor();
    assert.equal(await page.evaluate(()=>document.activeElement.tagName),'SUMMARY');assert.equal(await page.locator('.assistant details').getAttribute('open'),'');
    ready=Promise.withResolvers();await page.locator('#prompt').fill('Second');await page.locator('#send').click();const second=await ready.promise;await page.getByText('Initial draft',{exact:true}).waitFor();await page.locator('#stop').focus();
    second.end(finishFrames(second.sessionId,result('Second final')));await page.getByText('Second final',{exact:true}).waitFor();
    assert.equal(await page.evaluate(()=>document.activeElement.id),'prompt');
    ready=Promise.withResolvers();await page.locator('#prompt').fill('Third');await page.locator('#send').click();const third=await ready.promise;await page.getByText('Initial draft',{exact:true}).waitFor();
    const closed=new Promise(resolve=>third.once('close',resolve));await page.locator('#stop').click();await closed;
    third.end(finishFrames(third.sessionId,result('Late forbidden final')));
    await page.getByRole('button',{name:'Retry response'}).waitFor();assert.equal(await page.getByText('Late forbidden final',{exact:true}).count(),0);assert.match(await page.locator('.error-note').textContent(),/stopped/);
  }finally{await closeBrowser(browser);ui.closeAllConnections();api.closeAllConnections();await Promise.all([new Promise(resolve=>ui.close(resolve)),new Promise(resolve=>api.close(resolve))]);}
});
test('configuration HTTP failure blocks sending and keeps the draft',async()=>{
  const {browser,page}=await openStudio(route=>route.abort());
  try{await page.route('**/config.json',route=>fulfill(route,{status:503,body:'Unavailable'}));await reloadPage(page);await page.locator('#prompt').fill('Keep draft');await page.locator('#send').click();assert.equal(await page.locator('#prompt').inputValue(),'Keep draft');assert.equal(await page.locator('.message').count(),0);await page.locator('#settings-nav').click();assert.match(await page.locator('#model-status').textContent(),/configuration could not be loaded/);}finally{await closeBrowser(browser);}
});
test('empty catalog communicates unavailable choices without inventing a default',async()=>{
  const {browser,page}=await openStudio(route=>route.abort());
  try{await page.route('**/app/agent/models',route=>fulfill(route,{json:{models:[],default:null}}));await reloadPage(page);await page.locator('#settings-nav').click();await page.waitForFunction(()=>document.querySelector('#model-status').textContent.includes('No model choices'));const errors=[];page.on('pageerror',error=>errors.push(error.message));assert.equal(await page.locator('#backend-default').isDisabled(),true);await page.locator('#backend-default').dispatchEvent('click');assert.deepEqual(errors,[]);assert.equal(await page.evaluate(()=>JSON.parse(localStorage.getItem('api-agent-studio-v2')).settings.model),'');await page.locator('#close-settings').click();await page.locator('#prompt').fill('Keep draft');await page.locator('#send').click();assert.equal(await page.locator('.message').count(),0);assert.equal(await page.locator('#prompt').inputValue(),'Keep draft');}finally{await closeBrowser(browser);}
});
test('unversioned provider model succeeds without manufacturing a version',async()=>{
  const {browser,page}=await openStudio(route=>fulfill(route,{json:result('Unversioned response')}));
  try{await page.route('**/app/agent/models',route=>fulfill(route,{json:{models:[{...model,version:null}],default:'sample'}}));await reloadPage(page);await page.waitForFunction(()=>document.querySelector('#model option[value="sample"]'));await page.locator('#prompt').fill('Hello');await page.locator('#send').click();await page.getByText('Unversioned response',{exact:true}).waitFor();assert.match(await page.locator('.assistant .metadata-grid').textContent(),/Reported versionNot reported/);}finally{await closeBrowser(browser);}
});
test('network connection failure is incomplete and retryable',async()=>{
  const {browser,page}=await openStudio(route=>route.abort('failed'));
  try{await page.locator('#prompt').fill('Connect');await page.locator('#send').click();await page.getByRole('button',{name:'Retry response'}).waitFor();assert.match(await page.locator('.error-note').textContent(),/Couldn’t connect/);}finally{await closeBrowser(browser);}
});
test('corrupt undo schema or a valid older cache cannot replace the current conversation',async()=>{
  const {browser,page}=await openStudio(route=>route.abort());
  try{for(const previous of [{},{session:'previous',draft:'',settings:{model:'sample',version:'v1',reasoning:''},messages:[]}]){await page.evaluate(previous=>{const settings={model:'sample',version:'v1',reasoning:''};localStorage.setItem('api-agent-studio-v2',JSON.stringify({session:'current',draft:'',settings,messages:[{id:'u',role:'user',text:'Current message',requested:settings}]}));localStorage.setItem('api-agent-studio-v2:previous',JSON.stringify(previous));},previous);await reloadPage(page);assert.equal(await page.getByText('Current message',{exact:true}).count(),1);assert.equal(await page.locator('#undo-new-chat').isVisible(),false);}}finally{await closeBrowser(browser);}
});
test('new chat on an already empty conversation does not create an undo entry',async()=>{
  const {browser,page}=await openStudio(route=>route.abort());
  try{await page.locator('#new-chat').click();assert.equal(await page.locator('#undo-new-chat').isVisible(),false);assert.equal(await page.locator('#status').textContent(),'A new conversation is ready.');}finally{await closeBrowser(browser);}
});
test('retry refuses an unavailable original model without changing its saved text',async()=>{
  const {browser,page}=await openStudio(route=>route.abort());
  try{await page.evaluate(()=>{const requested={model:'removed',version:'old',reasoning:''},settings={model:'sample',version:'v1',reasoning:''};localStorage.setItem('api-agent-studio-v2',JSON.stringify({session:'saved',draft:'Next draft',settings,messages:[{id:'u',role:'user',text:'Original request',requested},{id:'a',role:'assistant',text:'Partial',request:'Original request',requested,error:'Earlier failure'}]}));});await reloadPage(page);await page.getByRole('button',{name:'Retry response'}).click();assert.match(await page.locator('#status').textContent(),/available model/);assert.equal(await page.locator('.assistant .message-body p').textContent(),'Partial');assert.equal(await page.locator('#prompt').inputValue(),'Next draft');}finally{await closeBrowser(browser);}
});
test('persisted question with an unavailable original model cannot resolve or consume its draft',async()=>{
  const {browser,page}=await openStudio(route=>route.abort());
  try{
    await page.route('**/transport.js',async route=>{const response=await route.fetch();await fulfill(route,{response,body:(await response.text()).replace('export const supportsContinuation = false;','export const supportsContinuation = true;')});});
    await page.evaluate(()=>{const settings={model:'sample',version:'v1',reasoning:''},requested={model:'removed',version:'old',reasoning:''};localStorage.setItem('api-agent-studio-v2',JSON.stringify({session:'saved',draft:'Edited answer',settings,reply:{messageId:'a',savedDraft:'Earlier draft'},messages:[{id:'a',role:'assistant',text:'Old question',requested,question:{id:'q',question:'Your choice?',kind:'clarification',options:['Suggested']}}]}));});
    await reloadPage(page);await page.waitForFunction(()=>document.querySelector('#model option[value="sample"]'));await page.locator('#send').click();
    assert.match(await page.locator('#status').textContent(),/question’s model selection is no longer available/);assert.equal(await page.locator('#prompt').inputValue(),'Edited answer');assert.equal(await page.locator('#reply-context').isVisible(),true);
  }finally{await closeBrowser(browser);}
});
test('continuation adapter abort is honestly interrupted and retains exact reply for retry',async()=>{
  const {browser,page}=await openStudio(route=>fulfill(route,{contentType:'text/event-stream',body:'data: '+JSON.stringify({kind:'clarification',payload:{id:'q',question:'Choose',options:['Suggested']}})+'\n\n'}));
  try{
    await page.route('**/transport.js',async route=>{const response=await route.fetch();const body=(await response.text()).replace('export const supportsContinuation = false;','export const supportsContinuation = true;').replace(/export async function submitContinuation\(\) \{[\s\S]*?\n\}/,`export async function submitContinuation(){throw new DOMException('External interruption','AbortError');}`);await fulfill(route,{response,body});});
    await reloadPage(page);await page.locator('#prompt').fill('Ask');await page.locator('#send').click();await page.getByRole('button',{name:'Suggested',exact:true}).click();await page.locator('#send').click();
    await page.getByRole('button',{name:'Retry response'}).waitFor();assert.match(await page.locator('.error-note').textContent(),/connection was interrupted/);
  }finally{await closeBrowser(browser);}
});
test('repeated adapter metadata cannot overwrite the first reported identity',async()=>{
  const {browser,page}=await openStudio(route=>fulfill(route,{json:result('Metadata response')}));
  try{
    await page.route('**/transport.js',async route=>{
      const response=await route.fetch();let body=await response.text();body=body.replace('export async function readResponse(response, callbacks = {})','async function baseReadResponse(response, callbacks = {})');
      body+=`\nexport async function readResponse(response,callbacks){callbacks.onMetadata({model:'first-model',version:'first-version',reasoning:'low',actual_model:'first-model',service_tier:'first-tier',serving_revision:'first-revision'});callbacks.onMetadata({model:'later-model',version:'later-version',reasoning:'high',actual_model:'later-model',service_tier:'later-tier',serving_revision:'later-revision'});return baseReadResponse(response,callbacks);}\n`;
      await fulfill(route,{response,body});
    });
    await reloadPage(page);await page.locator('#prompt').fill('Report identity');await page.locator('#send').click();await page.getByText('Metadata response',{exact:true}).waitFor();
    const metadata=await page.locator('.assistant .metadata-grid').textContent();assert.match(metadata,/first-model/);assert.match(metadata,/first-version/);assert.match(metadata,/first-revision/);assert.doesNotMatch(metadata,/later-/);
  }finally{await closeBrowser(browser);}
});
test('catalog raw cap cancels real HTTP before EOF and preserves the unsent draft',async()=>{
  const {createServer}=await import('node:http');let uiOrigin,posts=0;const closed=Promise.withResolvers();
  const api=createServer((request,response)=>{
    response.setHeader('Access-Control-Allow-Origin',uiOrigin);
    if(request.url==='/app/agent/models'){response.setHeader('Content-Type','application/json');response.once('close',()=>closed.resolve());response.on('error',()=>{});response.write(' '.repeat(131073)+JSON.stringify({models:[wireModel(model)],default:'sample',reasoning_default:null}));return;}
    posts++;response.writeHead(500);response.end();
  });
  await new Promise(resolve=>api.listen(0,'127.0.0.1',resolve));
  const {server:ui}=createStudioServer({UI_LISTENER_URL:'http://127.0.0.1:9443',AGENT_DJANGO_URL:'http://127.0.0.1:'+api.address().port,API_AGENT_UI_LAUNCH_TOKEN:'synthetic-browser-test-token'});
  await new Promise(resolve=>ui.listen(0,'127.0.0.1',resolve));uiOrigin='http://127.0.0.1:'+ui.address().port;
  const browser=await chromium.launch({headless:true,executablePath:process.env.STUDIO_BROWSER||undefined});
  try{
    const page=await browser.newPage();await startCoverage(page);await page.goto(uiOrigin);
    await page.locator('#prompt').fill('Retained unsent draft');await page.locator('#settings-nav').click();
    await page.waitForFunction(()=>document.querySelector('#model-status').textContent.includes('Model list unavailable'),null,{timeout:5000});
    await closed.promise;await page.locator('#close-settings').click();await page.locator('#send').click();
    assert.equal(await page.locator('#prompt').inputValue(),'Retained unsent draft');assert.equal(await page.locator('.message').count(),0);assert.equal(posts,0);
  }finally{await closeBrowser(browser);ui.closeAllConnections();api.closeAllConnections();await Promise.all([new Promise(resolve=>ui.close(resolve)),new Promise(resolve=>api.close(resolve))]);}
});
test('Stop during asynchronous request admission prevents invoking the transport',async()=>{
  let posts=0;const {browser,page}=await openStudio(route=>{posts++;return fulfill(route,{json:result('Unexpected')});});
  try{
    await page.waitForFunction(()=>document.querySelector('#model option[value="sample"]'));await page.locator('#prompt').fill('Stop before dispatch');
    await page.evaluate(()=>{const original=window.fetch.bind(window);window.fetch=(url,options)=>{if(String(url).includes('/chat/'))window.chatTransportCalled=true;return original(url,options);};document.querySelector('#composer').requestSubmit();document.querySelector('#stop').click();});
    await page.getByRole('button',{name:'Retry response'}).waitFor();assert.match(await page.locator('.error-note').textContent(),/stopped/);assert.equal(await page.evaluate(()=>window.chatTransportCalled),undefined);assert.equal(posts,0);
  }finally{await closeBrowser(browser);}
});
test('finite padded catalog rejects the raw bytes even though its semantic JSON is valid',async()=>{
  const {createServer}=await import('node:http');let uiOrigin,posts=0;
  const catalog={models:[model],default:'sample'},body=' '.repeat(131073)+JSON.stringify(catalog);
  assert.ok(Buffer.byteLength(body)>131072);assert.ok(Buffer.byteLength(JSON.stringify(catalog))<131072);
  const api=createServer((request,response)=>{
    response.setHeader('Access-Control-Allow-Origin',uiOrigin);
    if(request.url==='/app/agent/models'){response.setHeader('Content-Type','application/json');response.end(body);return;}
    posts++;response.writeHead(500);response.end();
  });
  await new Promise(resolve=>api.listen(0,'127.0.0.1',resolve));
  const {server:ui}=createStudioServer({UI_LISTENER_URL:'http://127.0.0.1:9443',AGENT_DJANGO_URL:'http://127.0.0.1:'+api.address().port,API_AGENT_UI_LAUNCH_TOKEN:'synthetic-browser-test-token'});
  await new Promise(resolve=>ui.listen(0,'127.0.0.1',resolve));uiOrigin='http://127.0.0.1:'+ui.address().port;
  const browser=await chromium.launch({headless:true,executablePath:process.env.STUDIO_BROWSER||undefined});
  try{
    const page=await browser.newPage();await startCoverage(page);await page.goto(uiOrigin);
    await page.waitForFunction(()=>document.querySelector('#model-status').textContent.includes('Model list unavailable') || document.querySelector('#model option[value="sample"]'),null,{timeout:5000});
    assert.equal(await page.locator('#model option[value="sample"]').count(),0);
    assert.match(await page.locator('#model-status').textContent(),/Model list unavailable/);
    await page.locator('#prompt').fill('Finite cap retained draft');await page.locator('#send').click();
    assert.equal(await page.locator('#prompt').inputValue(),'Finite cap retained draft');assert.equal(await page.locator('.message').count(),0);assert.equal(posts,0);
  }finally{await closeBrowser(browser);ui.closeAllConnections();api.closeAllConnections();await Promise.all([new Promise(resolve=>ui.close(resolve)),new Promise(resolve=>api.close(resolve))]);}
});
test('actual HTTP heartbeat survives hidden state, Stop and persisted pagehide cancel with admission',async()=>{
  const {createServer}=await import('node:http');const nonce='synthetic_lifecycle_nonce_0123456789';let uiOrigin;
  const calls=[];const streams=[];let rejectHeartbeat=false, completedHeartbeat=false, malformedHeartbeat='', failCancel=false, cancelFailures=0;
  const api=createServer((request,response)=>{
    response.setHeader('Access-Control-Allow-Origin',uiOrigin);
    if(request.method==='OPTIONS'){response.setHeader('Access-Control-Allow-Methods','GET, POST');response.setHeader('Access-Control-Allow-Headers','content-type,x-api-agent-session');response.writeHead(204);response.end();return;}
    if(request.url.endsWith('/models')){response.setHeader('Content-Type','application/json');response.end(JSON.stringify({models:[wireModel(model)],default:'sample',reasoning_default:null}));return;}
    let body='';request.on('data',chunk=>body+=chunk);request.on('end',()=>{
      const {session_id}=JSON.parse(body);calls.push({path:request.url,session_id,nonce:request.headers['x-api-agent-session'],origin:request.headers.origin});
      if(request.url.endsWith('/stream')){response.setHeader('Content-Type','text/event-stream');response.write(sessionFrame(session_id)+frame('token',{content:'Still running'}));streams.push(response);return;}
      if(request.url.endsWith('/cancel')&&failCancel){cancelFailures++;response.writeHead(503);response.end('Cancellation unavailable');return;}
      let value={session_id,status:request.url.endsWith('/cancel')?'cancelled':rejectHeartbeat?'unknown':completedHeartbeat?'completed':'running'};
      if(request.url.endsWith('/heartbeat')){
        if(malformedHeartbeat==='null')value=null;
        if(malformedHeartbeat==='session')value.session_id='foreign';
        if(malformedHeartbeat==='status')value.status='unexpected';
        if(malformedHeartbeat==='extra')value.extra=true;
      }
      response.setHeader('Content-Type','application/json');response.end(JSON.stringify(value));
    });
  });
  await new Promise(resolve=>api.listen(0,'127.0.0.1',resolve));
  const {server:ui}=createStudioServer({UI_LISTENER_URL:'http://127.0.0.1:9443',AGENT_DJANGO_URL:'http://127.0.0.1:'+api.address().port,API_AGENT_UI_LAUNCH_TOKEN:'synthetic-browser-test-token',API_AGENT_UI_ACCESS_TOKEN:nonce});
  await new Promise(resolve=>ui.listen(0,'127.0.0.1',resolve));uiOrigin='http://127.0.0.1:'+ui.address().port;
  const browser=await chromium.launch({headless:true,executablePath:process.env.STUDIO_BROWSER||undefined});
  try {
    const page=await browser.newPage();await startCoverage(page);await page.goto(uiOrigin);await page.waitForFunction(()=>document.querySelector('#model').value==='sample');await page.clock.install();
    await page.locator('#prompt').fill('First');await page.locator('#send').click();await page.getByText('Still running',{exact:true}).waitFor();
    await page.evaluate(()=>{Object.defineProperty(document,'hidden',{configurable:true,value:true});document.dispatchEvent(new Event('visibilitychange'));});
    await page.clock.fastForward(10001);await page.waitForTimeout(100);
    assert.equal(calls.filter(x=>x.path.endsWith('/heartbeat')).length,1);assert.equal(calls.filter(x=>x.path.endsWith('/cancel')).length,0);
    await page.locator('#stop').click();await page.getByRole('button',{name:'Retry response'}).waitFor();
    await page.waitForTimeout(100);assert.equal(calls.filter(x=>x.path.endsWith('/cancel')).length,1);
    const first=calls[0].session_id;await page.getByRole('button',{name:'Retry response'}).click();await page.getByText('Still running',{exact:true}).waitFor();
    await page.evaluate(()=>window.dispatchEvent(new PageTransitionEvent('pagehide',{persisted:true})));await page.getByRole('button',{name:'Retry response'}).waitFor();await page.waitForTimeout(100);
    assert.equal(calls.filter(x=>x.path.endsWith('/cancel')).length,2);assert.notEqual(calls.filter(x=>x.path.endsWith('/stream'))[1].session_id,first);
    const heartbeatCount=calls.filter(x=>x.path.endsWith('/heartbeat')).length;await page.clock.fastForward(30001);await page.waitForTimeout(100);assert.equal(calls.filter(x=>x.path.endsWith('/heartbeat')).length,heartbeatCount);
    completedHeartbeat=true;await page.getByRole('button',{name:'Retry response'}).click();await page.getByText('Still running',{exact:true}).waitFor();await page.clock.fastForward(10001);await page.waitForTimeout(100);assert.equal(await page.locator('#stop').isVisible(),true);const completing=streams.at(-1);const completingId=calls.filter(x=>x.path.endsWith('/stream')).at(-1).session_id;completing.end(finishFrames(completingId,result('Completed after heartbeat')));await page.getByText('Completed after heartbeat',{exact:true}).waitFor();completedHeartbeat=false;
    await page.locator('#prompt').fill('Another operation');await page.locator('#send').click();await page.getByText('Still running',{exact:true}).waitFor();await page.locator('#stop').click();await page.getByRole('button',{name:'Retry response'}).waitFor();
    for(const malformed of ['null','session','status','extra']){
      malformedHeartbeat=malformed;await page.getByRole('button',{name:'Retry response'}).click();await page.getByText('Still running',{exact:true}).waitFor();await page.clock.fastForward(10001);await page.getByRole('button',{name:'Retry response'}).waitFor();assert.match(await page.locator('.error-note').textContent(),/Invalid agent lifecycle response/);
    }
    malformedHeartbeat='';
    rejectHeartbeat=true;await page.getByRole('button',{name:'Retry response'}).click();await page.getByText('Still running',{exact:true}).waitFor();await page.clock.fastForward(10001);await page.getByRole('button',{name:'Retry response'}).waitFor();assert.match(await page.locator('.error-note').textContent(),/no longer running/);
    rejectHeartbeat=false;await page.getByRole('button',{name:'Retry response'}).click();await page.getByText('Still running',{exact:true}).waitFor();await page.clock.fastForward(150001);await page.getByRole('button',{name:'Retry response'}).waitFor();assert.match(await page.locator('.error-note').textContent(),/too long/);
    await page.evaluate(()=>window.dispatchEvent(new PageTransitionEvent('pagehide',{persisted:false})));
    failCancel=true;await page.getByRole('button',{name:'Retry response'}).click();await page.getByText('Still running',{exact:true}).waitFor();await page.locator('#stop').click();await page.getByRole('button',{name:'Retry response'}).waitFor();assert.match(await page.locator('.error-note').textContent(),/stopped/);for(let attempt=0;attempt<100&&cancelFailures===0;attempt++)await new Promise(resolve=>setTimeout(resolve,20));assert.equal(cancelFailures,1);failCancel=false;
    const exitPage=await browser.newPage();await startCoverage(exitPage);await exitPage.goto(uiOrigin);await exitPage.waitForFunction(()=>document.querySelector('#model').value==='sample');await exitPage.locator('#prompt').fill('Navigate away');await exitPage.locator('#send').click();await exitPage.getByText('Still running',{exact:true}).waitFor();const beforeExit=calls.filter(x=>x.path.endsWith('/cancel')).length;const navigating=streams.at(-1);await exitPage.goto('about:blank');
    for(let attempt=0;attempt<100&&!navigating.destroyed;attempt++)await new Promise(resolve=>setTimeout(resolve,20));assert.equal(navigating.destroyed,true);assert.ok(calls.filter(x=>x.path.endsWith('/cancel')).length<=beforeExit+1);
    await exitPage.goto(uiOrigin);await exitPage.waitForFunction(()=>document.querySelector('#model').value==='sample');await exitPage.locator('#prompt').fill('Close active tab');await exitPage.locator('#send').click();await exitPage.getByText('Still running',{exact:true}).waitFor();const beforeClose=calls.filter(x=>x.path.endsWith('/cancel')).length;const closing=streams.at(-1);await exitPage.close();
    for(let attempt=0;attempt<100&&!closing.destroyed;attempt++)await new Promise(resolve=>setTimeout(resolve,20));assert.equal(closing.destroyed,true);assert.ok(calls.filter(x=>x.path.endsWith('/cancel')).length<=beforeClose+1);
    assert.ok(calls.every(x=>x.nonce===nonce&&x.origin===uiOrigin));assert.equal(await page.evaluate(token=>JSON.stringify({...localStorage,...sessionStorage}).includes(token),nonce),false);
  } finally {for(const stream of streams)stream.end();await closeBrowser(browser);ui.closeAllConnections();api.closeAllConnections();await Promise.all([new Promise(resolve=>ui.close(resolve)),new Promise(resolve=>api.close(resolve))]);}
});
test('actual HTTP EOF truncations preserve partial text and remain incomplete and retryable',async()=>{
  const {createServer}=await import('node:http');const nonce='synthetic_eof_nonce_0123456789012345';let uiOrigin,ready=Promise.withResolvers();
  const api=createServer((request,response)=>{
    response.setHeader('Access-Control-Allow-Origin',uiOrigin);
    if(request.method==='OPTIONS'){response.setHeader('Access-Control-Allow-Methods','GET, POST');response.setHeader('Access-Control-Allow-Headers','content-type,x-api-agent-session');response.writeHead(204);response.end();return;}
    if(request.url.endsWith('/models')){response.setHeader('Content-Type','application/json');response.end(JSON.stringify({models:[wireModel(model)],default:'sample',reasoning_default:null}));return;}
    let body='';request.on('data',chunk=>body+=chunk);request.on('end',()=>{
      const {session_id}=JSON.parse(body);
      assert.equal(request.headers['x-api-agent-session'],nonce);
      if(!request.url.endsWith('/stream')){response.setHeader('Content-Type','application/json');response.end(JSON.stringify({session_id,status:'cancelled'}));return;}
      response.setHeader('Content-Type','text/event-stream');response.write(sessionFrame(session_id)+frame('token',{content:'Incremental partial'}));ready.resolve({response,session_id});
    });
  });
  await new Promise(resolve=>api.listen(0,'127.0.0.1',resolve));
  const {server:ui}=createStudioServer({UI_LISTENER_URL:'http://127.0.0.1:9443',AGENT_DJANGO_URL:'http://127.0.0.1:'+api.address().port,API_AGENT_UI_LAUNCH_TOKEN:'synthetic-browser-test-token',API_AGENT_UI_ACCESS_TOKEN:nonce});
  await new Promise(resolve=>ui.listen(0,'127.0.0.1',resolve));uiOrigin='http://127.0.0.1:'+ui.address().port;
  const browser=await chromium.launch({headless:true,executablePath:process.env.STUDIO_BROWSER||undefined});
  try {
    for(const ending of ['none','LF','CR']){
      ready=Promise.withResolvers();const page=await browser.newPage();await startCoverage(page);await page.goto(uiOrigin);await page.waitForFunction(()=>document.querySelector('#model').value==='sample');
      await page.locator('#prompt').fill('Original request');await page.locator('#send').click();const active=await ready.promise;
      await page.getByText('Incremental partial',{exact:true}).waitFor();assert.equal(await page.locator('#stop').isVisible(),true);
      const done=frame('done',{session_id:active.session_id}).slice(0,-2)+(ending==='none'?'':ending==='LF'?'\n':'\r');
      active.response.end(frame('response_complete',{session_id:active.session_id})+frame('result',wireResult(result('Authoritative partial'),active.session_id))+done);
      await page.getByRole('button',{name:'Retry response'}).waitFor();
      assert.match(await page.locator('.error-note').textContent(),/Incomplete|terminal/);assert.match(await page.locator('.assistant .metadata-grid').textContent(),/StatusIncomplete/);
      assert.notEqual(await page.locator('#status').textContent(),'Response complete.');assert.equal(await page.locator('.assistant .message-body').textContent(),'Authoritative partial\n');
      const saved=await page.evaluate(()=>JSON.parse(localStorage.getItem('api-agent-studio-v2')).messages.at(-1));assert.equal(saved.pending,false);assert.match(saved.error,/Incomplete|terminal/);
    }
  } finally {await closeBrowser(browser);ui.closeAllConnections();api.closeAllConnections();await Promise.all([new Promise(resolve=>ui.close(resolve)),new Promise(resolve=>api.close(resolve))]);}
});

async function controlledHTTPStudio(handle, ready = 'sample') {
  const {createServer}=await import('node:http');let uiOrigin;
  const api=createServer((request,response)=>{
    response.setHeader('Access-Control-Allow-Origin',uiOrigin);
    if(request.method==='OPTIONS'){
      response.setHeader('Access-Control-Allow-Methods','GET, POST');
      response.setHeader('Access-Control-Allow-Headers','content-type');
      response.writeHead(204);response.end();return;
    }
    let body='';request.on('data',chunk=>body+=chunk);
    request.on('end',()=>handle(request,response,body));
  });
  await new Promise(resolve=>api.listen(0,'127.0.0.1',resolve));
  const {server:ui}=createStudioServer({UI_LISTENER_URL:'http://127.0.0.1:9443',AGENT_DJANGO_URL:'http://127.0.0.1:'+api.address().port,API_AGENT_UI_LAUNCH_TOKEN:'synthetic-browser-test-token'});
  await new Promise(resolve=>ui.listen(0,'127.0.0.1',resolve));uiOrigin='http://127.0.0.1:'+ui.address().port;
  const browser=await chromium.launch({headless:true,executablePath:process.env.STUDIO_BROWSER||undefined});
  const page=await browser.newPage();await startCoverage(page);await page.goto(uiOrigin);
  await page.waitForFunction(value=>[...document.querySelector('#model').options].some(option=>option.value===value),ready);
  return {page,async close(){await closeBrowser(browser);ui.closeAllConnections();api.closeAllConnections();await Promise.all([new Promise(resolve=>ui.close(resolve)),new Promise(resolve=>api.close(resolve))]);}};
}

test('actual HTTP follows changing backend defaults until explicit choice and restores policy on reset',async()=>{
  const alternative={...model,selected_llm:'alternative',display_name:'Alternative model'};
  const posts=[];let defaultModel='sample',fail=false;
  const studio=await controlledHTTPStudio((request,response,body)=>{
    response.setHeader('Content-Type','application/json');
    if(request.url.endsWith('/models')){response.end(JSON.stringify({models:[wireModel(model),wireModel(alternative)],default:defaultModel,reasoning_default:null}));return;}
    const input=JSON.parse(body);
    if(!request.url.endsWith('/stream')){response.end(JSON.stringify({session_id:input.session_id,status:'cancelled'}));return;}
    posts.push(input);
    if(fail){response.writeHead(503);response.end('Retry this response');return;}
    response.end(JSON.stringify(wireResult(result('Reply '+posts.length),input.session_id)));
  });
  const {page}=studio;
  try{
    await page.locator('#prompt').fill('First default');await page.locator('#send').click();await page.getByText('Reply 1',{exact:true}).waitFor();
    assert.equal(posts[0].model,'sample');
    assert.equal(await page.evaluate(()=>JSON.parse(localStorage.getItem('api-agent-studio-v2')).settings.model),'');
    defaultModel='alternative';await reloadPage(page);await page.waitForFunction(()=>document.querySelector('#model').value==='alternative');
    assert.equal(await page.locator('#model-short-name').textContent(),'Alternative model');
    assert.equal(await page.locator('#model option').count(),2);
    fail=true;await page.locator('#prompt').fill('New default');await page.locator('#send').click();await page.getByRole('button',{name:'Retry response'}).waitFor();
    assert.equal(posts[1].model,'alternative');
    defaultModel='sample';await reloadPage(page);await page.waitForFunction(()=>document.querySelector('#model').value==='sample');
    fail=false;await page.getByRole('button',{name:'Retry response'}).click();await page.getByText('Reply 3',{exact:true}).waitFor();
    assert.equal(posts[2].model,'alternative');assert.notEqual(posts[2].session_id,posts[1].session_id);
    await page.locator('#settings-nav').click();await page.locator('#model').selectOption('sample');await page.locator('#reasoning').selectOption('off');await page.locator('#close-settings').click();
    defaultModel='alternative';await reloadPage(page);await page.waitForFunction(()=>document.querySelector('#model-short-name').textContent==='Sample model: off');
    assert.equal(await page.evaluate(()=>JSON.parse(localStorage.getItem('api-agent-studio-v2')).settings.model),'sample');
    await page.locator('#settings-nav').click();await page.getByRole('button',{name:'Use backend default'}).click();
    assert.equal(await page.locator('#model').inputValue(),'alternative');assert.equal(await page.locator('#model-short-name').textContent(),'Alternative model');
    assert.deepEqual(await page.evaluate(()=>JSON.parse(localStorage.getItem('api-agent-studio-v2')).settings),{model:'',version:'',reasoning:''});
    await page.locator('#close-settings').click();await page.locator('#prompt').fill('Reset policy');await page.locator('#send').click();await page.getByText('Reply 4',{exact:true}).waitFor();
    assert.equal(posts[3].model,'alternative');assert.equal('reasoning_effort' in posts[3],false);
  }finally{await studio.close();}
});

test('actual HTTP schedules advertised heartbeat timing and preserves absolute deadline and Stop cleanup',async()=>{
  const calls=[],streams=[];let invalid=false;
  const studio=await controlledHTTPStudio((request,response,body)=>{
    if(request.url.endsWith('/models')){response.setHeader('Content-Type','application/json');response.end(JSON.stringify({models:[wireModel(model)],default:'sample',reasoning_default:null}));return;}
    const {session_id}=JSON.parse(body);calls.push({path:request.url,session_id});
    if(request.url.endsWith('/stream')){
      response.setHeader('Content-Type','text/event-stream');
      response.write(frame('session',{session_id,heartbeat_interval_ms:invalid?0:5000,heartbeat_timeout_ms:15000}));
      if(!invalid)response.write(frame('token',{content:'Advertised timing draft'}));
      streams.push(response);return;
    }
    response.setHeader('Content-Type','application/json');response.end(JSON.stringify({session_id,status:request.url.endsWith('/cancel')?'cancelled':'running'}));
  });
  const {page}=studio;
  try{
    await page.clock.install();await page.clock.pauseAt(new Date(Date.now()+1000));await page.locator('#prompt').fill('Advertised cadence');await page.locator('#send').click();await page.getByText('Advertised timing draft',{exact:true}).waitFor();
    await page.clock.fastForward(4999);await page.waitForTimeout(50);assert.equal(calls.filter(x=>x.path.endsWith('/heartbeat')).length,0);
    await page.clock.fastForward(2);await page.waitForTimeout(100);
    assert.equal(calls.filter(x=>x.path.endsWith('/heartbeat')).length,1);
    const first=streams[0];first.end(finishFrames(calls[0].session_id,result('Advertised timing completed')));await page.getByText('Advertised timing completed',{exact:true}).waitFor();
    await page.clock.fastForward(5001);await page.waitForTimeout(50);assert.equal(calls.filter(x=>x.path.endsWith('/heartbeat')).length,1);
    await page.locator('#prompt').fill('Stop advertised cadence');await page.locator('#send').click();await page.getByText('Advertised timing draft',{exact:true}).waitFor();
    await page.locator('#stop').click();await page.getByRole('button',{name:'Retry response'}).waitFor();await page.waitForTimeout(50);
    const stoppedHeartbeats=calls.filter(x=>x.path.endsWith('/heartbeat')).length;
    await page.clock.fastForward(15001);await page.waitForTimeout(50);assert.equal(calls.filter(x=>x.path.endsWith('/heartbeat')).length,stoppedHeartbeats);
    await page.getByRole('button',{name:'Retry response'}).click();await page.getByText('Advertised timing draft',{exact:true}).waitFor();
    await page.clock.fastForward(150001);await page.getByRole('button',{name:'Retry response'}).waitFor();assert.match(await page.locator('.error-note').textContent(),/too long/);
    invalid=true;const before=calls.filter(x=>x.path.endsWith('/cancel')).length;
    await page.getByRole('button',{name:'Retry response'}).click();await page.getByRole('button',{name:'Retry response'}).waitFor();
    assert.match(await page.locator('.error-note').textContent(),/Invalid stream session/);
    for(let attempt=0;attempt<100&&calls.filter(x=>x.path.endsWith('/cancel')).length===before;attempt++)await new Promise(resolve=>setTimeout(resolve,10));
    assert.equal(calls.filter(x=>x.path.endsWith('/cancel')).length,before+1);
  }finally{for(const stream of streams)stream.end();await studio.close();}
});

test('native keyboard can pin the sole current model and reset backend policy without altering retry identity',async()=>{
  const alternative={...model,selected_llm:'alternative',display_name:'Alternative model'};
  const posts=[];let expanded=false,fail=true;
  const studio=await controlledHTTPStudio((request,response,body)=>{
    response.setHeader('Content-Type','application/json');
    if(request.url.endsWith('/models')){response.end(JSON.stringify({models:(expanded?[model,alternative]:[model]).map(wireModel),default:expanded?'alternative':'sample',reasoning_default:null}));return;}
    const input=JSON.parse(body);
    if(!request.url.endsWith('/stream')){response.end(JSON.stringify({session_id:input.session_id,status:'cancelled'}));return;}
    posts.push(input);
    if(fail){response.writeHead(503);response.end('Retry original pinned model');return;}
    response.end(JSON.stringify(wireResult(result('Pinned retry completed'),input.session_id)));
  });
  const {page}=studio;
  try{
    await page.locator('#settings-nav').click();
    assert.equal(await page.locator('#model option').count(),1);
    await page.locator('#model').focus();await page.keyboard.press('Space');await page.keyboard.press('Home');await page.keyboard.press('Enter');
    assert.equal(await page.getByRole('button',{name:'Keep selected model'}).isVisible(),true);
    await page.getByRole('button',{name:'Keep selected model'}).focus();await page.keyboard.press('Enter');
    assert.equal(await page.evaluate(()=>JSON.parse(localStorage.getItem('api-agent-studio-v2')).settings.model),'sample');
    await page.locator('#close-settings').click();
    expanded=true;await reloadPage(page);await page.waitForFunction(()=>document.querySelector('#model option[value="alternative"]'));
    assert.equal(await page.locator('#model').inputValue(),'sample');
    await page.locator('#prompt').fill('Pinned original');await page.locator('#send').click();await page.getByRole('button',{name:'Retry response'}).waitFor();
    assert.equal(posts[0].model,'sample');
    await page.locator('#settings-nav').click();await page.getByRole('button',{name:'Use backend default'}).click();
    assert.equal(await page.locator('#model').inputValue(),'alternative');
    assert.equal(await page.evaluate(()=>JSON.parse(localStorage.getItem('api-agent-studio-v2')).settings.model),'');
    await page.locator('#close-settings').click();fail=false;
    await page.getByRole('button',{name:'Retry response'}).click();await page.getByText('Pinned retry completed',{exact:true}).waitFor();
    assert.equal(posts[1].model,'sample');assert.notEqual(posts[1].session_id,posts[0].session_id);
  }finally{await studio.close();}
});


test('family picker sends exact version IDs and pins old retries across family/default changes',async()=>{
 const family=JSON.stringify(['family','gpt-luna']),solFamily=JSON.stringify(['family','gpt-sol']);
 const row=(selected_llm,display_name,version,group,reasoning_efforts)=>({...model,selected_llm,display_name,version,family:group,reasoning_efforts});
 const l6=row('gpt-6-luna','GPT-6-Luna','6','gpt-luna',['off','high']);
 const l56=row('gpt-5.6-luna','GPT-5.6-Luna','5.6','gpt-luna',['off','low']);
 const alias=row('luna-current','GPT-Luna',null,'gpt-luna',['off']);
 const sol=row('gpt-6-sol','GPT-6-Sol','6','gpt-sol',['off']);
 let rows=[l6,l56,alias,sol,model],defaultModel=l6.selected_llm,fail=false;
 const posts=[];
 const studio=await controlledHTTPStudio((request,response,body)=>{
  response.setHeader('Content-Type','application/json');
  if(request.url.endsWith('/models')){response.end(JSON.stringify({models:rows.map(wireModel),default:defaultModel,reasoning_default:null}));return;}
  const input=JSON.parse(body);
  if(!request.url.endsWith('/stream')){response.end(JSON.stringify({session_id:input.session_id,status:'cancelled'}));return;}
  posts.push(input);
  if(fail){response.writeHead(503);response.end('Synthetic retry');return;}
  response.end(JSON.stringify(wireResult(result('Family reply '+posts.length),input.session_id)));
 },family);
 const {page}=studio;
 try{
  await page.locator('#settings-nav').click();
  assert.deepEqual(await page.locator('#model option').allTextContents(),['GPT-Luna','GPT-Sol','Sample model']);
  assert.deepEqual(await page.locator('#version option').allTextContents(),['Provider default','6','5.6','Unknown version — luna-current']);
  await page.locator('#version').selectOption(l56.selected_llm);
  assert.deepEqual(await page.locator('#reasoning option').allTextContents(),['Provider default','off','low']);
  await page.locator('#reasoning').selectOption('low');assert.equal(await page.locator('#model-short-name').textContent(),'GPT-5.6-Luna: low');await page.locator('#close-settings').click();
  await page.locator('#prompt').fill('Older version');await page.locator('#send').click();await page.getByText('Family reply 1',{exact:true}).waitFor();
  assert.equal(posts[0].model,l56.selected_llm);assert.equal(posts[0].reasoning_effort,'low');
  await reloadPage(page);await page.waitForFunction(()=>document.querySelector('#model-status').dataset.catalogState==='settled');
  await page.evaluate(()=>{const value=JSON.parse(localStorage.getItem('api-agent-studio-v2'));value.settings={model:'',family:'gpt-luna',version:'5.6',reasoning:'low'};localStorage.setItem('api-agent-studio-v2',JSON.stringify(value));});
  await reloadPage(page);await page.waitForFunction(()=>document.querySelector('#model-status').dataset.catalogState==='settled');
  await page.locator('#settings-nav').click();assert.equal(await page.locator('#version').inputValue(),l56.selected_llm);assert.equal(await page.evaluate(()=>JSON.parse(localStorage.getItem('api-agent-studio-v2')).settings.model),'');
  await page.locator('#version').selectOption(l6.selected_llm);assert.equal(await page.locator('#reasoning').inputValue(),'');
  await page.locator('#reasoning').selectOption('high');await page.locator('#close-settings').click();
  fail=true;await page.locator('#prompt').fill('Pinned version');await page.locator('#send').click();await page.getByRole('button',{name:'Retry response'}).waitFor();
  assert.equal(posts[1].model,l6.selected_llm);assert.equal(posts[1].reasoning_effort,'high');
  defaultModel=alias.selected_llm;await reloadPage(page);await page.waitForFunction(()=>document.querySelector('#model-status').dataset.catalogState==='settled');
  await page.locator('#settings-nav').click();await page.locator('#model').selectOption(solFamily);
  assert.match(await page.locator('#model-status').textContent(),/Choose an advertised version/);
  await page.locator('#version').selectOption(sol.selected_llm);await page.locator('#reasoning').selectOption('off');await page.locator('#close-settings').click();
  fail=false;await page.getByRole('button',{name:'Retry response'}).click();await page.getByText('Family reply 3',{exact:true}).waitFor();
  assert.equal(posts[2].model,l6.selected_llm);assert.equal(posts[2].reasoning_effort,'high');
  await page.locator('#prompt').fill('New family');await page.locator('#send').click();await page.getByText('Family reply 4',{exact:true}).waitFor();
  assert.equal(posts[3].model,sol.selected_llm);assert.equal(posts[3].reasoning_effort,'off');
  await page.locator('#settings-nav').click();await page.locator('#model').selectOption(family);assert.equal(await page.locator('#version').inputValue(),'');
  assert.equal(await page.locator('#model-status').textContent(),''); // Explicit family follows only its advertised backend default.
  await page.locator('#close-settings').click();await page.locator('#prompt').fill('Family alias');await page.locator('#send').click();await page.getByText('Family reply 5',{exact:true}).waitFor();
  assert.equal(posts[4].model,alias.selected_llm);
  const saved=await page.evaluate(()=>JSON.parse(localStorage.getItem('api-agent-studio-v2')));
  assert.equal(saved.settings.family,'gpt-luna');assert.equal(saved.settings.model,'');
  assert.equal(saved.messages.at(-1).selectionSnapshot.model,alias.selected_llm);assert.equal(saved.messages.at(-1).selectionSnapshot.family,'');
  assert.equal(saved.messages[0].requested.model,l56.selected_llm);assert.equal(saved.messages[1].selectionSnapshot.model,l56.selected_llm);
  await page.locator('#settings-nav').click();await page.locator('#version').selectOption(alias.selected_llm);assert.equal(await page.locator('#version').inputValue(),alias.selected_llm);
  assert.equal(await page.locator('#model-status').textContent(),'');
  await page.locator('#version').selectOption('');assert.equal(await page.evaluate(()=>JSON.parse(localStorage.getItem('api-agent-studio-v2')).settings.family),'gpt-luna');
 }finally{await studio.close();}
});


test('family picker distinguishes ambiguous versions, opaque selector collisions and inherited reasoning',async()=>{
 const key=JSON.stringify(['family','gpt-luna']),family='~'+key;
 const row=(selected_llm,version,reasoning_efforts)=>({...model,selected_llm,version,family:'gpt-luna',display_name:version?'GPT-'+version+'-Luna':'GPT-Luna',reasoning_efforts});
 const rows=[row('l6','6',['off','high']),row('l6-copy','6',['off']),row('l56','5.6',['low']),row('unknown-a',null,['off']),row('unknown-b',null,['off']),{...model,selected_llm:key,display_name:'Opaque selector'},{...model,selected_llm:'gpt-luna',display_name:'Opaque family ID'}];
 const posts=[];
 const studio=await controlledHTTPStudio((request,response,body)=>{
  response.setHeader('Content-Type','application/json');
  if(request.url.endsWith('/models')){response.end(JSON.stringify({models:rows.map(wireModel),default:'l6',reasoning_default:'low'}));return;}
  const input=JSON.parse(body);
  if(!request.url.endsWith('/stream')){response.end(JSON.stringify({session_id:input.session_id,status:'cancelled'}));return;}
  posts.push(input);response.end(JSON.stringify(wireResult(result('Disambiguated '+posts.length),input.session_id)));
 },family);
 const {page}=studio;
 try{
  await page.locator('#settings-nav').click();
  assert.match(await page.locator('#model-status').textContent(),/configured reasoning default is unsupported/);
  assert.deepEqual(await page.locator('#version option').allTextContents(),['Provider default','6 — l6','6 — l6-copy','5.6','Unknown version — unknown-a','Unknown version — unknown-b']);
  await page.locator('#version').selectOption('l56');assert.equal(await page.locator('#model-status').textContent(),'');
  await page.locator('#close-settings').click();await page.locator('#prompt').fill('Inherited low');await page.locator('#send').click();await page.getByText('Disambiguated 1',{exact:true}).waitFor();
  assert.equal(posts[0].model,'l56');assert.equal('reasoning_effort' in posts[0],false);
  await page.locator('#settings-nav').click();await page.locator('#version').selectOption('l6-copy');await page.locator('#reasoning').selectOption('off');await page.locator('#close-settings').click();
  await page.locator('#prompt').fill('Exact duplicate ID');await page.locator('#send').click();await page.getByText('Disambiguated 2',{exact:true}).waitFor();assert.equal(posts[1].model,'l6-copy');
  await page.locator('#settings-nav').click();await page.locator('#version').selectOption('unknown-b');await page.locator('#reasoning').selectOption('off');await page.locator('#close-settings').click();
  await page.locator('#prompt').fill('Known ID unknown version');await page.locator('#send').click();await page.getByText('Disambiguated 3',{exact:true}).waitFor();assert.equal(posts[2].model,'unknown-b');
  await page.locator('#settings-nav').click();await page.locator('#model').selectOption(key);await page.locator('#reasoning').selectOption('low');await page.locator('#close-settings').click();
  await page.locator('#prompt').fill('Opaque collision');await page.locator('#send').click();await page.getByText('Disambiguated 4',{exact:true}).waitFor();assert.equal(posts[3].model,key);
  await page.locator('#settings-nav').click();await page.locator('#model').selectOption('gpt-luna');await page.locator('#close-settings').click();
  await page.locator('#prompt').fill('Opaque family name');await page.locator('#send').click();await page.getByText('Disambiguated 5',{exact:true}).waitFor();assert.equal(posts[4].model,'gpt-luna');
  // Saved family intent remains distinct and stale versions never pick an arbitrary row.
  await page.evaluate(()=>{const value=JSON.parse(localStorage.getItem('api-agent-studio-v2'));value.settings={model:'',family:'gpt-luna',version:'6',reasoning:''};localStorage.setItem('api-agent-studio-v2',JSON.stringify(value));});
  await reloadPage(page);await page.waitForFunction(()=>document.querySelector('#model-status').dataset.catalogState==='settled');
  await page.locator('#settings-nav').click();assert.match(await page.locator('#model-status').textContent(),/ambiguous/);assert.equal(await page.locator('#version option:checked').textContent(),'Unresolved version: 6');
  await page.locator('#close-settings').click();await page.locator('#prompt').fill('Must not send');await page.locator('#send').click();assert.match(await page.locator('#status').textContent(),/ambiguous/);assert.equal(posts.length,5);
  await page.evaluate(()=>{const value=JSON.parse(localStorage.getItem('api-agent-studio-v2'));value.settings.version='retired';localStorage.setItem('api-agent-studio-v2',JSON.stringify(value));});
  await reloadPage(page);await page.waitForFunction(()=>document.querySelector('#model-status').dataset.catalogState==='settled');
  await page.locator('#settings-nav').click();assert.match(await page.locator('#model-status').textContent(),/version is no longer/);
  await page.evaluate(()=>{const value=JSON.parse(localStorage.getItem('api-agent-studio-v2'));value.settings={model:'l56',family:'',version:'retired',reasoning:''};localStorage.setItem('api-agent-studio-v2',JSON.stringify(value));});
  await reloadPage(page);await page.waitForFunction(()=>document.querySelector('#model-status').dataset.catalogState==='settled');await page.locator('#settings-nav').click();assert.equal(await page.locator('#version option:checked').textContent(),'Unresolved version: retired');
  await page.locator('#model').dispatchEvent('change');assert.equal(await page.evaluate(()=>JSON.parse(localStorage.getItem('api-agent-studio-v2')).settings.version),'');
  await page.getByRole('button',{name:'Use backend default'}).click();await page.getByRole('button',{name:'Keep selected model'}).click();assert.equal(await page.evaluate(()=>JSON.parse(localStorage.getItem('api-agent-studio-v2')).settings.model),'l6');
  await page.evaluate(()=>{const value=JSON.parse(localStorage.getItem('api-agent-studio-v2'));value.settings={model:'',family:'missing-family',version:'',reasoning:''};localStorage.setItem('api-agent-studio-v2',JSON.stringify(value));});
  await reloadPage(page);await page.waitForFunction(()=>document.querySelector('#model-status').dataset.catalogState==='settled');await page.locator('#settings-nav').click();await page.locator('#model').dispatchEvent('change');assert.equal(await page.evaluate(()=>JSON.parse(localStorage.getItem('api-agent-studio-v2')).settings.family),'missing-family');assert.match(await page.locator('#model-status').textContent(),/family is no longer/);
 }finally{await studio.close();}
});

for(const target of ['settings','requested','snapshot'])for(const family of [3,'x'.repeat(201),'bad\nfamily','bad\u2000family','gpt-luna'])test('saved family intent rejects '+target+' '+JSON.stringify(family),async()=>{
 const {browser,page}=await openStudio(route=>fulfill(route,{json:result('Unexpected')}));
 try{
  await page.evaluate(({target,family})=>{
   const base={model:'sample',version:'',reasoning:''};
   const saved={session:crypto.randomUUID(),draft:'',messages:[],reply:null,settings:base};
   if(target==='settings')saved.settings={...base,family};
   else saved.messages=[{id:'u',role:'user',text:'Untrusted saved message',requested:target==='requested'?{...base,family}:base,...(target==='snapshot'?{selectionSnapshot:{...base,family}}:{})}];
   localStorage.setItem('api-agent-studio-v2',JSON.stringify(saved));
  },{target,family});
  await reloadPage(page);assert.equal(await page.locator('#storage-warning').isVisible(),true);
  assert.equal(await page.locator('.user').count(),0);assert.equal(await page.locator('#prompt').inputValue(),'');
 }finally{await closeBrowser(browser);}
});
for(const snapshot of [null,{model:'',family:'gpt-luna',version:'',reasoning:''}])test('saved family messages require an exact snapshot '+JSON.stringify(snapshot),async()=>{
 const {browser,page}=await openStudio(route=>fulfill(route,{json:result('Unexpected')}));
 try{
  await page.evaluate(snapshot=>localStorage.setItem('api-agent-studio-v2',JSON.stringify({session:crypto.randomUUID(),draft:'',reply:null,settings:{model:'sample',version:'',reasoning:''},messages:[{id:'u',role:'user',text:'Unpinned family message',requested:{model:'',family:'gpt-luna',version:'',reasoning:''},selectionSnapshot:snapshot}]})),snapshot);
  await reloadPage(page);assert.equal(await page.locator('#storage-warning').isVisible(),true);assert.equal(await page.locator('.user').count(),0);
 }finally{await closeBrowser(browser);}
});

test('family default retries keep their send-time ID after the configured family default changes',async()=>{
 const family=JSON.stringify(['family','gpt-luna']);
 const older={...model,selected_llm:'old',display_name:'GPT-5.6-Luna',version:'5.6',family:'gpt-luna'};
 const newer={...older,selected_llm:'new',display_name:'GPT-6-Luna',version:'6'};
 let defaultModel='new',fail=true;const posts=[];
 const studio=await controlledHTTPStudio((request,response,body)=>{
  response.setHeader('Content-Type','application/json');
  if(request.url.endsWith('/models')){response.end(JSON.stringify({models:[older,newer].map(wireModel),default:defaultModel,reasoning_default:null}));return;}
  const input=JSON.parse(body);
  if(!request.url.endsWith('/stream')){response.end(JSON.stringify({session_id:input.session_id,status:'cancelled'}));return;}
  posts.push(input);
  if(fail){response.writeHead(503);response.end('Controlled failure');return;}
  response.end(JSON.stringify(wireResult(result('Family retry pinned'),input.session_id)));
 },family);
 const {page}=studio;
 try{
  await page.locator('#settings-nav').click();await page.locator('#model').selectOption(family);await page.locator('#reasoning').selectOption('off');await page.locator('#close-settings').click();
  await page.locator('#prompt').fill('Family default retry');await page.locator('#send').click();await page.getByRole('button',{name:'Retry response'}).waitFor();
  assert.equal(posts[0].model,'new');defaultModel='old';fail=false;await reloadPage(page);await page.waitForFunction(()=>document.querySelector('#model-status').dataset.catalogState==='settled');
  assert.equal(await page.locator('#model-short-name').textContent(),'GPT-5.6-Luna: off');
  await page.getByRole('button',{name:'Retry response'}).click();await page.getByText('Family retry pinned',{exact:true}).waitFor();
  assert.equal(posts[1].model,'new');assert.equal(posts[1].reasoning_effort,'off');
  const saved=await page.evaluate(()=>JSON.parse(localStorage.getItem('api-agent-studio-v2')));
  assert.equal(saved.messages[0].requested.family,'gpt-luna');assert.equal(saved.messages[0].selectionSnapshot.model,'new');assert.equal(saved.messages[1].selectionSnapshot.model,'new');
  await page.locator('.assistant summary').click();assert.match(await page.locator('.assistant .metadata-grid').textContent(),/Resolved catalog modelnew/);
 }finally{await studio.close();}
});

test('family picker preserves display casing for whole dated and hyphenated version suffixes',async()=>{
 const rows=[{...model,selected_llm:'dated',display_name:'Family-2024-01-01',version:'2024-01-01',family:'family'},
  {...model,selected_llm:'preview',display_name:'Family-release-candidate',version:'release-candidate',family:'family'}];
 const posts=[],family=JSON.stringify(['family','family']);
 const studio=await controlledHTTPStudio((request,response,body)=>{
  response.setHeader('Content-Type','application/json');
  if(request.url.endsWith('/models')){response.end(JSON.stringify({models:rows.map(wireModel),default:'dated',reasoning_default:null}));return;}
  const input=JSON.parse(body);posts.push(input);response.end(JSON.stringify(wireResult(result('Selected suffix version'),input.session_id)));
 },family);
 try{
  const {page}=studio;await page.locator('#settings-nav').click();
  assert.deepEqual(await page.locator('#model option').allTextContents(),['Family']);
  assert.deepEqual(await page.locator('#version option').allTextContents(),['Provider default','2024-01-01','release-candidate']);
  await page.locator('#version').selectOption('preview');await page.locator('#reasoning').selectOption('off');await page.locator('#close-settings').click();
  await page.locator('#prompt').fill('Whole advertised version');await page.locator('#send').click();await page.getByText('Selected suffix version',{exact:true}).waitFor();
  assert.equal(posts.length,1);assert.equal(posts[0].model,'preview');assert.equal(posts[0].reasoning_effort,'off');
 }finally{await studio.close();}
});

test('missing saved family uses display-only intent despite opaque ID and handle collisions',async()=>{
 const family='retired-family',handle=JSON.stringify(['missing-family',family]);
 const rows=[model,{...model,selected_llm:family,display_name:'Opaque impostor'},
  {...model,selected_llm:handle,display_name:'Opaque handle'},
  {...model,selected_llm:'~'+handle,display_name:'Opaque prefixed handle'},
  {...model,selected_llm:'~~'+handle,display_name:'Other-6',family:'other',version:'6'}];
 const posts=[];
 const studio=await controlledHTTPStudio((request,response,body)=>{
  response.setHeader('Content-Type','application/json');
  if(request.url.endsWith('/models')){response.end(JSON.stringify({models:rows.map(wireModel),default:'sample',reasoning_default:null}));return;}
  const input=JSON.parse(body);posts.push(input);response.end(JSON.stringify(wireResult(result('Explicit opaque selection'),input.session_id)));
 });
 try{
  const {page}=studio;
  await page.evaluate(family=>{const saved={session:crypto.randomUUID(),draft:'',messages:[],reply:null,settings:{family,model:'',version:'',reasoning:''}};localStorage.setItem('api-agent-studio-v2',JSON.stringify(saved));},family);
  await reloadPage(page);await page.waitForFunction(()=>document.querySelector('#model-status').dataset.catalogState==='settled');await page.locator('#settings-nav').click();
  assert.equal(await page.locator('#model option:checked').textContent(),family+' (saved family)');
  assert.equal(await page.locator('#model option:checked').isDisabled(),true);
  const selected=await page.locator('#model').inputValue();assert.ok(!rows.some(row=>row.selected_llm===selected));
  assert.equal(await page.locator('#model option').evaluateAll(options=>new Set(options.map(option=>option.value)).size===options.length),true);
  await page.locator('#model').dispatchEvent('change');
  assert.deepEqual(await page.evaluate(()=>JSON.parse(localStorage.getItem('api-agent-studio-v2')).settings),{family,model:'',version:'',reasoning:''});
  await page.locator('#close-settings').click();await page.locator('#prompt').fill('Must preserve missing family');await page.locator('#send').click();
  assert.match(await page.locator('#status').textContent(),/family is no longer/);assert.equal(posts.length,0);
  assert.equal(await page.locator('.message').count(),0);
  await page.locator('#settings-nav').click();await page.locator('#model').selectOption(family);await page.locator('#reasoning').selectOption('off');await page.locator('#close-settings').click();await page.locator('#send').click();await page.getByText('Explicit opaque selection',{exact:true}).waitFor();
  assert.equal(posts.length,1);assert.equal(posts[0].model,family);
 }finally{await studio.close();}
});

test('family defaults pin requests and version changes retain only supported reasoning',async()=>{
 const family=JSON.stringify(['family','Chosen']);
 const row=(selected_llm,reasoning_efforts)=>({...model,selected_llm,version:selected_llm,family:'Chosen',reasoning_efforts});
 const rows=[model,row('new',['off','low']),row('old',['low','off']),row('limited',['off'])];
 const posts=[];let uniform=false;
 const studio=await controlledHTTPStudio((request,response,body)=>{
  response.setHeader('Content-Type','application/json');
  if(request.url.endsWith('/models')){response.end(JSON.stringify({models:(uniform ? rows.filter(row=>row.selected_llm!=='limited') : rows).map(wireModel),default:'sample',reasoning_default:null,family_defaults:{Chosen:'new'}}));return;}
  const input=JSON.parse(body);posts.push(input);response.end(JSON.stringify(wireResult(result('Family default request'),input.session_id)));
 },family);
 const {page}=studio;
 try{
  await page.locator('#settings-nav').click();await page.locator('#model').selectOption(family);
  assert.equal(await page.locator('#version').inputValue(),'');
  assert.equal(await page.locator('#reasoning').isDisabled(),true);
  assert.equal(await page.locator('#model-status').textContent(),'');
  await page.locator('#close-settings').click();await page.locator('#prompt').fill('Use family latest');await page.locator('#send').click();await page.getByText('Family default request',{exact:true}).waitFor();
  assert.equal(posts.length,1);assert.equal(posts[0].model,'new');assert.equal('reasoning_effort' in posts[0],false);
  await page.locator('#settings-nav').click();await page.locator('#version').selectOption('new');await page.locator('#reasoning').selectOption('low');
  await page.locator('#version').selectOption('old');assert.equal(await page.locator('#reasoning').inputValue(),'low');
  await page.locator('#version').selectOption('limited');assert.equal(await page.locator('#reasoning').inputValue(),'');
  await page.locator('#reasoning').selectOption('off');await page.locator('#version').selectOption('');assert.equal(await page.locator('#reasoning').inputValue(),'');assert.equal(await page.locator('#reasoning').isDisabled(),true);
  await page.locator('#version').selectOption('new');await page.locator('#reasoning').selectOption('low');await page.locator('#model').selectOption('sample');
  assert.equal(await page.locator('#version').inputValue(),'');assert.equal(await page.locator('#reasoning').inputValue(),'');
  uniform=true;await reloadPage(page);await page.waitForFunction(()=>document.querySelector('#model-status').dataset.catalogState==='settled');
  await page.locator('#settings-nav').click();await page.locator('#model').selectOption(family);
  assert.equal(await page.locator('#version').inputValue(),'');assert.equal(await page.locator('#reasoning').isDisabled(),false);
  assert.deepEqual(await page.locator('#reasoning option').allTextContents(),['Provider default','off','low']);
  await page.locator('#reasoning').selectOption('low');await page.locator('#close-settings').click();await page.locator('#prompt').fill('Uniform family latest');await page.locator('#send').click();
  await page.waitForFunction(()=>document.querySelectorAll('.message.assistant').length===2);
  assert.equal(posts.length,2);assert.equal(posts[1].model,'new');assert.equal(posts[1].reasoning_effort,'low');
 }finally{await studio.close();}
});
