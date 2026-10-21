"""Embed verifiable source objects for shipped workflow documents."""
import base64
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import tempfile
import os
import stat
import re

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


def _git(root, *args):
    result = subprocess.run(('git', '-C', str(root), *args), capture_output=True, timeout=20)
    if result.returncode:
        raise RuntimeError('Packaged workflow source has no committed provenance')
    return result.stdout


def _tree_entries(raw, size):
    cursor = 0
    entries = {}
    while cursor < len(raw):
        space = raw.index(b' ', cursor)
        end = raw.index(b'\0', space)
        name = raw[space + 1:end]
        entries[name] = (raw[cursor:space], raw[end + 1:end + 1 + size].hex())
        cursor = end + 1 + size
    return entries


def _proof(root):
    commit = _git(root, 'rev-parse', 'HEAD').decode().strip()
    algorithm = _git(root, 'rev-parse', '--show-object-format').decode().strip()
    if algorithm not in ('sha1', 'sha256'):
        raise RuntimeError('Unsupported Git object format')
    size = 20 if algorithm == 'sha1' else 32
    tracked = _git(root, 'ls-files', '-z', '--', 'demo', 'workflows', 'cfg', 'flow.md.html').split(b'\0')
    paths = sorted(Path(value.decode()) for value in tracked if value)
    if not paths:
        raise RuntimeError('Packaged workflow documents are absent')
    documents, objects = [], {}
    commit_raw = _git(root, 'cat-file', 'commit', commit)
    objects[commit] = base64.b64encode(commit_raw).decode()
    tree_root = commit_raw.split(b'\n', 1)[0][5:].decode()
    for path in paths:
        relative = path.as_posix()
        data = (root / path).read_bytes()
        rows = _git(root, 'ls-files', '--stage', '--', relative).decode().splitlines()
        head_oid = _git(root, 'rev-parse', 'HEAD:' + relative).decode().strip()
        if (len(rows) != 1 or not rows[0].endswith('\t' + relative)
                or rows[0].split()[1] != head_oid
                or data != _git(root, 'show', 'HEAD:' + relative)):
            raise RuntimeError('Packaged workflow document differs from committed source')
        documents.append(dict(owner='base', path=relative, length=len(data),
                              sha256=sha256(data).hexdigest()))
        current = tree_root
        for part in relative.encode().split(b'/'):
            raw = _git(root, 'cat-file', 'tree', current)
            objects[current] = base64.b64encode(raw).decode()
            _, current = _tree_entries(raw, size)[part]
        if current != head_oid:
            raise RuntimeError('Packaged workflow Git tree path mismatch')
    manifest = dict(version=1, owners=[dict(owner='base', commit=commit,
        object_format=algorithm, source_prefix='')], documents=documents)
    manifest['sha256'] = sha256(json.dumps(manifest, sort_keys=True,
        separators=(',', ':')).encode()).hexdigest()
    if _git(root, 'rev-parse', 'HEAD').decode().strip() != commit:
        raise RuntimeError('Packaged workflow source changed during build')
    return dict(version=1, manifest=manifest, objects=objects)


def _validate_frontend_assets(root):
    """Packaging consumes prepared bytes only and cannot run npm or fetch."""
    guidance = "Frontend assets are missing or invalid; run npm ci from the repository root before building."
    try:
        manifest = json.loads((root / 'tools/frontend-assets.json').read_text())
        expected = {
            '.private_resources/viewer/' + name for name in (
                'marked.js', 'dompurify.js', 'heading-id.js', 'marked-LICENSE.txt',
                'dompurify-LICENSE.txt', 'heading-id-LICENSE.txt', 'github-slugger-LICENSE.txt')
        } | {'apps/api-agent-ui/' + name for name in (
            'public/marked.js', 'public/dompurify.js', 'marked-LICENSE.txt', 'dompurify-LICENSE.txt')}
        if manifest.get('version') != 1 or {entry['target'] for entry in manifest['assets']} != expected or len(manifest['assets']) != len(expected):
            raise ValueError('invalid asset inventory')
        lock = json.loads((root / 'package-lock.json').read_text())
        definition = json.loads((root / 'package.json').read_text())
        if lock.get('lockfileVersion') != 3:
            raise ValueError('invalid lock version')
        for entry in manifest['assets']:
            if (not re.fullmatch(r'[0-9a-f]{64}', entry['sha256']) or
                    type(entry['size']) is not int or not 0 < entry['size'] <= 2_000_000 or
                    definition['dependencies'][entry['package']] != entry['version'] or
                    lock['packages']['']['dependencies'][entry['package']] != entry['version'] or
                    lock['packages']['node_modules/' + entry['package']]['version'] != entry['version']):
                raise ValueError('asset and lock differ')
            path = root / entry['target']
            if any(parent.is_symlink() for parent in (path, *path.parents) if parent.is_relative_to(root) and parent != root):
                raise ValueError('asset link rejected')
            descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_NOFOLLOW', 0))
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode) or info.st_size != entry['size']:
                    raise ValueError('asset is not the expected regular file')
                with os.fdopen(descriptor, 'rb') as asset:
                    descriptor = -1
                    data = asset.read(2_000_001)
                if len(data) != entry['size'] or sha256(data).hexdigest() != entry['sha256']:
                    raise ValueError('asset digest differs')
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise RuntimeError(guidance) from error


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        root = Path(self.root).resolve()
        _validate_frontend_assets(root)
        sidecar = root / 'workflow-source-proof.json'
        try:
            source_root = Path(_git(root, 'rev-parse', '--show-toplevel').decode().strip())
        except RuntimeError:
            source_root = None
        if source_root == root:
            proof = _proof(root)
            folder = Path(tempfile.mkdtemp(prefix='api-agent-proof-'))
            sidecar = folder / 'workflow-source-proof.json'
            sidecar.write_text(json.dumps(proof, sort_keys=True, separators=(',', ':')))
        elif not sidecar.is_file():
            return
        destination = ('workflow-source-proof.json' if self.target_name == 'sdist'
                       else 'api_agent/workflow-source-proof.json')
        build_data.setdefault('force_include', {})[str(sidecar)] = destination
