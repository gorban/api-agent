import test from 'node:test';
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {mkdtemp, writeFile, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {createConnection} from 'node:net';
import {fileURLToPath, pathToFileURL} from 'node:url';

function refusesConnection(port) {
  return new Promise((resolve,reject)=>{
    const socket=createConnection({host:'127.0.0.1',port});
    socket.once('connect',()=>{socket.destroy();reject(Error('A failed setup retained a listener'));});
    socket.once('error',error=>error.code==='ECONNREFUSED' ? resolve() : reject(error));
  });
}
test('missing browser executable releases both setup listeners and exits', {timeout:15000}, async t=>{
  const directory=await mkdtemp(join(tmpdir(),'studio-setup-'));
  t.after(()=>rm(directory,{recursive:true,force:true}));
  const preload=join(directory,'listener-receipts.mjs');
  await writeFile(preload,`import {Server} from 'node:net';
const listen=Server.prototype.listen;
Server.prototype.listen=function(...args){this.once('listening',()=>process.stdout.write('OWNED_PORT:'+this.address().port+'\\n'));return listen.apply(this,args);};
`);
  const environment={...process.env,STUDIO_BROWSER:join(directory,'missing-browser')};
  delete environment.NODE_TEST_CONTEXT;
  delete environment.STUDIO_ISTANBUL_FILE;
  delete environment.STUDIO_COVERAGE_FILE;
  delete environment.NODE_V8_COVERAGE;
  const child=spawn(process.execPath,['--import',pathToFileURL(preload).href,'--test','--test-name-pattern=version switch and New chat submit exact typed draft',fileURLToPath(new URL('./studio-new-chat.test.mjs',import.meta.url))],{
    env:environment,stdio:['ignore','pipe','pipe']
  });
  let output='';child.stdout.on('data',chunk=>output+=chunk);child.stderr.on('data',chunk=>output+=chunk);
  const outcome=await new Promise((resolve,reject)=>{
    const timer=setTimeout(()=>{child.kill('SIGKILL');},10000);
    child.once('error',error=>{clearTimeout(timer);reject(error);});
    child.once('close',(code,signal)=>{clearTimeout(timer);resolve({code,signal});});
  });

  assert.equal(outcome.signal,null,'Setup failure must exit without watchdog termination');
  assert.equal(outcome.code,1,output);
  assert.match(output,/browserType\.launch/);
  const ports=[...output.matchAll(/OWNED_PORT:(\d+)/g)].map(match=>Number(match[1]));
  assert.equal(ports.length,2,'The failed launch must have acquired both fixture listeners');
  await Promise.all(ports.map(refusesConnection));
});
