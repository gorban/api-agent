"""Prepare an index snapshot from reviewed installed npm originals, without npm."""
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat


GUIDANCE = 'Frontend dependencies are missing or invalid; run npm ci from the repository root.'
TARGETS = {
    '.private_resources/viewer/' + name for name in (
        'marked.js', 'dompurify.js', 'heading-id.js', 'marked-LICENSE.txt',
        'dompurify-LICENSE.txt', 'heading-id-LICENSE.txt', 'github-slugger-LICENSE.txt')
} | {'apps/api-agent-ui/' + name for name in (
    'public/marked.js', 'public/dompurify.js', 'marked-LICENSE.txt', 'dompurify-LICENSE.txt')}


def _read(root: Path, relative: str, limit: int = 2_000_000) -> bytes:
    parts = relative.split('/')
    if (not relative or len(relative) > 1024 or relative.startswith('/') or
            any(part in ('', '.', '..') or re.search(r'[\\\x00-\x1f\x7f]', part) for part in parts)):
        raise ValueError('Invalid frontend input path')
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        child = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
        with os.fdopen(child, 'rb') as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                raise ValueError('Frontend input must be a bounded regular file')
            data = source.read(limit + 1)
            if len(data) != info.st_size:
                raise ValueError('Frontend input changed while reading')
            return data
    finally:
        os.close(descriptor)



def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate frontend context key')
        result[key] = value
    return result


def _json(data):
    return json.loads(data, object_pairs_hook=_unique_object)


def _relative(value, *, dot=False):
    if dot and value == '.':
        return value
    if (not isinstance(value, str) or not value or len(value) > 1024 or
            len(value.split('/')) > 64 or value.startswith('/') or
            any(part in ('', '.', '..') or re.search(r'[\\\x00-\x1f\x7f]', part)
                for part in value.split('/'))):
        raise ValueError('Invalid frontend owner path')
    return value




def _context_relative(value, *, dot=False):
    _relative(value, dot=dot)
    if '?' in value or '#' in value:
        raise ValueError('Invalid frontend owner context path')
    return value


_EXCLUDED_OWNERS = frozenset({
    'node_modules', 'venv', '__pycache__', 'dist', 'build', 'coverage',
    'artifacts', 'site-packages', 'htmlcov',
})


def _owner_directory(root, relative):
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if relative != '.':
            for part in relative.split('/'):
                if part.startswith('.') or part in _EXCLUDED_OWNERS:
                    raise ValueError('Excluded frontend owner directory')
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
                try:
                    os.stat('.git', dir_fd=descriptor, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise ValueError('Foreign repository frontend owner rejected')
    finally:
        os.close(descriptor)

def owner_roots(root: Path) -> tuple[str, ...]:
    """Read only this tree's explicit contexts; graph semantics stay in view checks."""
    try:
        raw = _read(root, 'cfg/workflow-view-roots.json', 65536)
    except FileNotFoundError:
        return ('.',)
    document = _json(raw)
    if (not isinstance(document, dict) or set(document) != {'version', 'entries'} or
            type(document['version']) is not int or document['version'] != 1 or
            not isinstance(document['entries'], dict) or len(document['entries']) > 1000):
        raise ValueError('Invalid frontend owner contexts')
    owners = {'.'}
    for entry, context in document['entries'].items():
        _context_relative(entry)
        if (Path(entry).name != 'flow.md.html' or not isinstance(context, dict) or
                set(context) != {'base_root', 'pack_root', 'entry_root'} or
                context['entry_root'] not in ('base', 'pack')):
            raise ValueError('Invalid frontend owner context')
        _owner_directory(root, str(Path(entry).parent))
        _read(root, entry, 8 * 1024 * 1024)
        values = [_context_relative(context[key], dot=True) for key in ('base_root', 'pack_root')]
        if values == ['.', '.'] and context['entry_root'] == 'base':
            raise ValueError('Redundant frontend owner context')
        selected = context[context['entry_root'] + '_root']
        if selected != '.' and not entry.startswith(selected + '/'):
            raise ValueError('Frontend entry is outside selected owner')
        for relative in values:
            _owner_directory(root, relative)
            owners.add(relative)
    return tuple(sorted(owners))


def expanded_assets(root: Path, entries):
    owners = owner_roots(root)
    result = []
    for entry in entries:
        if entry['target'].startswith('.private_resources/viewer/'):
            for owner in owners:
                target = entry['target'] if owner == '.' else owner + '/' + entry['target']
                result.append((target, entry))
        else:
            result.append((entry['target'], entry))
    return result


def _parent(root: Path, relative: str):
    """Create only owned directory components; never traverse linked parents."""
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in relative.split('/')[:-1]:
            try:
                os.mkdir(part, 0o755, dir_fd=descriptor)
            except FileExistsError:
                pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise

def prepare_snapshot(snapshot: Path, installed_root: Path) -> None:
    """Trust staged contracts only; working manifests and generated JS are irrelevant."""
    try:
        manifest = _json(_read(snapshot, 'tools/frontend-assets.json'))
        lock = _json(_read(snapshot, 'package-lock.json'))
        definition = _json(_read(snapshot, 'package.json'))
        entries = manifest['assets']
        if (manifest['version'] != 1 or lock['lockfileVersion'] != 3 or
                len(entries) != len(TARGETS) or {entry['target'] for entry in entries} != TARGETS):
            raise ValueError('Invalid staged frontend inventory')
        prepared = []
        for entry in entries:
            package, version = entry['package'], entry['version']
            if (not re.fullmatch(r'[a-z0-9][a-z0-9-]*', package) or
                    not re.fullmatch(r'\d+\.\d+\.\d+', version) or
                    not re.fullmatch(r'[a-f0-9]{64}', entry['sha256']) or
                    type(entry['size']) is not int or not 0 < entry['size'] <= 2_000_000 or
                    definition['dependencies'][package] != version or
                    lock['packages']['']['dependencies'][package] != version):
                raise ValueError('Invalid staged frontend dependency')
            locked = lock['packages']['node_modules/' + package]
            if (locked['version'] != version or
                    not re.fullmatch(r'https://registry\.npmjs\.org/[A-Za-z0-9@/_.%~-]+', locked['resolved']) or
                    not re.fullmatch(r'sha512-[A-Za-z0-9+/]+={0,2}', locked['integrity'])):
                raise ValueError('Invalid staged frontend lock')
            installed = json.loads(_read(installed_root, 'node_modules/' + package + '/package.json'))
            if installed['name'] != package or installed['version'] != version:
                raise ValueError('Installed frontend identity differs')
            data = _read(installed_root, 'node_modules/' + package + '/' + entry['source'])
            if len(data) != entry['size'] or sha256(data).hexdigest() != entry['sha256']:
                raise ValueError('Installed frontend original differs from staged manifest')
            prepared.append((entry['target'], data))
        prepared = [(target, data) for relative, data in prepared
                    for target, _ in expanded_assets(snapshot, [{'target': relative}])]
        # Validate all existing outputs before creating any generated file.
        for relative, data in prepared:
            try:
                existing = _read(snapshot, relative)
            except FileNotFoundError:
                continue
            if existing != data:
                raise ValueError('Staged frontend output differs from reviewed original')
        for relative, data in prepared:
            descriptor = _parent(snapshot, relative)
            try:
                try:
                    child = os.open(relative.split('/')[-1], os.O_WRONLY | os.O_CREAT |
                                    os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=descriptor)
                except FileExistsError:
                    if _read(snapshot, relative) != data:
                        raise ValueError('Staged frontend output differs from reviewed original')
                else:
                    with os.fdopen(child, 'wb') as output:
                        output.write(data)
            finally:
                os.close(descriptor)
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise RuntimeError(GUIDANCE) from error
