// Instruments authored browser modules without relaxing the production host CSP.
import {mkdir, readFile, readdir, writeFile, unlink, rm} from 'node:fs/promises';
import {spawn} from 'node:child_process';
import {once} from 'node:events';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {assertCoverageMembership, assertCoverageHashes} from './helpers/coverage-contract.mjs';
const root=new URL('../../',import.meta.url);
const output=new URL('artifacts/frontend-coverage/',root);
await mkdir(output,{recursive:true});
const nodeOutput=new URL('node-v8/',output);
await mkdir(nodeOutput,{recursive:true});
for(const name of await readdir(nodeOutput)) if(name.endsWith('.json')) await unlink(new URL(name,nodeOutput));
for(const name of await readdir(output)) if(/^node-ast-\d+\.json$/.test(name)) await unlink(new URL(name,output));
for(const name of ['summary.json','coverage.json','browser-observations.json','source-receipt.json'])await rm(new URL(name,output),{force:true});
const sourceFiles=['server.mjs','launch.mjs',...['app.js','appearance.js','markdown.js','reply.js','catalog.js','transport.js'].map(name=>'public/'+name)].map(name=>'apps/api-agent-ui/'+name);
async function sourceReceipt(){
  const testFiles=(await readdir(new URL('tests/frontend/',root))).filter(name=>name.endsWith('.mjs')).map(name=>'tests/frontend/'+name);
  for(const name of await readdir(new URL('tests/frontend/helpers/',root)))if(name.endsWith('.mjs'))testFiles.push('tests/frontend/helpers/'+name);
  const receipt={};
  for(const name of [...sourceFiles,...testFiles,'package.json','.gitignore'].sort())receipt[name]=createHash('sha256').update(await readFile(new URL(name,root))).digest('hex');
  return receipt;
}
const before=await sourceReceipt();
const observations=new URL('browser-observations.json',output);
const tests=(await readdir(new URL('tests/frontend/',root))).filter(name=>name.endsWith('.test.mjs')).map(name=>'tests/frontend/'+name);
const child=spawn(process.execPath,['--test','--experimental-test-coverage','--test-coverage-lines=100','--test-coverage-branches=100','--test-coverage-functions=100','--test-coverage-include=**/apps/api-agent-ui/**',...tests],{cwd:root,stdio:'inherit',env:{...process.env,STUDIO_ISTANBUL_FILE:observations.pathname,NODE_V8_COVERAGE:nodeOutput.pathname}});
const [code]=await once(child,'exit');
if(code!==0) process.exit(code || 1);
const pure=spawn(process.execPath,['--import',new URL('tests/frontend/helpers/instrument-node.mjs',root).href,'--test','tests/frontend/reply.test.mjs'],{cwd:root,stdio:'inherit',env:{...process.env,STUDIO_NODE_AST_DIR:output.href}});
const [pureCode]=await once(pure,'exit');
if(pureCode!==0)process.exit(pureCode || 1);
const coverageLibrary=await import(process.env.STUDIO_COVERAGE_LIBRARY || 'istanbul-lib-coverage');
const map=(coverageLibrary.default || coverageLibrary).createCoverageMap({});
const allObservations=JSON.parse(await readFile(observations,'utf8'));
for(const observation of allObservations)map.merge(observation);
const browserFiles=map.files().map(file=>path.basename(file));
for(const name of await readdir(output)) if(/^node-ast-\d+\.json$/.test(name)){const observation=JSON.parse(await readFile(new URL(name,output),'utf8'));allObservations.push(observation);map.merge(observation);}
assertCoverageHashes(allObservations);
const after=await sourceReceipt();
if(JSON.stringify(before)!==JSON.stringify(after))throw Error('Source or tests changed during frontend coverage verification');
await writeFile(new URL('source-receipt.json',output),JSON.stringify(after,null,2)+'\n');
const nodeFiles=new Set();
for(const name of await readdir(nodeOutput)){
  if(!name.endsWith('.json'))continue;
  for(const entry of JSON.parse(await readFile(new URL(name,nodeOutput),'utf8')).result) {
    if(entry.url.startsWith(new URL('apps/api-agent-ui/',root).href) && entry.functions.some(fn=>fn.ranges.some(range=>range.count>0))) nodeFiles.add(path.basename(new URL(entry.url).pathname));
  }
}
assertCoverageMembership(browserFiles,[...nodeFiles]);
const summary={};
for(const file of map.files()){
  const coverage=map.fileCoverageFor(file);
  summary[path.basename(file)]={...coverage.toSummary().toJSON(),uncoveredLines:coverage.getUncoveredLines(),uncoveredBranches:Object.entries(coverage.b).filter(([,hits])=>hits.some(hit=>hit===0)).map(([id,hits])=>({location:coverage.branchMap[id].loc,hits}))};
}
await writeFile(new URL('coverage.json',output),JSON.stringify(map.toJSON()));
await writeFile(new URL('summary.json',output),JSON.stringify(summary,null,2)+'\n');
process.stdout.write(JSON.stringify(summary,null,2)+'\n');

if(Object.values(summary).some(file=>file.lines.pct!==100 || file.branches.pct!==100)){
  process.stderr.write('Authored browser line/branch coverage remains below 100%; see artifacts/frontend-coverage/summary.json.\n');
  process.exitCode=1;
}
