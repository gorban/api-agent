import subprocess
from pathlib import Path
from types import MappingProxyType

import pytest

from api_agent.workflows.definition import WorkflowDefinition, LayoutError, Resource
from api_agent.workflows.provenance import committed_documents


def git(root, *args):
    return subprocess.run(('git', '-C', str(root), *args), check=True,
                          capture_output=True)


def admitted(tmp_path):
    root = tmp_path / 'owner'
    root.mkdir()
    git(root, 'init', '-q')
    git(root, 'config', 'user.name', 'Example')
    git(root, 'config', 'user.email', 'example@example.invalid')
    file = root / 'flow.md.html'
    file.write_bytes(b'# Workflow v2\n')
    git(root, 'add', 'flow.md.html')
    git(root, 'commit', '-qm', 'add synthetic workflow')
    definition = WorkflowDefinition('Example', file, 'base',
                                    MappingProxyType({'base': root, 'pack': root}),
                                    (), (), 'a' * 64, None,
                                    MappingProxyType({file: file.read_bytes()}))
    return definition, file


def test_committed_manifest_uses_frozen_bytes(tmp_path):
    definition, file = admitted(tmp_path)
    manifest = committed_documents(definition)
    assert manifest['documents'] == [{'owner': 'base', 'path': 'flow.md.html',
                                      'length': len(file.read_bytes()),
                                      'sha256': __import__('hashlib').sha256(file.read_bytes()).hexdigest()}]
    assert len(manifest['sha256']) == 64


@pytest.mark.parametrize('change', ['staged', 'unstaged', 'untracked'])
def test_dirty_consumed_document_rejected(tmp_path, change):
    definition, file = admitted(tmp_path)
    if change == 'staged':
        file.write_bytes(b'changed')
        git(file.parent, 'add', file.name)
        file.write_bytes(definition.contents[file])
    elif change == 'unstaged':
        file.write_bytes(b'changed')
    else:
        extra = file.parent / 'new.md.html'
        extra.write_bytes(b'new')
        definition = WorkflowDefinition(definition.graph_id, extra, definition.entry_root,
            definition.roots, (), (), definition.definition_id, None,
            MappingProxyType({extra: extra.read_bytes()}))
    with pytest.raises(LayoutError):
        committed_documents(definition)


def test_reporting_marker_rejects_unverifiable_source_before_native_exec(monkeypatch, tmp_path):
    import shutil
    from api_agent.workflows import definition as module
    from api_agent.workflows.contrived import build
    from api_agent.providers.llm import LLMResult, Message, TextContent
    from pathlib import Path
    source_root = Path(__file__).resolve().parents[2]
    root = tmp_path / 'uncommitted-owner'
    shutil.copytree(source_root / 'demo/contrived', root / 'demo/contrived')
    shutil.copytree(source_root / 'demo/cfg', root / 'demo/cfg')
    git(root, 'init', '-q')
    async def complete(request):
        return LLMResult(Message('assistant', (TextContent('{}'),)))
    workflow = build(root, llm=complete, selection='example', trial='synthetic')
    source = next(step.source for step in workflow.definition.layout.steps if step.kind == 'node')
    monkeypatch.setenv('API_AGENT_ALLURE_REPORTING', '1')
    with pytest.raises(LayoutError, match='verifiable committed provenance'):
        module.load_native_catalog(workflow.definition, source, reporting_enabled=False)
    monkeypatch.setenv('API_AGENT_ALLURE_REPORTING', '0')
    assert 'produce' in module.load_native_catalog(workflow.definition, source)


def test_packaged_git_object_proof_verifies_without_checkout(tmp_path, monkeypatch):
    from api_agent.workflows.packaged_provenance import build_packaged_proof, verify_packaged_proof
    definition, file = admitted(tmp_path)
    proof = build_packaged_proof(definition)
    monkeypatch.setattr('api_agent.workflows.packaged_provenance._git',
                        lambda *args: (_ for _ in ()).throw(AssertionError('Git used during offline verification')))
    assert verify_packaged_proof(definition, proof) == proof['manifest']
    proof['objects'][proof['manifest']['owners'][0]['commit']] = 'Y29ycnVwdA=='
    with pytest.raises(LayoutError, match='commit identity'):
        verify_packaged_proof(definition, proof)


@pytest.mark.parametrize('damage', (
    'version', 'objects_type', 'manifest_digest', 'missing_document',
    'duplicate_owner', 'source_prefix', 'object_format', 'commit_encoding',
    'missing_tree', 'tree_identity'))
def test_packaged_receipt_rejects_independent_identity_damage(tmp_path, damage):
    import base64
    from copy import deepcopy
    from api_agent.workflows.packaged_provenance import build_packaged_proof, verify_packaged_proof

    definition, _ = admitted(tmp_path)
    proof = deepcopy(build_packaged_proof(definition))
    owner = proof['manifest']['owners'][0]
    if damage == 'version':
        proof['version'] = 2
    elif damage == 'objects_type':
        proof['objects'] = []
    elif damage == 'manifest_digest':
        proof['manifest']['sha256'] = '0' * 64
    elif damage == 'missing_document':
        proof['manifest']['documents'].clear()
    elif damage == 'duplicate_owner':
        proof['manifest']['owners'].append(deepcopy(owner))
    elif damage == 'source_prefix':
        owner['source_prefix'] = '../outside'
    elif damage == 'object_format':
        owner['object_format'] = 'unsupported'
    elif damage == 'commit_encoding':
        proof['objects'][owner['commit']] = '!invalid-base64!'
    else:
        commit = base64.b64decode(proof['objects'][owner['commit']])
        tree = commit.split(b'\n', 1)[0][5:].decode()
        if damage == 'missing_tree':
            proof['objects'].pop(tree)
        else:
            proof['objects'][tree] = base64.b64encode(b'invalid tree bytes').decode()
    if damage in ('duplicate_owner', 'source_prefix', 'object_format'):
        import hashlib
        import json
        unsigned = {key: value for key, value in proof['manifest'].items() if key != 'sha256'}
        proof['manifest']['sha256'] = hashlib.sha256(json.dumps(unsigned,
            sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    with pytest.raises(LayoutError):
        verify_packaged_proof(definition, proof)


def test_packaged_proof_survives_relocated_gitless_resources(tmp_path):
    from dataclasses import replace
    from api_agent.workflows.packaged_provenance import build_packaged_proof, verify_packaged_proof
    definition, file = admitted(tmp_path)
    proof = build_packaged_proof(definition)
    installed = tmp_path / 'installed'
    installed.mkdir()
    moved = installed / file.name
    moved.write_bytes(file.read_bytes())
    relocated = replace(definition, entry=moved,
        roots=MappingProxyType({'base': installed, 'pack': installed}),
        contents=MappingProxyType({moved: moved.read_bytes()}))
    assert verify_packaged_proof(relocated, proof) == proof['manifest']


def test_committed_subroot_records_git_path_prefix(tmp_path):
    from dataclasses import replace
    from api_agent.workflows.packaged_provenance import build_packaged_proof, verify_packaged_proof
    definition, file = admitted(tmp_path)
    subroot = file.parent / 'nested'
    subroot.mkdir()
    nested = subroot / 'flow.md.html'
    nested.write_bytes(file.read_bytes())
    git(file.parent, 'add', 'nested/flow.md.html')
    git(file.parent, 'commit', '-qm', 'add nested synthetic workflow')
    selected = replace(definition, entry=nested,
        roots=MappingProxyType({'base': subroot, 'pack': subroot}),
        contents=MappingProxyType({nested: nested.read_bytes()}))
    manifest = committed_documents(selected)
    assert manifest['owners'][0]['source_prefix'] == 'nested'
    proof = build_packaged_proof(selected)
    installed = tmp_path / 'installed'
    installed.mkdir()
    moved = installed / nested.name
    moved.write_bytes(nested.read_bytes())
    relocated = replace(selected, entry=moved,
        roots=MappingProxyType({'base': installed, 'pack': installed}),
        contents=MappingProxyType({moved: moved.read_bytes()}))
    assert verify_packaged_proof(relocated, proof) == manifest


def test_inline_prompt_is_proven_through_physical_parent(tmp_path):
    from dataclasses import replace
    from api_agent.workflows.definition import InlinePromptRef
    definition, file = admitted(tmp_path)
    file.write_bytes(b'# Workflow v2\n## Prompt: Intro\n\n```text\nHello\n```\n')
    git(file.parent, 'add', file.name)
    git(file.parent, 'commit', '-qm', 'add synthetic inline prompt')
    virtual = InlinePromptRef(file, 'Intro')
    selected = replace(definition, contents=MappingProxyType({
        file: file.read_bytes(), virtual: b'Hello\n'}))
    manifest = committed_documents(selected)
    assert [row['path'] for row in manifest['documents']] == ['flow.md.html']


def test_inline_prompt_must_match_admitted_parent_bytes(tmp_path):
    from dataclasses import replace
    from api_agent.workflows.definition import InlinePromptRef
    definition, file = admitted(tmp_path)
    virtual = InlinePromptRef(file, 'Intro')
    selected = replace(definition, contents=MappingProxyType({
        file: file.read_bytes(), virtual: b'Forged prompt\n'}))
    with pytest.raises(LayoutError, match='absent from its admitted parent'):
        committed_documents(selected)
    selected = replace(selected, contents=MappingProxyType({virtual: b'Forged prompt\n'}))
    with pytest.raises(LayoutError, match='no admitted parent'):
        committed_documents(selected)


def test_committed_manifest_rejects_changed_working_bytes_after_admission(tmp_path):
    definition, file = admitted(tmp_path)
    file.write_bytes(b'# Changed after admission\n')
    with pytest.raises(LayoutError, match='changed after admission'):
        committed_documents(definition)


def test_installed_sidecar_must_be_regular_valid_json(tmp_path):
    import json
    from dataclasses import replace
    from hatch_build import _proof
    from api_agent.workflows.packaged_provenance import installed_proof
    definition, file = admitted(tmp_path)
    installed = tmp_path / 'installed'
    installed.mkdir()
    moved = installed / file.name
    moved.write_bytes(file.read_bytes())
    selected = replace(definition, entry=moved,
        roots=MappingProxyType({'base': installed, 'pack': installed}),
        contents=MappingProxyType({moved: moved.read_bytes()}))
    sidecar = installed / 'workflow-source-proof.json'
    sidecar.write_text('not-json')
    with pytest.raises(LayoutError, match='Invalid installed workflow source proof'):
        installed_proof(selected)
    sidecar.unlink()
    receipt = installed / 'valid-proof.json'
    receipt.write_text(json.dumps(_proof(file.parent)))
    sidecar.symlink_to(receipt)
    with pytest.raises(LayoutError, match='symbolic link'):
        installed_proof(selected)


def test_packaged_git_object_parsers_reject_malformed_objects():
    from api_agent.workflows.packaged_provenance import _tree_entries, _commit_tree, _decode
    with pytest.raises(LayoutError, match='tree object'):
        _tree_entries(b'100644 file\0short', 20)
    with pytest.raises(LayoutError, match='tree path'):
        _tree_entries(b'100644 ../escape\0' + b'0' * 20, 20)
    with pytest.raises(LayoutError, match='commit object'):
        _commit_tree(b'parent 0123\n')
    with pytest.raises(LayoutError, match='exceeds bound'):
        _decode('A' * 100, 10)
    with pytest.raises(LayoutError, match='exceeds bound'):
        _decode('QUFBQUFBQUFB', 5)
    import base64
    with pytest.raises(LayoutError, match='exceeds bound'):
        _decode(base64.b64encode(b'abcdef').decode(), 5)


def test_packaged_receipt_must_bind_selected_bytes_to_git_blob(tmp_path):
    import hashlib
    import json
    from dataclasses import replace
    from api_agent.workflows.packaged_provenance import build_packaged_proof, verify_packaged_proof
    definition, file = admitted(tmp_path)
    proof = build_packaged_proof(definition)
    changed = b'# Forged selected document\n'
    selected = replace(definition, contents=MappingProxyType({file: changed}))
    row = proof['manifest']['documents'][0]
    row['length'] = len(changed)
    row['sha256'] = hashlib.sha256(changed).hexdigest()
    unsigned = {key: value for key, value in proof['manifest'].items() if key != 'sha256'}
    proof['manifest']['sha256'] = hashlib.sha256(json.dumps(unsigned,
        sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    with pytest.raises(LayoutError, match='blob identity'):
        verify_packaged_proof(selected, proof)


def test_installed_selected_owner_requires_own_documents(tmp_path):
    from types import SimpleNamespace
    from api_agent.workflows.packaged_provenance import installed_proof
    definition, file = admitted(tmp_path)
    empty = SimpleNamespace(roots=definition.roots, contents={}, resources=())
    with pytest.raises(LayoutError, match='no physical documents'):
        installed_proof(empty)
    with pytest.raises(LayoutError, match='no selected documents'):
        installed_proof(definition, selected_owners={'other'})
    outside = tmp_path / 'outside.md.html'
    outside.write_bytes(b'# Outside\n')
    orphan = SimpleNamespace(roots=definition.roots,
        contents={outside: outside.read_bytes()}, resources=())
    with pytest.raises(LayoutError, match='no admitted owner'):
        installed_proof(orphan)


def test_source_admission_rejects_orphan_empty_and_invalid_root_contracts(tmp_path):
    from dataclasses import replace
    from types import SimpleNamespace
    from api_agent.workflows.provenance import verified_documents
    definition, file = admitted(tmp_path)
    with pytest.raises(TypeError, match='admitted workflow definition'):
        committed_documents(SimpleNamespace())
    with pytest.raises(LayoutError, match='no documents'):
        committed_documents(replace(definition, contents=MappingProxyType({})))
    outside = tmp_path / 'outside.md.html'
    outside.write_bytes(b'# Orphan\n')
    orphan = replace(definition, contents=MappingProxyType({outside: outside.read_bytes()}))
    with pytest.raises(LayoutError, match='no owning root'):
        committed_documents(orphan)
    with pytest.raises(LayoutError, match='no owning root'):
        verified_documents(orphan)
    with pytest.raises(LayoutError, match='invalid owner path'):
        committed_documents(replace(definition,
            roots=MappingProxyType({'base': file, 'pack': file})))


def test_inline_virtual_bytes_must_match_fenced_parent_body(tmp_path):
    from dataclasses import replace
    from api_agent.workflows.definition import InlinePromptRef
    definition, file = admitted(tmp_path)
    file.write_bytes(b'# Workflow v2\n## Prompt: Intro\n\n```text\nHello\n```\n\n')
    git(file.parent, 'add', file.name)
    git(file.parent, 'commit', '-qm', 'add independent inline document')
    ref = InlinePromptRef(file, 'Intro')
    selected = replace(definition, contents=MappingProxyType({
        file: file.read_bytes(), ref: b'Forged\n'}))
    with pytest.raises(LayoutError, match='differs from its admitted parent'):
        committed_documents(selected)
    selected = replace(selected, contents=MappingProxyType({
        file: file.read_bytes(), ref: b'Hello\n'}))
    assert committed_documents(selected)['documents'][0]['path'] == file.name


def test_packaged_proof_rejects_missing_selected_row_and_changed_bytes(tmp_path):
    import hashlib
    import json
    from dataclasses import replace
    from api_agent.workflows.packaged_provenance import build_packaged_proof, verify_packaged_proof
    definition, file = admitted(tmp_path)
    proof = build_packaged_proof(definition)
    proof['manifest']['documents'].clear()
    unsigned = {key: value for key, value in proof['manifest'].items() if key != 'sha256'}
    proof['manifest']['sha256'] = hashlib.sha256(json.dumps(unsigned,
        sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    with pytest.raises(LayoutError, match='documents do not match'):
        verify_packaged_proof(definition, proof)
    proof = build_packaged_proof(definition)
    altered = replace(definition, contents=MappingProxyType({file: b'# Changed bytes\n'}))
    with pytest.raises(LayoutError, match='bytes do not match'):
        verify_packaged_proof(altered, proof)


def test_packaged_proof_rejects_orphan_selected_path_and_accepts_explicit_receipt(tmp_path):
    from dataclasses import replace
    from api_agent.workflows.packaged_provenance import build_packaged_proof, installed_proof, verify_packaged_proof
    definition, file = admitted(tmp_path)
    proof = build_packaged_proof(definition)
    outside = tmp_path / 'orphan.md.html'
    outside.write_bytes(file.read_bytes())
    orphan = replace(definition, contents=MappingProxyType({outside: outside.read_bytes()}))
    with pytest.raises(LayoutError, match='no owner'):
        verify_packaged_proof(orphan, proof)
    installed = tmp_path / 'installed'
    installed.mkdir()
    moved = installed / file.name
    moved.write_bytes(file.read_bytes())
    relocated = replace(definition, entry=moved,
        roots=MappingProxyType({'base': installed, 'pack': installed}),
        contents=MappingProxyType({moved: moved.read_bytes()}))
    assert installed_proof(relocated, packaged_proof=proof)['documents'][0]['path'] == file.name


def test_packaged_build_rejects_missing_committed_tree_entry(tmp_path, monkeypatch):
    import api_agent.workflows.packaged_provenance as module
    definition, _ = admitted(tmp_path)
    monkeypatch.setattr(module, '_tree_entries', lambda raw, size: {})
    with pytest.raises(LayoutError, match='absent from Git tree'):
        module.build_packaged_proof(definition)


def test_packaged_receipt_rejects_oversized_objects_and_wrong_git_tree_path(tmp_path):
    import base64
    import hashlib
    import json
    from api_agent.workflows.packaged_provenance import _oid, build_packaged_proof, verify_packaged_proof
    definition, _ = admitted(tmp_path)
    proof = build_packaged_proof(definition)
    proof['objects']['extra'] = 'x' * (16 * 1024 * 1024)
    with pytest.raises(LayoutError, match='exceeds bound'):
        verify_packaged_proof(definition, proof)
    del proof['objects']['extra']
    owner = proof['manifest']['owners'][0]
    original = base64.b64decode(proof['objects'][owner['commit']])
    empty_tree = b''
    empty_oid = _oid('tree', empty_tree, owner['object_format'])
    original_tree = original.split(b'\n', 1)[0]
    replacement = original.replace(original_tree, b'tree ' + empty_oid.encode(), 1)
    new_commit = _oid('commit', replacement, owner['object_format'])
    proof['objects'][new_commit] = base64.b64encode(replacement).decode()
    proof['objects'][empty_oid] = base64.b64encode(empty_tree).decode()
    owner['commit'] = new_commit
    unsigned = {key: value for key, value in proof['manifest'].items() if key != 'sha256'}
    proof['manifest']['sha256'] = hashlib.sha256(json.dumps(unsigned,
        sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    with pytest.raises(LayoutError, match='tree path mismatch'):
        verify_packaged_proof(definition, proof)


@pytest.mark.parametrize(('argument', 'replacement', 'message'), (
    ('--show-toplevel', b'/unrelated/checkout\n', 'outside its Git root'),
    ('--show-object-format', b'unsupported\n', 'Unsupported Git object format'),
))
def test_source_capture_rejects_inconsistent_git_metadata(tmp_path, monkeypatch,
                                                          argument, replacement, message):
    import api_agent.workflows.provenance as module
    definition, _ = admitted(tmp_path)
    original = module._git

    def changed(root, *args):
        return replacement if argument in args else original(root, *args)

    monkeypatch.setattr(module, '_git', changed)
    with pytest.raises(LayoutError, match=message):
        committed_documents(definition)


def test_source_capture_rejects_head_movement_during_verification(tmp_path, monkeypatch):
    import api_agent.workflows.provenance as module
    definition, _ = admitted(tmp_path)
    original = module._git
    seen = 0

    def moved(root, *args):
        nonlocal seen
        if args == ('rev-parse', 'HEAD'):
            seen += 1
            if seen == 2:
                return b'0' * 40 + b'\n'
        return original(root, *args)

    monkeypatch.setattr(module, '_git', moved)
    with pytest.raises(LayoutError, match='changed during capture'):
        committed_documents(definition)
    assert seen == 2


def test_source_inventory_rejects_duplicate_physical_identity(tmp_path, monkeypatch):
    import api_agent.workflows.provenance as module
    definition, file = admitted(tmp_path)

    class Alias:
        def __init__(self, path):
            self.path = path
        def __fspath__(self):
            return str(self.path)
        def is_relative_to(self, root):
            return self.path.is_relative_to(root)

    original = module.physical_documents
    monkeypatch.setattr(module, 'physical_documents', lambda definition: {
        Alias(file): file.read_bytes(), Alias(file): file.read_bytes()})
    with pytest.raises(LayoutError, match='Duplicate workflow document identity'):
        committed_documents(definition)
    monkeypatch.setattr(module, 'physical_documents', original)


def test_per_owner_router_uses_packaged_proof_for_non_checkout_root(tmp_path, monkeypatch):
    from dataclasses import replace
    import api_agent.workflows.provenance as module
    import api_agent.workflows.packaged_provenance as packaged
    definition, file = admitted(tmp_path)
    original = module._git
    expected = committed_documents(definition)
    selected = []
    def outside(root, *args):
        return b'/unrelated/checkout\n' if args == ('rev-parse', '--show-toplevel') else original(root, *args)
    def verified(definition, **kwargs):
        selected.append(kwargs['selected_owners'])
        return expected
    monkeypatch.setattr(module, '_git', outside)
    monkeypatch.setattr(packaged, 'installed_proof', verified)
    result = module.verified_documents(definition)
    assert selected == [{'base'}] and result['documents'] == expected['documents']
    with pytest.raises(LayoutError, match='no selected documents'):
        module.verified_documents(replace(definition, contents=MappingProxyType({})))


def test_source_capture_rejects_commit_movement_between_documents(tmp_path, monkeypatch):
    from dataclasses import replace
    import api_agent.workflows.provenance as module
    definition, file = admitted(tmp_path)
    second = file.parent / 'second.md.html'
    second.write_bytes(b'# Second selected document\n')
    git(file.parent, 'add', second.name)
    git(file.parent, 'commit', '-qm', 'add independent second document')
    selected = replace(definition, contents=MappingProxyType({
        file: file.read_bytes(), second: second.read_bytes()}))
    original = module._git
    seen = 0
    def moved(root, *args):
        nonlocal seen
        if args == ('rev-parse', 'HEAD'):
            seen += 1
            if seen == 2:
                return b'0' * 40 + b'\n'
        return original(root, *args)
    monkeypatch.setattr(module, '_git', moved)
    with pytest.raises(LayoutError, match='changed during capture'):
        committed_documents(selected)
    assert seen == 2


def test_unowned_native_resource_label_cannot_hide_document_bytes(tmp_path):
    from dataclasses import replace
    from types import SimpleNamespace
    from api_agent.workflows.provenance import physical_documents
    definition, file = admitted(tmp_path)
    selected = replace(definition, resources=(SimpleNamespace(
        kind='native_source', logical_path='unowned'),))
    assert physical_documents(selected) == {file: file.read_bytes()}


def test_native_python_change_is_outside_document_hash_policy(tmp_path):
    from dataclasses import replace
    from hashlib import sha256
    definition, file = admitted(tmp_path)
    source = file.parent / 'functions.py'
    source.write_bytes(b'def produce(): return 1\n')
    git(file.parent, 'add', 'functions.py')
    git(file.parent, 'commit', '-qm', 'add synthetic native source')
    frozen = source.read_bytes()
    selected = replace(definition, resources=(Resource('native_source',
        'base/functions.py', frozen, sha256(frozen).hexdigest()),),
        contents=MappingProxyType({file: file.read_bytes(), source: frozen}))
    source.write_bytes(b'def produce(): return 2\n')
    manifest = committed_documents(selected)
    assert [row['path'] for row in manifest['documents']] == ['flow.md.html']


def test_automatic_installed_sidecar_verifies_selected_bytes(tmp_path):
    import json
    from dataclasses import replace
    from api_agent.workflows.packaged_provenance import installed_proof
    from hatch_build import _proof
    definition, file = admitted(tmp_path)
    proof = _proof(file.parent)
    installed = tmp_path / 'installed'
    installed.mkdir()
    moved = installed / file.name
    moved.write_bytes(file.read_bytes())
    sidecar = installed / 'workflow-source-proof.json'
    sidecar.write_text(json.dumps(proof))
    relocated = replace(definition, entry=moved,
        roots=MappingProxyType({'base': installed, 'pack': installed}),
        contents=MappingProxyType({moved: moved.read_bytes()}))
    installed_manifest = installed_proof(relocated)
    assert installed_manifest['documents'] == [dict(owner='base', path='flow.md.html',
        length=len(file.read_bytes()), sha256=__import__('hashlib').sha256(file.read_bytes()).hexdigest())]
    sidecar.write_text(json.dumps({**proof, 'objects': {}}))
    with pytest.raises(LayoutError):
        installed_proof(relocated)


def test_two_installed_owners_require_independent_sidecars(tmp_path):
    import json
    from types import SimpleNamespace
    from hatch_build import _proof
    from api_agent.workflows.packaged_provenance import installed_proof
    first_home, second_home = tmp_path / 'first', tmp_path / 'second'
    first_home.mkdir()
    second_home.mkdir()
    first, first_file = admitted(first_home)
    second, second_file = admitted(second_home)
    second_file.write_bytes(b'# Different selected declaration\n')
    git(second_file.parent, 'add', second_file.name)
    git(second_file.parent, 'commit', '-qm', 'revise synthetic declaration')
    installed = tmp_path / 'installed'
    base, pack = installed / 'shared', installed / 'extension'
    base.mkdir(parents=True)
    pack.mkdir()
    base_file, pack_file = base / first_file.name, pack / second_file.name
    base_file.write_bytes(first_file.read_bytes())
    pack_file.write_bytes(second_file.read_bytes())
    (base / 'workflow-source-proof.json').write_text(json.dumps(_proof(first_file.parent)))
    pack_sidecar = pack / 'workflow-source-proof.json'
    pack_proof = _proof(second_file.parent)
    pack_sidecar.write_text(json.dumps(pack_proof))
    selected = SimpleNamespace(roots={'base': base, 'pack': pack},
        contents={base_file: base_file.read_bytes(), pack_file: pack_file.read_bytes()},
        resources=())
    manifest = installed_proof(selected)
    assert {(row['owner'], row['path']) for row in manifest['documents']} == {
        ('base', 'flow.md.html'), ('pack', 'flow.md.html')}
    pack_sidecar.unlink()
    (installed / 'workflow-source-proof.json').write_text(json.dumps(pack_proof))
    with pytest.raises(LayoutError, match='packaged source proof'):
        installed_proof(selected)
    pack_sidecar.write_text(json.dumps({**pack_proof, 'objects': {}}))
    with pytest.raises(LayoutError):
        installed_proof(selected)


def test_mixed_checkout_and_installed_owners_verify_independently(tmp_path):
    import json
    from types import SimpleNamespace
    from hatch_build import _proof
    from api_agent.workflows.provenance import verified_documents
    source_home, package_home = tmp_path / 'source', tmp_path / 'package'
    source_home.mkdir()
    package_home.mkdir()
    _, source_file = admitted(source_home)
    _, package_file = admitted(package_home)
    installed = tmp_path / 'installed-extension'
    installed.mkdir()
    installed_file = installed / package_file.name
    installed_file.write_bytes(package_file.read_bytes())
    sidecar = installed / 'workflow-source-proof.json'
    sidecar.write_text(json.dumps(_proof(package_file.parent)))
    selected = SimpleNamespace(definition_id='a' * 64,
        roots={'base': source_file.parent, 'pack': installed},
        contents={source_file: source_file.read_bytes(), installed_file: installed_file.read_bytes()},
        resources=())
    manifest = verified_documents(selected)
    assert {row['owner'] for row in manifest['owners']} == {'base', 'pack'}
    sidecar.unlink()
    with pytest.raises(LayoutError, match='packaged source proof'):
        verified_documents(selected)


def test_nested_logical_roots_share_one_distribution_without_duplicate_documents(tmp_path):
    import json
    from types import SimpleNamespace
    from hatch_build import _proof
    from api_agent.workflows.packaged_provenance import installed_proof
    definition, file = admitted(tmp_path)
    nested = file.parent / 'demo'
    nested.mkdir()
    extra = nested / 'extra.md.html'
    extra.write_bytes(b'# Nested declaration\n')
    git(file.parent, 'add', 'demo/extra.md.html')
    git(file.parent, 'commit', '-qm', 'add nested declaration')
    installed = tmp_path / 'installed'
    (installed / 'demo').mkdir(parents=True)
    direct = installed / file.name
    extension = installed / 'demo/extra.md.html'
    direct.write_bytes(file.read_bytes())
    extension.write_bytes(extra.read_bytes())
    (installed / 'workflow-source-proof.json').write_text(json.dumps(_proof(file.parent)))
    selected = SimpleNamespace(roots={'base': installed, 'pack': installed / 'demo'},
        contents={direct: direct.read_bytes(), extension: extension.read_bytes()},
        resources=())
    manifest = installed_proof(selected)
    assert [(row['owner'], row['path']) for row in manifest['documents']] == [
        ('base', 'flow.md.html'), ('pack', 'extra.md.html')]
    assert next(row for row in manifest['owners'] if row['owner'] == 'pack')['source_prefix'] == 'demo'


@pytest.mark.parametrize('ambient', ['none', 'untracked', 'tracked'])
def test_source_archive_and_wheel_preserve_same_proof(tmp_path, monkeypatch, ambient):
    import json
    import shutil
    import sys
    import tarfile
    import zipfile
    source = Path(__file__).resolve().parents[2]
    project = tmp_path / 'project'
    project.mkdir()
    for name in ('pyproject.toml', 'hatch_build.py'):
        shutil.copyfile(source / name, project / name)
    (project / 'LICENSE').write_text('Synthetic test license.\n')
    (project / 'README.md').write_text('Synthetic package.\n')
    (project / 'flow.md.html').write_text('# Workflow v2\n')
    (project / 'src/api_agent').mkdir(parents=True)
    (project / 'src/api_agent/__init__.py').write_text('"""Synthetic package."""\n')
    (project / 'workflows').mkdir()
    (project / 'workflows/example.md.html').write_text('# Component v2\n')
    for folder in ('demo', 'cfg', 'infra/compose', 'apps/api-agent-ui'):
        (project / folder).mkdir(parents=True)
        (project / folder / 'placeholder.txt').write_text('Synthetic package fixture.\n')
    # Supply the declared package inputs without making this provenance test
    # depend on an incomplete synthetic checkout. The archive boundary test
    # separately enforces the exact allowed input set.
    import tomllib
    configuration = tomllib.loads((source / 'pyproject.toml').read_text())
    assets = configuration['tool']['hatch']['build']['targets']['sdist']['force-include']
    for relative in (*assets, 'tools/checks.py', 'tools/pre_commit_dispatcher.py',
                     'tools/workflow_views.py'):
        target = project / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / relative, target)
    git(project, 'init', '-q')
    git(project, 'config', 'user.name', 'Example')
    git(project, 'config', 'user.email', 'example@example.invalid')
    git(project, 'add', '.')
    git(project, 'commit', '-qm', 'synthetic package')
    dist = tmp_path / 'dist'
    subprocess.run((sys.executable, '-I', '-m', 'build', '--no-isolation', '--sdist',
                    '--outdir', str(dist), str(project)), check=True, capture_output=True)
    archive = next(dist.glob('*.tar.gz'))
    consumer = tmp_path / 'extract'
    consumer.mkdir()
    if ambient != 'none':
        git(consumer, 'init', '-q')
        git(consumer, 'config', 'user.name', 'Example')
        git(consumer, 'config', 'user.email', 'example@example.invalid')
        (consumer / 'README').write_text('Synthetic consumer.\n')
        git(consumer, 'add', 'README')
        git(consumer, 'commit', '-qm', 'synthetic consumer')
    with tarfile.open(archive) as package:
        member = next(item for item in package if item.name.endswith('/workflow-source-proof.json'))
        from_source = package.extractfile(member).read()
        package.extractall(consumer, filter='data')
    extracted = next(path for path in consumer.iterdir() if path.is_dir() and path.name != '.git')
    if ambient == 'tracked':
        git(consumer, 'add', extracted.name)
        git(consumer, 'commit', '-qm', 'vendor synthetic source archive')
    subprocess.run((sys.executable, '-I', '-m', 'build', '--no-isolation', '--wheel',
                    '--outdir', str(dist), str(extracted)), check=True, capture_output=True)
    with zipfile.ZipFile(next(dist.glob('*.whl'))) as package:
        from_wheel = package.read('api_agent/workflow-source-proof.json')
    assert json.loads(from_source) == json.loads(from_wheel)
    site = consumer / 'environment/site-packages'
    installer = tmp_path / 'installer'
    subprocess.run((sys.executable, '-I', '-m', 'venv', str(installer)),
                   check=True, capture_output=True)
    import os
    python = installer / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    subprocess.run((str(python), '-I', '-m', 'pip', 'install', '--no-index', '--no-deps',
                    '--target', str(site), str(next(dist.glob('*.whl')))),
                   check=True, capture_output=True)
    if ambient == 'tracked':
        git(consumer, 'add', 'environment')
        git(consumer, 'commit', '-qm', 'vendor synthetic installed wheel')
    monkeypatch.syspath_prepend(str(site))
    from types import SimpleNamespace
    from api_agent.workflows.provenance import verified_documents
    package = site / 'api_agent'
    entry = package / 'flow.md.html'
    selected = SimpleNamespace(roots={'base': package},
                               contents={entry: entry.read_bytes()}, resources=(), definition_id='a' * 64)
    expected = (git(consumer, 'rev-parse', 'HEAD').stdout.decode().strip()
                if ambient == 'tracked' else json.loads(from_source)['manifest']['owners'][0]['commit'])
    assert verified_documents(selected)['owners'][0]['commit'] == expected


@pytest.fixture
def installed_distribution(tmp_path, monkeypatch):
    import json
    from dataclasses import replace
    from hatch_build import _proof
    definition, file = admitted(tmp_path)
    proof = _proof(file.parent)
    checkout = tmp_path / 'consumer'
    checkout.mkdir()
    git(checkout, 'init', '-q')
    git(checkout, 'config', 'user.name', 'Example')
    git(checkout, 'config', 'user.email', 'example@example.invalid')
    (checkout / 'README').write_text('Synthetic consumer.\n')
    git(checkout, 'add', 'README')
    git(checkout, 'commit', '-qm', 'synthetic consumer')
    site = checkout / 'environment/site-packages'
    package = site / 'synthetic_workflow'
    package.mkdir(parents=True)
    moved = package / file.name
    moved.write_bytes(file.read_bytes())
    receipt = package / 'workflow-source-proof.json'
    receipt.write_text(json.dumps(proof))
    info = site / 'synthetic_workflow-1.0.dist-info'
    info.mkdir()
    (info / 'METADATA').write_text('Metadata-Version: 2.1\nName: synthetic-workflow\nVersion: 1.0\n')
    (info / 'RECORD').write_text(''.join(str(path.relative_to(site)) + ',,\n'
        for path in (moved, receipt, info / 'METADATA', info / 'RECORD')))
    monkeypatch.syspath_prepend(str(site))
    selected = replace(definition, entry=moved,
        roots=MappingProxyType({'base': package, 'pack': package}),
        contents=MappingProxyType({moved: moved.read_bytes()}))
    return selected, checkout, site, info, proof


@pytest.mark.parametrize('tracked', [False, True])
def test_installed_distribution_under_unrelated_git_uses_its_proof(installed_distribution, tracked):
    from dataclasses import replace
    from api_agent.workflows.provenance import verified_documents
    selected, checkout, site, info, proof = installed_distribution
    moved = selected.entry
    if tracked:
        git(checkout, 'add', 'environment')
        git(checkout, 'commit', '-qm', 'vendor synthetic distribution')
    manifest = verified_documents(selected)
    expected = (git(checkout, 'rev-parse', 'HEAD').stdout.decode().strip()
                if tracked else proof['manifest']['owners'][0]['commit'])
    assert {row['commit'] for row in manifest['owners']} == {expected}
    moved.write_bytes(b'# Changed installed workflow\n')
    with pytest.raises(LayoutError, match='differs from|match receipt'):
        verified_documents(replace(selected, contents=MappingProxyType({moved: moved.read_bytes()})))


@pytest.mark.parametrize('state', ['clean', 'staged', 'unstaged', 'index-deleted', 'untracked', 'ignored'])
def test_nested_source_with_receipt_keeps_committed_source_gate(tmp_path, monkeypatch, state):
    import json
    from dataclasses import replace
    from api_agent.workflows.packaged_provenance import build_packaged_proof
    from api_agent.workflows.provenance import verified_documents
    definition, file = admitted(tmp_path)
    nested = file.parent / 'nested'
    nested.mkdir()
    moved = nested / file.name
    moved.write_bytes(file.read_bytes())
    git(file.parent, 'add', 'nested')
    git(file.parent, 'commit', '-qm', 'synthetic nested source')
    selected = replace(definition, entry=moved,
        roots=MappingProxyType({'base': nested, 'pack': nested}),
        contents=MappingProxyType({moved: moved.read_bytes()}))
    (nested / 'workflow-source-proof.json').write_text(json.dumps(build_packaged_proof(selected)))
    site = tmp_path / 'site-packages'
    info = site / 'synthetic_editable-1.0.dist-info'
    info.mkdir(parents=True)
    (site / 'synthetic_editable.pth').write_text(str(nested) + '\n')
    (info / 'METADATA').write_text('Metadata-Version: 2.1\nName: synthetic-editable\nVersion: 1.0\n')
    (info / 'RECORD').write_text('synthetic_editable.pth,,\nsynthetic_editable-1.0.dist-info/METADATA,,\nsynthetic_editable-1.0.dist-info/RECORD,,\n')
    monkeypatch.syspath_prepend(str(site))
    if state in ('untracked', 'ignored', 'index-deleted'):
        git(file.parent, 'rm', '--cached', 'nested/flow.md.html')
        if state != 'index-deleted':
            git(file.parent, 'commit', '-qm', 'remove synthetic document')
        if state == 'ignored':
            (file.parent / '.gitignore').write_text('nested/flow.md.html\n')
    elif state in ('staged', 'unstaged'):
        moved.write_bytes(b'# Changed source workflow\n')
        if state == 'staged':
            git(file.parent, 'add', 'nested/flow.md.html')
            moved.write_bytes(file.read_bytes())
    selected = replace(definition, entry=moved,
        roots=MappingProxyType({'base': nested, 'pack': nested}),
        contents=MappingProxyType({moved: moved.read_bytes()}))
    if state == 'clean':
        assert verified_documents(selected)['owners'][0]['source_prefix'] == 'nested'
    else:
        with pytest.raises(LayoutError, match='staged|differs from HEAD|untracked or ignored'):
            verified_documents(selected)


def test_distribution_ownership_requires_filesystem_inventory(tmp_path, monkeypatch):
    from importlib import metadata
    import api_agent.workflows.provenance as module
    definition, _ = admitted(tmp_path)
    provider = tmp_path / 'empty.dist-info'
    provider.mkdir()
    (provider / 'METADATA').write_text('Name: empty\n')
    (provider / 'RECORD').write_text('')
    missing = metadata.PathDistribution(provider)
    monkeypatch.setattr(metadata, 'distributions', lambda: [object(), missing])
    assert not module._distribution_owns_documents(definition, 'base')


@pytest.mark.parametrize('alias', ['RECORD', 'METADATA', 'directory'])
def test_distribution_metadata_alias_cannot_establish_ownership(installed_distribution, tmp_path, monkeypatch, alias):
    from importlib import metadata
    from api_agent.workflows.provenance import _distribution_owns_documents, verified_documents
    selected, _, _, info, _ = installed_distribution
    target = info if alias == 'directory' else info / alias
    outside = tmp_path / ('external-' + target.name)
    target.rename(outside)
    target.symlink_to(outside, target_is_directory=alias == 'directory')
    reads = []
    original = metadata.PathDistribution.read_text
    def read_text(distribution, name):
        reads.append(name)
        return original(distribution, name)
    monkeypatch.setattr(metadata, 'distributions', lambda: [metadata.PathDistribution(info)])
    monkeypatch.setattr(metadata.PathDistribution, 'read_text', read_text)
    assert not _distribution_owns_documents(selected, 'base')
    assert reads == []
    with pytest.raises(LayoutError):
        verified_documents(selected)


def test_distinct_distribution_claimants_are_ambiguous(installed_distribution):
    from api_agent.workflows.provenance import _distribution_owns_documents, verified_documents
    selected, _, site, info, _ = installed_distribution
    other = site / 'second_workflow-1.0.dist-info'
    other.mkdir()
    (other / 'METADATA').write_text('Name: second-workflow\nVersion: 1.0\n')
    (other / 'RECORD').write_text((info / 'RECORD').read_text().replace(info.name, other.name))
    assert not _distribution_owns_documents(selected, 'base')
    with pytest.raises(LayoutError):
        verified_documents(selected)


def test_repeated_physical_distribution_discovery_is_one_owner(installed_distribution, monkeypatch):
    from importlib import metadata
    from api_agent.workflows.provenance import _distribution_owns_documents
    selected, _, _, info, _ = installed_distribution
    monkeypatch.setattr(metadata, 'distributions', lambda: [metadata.PathDistribution(info), metadata.PathDistribution(info)])
    assert _distribution_owns_documents(selected, 'base')


def test_distribution_inventory_must_own_its_metadata(installed_distribution):
    from api_agent.workflows.provenance import _distribution_owns_documents
    selected, _, _, info, _ = installed_distribution
    rows = (info / 'RECORD').read_text().splitlines()
    (info / 'RECORD').write_text('\n'.join(row for row in rows if '.dist-info/' not in row))
    assert not _distribution_owns_documents(selected, 'base')


@pytest.mark.parametrize('marker', ['pyproject.toml', 'setup.py', 'setup.cfg'])
@pytest.mark.parametrize('index_deleted', [False, True])
def test_git_owned_project_marker_preserves_source_gate(installed_distribution, marker, index_deleted):
    from api_agent.workflows.provenance import verified_documents
    selected, checkout, _, _, _ = installed_distribution
    project = selected.roots['base'] / marker
    project.write_text('Synthetic source project marker.\n')
    relative = project.relative_to(checkout).as_posix()
    git(checkout, 'add', relative)
    git(checkout, 'commit', '-qm', 'track synthetic project marker')
    if index_deleted:
        git(checkout, 'rm', '--cached', relative)
    with pytest.raises(LayoutError, match='untracked or ignored'):
        verified_documents(selected)


def test_head_owned_document_keeps_source_gate_after_index_deletion(installed_distribution):
    from api_agent.workflows.provenance import verified_documents
    selected, checkout, _, _, _ = installed_distribution
    relative = selected.entry.relative_to(checkout).as_posix()
    git(checkout, 'add', relative)
    git(checkout, 'commit', '-qm', 'track synthetic workflow')
    git(checkout, 'rm', '--cached', relative)
    with pytest.raises(LayoutError, match='untracked or ignored'):
        verified_documents(selected)


@pytest.mark.parametrize('record_kind', ['regular', 'alias'])
def test_record_cannot_launder_staged_nested_source(tmp_path, monkeypatch, record_kind):
    import json
    from dataclasses import replace
    from api_agent.workflows.packaged_provenance import build_packaged_proof
    from api_agent.workflows.provenance import verified_documents
    definition, file = admitted(tmp_path)
    nested = file.parent / 'nested'
    nested.mkdir()
    moved = nested / file.name
    moved.write_bytes(file.read_bytes())
    git(file.parent, 'add', 'nested')
    git(file.parent, 'commit', '-qm', 'synthetic nested source')
    selected = replace(definition, entry=moved, roots={'base': nested},
                       contents={moved: moved.read_bytes()})
    receipt = nested / 'workflow-source-proof.json'
    receipt.write_text(json.dumps(build_packaged_proof(selected)))
    moved.write_bytes(b'# Different staged workflow\n')
    git(file.parent, 'add', 'nested/flow.md.html')
    moved.write_bytes(file.read_bytes())
    with pytest.raises(LayoutError, match='staged changes'):
        verified_documents(selected)
    info = file.parent / 'claimed_source-1.0.dist-info'
    info.mkdir()
    (info / 'METADATA').write_text('Name: claimed-source\nVersion: 1.0\n')
    external = tmp_path / 'external-record'
    external.write_text(''.join(str(path.relative_to(file.parent)) + ',,\n'
        for path in (moved, receipt, info / 'METADATA', info / 'RECORD')))
    if record_kind == 'alias':
        (info / 'RECORD').symlink_to(external)
    else:
        (info / 'RECORD').write_bytes(external.read_bytes())
    monkeypatch.syspath_prepend(str(file.parent))
    with pytest.raises(LayoutError, match='staged changes'):
        verified_documents(selected)
