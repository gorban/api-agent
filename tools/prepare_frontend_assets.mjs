/** Copy only reviewed official npm assets; runtime and Python builds never fetch. */
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
  return JSON.parse(readRegular(root, relative, limit).toString('utf8'));
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
    const target = boundedPath(root, entry.target);
    let existing;
    try { existing = readRegular(root, entry.target); }
    catch (error) { if (error.code !== 'ENOENT') throw error; }
    if (existing && (existing.length !== entry.size || digest(existing) !== entry.sha256)) {
      throw new Error('Existing frontend output differs from reviewed bytes; preserve/remove that generated file explicitly before npm ci.');
    }
    if (checkOnly && !existing) throw new Error(guidance);
    prepared.push({entry, bytes, target, existing});
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
