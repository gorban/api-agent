import test from 'node:test';
import assert from 'node:assert/strict';
import {requestPayload,readResponse,readJSONResponse,supportsContinuation,submitContinuation} from '../../apps/api-agent-ui/public/transport.js';
import {normalizeCatalog,resolveSelection} from '../../apps/api-agent-ui/public/catalog.js';
const id='a4e3aa9d-5387-49ef-92e0-6a7122079f28';
const usage={input_tokens:null,output_tokens:null,cached_input_tokens:null,reasoning_tokens:null};
const result={session_id:id,response_text:'final',outcome:'completed',usage};
const model={value:'m',label:'Model',description:'',features:['reasoning_effort'],version:'v',family:null,max_output_tokens:null,context_length:null,reasoning_efforts:['off','low']};
const catalog=(rows=[model],extra={})=>({models:rows,default:rows.length?'m':null,reasoning_default:null,...extra});
const response=(body,type='text/event-stream',status=200)=>new Response(body,{status,headers:{'content-type':type}});
const frame=(event,value)=>`event: ${event}\ndata: ${JSON.stringify(value)}\n\n`;
const start=frame('session',{session_id:id,heartbeat_interval_ms:10000,heartbeat_timeout_ms:30000});
const finish=value=>frame('response_complete',{session_id:id})+frame('result',value)+frame('done',{session_id:id});
test('request uses application DTO and omission differs from explicit off',()=>{
 const input={session:id,text:'next',messages:[{role:'assistant',text:'prior'}],requested:{model:'m',reasoning:'off'}};
 assert.deepEqual(requestPayload(input),{session_id:id,model:'m',messages:[{role:'assistant',content:'prior'},{role:'user',content:'next'}],reasoning_effort:'off'});
 assert.equal('reasoning_effort' in requestPayload({...input,requested:{model:'m'}}),false);
 for(const invalid of [{...input,reply:{}},{...input,requested:null},{...input,requested:{model:''}},{...input,session:2},{...input,text:2},{...input,text:'x'.repeat(32769)},{...input,messages:Array(64).fill({role:'user',text:'x'})},{...input,messages:[{role:'tool',text:'x'}]},{...input,requested:{model:'m',reasoning:'extra'}},{...input,text:'\x7f'.repeat(22000)}])assert.throws(()=>requestPayload(invalid));
});
test('catalog preserves exact backend default and advertised metadata',()=>{
 const rows=normalizeCatalog(catalog());
 assert.equal(rows.defaultSelection,'m');assert.equal(rows.reasoningDefault,null);
 assert.equal(rows[0].selected_llm,'m');assert.equal(rows[0].display_name,'Model');
 assert.equal(resolveSelection(rows,{model:'',reasoning:'off'}).valid,true);
 for(const selection of [{model:'missing'},{model:'m',version:'old'},{model:'m',reasoning:'high'}])assert.equal(resolveSelection(rows,selection).valid,false);
 assert.deepEqual(normalizeCatalog(catalog([])),[]);
 assert.equal(normalizeCatalog(catalog([model],{reasoning_default:'low'})).reasoningDefault,'low');
});
test('catalog rejects malformed and ambiguous public DTOs',()=>{
 for(const value of [null,[],{},catalog([model,model]),catalog([model],{default:'missing'}),catalog([model],{default:null}),catalog([model],{reasoning_default:'extra'}),catalog([model],{extra:1}),catalog(Array(257).fill(model)),...[
  {value:''},{description:'x'.repeat(513)},{label:'\n'},{version:2},{family:''},{extra:1},{max_output_tokens:0},{context_length:1.5},{features:['invented']},{features:['tools','tools']},{reasoning_efforts:['bad']},{reasoning_efforts:['off','off']},{reasoning_efforts:'off'}
 ].map(delta=>catalog([{...model,...delta}]))])assert.throws(()=>normalizeCatalog(value),/Invalid/);
 assert.equal(resolveSelection(normalizeCatalog(catalog([{...model,features:[],reasoning_efforts:null}])),{model:'m',reasoning:'off'}).valid,false);
});
test('named SSE is genuinely incremental across UTF-8 and CRLF byte splits',async()=>{
 const bytes=new TextEncoder().encode((start+frame('token',{content:'é'})+finish({...result,actual_model:'reported'})).replaceAll('\n','\r\n'));
 let offset=0;const seen=[],metadata=[];
 const stream=new ReadableStream({pull(controller){if(offset===bytes.length)controller.close();else controller.enqueue(bytes.slice(offset,++offset));}});
 await readResponse(new Response(stream,{headers:{'content-type':'text/event-stream'}}),{sessionId:id,onText:(...value)=>seen.push(value),onMetadata:value=>metadata.push(value)});
 assert.deepEqual(seen,[['é',false],['final',true]]);assert.deepEqual(metadata,[{actual_model:'reported',model:'reported'}]);
});
test('valid JSON result preserves nullable evidence and reported billing',async()=>{
 let metadata;await readResponse(response(JSON.stringify({...result,billing:{usd:null,request_id:null,generation_id:null}}),'application/json'),{sessionId:id,onMetadata:value=>metadata=value});assert.deepEqual(metadata,{});
 assert.deepEqual(await readJSONResponse(response('{}','application/json')),{});
});
test('invalid sequence, missing done and foreign sessions never become successful',async()=>{
 for(const text of ['',frame('token',{content:'early'}),start+frame('token',{content:'partial'}),start+frame('result',result),start+frame('done',{session_id:id}),start+frame('response_complete',{session_id:'foreign'}),start+finish(result)+frame('token',{content:'late'}),start+start,start+frame('unknown',{}),start+frame('token',{content:2}),start+frame('response_complete',{session_id:id,extra:1}),start+finish({...result,session_id:'foreign'}),start+finish(result).replace('event: done','event: done\nevent: done')])await assert.rejects(readResponse(response(text),{sessionId:id}));
 await assert.rejects(readResponse(response(start+finish(result)),{sessionId:'foreign'}),/session/);
});
test('errors and cancellation fail while named questions preserve explicit options',async()=>{
 for(const name of ['error','cancelled'])await assert.rejects(readResponse(response(start+frame(name,{session_id:id,code:'failed',message:'failure'}))),/failure|cancelled/);
 for(const kind of ['clarification','approval']){
  const question={id:'q',question:'Choose',options:kind==='clarification'?['One']:[{id:'yes',label:'Allow'}]};let seen;
  await readResponse(response(start+frame(kind,question)),{onClarification:value=>seen=value,onApproval:value=>seen=value});assert.deepEqual(seen,question);
  await assert.rejects(readResponse(response(start+frame(kind,{...question,options:[4]}))));
 }
 assert.equal(supportsContinuation,false);await assert.rejects(submitContinuation(),/not implemented/);
});
test('strict result validation rejects unsupported or dishonest evidence',async()=>{
 for(const value of [null,{...result,session_id:2},{...result,response_text:7},{...result,outcome:'fake'},{...result,usage:[]},{...result,usage:{}},{...result,usage:{...usage,x:1}},{...result,usage:{...usage,input_tokens:-1}},{...result,usage:{...usage,input_tokens:1,cached_input_tokens:2}},{...result,usage:{...usage,output_tokens:1,reasoning_tokens:2}},{...result,actual_model:''},{...result,extra:1},{...result,billing:{usd:-1,request_id:null,generation_id:null}},{...result,billing:[]},{...result,billing:{usd:0,request_id:'bad id',generation_id:null}}])await assert.rejects(readResponse(response(JSON.stringify(value),'application/json')));
});
test('bounded HTTP readers cancel malformed, oversized and aborted bodies',async()=>{
 for(const r of [response('Unavailable','application/json',503),response('','application/json',501),response('bad','text/plain'),new Response(null),response('x'.repeat(131073),'application/json'),new Response(new Uint8Array([255]),{headers:{'content-type':'application/json'}}),response('bad: field\n\n'),response('data: null\n\n')])await assert.rejects(readResponse(r));
 const controller=new AbortController();let cancelled=false;
 const r=new Response(new ReadableStream({pull(){controller.abort();},cancel(){cancelled=true;}}),{headers:{'content-type':'text/event-stream'}});
 await assert.rejects(readResponse(r,{signal:controller.signal}),{name:'AbortError'});assert.equal(cancelled,true);
 await assert.rejects(readResponse(response('{}','application/json'),{signal:controller.signal}),{name:'AbortError'});
});
test('SSE comments and allowed framing retain terminal validation',async()=>{
 await readResponse(response(': heartbeat\nid: 1\nretry: 50\n'+start+finish(result)));
 await assert.rejects(readResponse(response(start+'event: token\ndata: {"content":"partial"}\r')),/terminal|Incomplete/);
});
test('stream budget permits token plus authoritative terminal overhead and bounds frames separately',async()=>{
 const content='é'.repeat(20000);let text='';
 await readResponse(response(start+frame('token',{content})+finish({...result,response_text:content})),{onText:(value,replace)=>text=replace?value:text+value});assert.equal(text,content);
 await assert.rejects(readResponse(response(start+frame('token',{content:'x'.repeat(131073)}))),/frame.*byte limit/);
 await assert.rejects(readResponse(response(start+frame('token',{content:''}).repeat(8192))),/event limit/);
 await assert.rejects(readResponse(response(start+(': '+'x'.repeat(1000)+'\n\n').repeat(1050))),/byte limit/);
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
 const stream=new ReadableStream({pull(c){if(!sent){sent=true;c.enqueue(new TextEncoder().encode(start+finish(result)));}else c.close();},cancel(){return Promise.reject(Error('cleanup'));}});
 await readResponse(new Response(stream,{headers:{'content-type':'text/event-stream'}}));
});
test('callback failure survives rejected cleanup cancellation',async()=>{
 const stream=new ReadableStream({start(c){c.enqueue(new TextEncoder().encode(start+frame('token',{content:'partial'})));},cancel(){return Promise.reject(Error('cleanup failed'));}});
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
test('nullable counts, default question callbacks and optional session observer retain strict boundaries',async()=>{
 await readResponse(response(JSON.stringify({...result,usage:{input_tokens:0,output_tokens:0,cached_input_tokens:0,cache_write_tokens:0,reasoning_tokens:0}}),'application/json'));
 for(const kind of ['clarification','approval'])await readResponse(response(start+frame(kind,{id:'q',question:'Q',options:[]})));
 let admitted;await readResponse(response(start+finish(result)),{onSession:value=>admitted=value});assert.equal(admitted.session_id,id);
 await assert.rejects(readResponse(response(start+frame('error',{session_id:id,code:'failed'}))),/Agent stream failed/);
 await assert.rejects(readResponse(response(frame('clarification',{id:'q',question:'Q',options:[]}))),/question sequence/);
 await assert.rejects(readResponse(response('x'.repeat(131073))),/frame.*byte limit/);
 const unicode={...model,value:'😀'.repeat(150),version:'😀'.repeat(70)};
 assert.equal(normalizeCatalog(catalog([unicode],{default:unicode.value}))[0].version,unicode.version);
 assert.throws(()=>normalizeCatalog(catalog([{...model,context_length:2147483648}])));
});
test('known totals permit unknown component counts without inventing component evidence',async()=>{
 await readResponse(response(JSON.stringify({...result,usage:{...usage,input_tokens:0,output_tokens:0}}),'application/json'));
});
test('application result rejects legacy text and every completion frame binds its admitted session',async()=>{
 await assert.rejects(readResponse(response(JSON.stringify({...result,text:'legacy'}),'application/json')),/Unsupported/);
 await assert.rejects(readResponse(response(start+frame('response_complete',{session_id:'foreign'})+frame('result',result)+frame('done',{session_id:id}))),/session/);
 const other={...model,value:'other',label:'Other'};const rows=normalizeCatalog(catalog([other,model]));assert.equal(resolveSelection(rows,{model:''}).selection.selected_llm,'m');
});
test('CRLF framing counts both raw separator bytes independently of semantic JSON size',async()=>{
 const oversized='event: token\r\n'+':\r\n'.repeat(44000)+'data: {"content":"x"}\r\n\r\n';
 assert.ok(new TextEncoder().encode(oversized).byteLength>131072);
 await assert.rejects(readResponse(response(start+oversized+finish(result))),/frame.*byte limit/);
});

for(const [name,ending] of [['missing both LF delimiters',frame('done',{session_id:id}).slice(0,-2)],['missing blank line',frame('done',{session_id:id}).slice(0,-1)],['single trailing CR',frame('done',{session_id:id}).slice(0,-2)+'\r']])test('truncated terminal framing rejects '+name,async()=>{
 await assert.rejects(readResponse(response(start+frame('response_complete',{session_id:id})+frame('result',result)+ending)),/Incomplete|terminal/);
});
for(const [name,newline] of [['LF','\n'],['CRLF','\r\n'],['CR','\r']])test('complete '+name+' delimiters admit terminal and question frames with trailing comments',async()=>{
 await readResponse(response((start+finish(result)).replaceAll('\n',newline)+': trailing comment'));
 let question;await readResponse(response((start+frame('clarification',{id:'q',question:'Q',options:['One']})).replaceAll('\n',newline)+': trailing comment'+newline),{onClarification:value=>question=value});assert.equal(question.id,'q');
});
