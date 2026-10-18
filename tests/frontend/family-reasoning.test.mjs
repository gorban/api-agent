import test from 'node:test';
import assert from 'node:assert/strict';
import {normalizeCatalog, reasoningEfforts, resolveSelection} from '../../apps/api-agent-ui/public/catalog.js';
const row = (value, family, reasoning_efforts, features=['reasoning_effort']) => ({value,label:value,description:'Synthetic capability',features,version:value,family,reasoning_efforts,max_output_tokens:null,context_length:null});
function catalog(rows) { return normalizeCatalog({models:rows,default:'default',reasoning_default:null}); }
const base = row('default','Other',['off']);
const family = {model:'',family:'Chosen',version:'',reasoning:''};
test('unresolved family efforts are the verified intersection without resolving a version',()=>{
  const rows=catalog([base,row('one','Chosen',['off','low','high']),row('two','Chosen',['off','low','medium'])]);
  assert.deepEqual(reasoningEfforts(rows,family),['off','low']);
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
