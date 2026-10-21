import test from 'node:test';
import assert from 'node:assert/strict';
import * as fs from 'node:fs';
import path from 'node:path';
import {tmpdir} from 'node:os';
import {fileURLToPath, pathToFileURL} from 'node:url';
import {execFileSync} from 'node:child_process';
import {prepareAssets, validateNodeVersion} from '../../tools/prepare_frontend_assets.mjs';

const source = fileURLToPath(new URL('../../', import.meta.url));
const manifest = JSON.parse(fs.readFileSync(path.join(source, 'tools/frontend-assets.json')));
function fixture(t) {
  const root = fs.mkdtempSync(path.join(tmpdir(), 'frontend-assets-'));
  t.after(() => fs.rmSync(root, {recursive: true, force: true}));
  const copy = relative => {
    const target = path.join(root, relative);
    fs.mkdirSync(path.dirname(target), {recursive: true});
    fs.copyFileSync(path.join(source, relative), target);
  };
  for (const relative of ['package.json', 'package-lock.json', 'tools/frontend-assets.json']) copy(relative);
  for (const entry of manifest.assets) {
    copy('node_modules/' + entry.package + '/package.json');
    copy('node_modules/' + entry.package + '/' + entry.source);
    fs.mkdirSync(path.dirname(path.join(root, entry.target)), {recursive: true});
  }
  return root;
}
function editJson(root, relative, change) {
  const location = path.join(root, relative);
  const value = JSON.parse(fs.readFileSync(location));
  change(value);
  fs.writeFileSync(location, JSON.stringify(value));
}

test('locked preparation copies all official bytes, notices and remains idempotent offline', t => {
  const root = fixture(t);
  assert.equal(prepareAssets({root}), 11);
  const identities = manifest.assets.map(entry => fs.statSync(path.join(root, entry.target)).ino);
  for (const entry of manifest.assets) {
    assert.deepEqual(fs.readFileSync(path.join(root, entry.target)),
      fs.readFileSync(path.join(root, 'node_modules', entry.package, entry.source)));
  }
  assert.equal(prepareAssets({root, checkOnly: true}), 11);
  assert.equal(prepareAssets({root}), 11);
  assert.deepEqual(manifest.assets.map(entry => fs.statSync(path.join(root, entry.target)).ino), identities);
});

test('check mode never creates missing runtime outputs', t => {
  const root = fixture(t);
  assert.throws(() => prepareAssets({root, checkOnly: true}), /Run npm ci/);
  assert.equal(fs.existsSync(path.join(root, manifest.assets[0].target)), false);
});

test('modified upstream asset rejects before any copying', t => {
  const root = fixture(t);
  const entry = manifest.assets[0];
  fs.appendFileSync(path.join(root, 'node_modules', entry.package, entry.source), '\nchanged');
  assert.throws(() => prepareAssets({root}), /reviewed upstream bytes/);
  assert.equal(fs.existsSync(path.join(root, entry.target)), false);
});

test('unknown runtime output is preserved rather than overwritten', t => {
  const root = fixture(t);
  const output = path.join(root, manifest.assets[0].target);
  fs.writeFileSync(output, 'human content');
  assert.throws(() => prepareAssets({root}), /preserve\/remove/);
  assert.equal(fs.readFileSync(output, 'utf8'), 'human content');
});

test('symlink runtime output cannot overwrite an outside file', t => {
  const root = fixture(t);
  const outside = path.join(root, 'outside');
  fs.writeFileSync(outside, 'human');
  fs.symlinkSync(outside, path.join(root, manifest.assets[0].target));
  assert.throws(() => prepareAssets({root}), /bounded regular file/);
  assert.equal(fs.readFileSync(outside, 'utf8'), 'human');
});

test('symlinked npm source parent is rejected', t => {
  const root = fixture(t);
  const packagePath = path.join(root, 'node_modules/marked');
  fs.renameSync(packagePath, packagePath + '-retained');
  fs.symlinkSync(packagePath + '-retained', packagePath, 'dir');
  assert.throws(() => prepareAssets({root}), /owned directory/);
});

test('manifest cannot redirect copies to first-party source or outside the repository', t => {
  const root = fixture(t);
  editJson(root, 'tools/frontend-assets.json', value => { value.assets[0].target = '../outside.js'; });
  assert.throws(() => prepareAssets({root}), /Invalid reviewed/);
  assert.equal(fs.existsSync(path.join(root, '../outside.js')), false);
});

test('npm source path cannot traverse into another package', t => {
  const root = fixture(t);
  editJson(root, 'tools/frontend-assets.json', value => { value.assets[0].source = '../dompurify/package.json'; });
  assert.throws(() => prepareAssets({root}), /Invalid frontend asset path/);
});

for (const [label, change] of [
  ['private registry', value => { value.packages['node_modules/marked'].resolved = 'https://registry.example.invalid/marked.tgz'; }],
  ['missing integrity', value => { delete value.packages['node_modules/marked'].integrity; }],
  ['wrong lock version', value => { value.lockfileVersion = 2; }],
  ['dependency drift', value => { value.packages['node_modules/marked'].version = '0.0.0'; }],
]) {
  test(label + ' fails before output publication', t => {
    const root = fixture(t);
    editJson(root, 'package-lock.json', change);
    assert.throws(() => prepareAssets({root}), /lock|integrity|manifest/);
    assert.equal(fs.existsSync(path.join(root, manifest.assets[0].target)), false);
  });
}

test('installed package identity and manifest inventory remain bounded', t => {
  const root = fixture(t);
  editJson(root, 'node_modules/marked/package.json', value => { value.version = '0.0.0'; });
  assert.throws(() => prepareAssets({root}), /Installed frontend package identity/);
});

test('Node version contract rejects unsupported runtimes and accepts compatible modern runtimes', () => {
  assert.throws(() => validateNodeVersion('23.9.0'), /Node.js 24/);
  assert.throws(() => validateNodeVersion('not-a-version'), /Node.js 24/);
  validateNodeVersion('24.18.0');
  validateNodeVersion('26.0.0');
});

test('source Studio startup fails before listening when prepared dependencies are absent', async t => {
  const root = fs.mkdtempSync(path.join(tmpdir(), 'missing-studio-assets-'));
  t.after(() => fs.rmSync(root, {recursive: true, force: true}));
  const server = path.join(root, 'server.mjs');
  fs.copyFileSync(path.join(source, 'apps/api-agent-ui/server.mjs'), server);
  const {createStudioServer} = await import(pathToFileURL(server).href);
  assert.throws(() => createStudioServer({}), /source checkout run npm ci/);
});

test('CLI prepares and checks only its own locked root and reports unsupported arguments', t => {
  const root = fixture(t);
  const helper = path.join(root, 'tools/prepare_frontend_assets.mjs');
  fs.copyFileSync(path.join(source, 'tools/prepare_frontend_assets.mjs'), helper);
  assert.match(execFileSync(process.execPath, [helper], {encoding: 'utf8'}), /Verified 11/);
  assert.match(execFileSync(process.execPath, [helper, '--check'], {encoding: 'utf8'}), /Verified 11/);
  assert.throws(() => execFileSync(process.execPath, [helper, '--invalid'], {stdio: 'pipe'}), error => {
    assert.match(error.stderr.toString(), /Usage:.*\nRun npm ci/s);
    return true;
  });
});

test('opening and reading altered inode metadata abort before publication', async t => {
  const root = fixture(t);
  const {default: mutableFs} = await import('node:fs');
  const {syncBuiltinESMExports} = await import('node:module');
  const original = mutableFs.fstatSync;
  try {
    mutableFs.fstatSync = descriptor => {
      const info = original(descriptor);
      info.ino += 1;
      return info;
    };
    syncBuiltinESMExports();
    assert.throws(() => prepareAssets({root}), /changed while opening/);
  } finally {
    mutableFs.fstatSync = original;
    syncBuiltinESMExports();
  }
  const read = mutableFs.readFileSync;
  try {
    mutableFs.readFileSync = (...args) => typeof args[0] === 'number' ? Buffer.alloc(0) : read(...args);
    syncBuiltinESMExports();
    assert.throws(() => prepareAssets({root}), /changed while reading/);
  } finally {
    mutableFs.readFileSync = read;
    syncBuiltinESMExports();
  }
});

test('repository CLI checks its prepared source and rejects excess arguments', () => {
  const helper = path.join(source, 'tools/prepare_frontend_assets.mjs');
  assert.match(execFileSync(process.execPath, [helper, '--check'], {encoding: 'utf8'}), /Verified 11/);
  assert.throws(() => execFileSync(process.execPath, [helper, '--check', 'extra'], {stdio: 'pipe'}), error => {
    assert.match(error.stderr.toString(), /Usage:/);
    return true;
  });
});

for (const [field, value] of [['version', null], ['sha256', null], ['size', 0], ['package', '../marked']]) {
  test('manifest rejects invalid ' + field + ' before publication', t => {
    const root = fixture(t);
    editJson(root, 'tools/frontend-assets.json', document => { document.assets[0][field] = value; });
    assert.throws(() => prepareAssets({root}), /Invalid reviewed/);
  });
}

test('missing lock URL and malformed manifest inventory are rejected', t => {
  const root = fixture(t);
  editJson(root, 'package-lock.json', document => { delete document.packages['node_modules/marked'].resolved; });
  assert.throws(() => prepareAssets({root}), /public registry/);
  const other = fixture(t);
  editJson(other, 'tools/frontend-assets.json', document => { document.version = 2; });
  assert.throws(() => prepareAssets({root: other}), /Invalid locked/);
});

test('post-copy changes are detected and never silently accepted', async t => {
  const root = fixture(t);
  const {default: mutableFs} = await import('node:fs');
  const {syncBuiltinESMExports} = await import('node:module');
  const original = mutableFs.fsyncSync;
  try {
    mutableFs.fsyncSync = descriptor => {
      original(descriptor);
      mutableFs.writeSync(descriptor, Buffer.from('x'), 0, 1, 0);
    };
    syncBuiltinESMExports();
    assert.throws(() => prepareAssets({root}), /changed during copying/);
  } finally {
    mutableFs.fsyncSync = original;
    syncBuiltinESMExports();
  }
});

test('source Studio dependency preflight reports preparation before configuration or listen', async () => {
  const {createStudioServer} = await import('../../apps/api-agent-ui/server.mjs');
  const {default: mutableFs} = await import('node:fs');
  const {syncBuiltinESMExports} = await import('node:module');
  const original = mutableFs.readFileSync;
  try {
    mutableFs.readFileSync = (location, ...options) => {
      if (location instanceof URL && location.pathname.endsWith('/public/marked.js')) {
        throw Object.assign(new Error('missing prepared asset'), {code: 'ENOENT'});
      }
      return original(location, ...options);
    };
    syncBuiltinESMExports();
    assert.throws(() => createStudioServer({}), /source checkout run npm ci/);
  } finally {
    mutableFs.readFileSync = original;
    syncBuiltinESMExports();
  }
});
