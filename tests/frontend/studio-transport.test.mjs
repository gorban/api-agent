import test from 'node:test';
import assert from 'node:assert/strict';
const load = name => import(new URL('../../apps/api-agent-ui/public/' + name,import.meta.url));
const {requestPayload,readResponse,readJSONResponse} = await load('transport.js');
const {normalizeCatalog,resolveSelection} = await load('catalog.js');
const result = {text:'final',outcome:'completed',usage:{input_tokens:null,output_tokens:null,cached_input_tokens:null,reasoning_tokens:null}};
const response = (text,type='text/event-stream',status=200) => new Response(text,{status,headers:{'content-type':type}});
test('DEL text uses the same escaped JSON byte budget as the agent', async () => {
 const text='\x7f'.repeat(22000);
 assert.throws(()=>requestPayload({text,requested:{model:'m'}}),/byte limit/);
 await assert.rejects(readResponse(response(JSON.stringify({...result,text}),'application/json')),/byte limit/);
});
test('request preserves explicit off and real conversation wire shape', () => {
 const input = {session:'s',messages:[{role:'assistant',text:'prior'}],text:'next',requested:{model:'m',reasoning:'off'}};
 assert.deepEqual(requestPayload(input),{selected_llm:'m',request_id:'s',messages:[{role:'assistant',text:'prior'},{role:'user',text:'next'}],max_output_tokens:null,cache_policy:null,reasoning_effort:'off'});
 assert.equal('reasoning_effort' in requestPayload({...input,requested:{model:'m',reasoning:''}}),false);
 assert.throws(() => requestPayload({...input,reply:{id:'q'}}),/not implemented/);
 assert.throws(() => requestPayload({...input,text:'x'.repeat(32769)}),/Invalid/);
});
test('catalog uses exact selection and rejects unsupported stale controls', () => {
 const row={selected_llm:'m',description:'',features:['reasoning_effort'],display_name:'Model',version:'v',family:null,max_output_tokens:null,context_length:null,reasoning_efforts:['off']};
 const rows=normalizeCatalog([row]); assert.deepEqual(rows,[row]);
 assert.equal(resolveSelection(rows,{model:'m',version:'v',reasoning:'off'}).valid,true);
 for(const s of [{model:'x'},{model:'m',version:'old'},{model:'m',reasoning:'high'}]) assert.equal(resolveSelection(rows,s).valid,false);
 assert.throws(() => normalizeCatalog([row,row]),/Invalid/);
 assert.throws(() => normalizeCatalog([{...row,features:['imaginary']}]),/Invalid/);
});
test('SSE split bytes and CRLF replace partial with authoritative terminal text', async () => {
 const text='data: '+JSON.stringify({kind:'text',payload:{text:'é'}})+'\r\n\r\ndata: '+JSON.stringify({kind:'completed',payload:{...result,actual_model:'reported'}})+'\r\n\r\n';
 const bytes=new TextEncoder().encode(text); let i=0;
 const r=new Response(new ReadableStream({pull(c){if(i===bytes.length)c.close();else c.enqueue(bytes.slice(i,++i));}}),{headers:{'content-type':'text/event-stream'}});
 const seen=[],meta=[];await readResponse(r,{onText:(...v)=>seen.push(v),onMetadata:v=>meta.push(v)});
 assert.deepEqual(seen,[['é',false],['final',true]]);assert.deepEqual(meta,[{actual_model:'reported',model:'reported'}]);
});
test('JSON result replaces and unknown metadata stays absent',async()=>{let actual;await readResponse(response(JSON.stringify(result),'application/json'),{onMetadata:v=>actual=v});assert.deepEqual(actual,{});});
test('errors, missing terminal and post-terminal frames fail honestly',async()=>{
 for(const [body,pattern] of [['data: {"kind":"text","payload":{"text":"partial"}}\n\n',/without a terminal/],['data: {"kind":"error","message":"failed"}\n\n',/failed/],['data: {"kind":"heartbeat"}\n\n',/Unsupported/],['data: '+JSON.stringify({kind:'completed',payload:result})+'\n\ndata: {"kind":"text","payload":{"text":"late"}}\n\n',/follows terminal/]]) await assert.rejects(readResponse(response(body)),pattern);
 await assert.rejects(readResponse(response('deferred','application/json',501)),/HTTP 501: deferred/);
 await assert.rejects(readResponse(response('x'.repeat(131073),'application/json')),/byte limit/);
});
test('structured question retains options and cancellation aborts',async()=>{
 const q={id:'q',question:'Choose',options:['One','Custom']};let seen;
 await readResponse(response('data: '+JSON.stringify({kind:'clarification',payload:q})+'\n\n'),{onClarification:v=>seen=v});assert.deepEqual(seen,q);
 const controller=new AbortController();controller.abort();await assert.rejects(readResponse(response('{}','application/json'),{signal:controller.signal}),{name:'AbortError'});
});
test('malformed catalogs and optional capability metadata are rejected precisely',()=>{
 const row={selected_llm:'m',description:'',features:[],display_name:'',version:null,family:null,max_output_tokens:null,context_length:null};
 assert.deepEqual(normalizeCatalog([]),[]);assert.deepEqual(normalizeCatalog([row]),[row]);
 for(const bad of [null,{},Array(257).fill(row),[{...row,description:'x'.repeat(513)}],[{...row,selected_llm:''}],[{...row,version:12}],[{...row,family:''}],[{...row,display_name:'\n'}],[{...row,extra:1}],[{...row,max_output_tokens:0}],[{...row,context_length:1.5}],[{...row,reasoning_efforts:['bad']}],[{...row,features:['tools','tools']}],[{...row,reasoning_efforts:['off','off']}],[{...row,features:'tools'}],[{...row,reasoning_efforts:'off'}],[{...row,family:'f',max_output_tokens:12,description:'x'.repeat(131073)}]]) assert.throws(()=>normalizeCatalog(bad),/Invalid/);
 assert.equal(resolveSelection([row],{model:'m',reasoning:'off'}).valid,false);
 assert.equal(resolveSelection([{...row,features:['reasoning_effort']}],{model:'m',reasoning:'low'}).valid,false);
});
test('request bounds and effort guard reject malformed inputs',()=>{
 const x={text:'x',requested:{model:'m'}};assert.equal(requestPayload(x).request_id,'');
 for(const bad of [{...x,requested:null},{...x,requested:{model:''}},{...x,session:2},{...x,text:3},{...x,messages:[{role:'tool',text:'x'}]},{...x,messages:Array(64).fill({role:'user',text:'x'})},{...x,requested:{model:'m',reasoning:'extra'}},{...x,messages:Array(5).fill({role:'user',text:'x'.repeat(32768)})}]) assert.throws(()=>requestPayload(bad));
});
test('strict results reject invalid usage, metadata, encoding and content',async()=>{
 for(const bad of [null,{...result,text:7},{...result,outcome:'fake'},{...result,usage:[]},{...result,usage:{}},{...result,usage:{...result.usage,x:1}},{...result,usage:{...result.usage,input_tokens:-1}},{...result,usage:{...result.usage,input_tokens:1,cached_input_tokens:2}},{...result,usage:{...result.usage,output_tokens:1,reasoning_tokens:2}},{...result,actual_model:''}]) await assert.rejects(readResponse(response(JSON.stringify(bad),'application/json')),/Invalid/);
 await assert.rejects(readResponse(response('bad','text/plain')),/content type/);
 await assert.rejects(readResponse(new Response(null)),/Empty/);
 await assert.rejects(readResponse(new Response(new Uint8Array([255]),{headers:{'content-type':'application/json'}})));
});
test('SSE fields, final frame, heartbeats and structured approval',async()=>{
 let activity=0,approval;
 const q={id:'a',question:'Continue?',options:[{id:'yes',label:'Allow'}]};
 await readResponse(response(': comment\nevent: result\nid: 1\nretry: 50\ndata: '+JSON.stringify({kind:'heartbeat'})+'\n\ndata: '+JSON.stringify({kind:'approval',payload:q})+'\r'),{heartbeat:true,onActivity:()=>activity++,onApproval:v=>approval=v});
 assert.equal(activity,2);assert.deepEqual(approval,q);
 for(const frame of ['bad: x','data: null','data: {}','data: {"kind":"clarification","payload":{"id":"x","question":"q","options":[4]}}','data: {"kind":"approval","payload":{"id":"x","question":"q","options":[{}]}}','data: {"kind":"error"}']) await assert.rejects(readResponse(response(frame+'\n\n')));
});
test('abort during pending read cancels owned reader',async()=>{
 const c=new AbortController();let cancelled=false;
 const r=new Response(new ReadableStream({pull(){c.abort();},cancel(){cancelled=true;}}),{headers:{'content-type':'text/event-stream'}});
 await assert.rejects(readResponse(r,{signal:c.signal}),{name:'AbortError'});assert.equal(cancelled,true);
});
test('remaining optional boundaries retain safe defaults',async()=>{
 const {supportsContinuation,submitContinuation}=await load('transport.js');assert.equal(supportsContinuation,false);await assert.rejects(submitContinuation({}),/not implemented/);
 await assert.rejects(readResponse(response('','application/json',501)),/HTTP 501$/);
 await readResponse(response(JSON.stringify({...result,usage:{...result.usage,input_tokens:0,output_tokens:0}}),'application/json'));
 await readResponse(response('data: '+JSON.stringify({kind:'clarification',payload:{id:'x',question:'Q',options:[]}})+'\n\n'));
 await readResponse(response('data: '+JSON.stringify({kind:'approval',payload:{id:'x',question:'Q',options:[]}})+'\n\n'));
 const row={selected_llm:'m',description:'',features:[],display_name:'',version:null,family:null,max_output_tokens:2147483648,context_length:null};assert.throws(()=>normalizeCatalog([row]),/Invalid/);
});
test('server default is explicit, validated and never guessed',()=>{
 const row={selected_llm:'m',description:'',features:[],display_name:'',version:null,family:null,max_output_tokens:null,context_length:null};
 const rows=normalizeCatalog({models:[row],default:'m'});
 assert.equal(rows.defaultSelection,'m');assert.equal(resolveSelection(rows,{model:''}).selection.selected_llm,'m');
 assert.equal(resolveSelection(normalizeCatalog([row]),{model:''}).valid,false);
 assert.equal(resolveSelection(normalizeCatalog({models:[row],default:null}),{model:''}).valid,false);
 for(const envelope of [{models:[row],default:'missing'},{models:[row],default:2},{models:[row]}, {models:[row],default:'m',extra:1}]) assert.throws(()=>normalizeCatalog(envelope),/Invalid/);
});
test('text results reject unknown, rich and invalid billing before terminal callbacks',async()=>{
 for(const invalid of [{...result,unknown:'unexpected'},{...result,version:2,payload:{calls:[{name:'tool'}]}},{...result,billing:{usd:-1,request_id:null,generation_id:null}}]) {
  const seen=[];
  await assert.rejects(readResponse(response(JSON.stringify(invalid),'application/json'),{onText:(...v)=>seen.push(v)}),/Unsupported inference result fields|Invalid reported billing evidence/);
  await assert.rejects(readResponse(response('data: '+JSON.stringify({kind:'completed',payload:invalid})+'\n\n'),{onText:(...v)=>seen.push(v)}),/Unsupported inference result fields|Invalid reported billing evidence/);
  assert.deepEqual(seen,[]);
 }
 for(const billing of [null,{usd:null,request_id:null,generation_id:null},{usd:0.01,request_id:'r:1',generation_id:'g-2'}]) await readResponse(response(JSON.stringify({...result,billing}),'application/json'));
 for(const billing of [7,[],{usd:0},{usd:1e13,request_id:null,generation_id:null},{usd:'1',request_id:null,generation_id:null},{usd:0,request_id:3,generation_id:null},{usd:0,request_id:'bad id',generation_id:null}]) await assert.rejects(readResponse(response(JSON.stringify({...result,billing}),'application/json')),/Invalid reported billing evidence/);
});
test('Python codepoint and ASCII escaped wire byte bounds match',async()=>{
 const model='😀'.repeat(150);
 assert.equal(requestPayload({text:'ok',requested:{model}}).selected_llm,model);
 assert.throws(()=>requestPayload({text:'漢'.repeat(22000),requested:{model:'m'}}),/byte limit/);
 const row={selected_llm:model,description:'',features:[],display_name:'',version:'😀'.repeat(70),family:null,max_output_tokens:null,context_length:null};
 assert.equal(normalizeCatalog([row])[0].version,row.version);
 const huge=Array.from({length:60},(_,i)=>({...row,selected_llm:'m'+i,description:'漢'.repeat(400)}));
 assert.throws(()=>normalizeCatalog(huge),/Invalid provider catalog/);
 const asciiHuge=Array.from({length:256},(_,i)=>({...row,selected_llm:'m'+i,version:null,description:'x'.repeat(512)}));
 assert.throws(()=>normalizeCatalog(asciiHuge),/Invalid provider catalog/);
 await assert.rejects(readResponse(response(JSON.stringify({...result,text:'漢'.repeat(22000)}),'application/json')),/byte limit/);
 await readResponse(response(JSON.stringify({...result,actual_model:model}),'application/json'));
});
test('rejecting underlying cancellation remains handled and preserves AbortError',async()=>{
 const controller=new AbortController();const unhandled=[];
 const listener=error=>unhandled.push(error);process.on('unhandledRejection',listener);
 try {
  const stream=new ReadableStream({pull(){controller.abort();},cancel(){throw Error('cancel failed');}});
  await assert.rejects(readResponse(new Response(stream,{headers:{'content-type':'text/event-stream'}}),{signal:controller.signal}),{name:'AbortError'});
  await new Promise(resolve=>setImmediate(resolve));assert.deepEqual(unhandled,[]);
 } finally {process.off('unhandledRejection',listener);}
});
test('cleanup catches disposal rejection without masking terminal result',async()=>{
 let sent=false;
 const stream=new ReadableStream({pull(c){if(!sent){sent=true;c.enqueue(new TextEncoder().encode('data: '+JSON.stringify({kind:'completed',payload:result})+'\n\n'));}else c.close();},cancel(){return Promise.reject(Error('cleanup'));}});
 await readResponse(new Response(stream,{headers:{'content-type':'text/event-stream'}}));
});
test('callback failure survives rejected cleanup cancellation',async()=>{
 const stream=new ReadableStream({start(c){c.enqueue(new TextEncoder().encode('data: {"kind":"text","payload":{"text":"partial"}}\n\n'));},cancel(){return Promise.reject(Error('cleanup failed'));}});
 await assert.rejects(readResponse(new Response(stream,{headers:{'content-type':'text/event-stream'}}),{onText(){throw Error('callback failed');}}),/callback failed/);
});
test('bounded JSON reader admits exact raw limit and rejects one extra whitespace byte',async()=>{
 const data={models:[],default:null},body=JSON.stringify(data),limit=131072;
 assert.deepEqual(await readJSONResponse(response(' '.repeat(limit-body.length)+body,'application/json')),data);
 await assert.rejects(readJSONResponse(response(' '.repeat(limit-body.length+1)+body,'application/json')),/byte limit/);
});
test('catalog JSON raw cap cancels an oversized open stream before EOF',async()=>{
 let cancelled=false;
 const stream=new ReadableStream({start(controller){controller.enqueue(new TextEncoder().encode(' '.repeat(131073)+'{"models":[],"default":null}'));},cancel(){cancelled=true;}});
 await assert.rejects(readJSONResponse(new Response(stream,{headers:{'content-type':'application/json','content-length':'1'}})),/byte limit/);
 assert.equal(cancelled,true);assert.equal(stream.locked,false);
});
test('bounded JSON reader decodes byte-split Unicode and releases malformed UTF8 streams',async()=>{
 const text=JSON.stringify({title:'café😀'}),bytes=new TextEncoder().encode(text);
 const stream=new ReadableStream({start(controller){for(const byte of bytes)controller.enqueue(new Uint8Array([byte]));controller.close();}});
 assert.deepEqual(await readJSONResponse(new Response(stream,{headers:{'content-type':'Application/JSON; charset=utf-8'}})),{title:'café😀'});
 for(const bytes of [new Uint8Array([0xc3,0x28]),new Uint8Array([0xe2])]){
  const invalid=new ReadableStream({start(controller){controller.enqueue(bytes);controller.close();}});
  await assert.rejects(readJSONResponse(new Response(invalid,{headers:{'content-type':'application/json'}})),TypeError);
  assert.equal(invalid.locked,false);
 }
});
test('bounded JSON reader rejects status, type, syntax and escaped semantic overflow',async()=>{
 await assert.rejects(readJSONResponse(response('unavailable','application/json',503)),/HTTP 503: unavailable/);
 await assert.rejects(readJSONResponse(response('{}','text/html')),/Unsupported/);
 await assert.rejects(readJSONResponse(response('[','application/json')),SyntaxError);
 await assert.rejects(readJSONResponse(new Response(null,{status:204})),/Empty agent response/);
 await assert.rejects(readJSONResponse(response(JSON.stringify({text:'漢'.repeat(22000)}),'application/json')),/byte limit/);
 assert.equal(await readJSONResponse(response('null','application/json')),null);
});
test('bounded JSON reader preserves abort over a rejected source cancellation',async()=>{
 const controller=new AbortController(),unhandled=[];const listener=error=>unhandled.push(error);process.on('unhandledRejection',listener);
 try{
  const stream=new ReadableStream({pull(){controller.abort();},cancel(){throw Error('cancel failure');}});
  await assert.rejects(readJSONResponse(new Response(stream,{headers:{'content-type':'application/json'}}),{signal:controller.signal}),{name:'AbortError'});
  await new Promise(resolve=>setImmediate(resolve));assert.deepEqual(unhandled,[]);assert.equal(stream.locked,false);
 }finally{process.off('unhandledRejection',listener);}
});
