/** Copy reviewed official npm assets; this helper and Python builds never fetch. */
import * as fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {createHash} from 'node:crypto';

const projectRoot = fileURLToPath(new URL('..', import.meta.url));
export const destinations = new Set([
  '.private_resources/viewer/marked.js',
  '.private_resources/viewer/dompurify.js',
  '.private_resources/viewer/heading-id.js',
  '.private_resources/viewer/marked-LICENSE.txt',
  '.private_resources/viewer/dompurify-LICENSE.txt',
  '.private_resources/viewer/heading-id-LICENSE.txt',
  '.private_resources/viewer/github-slugger-LICENSE.txt',
  'apps/api-agent-ui/public/marked.js',
  'apps/api-agent-ui/public/dompurify.js',
  'apps/api-agent-ui/marked-LICENSE.txt',
  'apps/api-agent-ui/dompurify-LICENSE.txt',
]);
const digest = bytes => createHash('sha256').update(bytes).digest('hex');
const guidance = 'Run npm ci from the repository root to prepare offline frontend assets.';

export function validateNodeVersion(version) {
  if (!/^\d+\.\d+\.\d+(?:[-+].*)?$/.test(version) || Number(version.split('.')[0]) < 24) {
    throw new Error('Frontend preparation requires Node.js 24 or newer.');
  }
}

function boundedPath(root, relative) {
  if (typeof relative !== 'string' || !relative || relative.length > 1024 ||
      relative.split('/').some(part => !part || part === '.' || part === '..' || /[\\\x00-\x1f\x7f]/.test(part)) ||
      path.isAbsolute(relative)) throw new Error('Invalid frontend asset path.');
  const pieces = relative.split('/');
  let current = root;
  for (const part of pieces.slice(0, -1)) {
    current = path.join(current, part);
    const info = fs.lstatSync(current);
    if (!info.isDirectory() || info.isSymbolicLink()) throw new Error('Frontend asset parent must be an owned directory, not a link.');
  }
  return path.join(root, ...pieces);
}

function readRegular(root, relative, limit = 2_000_000) {
  const location = boundedPath(root, relative);
  const info = fs.lstatSync(location);
  if (!info.isFile() || info.isSymbolicLink() || info.size > limit) throw new Error('Frontend asset must be a bounded regular file.');
  const descriptor = fs.openSync(location, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW);
  try {
    const opened = fs.fstatSync(descriptor);
    if (!opened.isFile() || opened.size > limit || opened.dev !== info.dev || opened.ino !== info.ino) {
      throw new Error('Frontend asset changed while opening.');
    }
    const bytes = fs.readFileSync(descriptor);
    if (bytes.length !== opened.size) throw new Error('Frontend asset changed while reading.');
    return bytes;
  } finally { fs.closeSync(descriptor); }
}

function readJson(root, relative, limit) {
  return parseUniqueJson(readRegular(root, relative, limit).toString('utf8'));
}


function parseUniqueJson(text) {
  const value = JSON.parse(text);
  const tokens = text.match(/"(?:\\.|[^"\\])*"|[{}\[\]:,]|[^\s{}\[\]:,]+/g) || [];
  const stack = [];
  for (let index = 0; index < tokens.length; index++) {
    const token = tokens[index];
    if (token === '{' || token === '[') stack.push(token === '{' ? new Set() : null);
    else if (token === '}' || token === ']') stack.pop();
    else if (token.startsWith('"') && tokens[index + 1] === ':') {
      const key = JSON.parse(token);
      const keys = stack.at(-1);
      if (!keys || keys.has(key)) throw new Error('Duplicate frontend context key.');
      keys.add(key);
    }
  }
  return value;
}

function relativeOwner(value, dot = false) {
  if (dot && value === '.') return value;
  if (typeof value !== 'string' || !value || value.length > 1024 || value.split('/').length > 64 ||
      value.startsWith('/') || /[?#]/.test(value) || value.split('/').some(part => !part || part === '.' || part === '..' || /[\\\x00-\x1f\x7f]/.test(part))) {
    throw new Error('Invalid frontend owner path.');
  }
  return value;
}


const excludedOwners = new Set(['node_modules', 'venv', '__pycache__', 'dist', 'build',
  'coverage', 'artifacts', 'site-packages', 'htmlcov']);
function ownerDirectory(root, relative) {
  let current = root;
  if (relative === '.') return;
  for (const part of relative.split('/')) {
    if (part.startsWith('.') || excludedOwners.has(part)) throw new Error('Excluded frontend owner directory.');
    current = path.join(current, part);
    const info = fs.lstatSync(current);
    if (!info.isDirectory() || info.isSymbolicLink()) throw new Error('Frontend owner must be an owned directory.');
    try { fs.lstatSync(path.join(current, '.git')); }
    catch (error) { if (error.code === 'ENOENT') continue; throw error; }
    throw new Error('Foreign repository frontend owner rejected.');
  }
}

export function ownerRoots(root) {
  let document;
  try { document = readJson(root, 'cfg/workflow-view-roots.json', 65536); }
  catch (error) { if (error.code === 'ENOENT') return ['.']; throw error; }
  const object = value => value && typeof value === 'object' && !Array.isArray(value);
  if (!object(document) || Object.keys(document).sort().join(',') !== 'entries,version' || document.version !== 1 ||
      !object(document.entries) || Object.keys(document.entries).length > 1000) throw new Error('Invalid frontend owner contexts.');
  const owners = new Set(['.']);
  for (const [entry, context] of Object.entries(document.entries)) {
    relativeOwner(entry);
    if (path.posix.basename(entry) !== 'flow.md.html' || !object(context) ||
        Object.keys(context).sort().join(',') !== 'base_root,entry_root,pack_root' ||
        !['base', 'pack'].includes(context.entry_root)) throw new Error('Invalid frontend owner context.');
    ownerDirectory(root, path.posix.dirname(entry));
    readRegular(root, entry, 8 * 1024 * 1024);
    const values = [relativeOwner(context.base_root, true), relativeOwner(context.pack_root, true)];
    if (values.every(value => value === '.') && context.entry_root === 'base') throw new Error('Redundant frontend owner context.');
    const selected = context[context.entry_root + '_root'];
    if (selected !== '.' && !entry.startsWith(selected + '/')) throw new Error('Frontend entry is outside selected owner.');
    for (const owner of values) {
      ownerDirectory(root, owner);
      owners.add(owner);
    }
  }
  return [...owners].sort();
}

function ensureOutputParents(root, relative) {
  let current = root;
  for (const part of relative.split('/').slice(0, -1)) {
    current = path.join(current, part);
    try { fs.mkdirSync(current); }
    catch (error) { if (error.code !== 'EEXIST') throw error; }
    const info = fs.lstatSync(current);
    if (!info.isDirectory() || info.isSymbolicLink()) throw new Error('Frontend asset parent must be an owned directory, not a link.');
  }
}

export function prepareAssets({root = projectRoot, checkOnly = false} = {}) {
  validateNodeVersion(process.versions.node);
  root = fs.realpathSync(root);
  const manifest = readJson(root, 'tools/frontend-assets.json');
  const definition = readJson(root, 'package.json');
  const lock = readJson(root, 'package-lock.json');
  if (manifest.version !== 1 || !Array.isArray(manifest.assets) ||
      manifest.assets.length !== destinations.size || lock.lockfileVersion !== 3 ||
      !lock.packages || !lock.packages['']) throw new Error('Invalid locked frontend asset manifest.');
  for (const [name, details] of Object.entries(lock.packages)) {
    if (!name) continue;
    if (!name.startsWith('node_modules/') || !/^https:\/\/registry\.npmjs\.org\/[A-Za-z0-9@/_.%~-]+$/.test(details.resolved || '') ||
        !/^sha512-[A-Za-z0-9+/]+={0,2}$/.test(details.integrity || '')) {
      throw new Error('Frontend lock entries require public registry URLs and SHA-512 integrity.');
    }
  }
  const owners = ownerRoots(root);
  const seen = new Set();
  const prepared = [];
  for (const entry of manifest.assets) {
    if (!entry || typeof entry.package !== 'string' || !/^[a-z0-9][a-z0-9-]*$/.test(entry.package) ||
        !/^\d+\.\d+\.\d+$/.test(entry.version || '') || !destinations.has(entry.target) || seen.has(entry.target) ||
        !/^[a-f0-9]{64}$/.test(entry.sha256 || '') || !Number.isSafeInteger(entry.size) || entry.size < 1 || entry.size > 2_000_000) {
      throw new Error('Invalid reviewed frontend asset entry.');
    }
    seen.add(entry.target);
    if (definition.dependencies?.[entry.package] !== entry.version ||
        lock.packages[''].dependencies?.[entry.package] !== entry.version ||
        lock.packages['node_modules/' + entry.package]?.version !== entry.version) {
      throw new Error('Frontend dependency version differs from the reviewed lock.');
    }
    const installed = readJson(root, 'node_modules/' + entry.package + '/package.json');
    if (installed.name !== entry.package || installed.version !== entry.version) throw new Error('Installed frontend package identity differs from the lock.');
    const bytes = readRegular(root, 'node_modules/' + entry.package + '/' + entry.source);
    if (bytes.length !== entry.size || digest(bytes) !== entry.sha256) throw new Error('Installed frontend asset differs from the reviewed upstream bytes.');
    const targets = entry.target.startsWith('.private_resources/viewer/')
      ? owners.map(owner => owner === '.' ? entry.target : owner + '/' + entry.target)
      : [entry.target];
    for (const relative of targets) {
      if (!checkOnly) ensureOutputParents(root, relative);
      const target = boundedPath(root, relative);
      let existing;
      try { existing = readRegular(root, relative); }
      catch (error) { if (error.code !== 'ENOENT') throw error; }
      if (existing && (existing.length !== entry.size || digest(existing) !== entry.sha256)) {
        throw new Error('Existing frontend output differs from reviewed bytes; preserve/remove that generated file explicitly before npm ci.');
      }
      if (checkOnly && !existing) throw new Error(guidance);
      prepared.push({entry: {...entry, target: relative}, bytes, target, existing});
    }
  }

  for (const {entry, bytes, target, existing} of prepared) {
    if (existing || checkOnly) continue;
    // Exclusive creation never follows or overwrites an unexpected final inode.
    const descriptor = fs.openSync(target, fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_EXCL | fs.constants.O_NOFOLLOW, 0o644);
    try { fs.writeFileSync(descriptor, bytes); fs.fsyncSync(descriptor); }
    finally { fs.closeSync(descriptor); }
    if (digest(readRegular(root, entry.target)) !== entry.sha256) throw new Error('Prepared frontend output changed during copying.');
  }
  return prepared.length;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    if (process.argv.length > 3 || (process.argv[2] && process.argv[2] !== '--check')) throw new Error('Usage: node tools/prepare_frontend_assets.mjs [--check]');
    console.log(`Verified ${prepareAssets({checkOnly: process.argv[2] === '--check'})} offline frontend assets.`);
  } catch (error) {
    console.error(`${error.message}\n${guidance}`);
    process.exitCode = 1;
  }
}
