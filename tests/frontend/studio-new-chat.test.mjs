import test from 'node:test';
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {createStudioServer} from '../../apps/api-agent-ui/server.mjs';
const {chromium} = await import(process.env.STUDIO_PLAYWRIGHT_MODULE || 'playwright');
const frame = (event, data) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
const row = (value, version) => ({value, label:`Synthetic ${version}`, description:'Controlled model', family:'Synthetic', version, features:['reasoning_effort'], reasoning_efforts:['off','low'], max_output_tokens:null, context_length:null});
async function openStudio(t, {unknown = false, familyEfforts = null, reasoningDefault = 'low'} = {}) {
  const requests = [], waiting = [];
  let server, browser;
  let next = Promise.withResolvers();
  const api = createServer((request, response) => {
    response.setHeader('Access-Control-Allow-Origin','*');
    if(request.method==='OPTIONS'){response.setHeader('Access-Control-Allow-Methods','POST');response.setHeader('Access-Control-Allow-Headers','content-type');response.writeHead(204);response.end();return;}
    if (request.url === '/app/agent/models') { response.setHeader('Content-Type','application/json'); response.end(JSON.stringify({models:familyEfforts ? [{...row('outside','1'),family:'Other'},...familyEfforts.map((efforts,i)=>({...row('synthetic-'+i,String(i)),reasoning_efforts:efforts}))] : [row('synthetic-1','1'),{...row('synthetic-56','5.6'),reasoning_efforts:unknown ? null : ['off','low']}],default:familyEfforts ? 'outside' : 'synthetic-1',reasoning_default:reasoningDefault})); return; }
    let body=''; request.on('data', chunk=>body+=chunk); request.on('end',()=>{
      const data=JSON.parse(body);
      if (request.url !== '/app/agent/chat/stream') { response.setHeader('Content-Type','application/json'); response.end(JSON.stringify({session_id:data.session_id,status:'cancelled'})); return; }
      requests.push(data); response.setHeader('Content-Type','text/event-stream');
      response.write(frame('session',{session_id:data.session_id,heartbeat_interval_ms:10000,heartbeat_timeout_ms:30000})+frame('token',{content:'Controlled partial'}));
      response.on('error',()=>{}); waiting.push(response); next.resolve(response);
    });
  });
  t.after(async()=>{
    try { await browser?.close(); }
    finally {
      for (const listener of [server,api]) listener?.closeAllConnections();
      await Promise.all([server,api].filter(listener=>listener?.listening).map(listener=>new Promise(resolve=>listener.close(resolve))));
    }
  });
  await new Promise(resolve=>api.listen(0,'127.0.0.1',resolve));
  ({server}=createStudioServer({UI_LISTENER_URL:'http://127.0.0.1:9443',AGENT_DJANGO_URL:'http://127.0.0.1:'+api.address().port,API_AGENT_UI_LAUNCH_TOKEN:'synthetic-browser-test-token'}));
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  browser=await chromium.launch({headless:true,executablePath:process.env.STUDIO_BROWSER || undefined});
  const page=await browser.newPage(), errors=[];page.on('pageerror',error=>errors.push(error.message));
  await page.goto('http://127.0.0.1:'+server.address().port);
  await page.waitForFunction(()=>document.querySelector('#model-status').dataset.catalogState==='settled');
  return {page,requests,errors, correctCapabilities(){unknown=false;}, async dispatched(){const response=await next.promise;next=Promise.withResolvers();return response;}, finish(response,text){const id=requests[waiting.indexOf(response)].session_id;response.end(frame('response_complete',{session_id:id})+frame('result',{session_id:id,response_text:text,outcome:'completed',usage:{input_tokens:1,output_tokens:1,cached_input_tokens:0,reasoning_tokens:0}})+frame('done',{session_id:id}));}};
}
async function selectVersion(page) {
  await page.locator('#settings-nav').click();
  await page.locator('#version').selectOption('synthetic-56');
  await page.locator('#reasoning').selectOption('off');
  await page.locator('#close-settings').click();
}
test('version switch and New chat submit exact typed draft with Ctrl+Enter',async t=>{
  const studio=await openStudio(t), {page}=studio;
  await selectVersion(page);
  await page.locator('#new-chat').click();
  await page.locator('#prompt').fill('Controlled fresh question');
  await page.locator('#prompt').press('Control+Enter');
  const response=await studio.dispatched();
  assert.equal(studio.requests.length,1);
  assert.equal(studio.requests[0].model,'synthetic-56');
  assert.equal(studio.requests[0].reasoning_effort,'off');
  assert.deepEqual(studio.requests[0].messages,[{role:'user',content:'Controlled fresh question'}]);
  studio.finish(response,'Controlled final');
  await page.getByText('Controlled final',{exact:true}).waitFor();
  assert.equal(await page.locator('#prompt').inputValue(),'');
  assert.deepEqual(studio.errors,[]);
});
test('active stream prevents duplicate keyboard sends and reset, then Stop permits New chat and Undo',async t=>{
  const studio=await openStudio(t), {page}=studio;
  await selectVersion(page);await page.locator('#prompt').fill('First question');await page.locator('#prompt').press('Control+Enter');
  const response=await studio.dispatched();await page.getByText('Controlled partial',{exact:true}).waitFor();
  await page.locator('#prompt').fill('Next draft');await page.locator('#prompt').press('Control+Enter');
  await page.locator('#new-chat').dispatchEvent('click');
  assert.equal(studio.requests.length,1);assert.equal(await page.locator('#prompt').inputValue(),'Next draft');
  const closed=new Promise(resolve=>response.once('close',resolve));await page.locator('#stop').click();await closed;
  await page.getByRole('button',{name:'Retry response'}).waitFor();
  await page.locator('#new-chat').click();assert.equal(await page.locator('#prompt').inputValue(),'');
  await page.locator('#undo-new-chat').click();assert.equal(await page.locator('#prompt').inputValue(),'Next draft');
  assert.equal(await page.locator('.user').count(),1);
  await page.locator('#new-chat').click();await page.locator('#prompt').fill('New conversation');await page.locator('#prompt').press('Control+Enter');
  const second=await studio.dispatched();assert.deepEqual(studio.requests[1].messages,[{role:'user',content:'New conversation'}]);assert.equal(studio.requests[1].model,'synthetic-56');
  studio.finish(second,'Second final');await page.getByText('Second final',{exact:true}).waitFor();assert.deepEqual(studio.errors,[]);
});
test('unknown configured effort explains the blocked draft and corrected catalog restores keyboard sending',async t=>{
  const studio=await openStudio(t,{unknown:true}), {page}=studio;
  await page.locator('#settings-nav').click();
  await page.locator('#version').selectOption('synthetic-56');
  await page.locator('#close-settings').click();
  await page.locator('#new-chat').click();
  await page.locator('#prompt').fill('Preserved blocked draft');
  await page.locator('#prompt').press('Control+Enter');

  assert.equal(studio.requests.length,0);
  assert.equal(await page.locator('#prompt').inputValue(),'Preserved blocked draft');
  assert.equal(await page.locator('#prompt-error').isVisible(),true);
  assert.match(await page.locator('#prompt-error').textContent(),/Reasoning support.*unknown/);
  assert.equal(await page.locator('#prompt').getAttribute('aria-invalid'),'true');
  await page.locator('#send').click();
  assert.equal(await page.evaluate(()=>document.activeElement?.id),'prompt');
  assert.equal(await page.locator('#prompt').getAttribute('aria-describedby'),'prompt-error');
  assert.equal(await page.locator('#status').textContent(),await page.locator('#prompt-error').textContent());
  assert.equal(await page.locator('.message').count(),0);
  await page.locator('#new-chat').click();
  await page.locator('#prompt').press('Control+Enter');
  assert.equal(await page.locator('#prompt-error').textContent(),'Write a message to get started.');
  await page.locator('#undo-new-chat').click();
  assert.equal(await page.locator('#prompt').inputValue(),'Preserved blocked draft');

  studio.correctCapabilities();
  await page.reload();
  await page.waitForFunction(()=>document.querySelector('#model-status').dataset.catalogState==='settled');
  await page.locator('#new-chat').click();
  await page.locator('#prompt').fill('Corrected fresh question');
  await page.locator('#prompt').press('Control+Enter');
  const response=await studio.dispatched();
  assert.equal(studio.requests[0].model,'synthetic-56');
  assert.equal(Object.hasOwn(studio.requests[0],'reasoning_effort'),false);
  assert.deepEqual(studio.requests[0].messages,[{role:'user',content:'Corrected fresh question'}]);
  studio.finish(response,'Capability correction complete');
  await page.getByText('Capability correction complete',{exact:true}).waitFor();
  assert.deepEqual(studio.errors,[]);
});
for (const [name, efforts, expected] of [
  ['common', [['off','low'],['off','low']], ['Provider default','off','low']],
  ['intersection', [['off','low','high'],['off','medium']], ['Provider default','off']],
  ['unknown', [['off','low'],null], ['Provider default']]
]) test(`unresolved family renders ${name} reasoning capabilities without selecting a version`,async t=>{
  const studio=await openStudio(t,{familyEfforts:efforts}), {page}=studio;
  await page.locator('#settings-nav').click();
  await page.locator('#model').selectOption(JSON.stringify(['family','Synthetic']));
  assert.deepEqual(await page.locator('#reasoning option').allTextContents(),expected);
  assert.equal(await page.locator('#version').inputValue(),'');
  await page.locator('#close-settings').click();
  await page.locator('#prompt').fill('Unresolved family draft');
  await page.locator('#prompt').press('Control+Enter');
  assert.match(await page.locator('#prompt-error').textContent(),/Choose an advertised version/);
  assert.equal(studio.requests.length,0);
  assert.equal(await page.locator('#prompt').inputValue(),'Unresolved family draft');
  assert.deepEqual(studio.errors,[]);
});
for (const configured of [null,'low']) test(`empty reasoning keeps provider policy ${configured ?? 'unset'} without a wire override`,async t=>{
  const studio=await openStudio(t,{familyEfforts:[['off','low'],['off','low']],reasoningDefault:configured}), {page}=studio;
  await page.locator('#settings-nav').click();
  await page.locator('#model').selectOption(JSON.stringify(['family','Synthetic']));
  await page.locator('#version').selectOption('synthetic-1');
  assert.equal(await page.locator('#reasoning').inputValue(),'');
  await page.locator('#close-settings').click();
  await page.locator('#new-chat').click();
  await page.locator('#prompt').fill('Explicit version with inherited policy');
  await page.locator('#prompt').press('Control+Enter');
  const response=await studio.dispatched();
  assert.equal(studio.requests[0].model,'synthetic-1');
  assert.equal(Object.hasOwn(studio.requests[0],'reasoning_effort'),false);
  studio.finish(response,'Inherited policy complete');
  await page.getByText('Inherited policy complete',{exact:true}).waitFor();
  assert.deepEqual(studio.errors,[]);
});
