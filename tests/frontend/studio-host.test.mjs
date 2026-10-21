import test from 'node:test';
import assert from 'node:assert/strict';
import { once } from 'node:events';
import { createStudioServer, origin } from '../../apps/api-agent-ui/server.mjs';

const env = { UI_LISTENER_URL: 'http://127.0.0.1:9443', AGENT_DJANGO_URL: 'https://localhost:8443', API_AGENT_UI_LAUNCH_TOKEN: 'a'.repeat(32) };

test('host bounds origins and launch identity before listening', () => {
  for (const value of ['file:///etc/passwd', 'http://localhost:0', 'https://127.0.0.1:0', 'https://user:pass@localhost', 'https://localhost/path', 'https://localhost/?secret=yes', 'https://localhost/#secret']) assert.throws(() => origin(value));
  assert.throws(() => origin('http://example.com', {listener:true}));
  assert.throws(() => origin('invalid'));
  assert.throws(() => createStudioServer({...env, API_AGENT_UI_LAUNCH_TOKEN:'bad'}));
  assert.throws(() => createStudioServer({...env, UI_LISTENER_URL:'https://localhost:9443'}));
});

test('explicit routes expose bounded config and identity; reject traversal and writes', async t => {
  const {server} = createStudioServer(env);
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  t.after(() => {server.close(); server.closeAllConnections();});
  const base = `http://127.0.0.1:${server.address().port}`;
  const response = await fetch(base + '/');
  assert.equal(response.status, 200);
  assert.equal(response.headers.get('x-ui-launch-token'), env.API_AGENT_UI_LAUNCH_TOKEN);
  assert.match(await response.text(), /<html/);
  assert.deepEqual(await (await fetch(base+'/config.json')).json(), {apiOrigin:'https://localhost:8443'});
  assert.equal((await fetch(base+'/config.json', {method:'HEAD'})).status, 200);
  assert.equal(await (await fetch(base+'/', {method:'HEAD'})).text(), '');
  for (const path of ['/server.mjs', '/.env', '/%2e%2e/server.mjs', '/index.html?secret=1', '/missing', '/LICENSE.marked']) assert.equal((await fetch(base+path)).status,404);
  assert.equal((await fetch(base+'/', {method:'POST'})).status,405);
});

test('production host serves the complete referenced frontend asset graph', async t => {
  const {server} = createStudioServer(env);
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  t.after(() => {server.close(); server.closeAllConnections();});
  const base = `http://127.0.0.1:${server.address().port}`;
  const pending = ['/'], seen = new Set();
  while (pending.length) {
    const path = pending.pop();
    if (seen.has(path)) continue;
    seen.add(path);
    const response = await fetch(base + path);
    assert.equal(response.status, 200, `Referenced asset unavailable: ${path}`);
    const type = response.headers.get('content-type');
    if (!/html|javascript|css/.test(type)) continue;
    const body = await response.text();
    const references = type.includes('html')
      ? [...body.matchAll(/(?:src|href)=["']([^"']+)["']/g)]
      : type.includes('javascript')
        ? [...body.matchAll(/^\s*(?:import|export)\s+(?:[^;\n]*?\s+from\s+)?["']([^"']+)["']/gm)]
        : [...body.matchAll(/url\(["']?([^\s)"']+)["']?\)/g)];
    for (const [, reference] of references) {
      const url = new URL(reference, base + path);
      if (url.origin === base && !reference.startsWith('#')) pending.push(url.pathname);
    }
  }
  assert.ok(seen.has('/catalog.js'));
  assert.ok(seen.has('/reply.js'));
  assert.ok(seen.has('/Geist.woff2'));
});

import { execFileSync, spawn } from 'node:child_process';
import { mkdtempSync, rmSync, readFileSync, copyFileSync, mkdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import https from 'node:https';
import net from 'node:net';
import { launch, listenerPort } from '../../apps/api-agent-ui/server.mjs';

test('verified TLS serves exact process identity and launch owns shutdown', async t => {
  const directory = mkdtempSync(join(tmpdir(), 'studio-host-'));
  t.after(() => rmSync(directory, {recursive:true, force:true}));
  const cert = join(directory, 'cert.pem'), key = join(directory, 'key.pem');
  execFileSync('openssl', ['req','-x509','-newkey','rsa:2048','-nodes','-keyout',key,'-out',cert,'-days','1','-subj','/CN=localhost','-addext','subjectAltName=DNS:localhost,IP:127.0.0.1'], {stdio:'ignore'});
  const tlsenv = {...env, UI_LISTENER_URL:'https://127.0.0.1:9443', API_AGENT_UI_TLS_CERT:cert, API_AGENT_UI_TLS_KEY:key};
  const {server} = createStudioServer(tlsenv);
  server.listen(0, '127.0.0.1'); await once(server,'listening');
  t.after(() => {server.close();server.closeAllConnections();});
  const identity = await new Promise((resolve,reject) => {
    https.get({host:'127.0.0.1',port:server.address().port,ca:readFileSync(cert)}, response => {response.resume();resolve(response.headers['x-ui-launch-token']);}).on('error',reject);
  });
  assert.equal(identity,env.API_AGENT_UI_LAUNCH_TOKEN);
  const blocker = createStudioServer(env).server;
  blocker.listen(0,'127.0.0.1'); await once(blocker,'listening');
  const port=blocker.address().port;
  await assert.rejects(launch({...env,UI_LISTENER_URL:`http://127.0.0.1:${port}`}));
  await new Promise(resolve=>blocker.close(resolve));
  const previous=process.env;
  process.env={...env,UI_LISTENER_URL:`http://127.0.0.1:${port}`};
  let launched;
  try { launched=await launch(); } finally {process.env=previous;}
  const closed=once(launched,'close');
  process.emit('SIGTERM'); await closed;
});

test('standalone launch rejects invalid configuration with bounded diagnostics', async () => {
  const child=spawn(process.execPath,['apps/api-agent-ui/launch.mjs'], {cwd:new URL('../../',import.meta.url),env:{...process.env,UI_LISTENER_URL:'file:///bad'}});
  let diagnostic='';child.stderr.on('data', chunk=>diagnostic+=chunk);
  const [code]=await once(child,'exit');
  assert.equal(code,1);assert.equal(diagnostic,'studio_host_start_failed\n');
});

 test('origin defaults and standard ports remain deterministic', () => {
  assert.equal(listenerPort(origin('http://localhost')),80);
  assert.equal(listenerPort(origin('https://localhost')),443);
  assert.equal(listenerPort(origin('http://localhost:9000')),9000);
  assert.throws(()=>createStudioServer({}));
  const original=process.env;
  process.env={...env};
  try { const {server}=createStudioServer();server.close(); } finally {process.env=original;}
 });

test('relocated installed host reads its own resources and handles absent assets', async t => {
 const dir=mkdtempSync(join(tmpdir(),'studio-installed-'));
 t.after(()=>rmSync(dir,{recursive:true,force:true}));
 copyFileSync(new URL('../../apps/api-agent-ui/server.mjs',import.meta.url),join(dir,'server.mjs'));
 mkdirSync(join(dir,'public'));
 for(const name of ['index.html','marked.js','dompurify.js']) {
  copyFileSync(new URL('../../apps/api-agent-ui/public/'+name,import.meta.url),join(dir,'public',name));
 }
 const module=await import('file://'+join(dir,'server.mjs'));
 const {server}=module.createStudioServer(env);server.listen(0,'127.0.0.1');await once(server,'listening');
 t.after(()=>{server.close();server.closeAllConnections();});
 const base=`http://127.0.0.1:${server.address().port}`;
 assert.equal((await fetch(base+'/')).status,200);
 assert.equal((await fetch(base+'/style.css')).status,404);
});

test('an allowlisted asset read failure returns a bounded 404', async () => {
  const script = `
    import fs from 'node:fs';
    import {syncBuiltinESMExports} from 'node:module';
    import {once} from 'node:events';
    const read=fs.readFileSync;
    fs.readFileSync=(path,...args)=>{if(String(path).endsWith('/Geist.woff2'))throw new Error('missing');return read(path,...args);};
    syncBuiltinESMExports();
    const {createStudioServer}=await import('./apps/api-agent-ui/server.mjs');
    const {server}=createStudioServer(${JSON.stringify(env)});
    server.listen(0,'127.0.0.1');await once(server,'listening');
    const response=await fetch('http://127.0.0.1:'+server.address().port+'/Geist.woff2');
    if(response.status!==404 || await response.text()!=='')process.exitCode=1;
    server.close();server.closeAllConnections();
  `;
  const child=spawn(process.execPath,['--input-type=module','-e',script],{cwd:new URL('../../',import.meta.url),stdio:'pipe'});
  const [code]=await once(child,'exit');assert.equal(code,0);
});

test('SIGTERM closes an accepted connection before its TLS handshake completes', {timeout:10000}, async t => {
  const directory=mkdtempSync(join(tmpdir(),'studio-stalled-tls-'));
  t.after(()=>rmSync(directory,{recursive:true,force:true}));
  const cert=join(directory,'cert.pem'),key=join(directory,'key.pem');
  execFileSync('openssl',['req','-x509','-newkey','rsa:2048','-nodes','-keyout',key,'-out',cert,'-days','1','-subj','/CN=localhost'],{stdio:'ignore'});
  const childEnv={...env,UI_LISTENER_URL:'https://127.0.0.1:9443',API_AGENT_UI_TLS_CERT:cert,API_AGENT_UI_TLS_KEY:key};
  const script=`
    import net from 'node:net';
    import {launch} from './apps/api-agent-ui/server.mjs';
    const listen=net.Server.prototype.listen;
    // Allocate an ephemeral port without changing the real launch or shutdown path.
    net.Server.prototype.listen=function(port,host,callback){return listen.call(this,0,host,callback);};
    const server=await launch(${JSON.stringify(childEnv)});
    server.once('connection',()=>process.send({accepted:true}));
    process.send({port:server.address().port});
  `;
  const child=spawn(process.execPath,['--input-type=module','-e',script],{cwd:new URL('../../',import.meta.url),stdio:['ignore','pipe','pipe','ipc']});
  let socket;
  t.after(async()=>{
    socket?.destroy();
    if(child.exitCode===null && child.signalCode===null){const exited=once(child,'exit');child.kill('SIGKILL');await exited;}
  });
  const [{port}]=await once(child,'message');
  const accepted=once(child,'message');
  socket=net.createConnection({host:'127.0.0.1',port});
  await once(socket,'connect');
  assert.deepEqual((await accepted)[0],{accepted:true});
  const closed=once(socket,'close'),exited=once(child,'exit');
  child.kill('SIGTERM');
  let timer;
  try{
    const [exit]=await Promise.race([
      Promise.all([exited,closed]),
      new Promise((_,reject)=>{timer=setTimeout(()=>reject(Error('Stalled TLS socket survived shutdown')),5000);}),
    ]);
    assert.deepEqual(exit,[0,null]);
  }finally{clearTimeout(timer);}
});
test('optional ephemeral access token has exact bounds and config header shape', async t => {
  for (const token of ['', 'a'.repeat(31), 'a'.repeat(129), 'a'.repeat(31)+'!', 32, null]) assert.throws(() => createStudioServer({...env,API_AGENT_UI_ACCESS_TOKEN:token}),/invalid_ui_access_token/);
  for (const token of ['a'.repeat(32),'_-'.repeat(64)]) {
    const {server}=createStudioServer({...env,API_AGENT_UI_ACCESS_TOKEN:token});
    server.listen(0,'127.0.0.1');await once(server,'listening');
    t.after(() => {server.close();server.closeAllConnections();});
    const response=await fetch(`http://127.0.0.1:${server.address().port}/config.json`);
    assert.deepEqual(await response.json(),{apiOrigin:env.AGENT_DJANGO_URL,apiHeaders:{'X-API-Agent-Session':token}});
    assert.equal(response.headers.get('cache-control'),'no-store');
    assert.equal(response.headers.get('x-api-agent-session'),null);
  }
});

test('managed parent stdin EOF stops host; manual host ignores stdin EOF', {timeout:10000}, async t => {
  for (const managed of [true, false]) {
    const childEnv={...env, ...(managed ? {API_AGENT_UI_PARENT_LIVENESS:'stdin-v1'} : {})};
    const script=`
      import net from 'node:net';
      import {launch} from './apps/api-agent-ui/server.mjs';
      const listen=net.Server.prototype.listen;
      net.Server.prototype.listen=function(port,host,callback){return listen.call(this,0,host,callback);};
      const server=await launch(${JSON.stringify(childEnv)});
      process.send({port:server.address().port});
    `;
    const child=spawn(process.execPath,['--input-type=module','-e',script],{cwd:new URL('../../',import.meta.url),stdio:['pipe','pipe','pipe','ipc']});
    t.after(async()=>{if(child.exitCode===null && child.signalCode===null){const exited=once(child,'exit');child.kill('SIGKILL');await exited;}});
    const [{port}]=await once(child,'message');
    assert.equal((await fetch(`http://127.0.0.1:${port}/`)).status,200);
    const exited=once(child,'exit');
    child.stdin.end();
    if (!managed) {
      await new Promise(resolve=>setTimeout(resolve,80));
      assert.equal(child.exitCode,null);
      assert.equal((await fetch(`http://127.0.0.1:${port}/`)).status,200);
      child.kill('SIGTERM');
    }
    assert.deepEqual(await exited,[0,null]);
    await assert.rejects(fetch(`http://127.0.0.1:${port}/`));
  }
});
