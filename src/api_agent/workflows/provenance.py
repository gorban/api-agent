"""Verify admitted workflow documents against their owning committed Git trees."""
from hashlib import sha256
from importlib import metadata
import json
from pathlib import Path
import subprocess

from .definition import InlinePromptRef, LayoutError, _read_bounded, _prompt_body


class WorkingTreeChanges(LayoutError):
    """Tracked workflow bytes or index differ from the current commit."""


def _git(root, *arguments):
    result = subprocess.run(('git', '-C', str(root), *arguments),
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            check=False, timeout=10)
    if result.returncode:
        raise LayoutError('Workflow document has no verifiable committed provenance')
    return result.stdout


def verify_inline_prompts(definition):
    """Re-derive each virtual prompt from its admitted physical declaration."""
    for ref, frozen in definition.contents.items():
        if not isinstance(ref, InlinePromptRef):
            continue
        parent = definition.contents.get(ref.owner)
        if type(parent) is not bytes:
            raise LayoutError('Inline prompt has no admitted parent document')
        scope = None
        if definition.layout is not None and definition.selected_declarations is not None:
            rows = [row for row in definition.layout.tables['Prompts'] if row['File'] == ref]
            scopes = {row.get('_owner_id') or row['Node'] for row in rows}
            if len(scopes) != 1:
                raise LayoutError('Inline prompt has no selected declaration scope')
            scope = scopes.pop()
            selected = definition.selected_declarations.get(scope)
            if selected is None or selected[2] != ref.owner:
                raise LayoutError('Inline prompt has no selected declaration scope')
        body = _prompt_body(ref.owner, parent, ref.id, scope=scope)
        if body is None:
            raise LayoutError('Inline prompt is absent from its admitted parent')
        raw = [value for _, value in body]
        while raw and not raw[0].strip():
            raw.pop(0)
        while raw and not raw[-1].strip():
            raw.pop()
        if (len(raw) < 2 or raw[0] != '```text' or raw[-1] != '```'
                or ('\n'.join(raw[1:-1]) + '\n').encode() != frozen):
            raise LayoutError('Inline prompt differs from its admitted parent')


def physical_documents(definition):
    """Select the document hash pathway; native Python has separate identity."""
    native = set()
    for resource in definition.resources:
        if resource.kind not in ('native_source', 'native_dependency'):
            continue
        owner, _, relative = resource.logical_path.partition('/')
        root = definition.roots.get(owner)
        if root is not None and relative:
            native.add(root / relative)
    return {path: frozen for path, frozen in definition.contents.items()
            if isinstance(path, Path) and path not in native}


def committed_documents(definition, *, selected_owners=None):
    """Check the exact frozen bytes before native import or graph effects.

    A caller must invoke this for every reporting-enabled selected definition.
    Installed resources require a separately verified package receipt and are
    intentionally rejected by this Git-only verifier.
    """
    if not all(hasattr(definition, name) for name in ('roots', 'contents', 'definition_id')):
        raise TypeError('An admitted workflow definition is required')
    manifest = []
    owners = {}
    verify_inline_prompts(definition)
    for path, frozen in physical_documents(definition).items():
        path = Path(path)
        matching = [(name, root) for name, root in definition.roots.items()
                    if path.is_relative_to(root)]
        if not matching:
            raise LayoutError('Workflow document has no owning root')
        name, root = max(matching, key=lambda item: len(item[1].parts))
        if selected_owners is not None and name not in selected_owners:
            continue
        relative = path.relative_to(root).as_posix()
        if relative in ('', '.'):
            raise LayoutError('Workflow document has an invalid owner path')
        top = Path(_git(root, 'rev-parse', '--show-toplevel').decode().strip())
        if not root.is_relative_to(top):
            raise LayoutError('Workflow owner is outside its Git root')
        prefix = root.relative_to(top).as_posix()
        prefix = '' if prefix == '.' else prefix
        repo_path = (prefix + '/' if prefix else '') + relative
        commit = _git(root, 'rev-parse', 'HEAD').decode().strip()
        format_name = _git(root, 'rev-parse', '--show-object-format').decode().strip()
        if format_name not in ('sha1', 'sha256'):
            raise LayoutError('Unsupported Git object format')
        previous = owners.setdefault(name, dict(owner=name, commit=commit, object_format=format_name,
                                                source_prefix=prefix))
        if previous['commit'] != commit or previous['source_prefix'] != prefix:
            raise LayoutError('Workflow owner changed during capture')
        # `ls-files` catches untracked and ignored inputs. Comparing the index
        # blob with HEAD catches staged edits even when working bytes match HEAD.
        indexed = _git(top, 'ls-files', '--stage', '--', repo_path)
        rows = indexed.decode().splitlines()
        if not rows:
            untracked = _git(top, 'ls-files', '--others', '--exclude-standard', '--', repo_path).decode().splitlines()
            if root == top and untracked == [repo_path]:
                raise WorkingTreeChanges('Workflow document is untracked')
            raise LayoutError('Workflow document is untracked or ignored')
        if len(rows) != 1 or not rows[0].endswith('\t' + repo_path) or rows[0].split()[2] != '0':
            raise LayoutError('Workflow document has unmerged changes')
        head_entry = _git(top, 'ls-tree', 'HEAD', '--', repo_path).decode().splitlines()
        if not head_entry:
            if root == top:
                raise WorkingTreeChanges('Workflow document is newly staged')
            raise LayoutError('Workflow document is untracked or ignored')
        if len(head_entry) != 1 or not head_entry[0].endswith('\t' + repo_path):
            raise LayoutError('Workflow document has ambiguous committed provenance')
        head_blob = head_entry[0].split()[2]
        index_blob = rows[0].split()[1]
        if index_blob != head_blob:
            raise WorkingTreeChanges('Workflow document has staged changes')
        committed = _git(top, 'show', f'HEAD:{repo_path}')
        if _read_bounded(path, root) != frozen:
            raise LayoutError('Workflow document changed after admission or differs from HEAD')
        if committed != frozen:
            raise WorkingTreeChanges('Workflow document differs from HEAD')
        manifest.append(dict(owner=name, path=relative, length=len(frozen), sha256=sha256(frozen).hexdigest()))
    if not manifest:
        raise LayoutError('Workflow definition contains no documents')
    manifest.sort(key=lambda item: (item['owner'], item['path']))
    if len({(item['owner'], item['path']) for item in manifest}) != len(manifest):
        raise LayoutError('Duplicate workflow document identity')
    # A changed HEAD during verification invalidates the coherent snapshot.
    for name, owner in owners.items():
        if _git(definition.roots[name], 'rev-parse', 'HEAD').decode().strip() != owner['commit']:
            raise LayoutError('Workflow owner changed during capture')
    payload = dict(version=1, owners=sorted(owners.values(), key=lambda item: item['owner']),
                   documents=manifest)
    payload['sha256'] = sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return payload


def working_documents(definition, *, selected_owners):
    """Record admitted tracked working bytes without claiming they are committed."""
    verify_inline_prompts(definition)
    documents, owners = [], {}
    for path, frozen in physical_documents(definition).items():
        matching = [(name, root) for name, root in definition.roots.items()
                    if path.is_relative_to(root)]
        if not matching:
            raise LayoutError('Workflow document has no owning root')
        name, root = max(matching, key=lambda item: len(item[1].parts))
        if name not in selected_owners:
            continue
        top = Path(_git(root, 'rev-parse', '--show-toplevel').decode().strip())
        if not root.is_relative_to(top):
            raise LayoutError('Workflow owner is outside its Git root')
        relative = path.relative_to(root).as_posix()
        prefix = root.relative_to(top).as_posix()
        prefix = '' if prefix == '.' else prefix
        repo_path = (prefix + '/' if prefix else '') + relative
        commit = _git(root, 'rev-parse', 'HEAD').decode().strip()
        object_format = _git(root, 'rev-parse', '--show-object-format').decode().strip()
        if object_format not in ('sha1', 'sha256'):
            raise LayoutError('Unsupported Git object format')
        indexed = _git(top, 'ls-files', '--stage', '--', repo_path).decode().splitlines()
        if not indexed:
            untracked = _git(top, 'ls-files', '--others', '--exclude-standard', '--', repo_path).decode().splitlines()
            if root != top or untracked != [repo_path]:
                raise LayoutError('Workflow document is untracked or ignored')
        elif (len(indexed) != 1 or not indexed[0].endswith('\t' + repo_path)
              or indexed[0].split()[2] != '0'):
            raise LayoutError('Workflow document has unmerged changes')
        if _read_bounded(path, root) != frozen:
            raise LayoutError('Workflow document changed after admission')
        record = dict(owner=name, commit=None, base_commit=commit, state='working_tree',
                      object_format=object_format, source_prefix=prefix)
        if name in owners and owners[name] != record:
            raise LayoutError('Workflow owner changed during capture')
        owners[name] = record
        documents.append(dict(owner=name, path=relative, length=len(frozen), sha256=sha256(frozen).hexdigest()))
    if not documents:
        raise LayoutError('Workflow definition contains no documents')
    for name, owner in owners.items():
        if _git(definition.roots[name], 'rev-parse', 'HEAD').decode().strip() != owner['base_commit']:
            raise LayoutError('Workflow owner changed during capture')
    payload = dict(version=1, owners=sorted(owners.values(), key=lambda row: row['owner']),
                   documents=sorted(documents, key=lambda row: (row['owner'], row['path'])))
    payload['sha256'] = sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return payload



def _owner_documents(definition, owner):
    root = definition.roots[owner]
    return {path for path in physical_documents(definition)
            if path.is_relative_to(root) and owner == max(
                ((name, candidate) for name, candidate in definition.roots.items()
                 if path.is_relative_to(candidate)),
                key=lambda item: len(item[1].parts))[0]}


def _git_owns_documents(definition, owner, top):
    """Positive source ownership takes precedence over ambient package metadata."""
    root = definition.roots[owner]
    paths = _owner_documents(definition, owner) | {
        root / name for name in ('pyproject.toml', 'setup.py', 'setup.cfg')}
    relative = sorted(path.relative_to(top).as_posix() for path in paths)
    return bool(_git(top, 'ls-files', '--stage', '--', *relative)
                or _git(top, 'ls-tree', '-z', 'HEAD', '--', *relative))


def _distribution_owns_documents(definition, owner):
    """Identify installed bytes by physical inventory, independently of Git ancestors."""
    root = definition.roots[owner]
    documents = _owner_documents(definition, owner)
    receipts = {candidate / 'workflow-source-proof.json'
                for candidate in definition.roots.values()
                if root.is_relative_to(candidate)}
    claimants = set()
    for distribution in metadata.distributions():
        if not isinstance(distribution, metadata.PathDistribution):
            continue
        # Admit the physical metadata before asking importlib to read RECORD.
        # Editable/source inventories and filesystem aliases do not establish
        # installed ownership of documents outside their metadata directory.
        try:
            provider = Path(distribution._path)
        except TypeError:
            # Archive-backed distributions expose traversable metadata rather
            # than a filesystem inventory capable of owning physical documents.
            continue
        provider = provider.absolute()
        metadata_files = {provider / 'METADATA', provider / 'RECORD'}
        if (not provider.name.endswith('.dist-info') or provider.resolve() != provider
                or not all(path.is_file() and path.resolve() == path for path in metadata_files)):
            continue
        inventory = distribution.files
        if inventory is None:
            continue
        location = Path(distribution.locate_file('')).resolve()
        owned = set()
        required = documents | receipts | metadata_files
        for item in inventory:
            relative = Path(str(item))
            if relative.is_absolute() or '..' in relative.parts:
                continue
            physical = Path(distribution.locate_file(item)).absolute()
            if (physical in required and physical.is_relative_to(location) and physical.resolve() == physical
                    and physical.is_file()):
                owned.add(physical)
        if metadata_files <= owned and documents <= owned and receipts & owned:
            identity = provider.stat()
            claimants.add((identity.st_dev, identity.st_ino))
    return len(claimants) == 1


def verified_documents(definition, *, packaged_proof=None):
    """Verify each selected logical owner at its own checkout or package root."""
    verify_inline_prompts(definition)
    selected = set()
    for path in physical_documents(definition):
        matching = [(name, root) for name, root in definition.roots.items()
                    if path.is_relative_to(root)]
        if not matching:
            raise LayoutError('Workflow document has no owning root')
        selected.add(max(matching, key=lambda item: len(item[1].parts))[0])
    source, installed = set(), set()
    for owner in selected:
        root = definition.roots[owner]
        try:
            top = Path(_git(root, 'rev-parse', '--show-toplevel').decode().strip())
        except LayoutError:
            installed.add(owner)
            continue
        if (root.is_relative_to(top) and (root == top
                or _git_owns_documents(definition, owner, top)
                or not _distribution_owns_documents(definition, owner))):
            source.add(owner)
        else:
            installed.add(owner)
    manifests = []
    if source:
        try:
            manifests.append(committed_documents(definition, selected_owners=source))
        except WorkingTreeChanges:
            if any(_distribution_owns_documents(definition, owner) for owner in source):
                raise
            manifests.append(working_documents(definition, selected_owners=source))
    if installed:
        from .packaged_provenance import installed_proof
        manifests.append(installed_proof(definition, selected_owners=installed,
                                         packaged_proof=packaged_proof))
    if not manifests:
        raise LayoutError('Workflow definition contains no selected documents')
    result = dict(version=1, owners=sorted(
        (row for manifest in manifests for row in manifest['owners']),
        key=lambda row: row['owner']), documents=sorted(
        (row for manifest in manifests for row in manifest['documents']),
        key=lambda row: (row['owner'], row['path'])))
    result['sha256'] = sha256(json.dumps(result, sort_keys=True,
        separators=(',', ':')).encode()).hexdigest()
    return result
