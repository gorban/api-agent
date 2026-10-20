"""Offline Git object proof for packaged workflow documents."""
import base64
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from .definition import InlinePromptRef, LayoutError
from .provenance import _git
from . import provenance


def _oid(kind, data, algorithm):
    encoded = kind.encode() + b' ' + str(len(data)).encode() + b'\0' + data
    return hashlib.new(algorithm, encoded).hexdigest()


def _tree_entries(raw, size):
    cursor = 0
    entries = {}
    while cursor < len(raw):
        divider = raw.find(b' ', cursor)
        separator = raw.find(b'\0', divider + 1)
        if divider < 0 or separator < 0 or separator + 1 + size > len(raw):
            raise LayoutError('Invalid packaged Git tree object')
        mode = raw[cursor:divider]
        name = raw[divider + 1:separator]
        if not name or b'/' in name or name in (b'.', b'..') or name in entries:
            raise LayoutError('Invalid packaged Git tree path')
        entries[name] = (mode, raw[separator + 1:separator + 1 + size].hex())
        cursor = separator + 1 + size
    return entries


def _commit_tree(raw):
    first = raw.split(b'\n', 1)[0]
    if not first.startswith(b'tree '):
        raise LayoutError('Invalid packaged Git commit object')
    return first[5:].decode('ascii')


def _decode(value, limit):
    if type(value) is not str or len(value) > limit * 2:
        raise LayoutError('Packaged provenance object exceeds bound')
    try:
        data = base64.b64decode(value, validate=True)
    except (ValueError, TypeError):
        raise LayoutError('Invalid packaged provenance encoding') from None
    if len(data) > limit:
        raise LayoutError('Packaged provenance object exceeds bound')
    return data


def build_packaged_proof(definition):
    """Build only after source/index/working bytes match HEAD."""
    manifest = provenance.committed_documents(definition)
    proof = {'version': 1, 'manifest': manifest, 'objects': {}}
    for owner in manifest['owners']:
        root = definition.roots[owner['owner']]
        algorithm = owner['object_format']
        size = 20 if algorithm == 'sha1' else 32
        commit = owner['commit']
        proof['objects'][commit] = base64.b64encode(_git(root, 'cat-file', 'commit', commit)).decode()
        tree = _commit_tree(base64.b64decode(proof['objects'][commit]))
        for document in (row for row in manifest['documents'] if row['owner'] == owner['owner']):
            current = tree
            repo_path = '/'.join(part for part in (owner['source_prefix'], document['path']) if part)
            parts = repo_path.encode('utf-8').split(b'/')
            for index, part in enumerate(parts):
                raw = _git(root, 'cat-file', 'tree', current)
                proof['objects'][current] = base64.b64encode(raw).decode()
                mode, next_oid = _tree_entries(raw, size).get(part, (None, None))
                if mode is None or (index < len(parts) - 1 and mode != b'40000'):
                    raise LayoutError('Committed workflow document is absent from Git tree')
                current = next_oid
    return proof


def verify_packaged_proof(definition, proof):
    """Verify commit→tree→blob identity from bytes, with no Git operation."""
    provenance.verify_inline_prompts(definition)
    if type(proof) is not dict or set(proof) != {'version', 'manifest', 'objects'} or proof['version'] != 1:
        raise LayoutError('Invalid packaged workflow provenance')
    manifest, objects = proof['manifest'], proof['objects']
    if type(manifest) is not dict or type(objects) is not dict or len(objects) > 8192:
        raise LayoutError('Invalid packaged workflow provenance')
    if len(str(proof)) > 16 * 1024 * 1024:
        raise LayoutError('Packaged workflow provenance exceeds bound')
    documents = manifest.get('documents')
    owners = manifest.get('owners')
    manifest_hash = manifest.get('sha256')
    unsigned_manifest = {key: value for key, value in manifest.items() if key != 'sha256'}
    if (type(manifest_hash) is not str or hashlib.sha256(json.dumps(unsigned_manifest,
            sort_keys=True, separators=(',', ':')).encode()).hexdigest() != manifest_hash):
        raise LayoutError('Packaged workflow manifest identity mismatch')
    physical = provenance.physical_documents(definition)
    if type(documents) is not list or type(owners) is not list or len(documents) < len(physical):
        raise LayoutError('Packaged workflow documents do not match the admitted definition')
    owner_rows = {row['owner']: row for row in owners}
    document_rows = {(row['owner'], row['path']): row for row in documents}
    if len(owner_rows) != len(owners) or len(document_rows) != len(documents):
        raise LayoutError('Duplicate packaged workflow provenance identity')
    for path, frozen in physical.items():
        matching = [(name, root) for name, root in definition.roots.items() if Path(path).is_relative_to(root)]
        if not matching:
            raise LayoutError('Packaged workflow document has no owner')
        name, root = max(matching, key=lambda item: len(item[1].parts))
        relative = Path(path).relative_to(root).as_posix()
        row = document_rows.get((name, relative))
        owner = owner_rows.get(name)
        if row is None or owner is None or row['length'] != len(frozen) or row['sha256'] != hashlib.sha256(frozen).hexdigest():
            raise LayoutError('Packaged workflow bytes do not match receipt')
        algorithm = owner['object_format']
        prefix = owner.get('source_prefix')
        if type(prefix) is not str or prefix.startswith('/') or any(
                part in ('', '.', '..') for part in prefix.split('/') if prefix):
            raise LayoutError('Invalid packaged Git source prefix')
        if algorithm not in ('sha1', 'sha256'):
            raise LayoutError('Unsupported packaged Git object format')
        size = 20 if algorithm == 'sha1' else 32
        commit_id = owner['commit']
        commit = _decode(objects.get(commit_id), 2 * 1024 * 1024)
        if _oid('commit', commit, algorithm) != commit_id:
            raise LayoutError('Packaged Git commit identity mismatch')
        current = _commit_tree(commit)
        repo_path = '/'.join(part for part in (prefix, relative) if part)
        parts = repo_path.encode('utf-8').split(b'/')
        for index, part in enumerate(parts):
            tree = _decode(objects.get(current), 8 * 1024 * 1024)
            if _oid('tree', tree, algorithm) != current:
                raise LayoutError('Packaged Git tree identity mismatch')
            mode, current = _tree_entries(tree, size).get(part, (None, None))
            if mode is None or (index < len(parts) - 1 and mode != b'40000'):
                raise LayoutError('Packaged Git tree path mismatch')
        if _oid('blob', frozen, algorithm) != current:
            raise LayoutError('Packaged Git blob identity mismatch')
    return manifest


def installed_proof(definition, *, selected_owners=None, packaged_proof=None):
    """Verify each logical owner's selected bytes in its owning distribution."""
    from .definition import _read_bounded
    provenance.verify_inline_prompts(definition)
    physical = provenance.physical_documents(definition)
    if not physical:
        raise LayoutError('Installed workflow has no physical documents')
    groups = {}
    for path, frozen in physical.items():
        matching = [(name, root) for name, root in definition.roots.items()
                    if path.is_relative_to(root)]
        if not matching:
            raise LayoutError('Installed workflow document has no admitted owner')
        owner, root = max(matching, key=lambda item: len(item[1].parts))
        if selected_owners is not None and owner not in selected_owners:
            continue
        groups.setdefault(owner, {})[path] = frozen
    if not groups:
        raise LayoutError('Installed workflow has no selected documents')
    owners, documents = [], []
    for owner, selected_bytes in groups.items():
        owned_root = definition.roots[owner]
        # A nested admitted root may share an enclosing admitted distribution.
        # Never search an unrelated filesystem ancestor for a receipt.
        candidates = sorted({root for root in definition.roots.values()
                             if owned_root.is_relative_to(root)},
                            key=lambda root: len(root.parts), reverse=True)
        explicit = (packaged_proof.get(owner) if type(packaged_proof) is dict
                    and set(packaged_proof) != {'version', 'manifest', 'objects'}
                    else packaged_proof)
        receipt_root = (owned_root if explicit is not None else next(
            (root for root in candidates if (root / 'workflow-source-proof.json').is_file()), None))
        if receipt_root is None:
            raise LayoutError('Workflow document has no verifiable committed provenance or packaged source proof')
        if explicit is None:
            sidecar = receipt_root / 'workflow-source-proof.json'
            if sidecar.is_symlink():
                raise LayoutError('Installed workflow source proof is a symbolic link')
            try:
                proof = json.loads(_read_bounded(sidecar, receipt_root))
            except (ValueError, OSError):
                raise LayoutError('Invalid installed workflow source proof') from None
        else:
            proof = explicit
        selected = SimpleNamespace(roots={'base': receipt_root},
                                   contents=selected_bytes, resources=())
        manifest = verify_packaged_proof(selected, proof)
        package_owner = manifest['owners'][0]
        nested = owned_root.relative_to(receipt_root).as_posix()
        nested = '' if nested == '.' else nested
        source_prefix = '/'.join(part for part in (package_owner['source_prefix'], nested) if part)
        owners.append(dict(owner=owner, commit=package_owner['commit'],
                           object_format=package_owner['object_format'],
                           source_prefix=source_prefix))
        indexed = {row['path']: row for row in manifest['documents']}
        for path, frozen in selected_bytes.items():
            receipt_path = path.relative_to(receipt_root).as_posix()
            proven = indexed[receipt_path]
            documents.append(dict(owner=owner, path=path.relative_to(owned_root).as_posix(),
                                  length=proven['length'], sha256=proven['sha256']))
    result = dict(version=1, owners=sorted(owners, key=lambda row: row['owner']),
                  documents=sorted(documents, key=lambda row: (row['owner'], row['path'])))
    result['sha256'] = hashlib.sha256(json.dumps(result, sort_keys=True,
        separators=(',', ':')).encode()).hexdigest()
    return result
