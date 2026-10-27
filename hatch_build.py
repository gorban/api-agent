"""Embed verifiable source objects for shipped workflow documents."""
import base64
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import tempfile
import re
import runpy
import tomllib

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


def _git(root, *args):
    result = subprocess.run(('git', '--literal-pathspecs', '-C', str(root), *args), capture_output=True, timeout=20)
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
    helper = _frontend_helper(root) if (root / 'tools/frontend_assets.py').is_file() else None
    if helper is None and (root / 'cfg/workflow-view-roots.json').exists():
        raise RuntimeError('Explicit workflow owners require the frontend source helper')
    prefixes = [] if helper is None else [owner for owner in helper['owner_roots'](root) if owner != '.']
    tracked = _git(root, 'ls-files', '-z', '--', 'demo', 'workflows', 'cfg', 'flow.md.html', *prefixes).split(b'\0')
    paths = sorted(Path(value.decode()) for value in tracked if value)
    if not paths:
        raise RuntimeError('Packaged workflow documents are absent')
    documents, objects = [], {}
    commit_raw = _git(root, 'cat-file', 'commit', commit)
    objects[commit] = base64.b64encode(commit_raw).decode()
    tree_root = commit_raw.split(b'\n', 1)[0][5:].decode()
    for path in paths:
        relative = path.as_posix()
        data = ((root / path).read_bytes() if helper is None else
                helper['_read'](root, relative, 8 * 1024 * 1024))
        rows = [row for row in _git(root, 'ls-files', '--stage', '-z', '--', relative).split(b'\0') if row]
        head_oid = _git(root, 'rev-parse', 'HEAD:' + relative).decode().strip()
        if (len(rows) != 1 or rows[0].split(b'\t', 1)[1] != relative.encode()
                or rows[0].split(b'\t', 1)[0].split()[1].decode() != head_oid
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



_VIEWER_NAMES = frozenset((
    'viewer.js', 'viewer.css', 'flow-modal.js', 'flow-modal.css', 'svg-links.js', 'marked.js', 'dompurify.js',
    'heading-id.js', 'marked-LICENSE.txt', 'dompurify-LICENSE.txt',
    'heading-id-LICENSE.txt', 'github-slugger-LICENSE.txt',
    'PlantUML-NOTICE.txt', 'VIEWER-ASSET-NOTICE.txt',
))


def _frontend_helper(root):
    return runpy.run_path(str(root / 'tools/frontend_assets.py'))



def _ancestor_includes(root, owners, helper):
    """Finite replacements retain original wheel destinations for ancestor trees."""
    try:
        configuration = tomllib.loads(helper['_read'](root, 'pyproject.toml').decode())
    except FileNotFoundError:
        return {}
    configured = configuration.get('tool', {}).get('hatch', {}).get('build', {}).get(
        'targets', {}).get('wheel', {}).get('force-include', {})
    result = {}
    for source, destination in configured.items():
        helper['_relative'](source)
        helper['_relative'](destination)
        if ((root / source).is_dir() and any(owner != '.' and
                (owner == source or owner.startswith(source + '/') or source.startswith(owner + '/'))
                for owner in owners)):
            helper['_owner_directory'](root, source)
            result[source] = destination
    return dict(sorted(result.items()))


def _wheel_destination(relative, directories):
    for source in sorted(directories, key=len, reverse=True):
        if relative.startswith(source + '/'):
            return directories[source] + relative[len(source):]
    return 'api_agent/' + relative

def _package_path(relative, owners, helper, directories=None):
    """Admit only viewer artifacts or public files in explicit owner prefixes."""
    helper['_relative'](relative)
    for owner in sorted(owners, key=len, reverse=True):
        prefix = '' if owner == '.' else owner + '/'
        if not relative.startswith(prefix):
            continue
        local = relative[len(prefix):]
        if local.startswith('.private_resources/viewer/'):
            return local.removeprefix('.private_resources/viewer/') in _VIEWER_NAMES
        if local.startswith('.private_resources/diagrams/'):
            suffix = local.removeprefix('.private_resources/diagrams/')
            reference_page = re.fullmatch(r'contracts/[0-9a-f]{24}\.md\.html', suffix)
            return (bool(reference_page) or
                    (Path(suffix).suffix in ('.svg', '.md') and
                     all(not part.startswith('.') for part in suffix.split('/'))))
        if owner != '.' and all(not part.startswith('.') and part != 'node_modules'
                                for part in local.split('/')):
            return True
    for source in directories or ():
        if relative.startswith(source + '/'):
            local = relative[len(source) + 1:]
            if all(not part.startswith('.') and part not in helper['_EXCLUDED_OWNERS']
                   for part in local.split('/')):
                return True
    return False


def _package_inventory(root, git_source):
    """A Git-derived finite list survives no-Git source archive rebuilding."""
    helper = _frontend_helper(root)
    owners = helper['owner_roots'](root)
    directories = _ancestor_includes(root, owners, helper)
    sidecar = root / 'workflow-package-files.json'
    if git_source:
        paths = set()
        tracked = set()
        for raw in _git(root, 'ls-files', '-z').split(b'\0'):
            if not raw:
                continue
            relative = raw.decode()
            tracked.add(relative)
            if _package_path(relative, owners, helper, directories):
                paths.add(relative)
        manifest = helper['_json'](helper['_read'](root, 'tools/frontend-assets.json'))
        paths.update(relative for relative, _ in helper['expanded_assets'](root, manifest['assets'])
                     if '/.private_resources/' in '/' + relative)
        rows = []
        for relative in sorted(paths):
            data = helper['_read'](root, relative, 8 * 1024 * 1024)
            mode = 0o755 if (root / relative).stat().st_mode & 0o111 else 0o644
            if relative in tracked:
                tree_row = _git(root, 'ls-tree', '-z', 'HEAD', '--', relative)
                expected_mode = b'100755 ' if mode == 0o755 else b'100644 '
                if (not tree_row.startswith(expected_mode) or
                        data != _git(root, 'show', 'HEAD:' + relative)):
                    raise RuntimeError('Workflow package file differs from committed source')
            rows.append(dict(path=relative, size=len(data), sha256=sha256(data).hexdigest(), mode=mode))
        document = dict(version=1, owners=list(owners), directories=directories, files=rows)
    else:
        try:
            document = helper['_json'](helper['_read'](root, sidecar.name, 4 * 1024 * 1024))
        except FileNotFoundError:
            if owners == ('.',) and not (root / 'cfg/workflow-view-roots.json').exists():
                return None
            raise RuntimeError('Source archive requires its finite workflow package inventory')
    if (not isinstance(document, dict) or set(document) != {'version', 'owners', 'directories', 'files'} or
            type(document['version']) is not int or document['version'] != 1 or
            document['owners'] != list(owners) or document['directories'] != directories or
            not isinstance(document['files'], list) or
            len(document['files']) > 10000):
        raise RuntimeError('Invalid workflow package inventory')
    seen = set()
    total = 0
    for row in document['files']:
        if (not isinstance(row, dict) or set(row) != {'path', 'size', 'sha256', 'mode'} or
                not isinstance(row['path'], str) or row['path'] in seen or
                not _package_path(row['path'], owners, helper, directories) or type(row['size']) is not int or
                not 0 <= row['size'] <= 8 * 1024 * 1024 or
                type(row['mode']) is not int or row['mode'] not in (0o644, 0o755) or
                not isinstance(row['sha256'], str) or not re.fullmatch(r'[a-f0-9]{64}', row['sha256'])):
            raise RuntimeError('Invalid workflow package file')
        seen.add(row['path'])
        data = helper['_read'](root, row['path'], 8 * 1024 * 1024)
        mode = 0o755 if (root / row['path']).stat().st_mode & 0o111 else 0o644
        if len(data) != row['size'] or sha256(data).hexdigest() != row['sha256'] or mode != row['mode']:
            raise RuntimeError('Workflow package file differs from finite inventory')
        total += len(data)
    expected = {(name if owner == '.' else owner + '/' + name)
                for owner in owners for name in
                ('.private_resources/viewer/' + filename for filename in _VIEWER_NAMES)}
    if not expected <= seen or total > 64 * 1024 * 1024:
        raise RuntimeError('Incomplete or oversized workflow package inventory')
    return document

def _validate_frontend_assets(root):
    """Packaging consumes prepared bytes only and cannot run npm or fetch."""
    guidance = "Frontend assets are missing or invalid; run npm ci from the repository root before building."
    try:
        helper = _frontend_helper(root)
        manifest = helper['_json'](helper['_read'](root, 'tools/frontend-assets.json'))
        expected = {
            '.private_resources/viewer/' + name for name in (
                'marked.js', 'dompurify.js', 'heading-id.js', 'marked-LICENSE.txt',
                'dompurify-LICENSE.txt', 'heading-id-LICENSE.txt', 'github-slugger-LICENSE.txt')
        } | {'apps/api-agent-ui/' + name for name in (
            'public/marked.js', 'public/dompurify.js', 'marked-LICENSE.txt', 'dompurify-LICENSE.txt')}
        if manifest.get('version') != 1 or {entry['target'] for entry in manifest['assets']} != expected or len(manifest['assets']) != len(expected):
            raise ValueError('invalid asset inventory')
        lock = helper['_json'](helper['_read'](root, 'package-lock.json'))
        definition = helper['_json'](helper['_read'](root, 'package.json'))
        if lock.get('lockfileVersion') != 3:
            raise ValueError('invalid lock version')
        expanded = helper['expanded_assets'](root, manifest['assets'])
        for relative, entry in expanded:
            if (not re.fullmatch(r'[0-9a-f]{64}', entry['sha256']) or
                    type(entry['size']) is not int or not 0 < entry['size'] <= 2_000_000 or
                    definition['dependencies'][entry['package']] != entry['version'] or
                    lock['packages']['']['dependencies'][entry['package']] != entry['version'] or
                    lock['packages']['node_modules/' + entry['package']]['version'] != entry['version']):
                raise ValueError('asset and lock differ')
            data = helper['_read'](root, relative)
            if len(data) != entry['size'] or sha256(data).hexdigest() != entry['sha256']:
                raise ValueError('asset digest differs')
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise RuntimeError(guidance) from error




class _OwnerExclusions:
    """Hatch's exclusion protocol with literal repository path boundaries."""

    def __init__(self, previous, prefixes):
        self.previous = previous
        self.prefixes = tuple(prefixes)

    def match_file(self, relative_path):
        relative = relative_path.replace('\\', '/')
        if any(relative == prefix or relative.startswith(prefix + '/')
               for prefix in self.prefixes):
            return True
        return bool(self.previous and self.previous.match_file(relative_path))

def _limit_owner_traversal(config, root, owners, directories=None):
    """Exact force-includes replace broad traversal inside explicit owner roots."""
    prefixes = sorted({owner for owner in owners if owner != '.'} | set(directories or ()))
    if not prefixes:
        return
    config.exclude_spec = _OwnerExclusions(config.exclude_spec, prefixes)
    for source in tuple(config.force_include):
        path = Path(source)
        if not path.is_dir() or not path.is_relative_to(root):
            continue
        relative = path.relative_to(root).as_posix()
        if any(relative == owner or relative.startswith(owner + '/') for owner in prefixes):
            del config.force_include[source]
        elif any(owner.startswith(relative + '/') for owner in prefixes):
            # This ancestor is represented by finite rows with its original destination.
            del config.force_include[source]


def _owner_proofs(proof, owners):
    """Rebase committed receipt paths without changing their Git object anchor."""
    if (not isinstance(proof, dict) or set(proof) != {'version', 'manifest', 'objects'} or
            type(proof['version']) is not int or proof['version'] != 1 or
            not isinstance(proof['manifest'], dict) or not isinstance(proof['objects'], dict)):
        raise RuntimeError('Invalid packaged source proof')
    original = proof['manifest']
    unsigned = {key: value for key, value in original.items() if key != 'sha256'}
    if (original.get('sha256') != sha256(json.dumps(unsigned, sort_keys=True,
            separators=(',', ':')).encode()).hexdigest() or
            len(original.get('owners', [])) != 1 or original['owners'][0]['owner'] != 'base'):
        raise RuntimeError('Invalid packaged source manifest')
    source_owner = original['owners'][0]
    result = {}
    for owner in owners:
        if owner == '.':
            continue
        documents = [dict(row, path=row['path'][len(owner) + 1:])
                     for row in original['documents'] if row['path'].startswith(owner + '/')]
        prefix = '/'.join(part for part in (source_owner['source_prefix'], owner) if part)
        manifest = dict(version=1, owners=[dict(source_owner, source_prefix=prefix)],
                        documents=documents)
        manifest['sha256'] = sha256(json.dumps(manifest, sort_keys=True,
            separators=(',', ':')).encode()).hexdigest()
        result[owner] = dict(version=1, manifest=manifest, objects=proof['objects'])
    return result

class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        root = Path(self.root).resolve()
        _validate_frontend_assets(root)
        sidecar = root / 'workflow-source-proof.json'
        try:
            source_root = Path(_git(root, 'rev-parse', '--show-toplevel').decode().strip())
        except (RuntimeError, FileNotFoundError):
            source_root = None
        inventory = _package_inventory(root, source_root == root)
        if inventory is not None:
            _limit_owner_traversal(self.build_config, root, inventory['owners'], inventory['directories'])
            destination_prefix = '' if self.target_name == 'sdist' else 'api_agent/'
            includes = build_data.setdefault('force_include', {})
            for row in inventory['files']:
                includes[str(root / row['path'])] = (row['path'] if self.target_name == 'sdist'
                    else _wheel_destination(row['path'], inventory['directories']))
            folder = Path(tempfile.mkdtemp(prefix='api-agent-inventory-'))
            package_sidecar = folder / 'workflow-package-files.json'
            package_sidecar.write_text(json.dumps(inventory, sort_keys=True, separators=(',', ':')))
            includes[str(package_sidecar)] = destination_prefix + package_sidecar.name
        if source_root == root:
            proof = _proof(root)
            folder = Path(tempfile.mkdtemp(prefix='api-agent-proof-'))
            sidecar = folder / 'workflow-source-proof.json'
            sidecar.write_text(json.dumps(proof, sort_keys=True, separators=(',', ':')))
        elif not sidecar.is_file():
            return
        elif inventory is not None:
            helper = _frontend_helper(root)
            proof = helper['_json'](helper['_read'](root, sidecar.name, 16 * 1024 * 1024))
        if inventory is not None:
            folder = Path(tempfile.mkdtemp(prefix='api-agent-owner-proofs-'))
            for index, (owner, receipt) in enumerate(_owner_proofs(proof, inventory['owners']).items()):
                local = folder / (str(index) + '.json')
                local.write_text(json.dumps(receipt, sort_keys=True, separators=(',', ':')))
                relative = owner + '/workflow-source-proof.json'
                target = relative if self.target_name == 'sdist' else _wheel_destination(
                    relative, inventory['directories'])
                build_data.setdefault('force_include', {})[str(local)] = target
        destination = ('workflow-source-proof.json' if self.target_name == 'sdist'
                       else 'api_agent/workflow-source-proof.json')
        build_data.setdefault('force_include', {})[str(sidecar)] = destination
