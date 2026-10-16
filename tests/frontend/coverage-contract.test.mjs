import test from 'node:test';
import assert from 'node:assert/strict';
import {assertCoverageMembership} from './helpers/coverage-contract.mjs';
const browser=['app.js','appearance.js','markdown.js','reply.js'];
const node=['catalog.js','transport.js','server.mjs','launch.mjs'];
test('coverage gate requires every authored browser and Node module',()=>{
  assert.doesNotThrow(()=>assertCoverageMembership(browser,node));
  for(const missing of browser) assert.throws(()=>assertCoverageMembership(browser.filter(x=>x!==missing),node),new RegExp(missing.replace('.','\\.')));
  for(const missing of node) assert.throws(()=>assertCoverageMembership(browser,node.filter(x=>x!==missing)),new RegExp(missing.replace('.','\\.')));
});
test('empty observations cannot satisfy a vacuous 100 percent coverage gate',()=>{
  assert.throws(()=>assertCoverageMembership([],node),/Missing authored browser coverage/);
  assert.throws(()=>assertCoverageMembership(browser,[]),/Missing authored Node coverage/);
  assert.throws(()=>assertCoverageMembership([],[]),/Missing authored browser coverage/);
});
test('coverage observations must share immutable source identity',async()=>{
  const {assertCoverageHashes}=await import('./helpers/coverage-contract.mjs');
  assert.doesNotThrow(()=>assertCoverageHashes([{'app.js':{hash:'first'}},{'app.js':{hash:'first'},'reply.js':{hash:'other'}}]));
  assert.throws(()=>assertCoverageHashes([{'app.js':{} }]),/Missing coverage source identity/);
  assert.throws(()=>assertCoverageHashes([{'app.js':{hash:32}}]),/Missing coverage source identity/);
  assert.throws(()=>assertCoverageHashes([{'app.js':{hash:''}}]),/Missing coverage source identity/);
  assert.throws(()=>assertCoverageHashes([{'app.js':{hash:'first'}},{'app.js':{hash:'changed'}}]),/source changed/);
});
