import test from 'node:test';
import assert from 'node:assert/strict';
import {normalizeCatalog, reasoningEfforts, resolveSelection} from '../../apps/api-agent-ui/public/catalog.js';
const row = (value, family, reasoning_efforts, features=['reasoning_effort']) => ({value,label:value,description:'Synthetic capability',features,version:value,family,reasoning_efforts,max_output_tokens:null,context_length:null});
function catalog(rows) { return normalizeCatalog({models:rows,default:'default',reasoning_default:null}); }
const base = row('default','Other',['off']);
const family = {model:'',family:'Chosen',version:'',reasoning:''};
test('differing family effort sets disable reasoning without an explicit version',()=>{
  const rows=catalog([base,row('one','Chosen',['off','low','high']),row('two','Chosen',['off','low','medium'])]);
  assert.deepEqual(reasoningEfforts(rows,family),[]);
  assert.equal(resolveSelection(rows,family).valid,false);
  assert.equal(resolveSelection(rows,{...family,reasoning:'off'}).selection,undefined);
});
test('common capabilities preserve the complete advertised effort set',()=>{
  const rows=catalog([base,row('one','Chosen',['off','low','medium','high']),row('two','Chosen',['off','low','medium','high'])]);
  assert.deepEqual(reasoningEfforts(rows,family),['off','low','medium','high']);
});
for (const [name, values] of [['unknown',null],['empty',[]]]) test(`one ${name} family row prevents invented common support`,()=>{
  const rows=catalog([base,row('one','Chosen',['off','low']),row('two','Chosen',values)]);
  assert.deepEqual(reasoningEfforts(rows,family),[]);
});
test('unverified feature support prevents choices even when an effort list exists',()=>{
  const rows=catalog([base,row('one','Chosen',['off']),row('two','Chosen',['off'],[])]);
  assert.deepEqual(reasoningEfforts(rows,family),[]);
});
test('exact selections use their own verified capabilities and stale intent gets no fallback',()=>{
  const rows=catalog([base,row('one','Chosen',['off','low']),row('unknown',null,null)]);
  assert.deepEqual(reasoningEfforts(rows,{model:'one'}),['off','low']);
  assert.deepEqual(reasoningEfforts(rows,{model:'unknown'}),[]);
  assert.deepEqual(reasoningEfforts(rows,{model:'missing'}),[]);
  assert.deepEqual(reasoningEfforts(rows,{model:'',family:'Missing'}),[]);
  assert.deepEqual(reasoningEfforts(rows,{...family,model:'missing'}),[]);
  assert.deepEqual(reasoningEfforts(rows,{...family,version:'stale'}),[]);
  assert.deepEqual(reasoningEfforts(rows,{model:''}),['off']);
});

test('family equality ignores effort ordering and includes every version even the global default',()=>{
  const rows=catalog([row('default','Chosen',['high','off']),row('two','Chosen',['off','high'])]);
  assert.deepEqual(reasoningEfforts(rows,family),['off','high']);
  const mixed=catalog([row('default','Chosen',['off']),row('two','Chosen',['off','high'])]);
  assert.deepEqual(reasoningEfforts(mixed,family),[]);
  assert.deepEqual(reasoningEfforts(mixed,{model:'two',version:'two'}),['off','high']);
});

test('advertised family default resolves independently of global default and row order',()=>{
  const rows=normalizeCatalog({models:[base,row('new','Chosen',['off','low']),row('old','Chosen',['low','off'])],default:'default',reasoning_default:null,family_defaults:{Chosen:'new'}});
  assert.equal(resolveSelection(rows,family).selection.selected_llm,'new');
  assert.equal(resolveSelection(rows,{...family,reasoning:'low'}).valid,true);
  assert.equal(resolveSelection(rows,{...family,version:'old'}).selection.selected_llm,'old');
});
test('saved reasoning cannot bypass a mixed family gate when its default supports the effort',()=>{
  const rows=normalizeCatalog({models:[base,row('new','Chosen',['off','low']),row('old','Chosen',['off'])],default:'default',reasoning_default:null,family_defaults:{Chosen:'new'}});
  assert.deepEqual(reasoningEfforts(rows,family),[]);
  assert.equal(resolveSelection(rows,{...family,reasoning:'low'}).valid,false);
  assert.match(resolveSelection(rows,{...family,reasoning:'low'}).error,/before setting reasoning/);
});
for (const family_defaults of [null,[],{Chosen:'default'},{Missing:'default'},{Chosen:42}]) test('reject malformed or cross-family default '+JSON.stringify(family_defaults),()=>{
  assert.throws(()=>normalizeCatalog({models:[base,row('one','Chosen',['off'])],default:'default',reasoning_default:null,family_defaults}),/Invalid provider catalog/);
});

test('family default cannot contradict the configured exact default in its family',()=>{
  const input={models:[row('default','Chosen',['off']),row('other','Chosen',['off'])],default:'default',reasoning_default:null};
  assert.throws(()=>normalizeCatalog({...input,family_defaults:{Chosen:'other'}}),/Invalid provider catalog/);
  assert.equal(resolveSelection(normalizeCatalog({...input,family_defaults:{Chosen:'default'}}),family).selection.selected_llm,'default');
  assert.equal(resolveSelection(normalizeCatalog(input),family).selection.selected_llm,'default');
});

test('implicit configured family gates explicit reasoning but preserves exact and omitted intent',()=>{
 const mixed=normalizeCatalog({models:[row('default','Chosen',['off','low']),row('other','Chosen',['off'])],default:'default',reasoning_default:'low'});
 assert.deepEqual(reasoningEfforts(mixed,{model:'',version:'',reasoning:''}),[]);
 assert.match(resolveSelection(mixed,{model:'',version:'',reasoning:'low'}).error,/before setting reasoning/);
 assert.equal(resolveSelection(mixed,{model:'',version:'',reasoning:''}).valid,true);
 assert.equal(resolveSelection(mixed,{model:'default',version:'',reasoning:'low'}).valid,true);
 assert.deepEqual(reasoningEfforts(mixed,{model:'default',version:''}),['off','low']);
 const uniform=normalizeCatalog({models:[row('default','Chosen',['off','low']),row('other','Chosen',['low','off'])],default:'default',reasoning_default:null});
 assert.deepEqual(reasoningEfforts(uniform,{}),['off','low']);
 assert.equal(resolveSelection(uniform,{reasoning:'low'}).valid,true);
});
