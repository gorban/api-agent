"""Embed verifiable source objects for shipped workflow documents."""
import base64
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import tempfile

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


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        root = Path(self.root).resolve()
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
