"""Pack workflow selection is explicit, portable and inert until requested."""
from pathlib import Path
import shutil

import pytest

from api_agent.runtime.resource_pack import (
    ResourcePackError, load_launch_context, load_selected_definition, load_workflow,
)
from test_workflow_definition import make_graph


def pack(tmp_path, declaration=''):
    base = tmp_path / 'base'
    selected = base / 'demo'
    selected.mkdir(parents=True)
    (selected / 'cfg.toml').write_text('version=1\n')
    (base / 'flow.md.html').write_text('# Workflow v2\n')
    (selected / 'flow.md.html').write_text('# Workflow v2\n')
    (selected / 'pack.toml').write_text('version=1\nid="demo"\nowner=".."\nconfig="cfg.toml"\n' + declaration)
    return base, selected


def test_omitted_workflow_inherits_base_application_entry(tmp_path):
    base, _ = pack(tmp_path)
    context = load_launch_context(base)
    assert context.workflow_entry is None
    assert context.workflow_replacements == ()
    assert (context.base_root / 'flow.md.html').is_file()


def test_missing_inherited_application_entry_fails_before_binding(tmp_path):
    base, _ = pack(tmp_path)
    (base / 'flow.md.html').unlink()
    context = load_launch_context(base)

    with pytest.raises(ResourcePackError, match='bundled application workflow entry is unavailable'):
        load_selected_definition(context)


def test_declared_root_is_explicit_and_independent_of_cwd(tmp_path, monkeypatch):
    base, selected = pack(tmp_path, '[workflow]\nversion=2\nroot="pack"\nentry="flow.md.html"\n')
    monkeypatch.chdir(tmp_path)
    assert load_launch_context(base).workflow_entry == selected / 'flow.md.html'


@pytest.mark.parametrize('declaration', [
    'workflow="invalid"', 'workflow={version=1,root="pack",entry="flow.md.html"}',
    'workflow={version=true,root="pack",entry="flow.md.html"}',
    'workflow={version=2,entry="flow.md.html"}',
    'workflow={version=2,root="pack",entry="flow.md.html",extra=true}',
    'workflow={version=2,root="pack",entry="/tmp/flow.md.html"}',
    'workflow={version=2,root="pack",entry="missing.md.html"}',
    'workflow={version=2,root="pack",entry="cfg.toml"}',
    'workflow={version=2,root="unknown",entry="flow.md.html"}',
    'workflow={version=2,root=[],entry="flow.md.html"}',
    'workflow={version=2,root=1,entry="flow.md.html"}',
    'workflow={version=2,root="pack",entry="../flow.md.html"}',
    'workflow={version=2,root="pack",entry="flow.md.html",replacements="bad"}',
    'workflow={version=2,root="pack",entry="flow.md.html",replacements=[{target_id="x",entry="flow.md.html"}]}',
    'workflow={version=2,root="pack",entry="flow.md.html",replacements=[{target_id="x",entry="flow.md.html",root_id=[]}]}',
    'workflow={version=2,root="pack",entry="flow.md.html",replacements=[{target_id="x",entry="flow.md.html",root_id=1}]}',
    'workflow={version=2,root="pack",entry="flow.md.html",replacements=[{target_id="x",entry="flow.md.html",root_id="pack"},{target_id="x",entry="flow.md.html",root_id="base"}]}',
])
def test_invalid_workflow_rejected_before_loading_functions(tmp_path, declaration):
    base, _ = pack(tmp_path, declaration + '\n')
    with pytest.raises(ResourcePackError, match='workflow'):
        load_launch_context(base)


def test_symlink_outside_declared_root_is_rejected(tmp_path):
    base, selected = pack(tmp_path, '[workflow]\nversion=2\nroot="pack"\nentry="linked.md.html"\n')
    outside = tmp_path / 'outside.md.html'
    outside.write_text('not an artifact')
    (selected / 'linked.md.html').symlink_to(outside)
    with pytest.raises(ResourcePackError, match='declared root'):
        load_launch_context(base)


def test_external_pack_can_explicitly_select_base_graph(tmp_path):
    base, _ = pack(tmp_path)
    external = tmp_path / 'external'
    external.mkdir()
    (external / 'cfg.toml').write_text('version=1\n')
    (external / 'pack.toml').write_text('version=1\nid="external"\nconfig="cfg.toml"\n'
                                        '[workflow]\nversion=2\nroot="base"\nentry="flow.md.html"\n')
    assert load_launch_context(base, external).workflow_entry == base / 'flow.md.html'


def test_replacement_paths_are_root_scoped_and_ordered(tmp_path):
    base, selected = pack(tmp_path, '[workflow]\nversion=2\nroot="base"\nentry="flow.md.html"\n'
                               'replacements=[{target_id="first",entry="flow.md.html",root_id="pack"},'
                               '{target_id="second",entry="flow.md.html",root_id="base"}]\n')
    assert load_launch_context(base).workflow_replacements == (
        ('first', Path('flow.md.html'), 'pack'),
        ('second', Path('flow.md.html'), 'base'),
    )


def test_manifest_replacement_reaches_selected_definition(tmp_path):
    base = tmp_path / 'base'
    base.mkdir()
    make_graph(base)
    pack_root = tmp_path / 'pack'
    shutil.copytree(base, pack_root)
    alternate = pack_root / 'workflows/alternate.md.html'
    alternate.write_text((pack_root / 'workflows/inspect.md.html').read_text())
    (pack_root / 'cfg.toml').write_text('version=1\n')
    (pack_root / 'pack.toml').write_text(
        'version=1\nid="selected"\nconfig="cfg.toml"\n'
        '[workflow]\nversion=2\nroot="base"\nentry="flow.md.html"\n'
        'replacements=[{target_id="inspect",entry="workflows/alternate.md.html",root_id="pack"}]\n')
    context = load_launch_context(base, pack_root)

    definition = load_selected_definition(context)
    from api_agent.workflows.definition import load_definition
    original = load_definition(base / 'flow.md.html', base_root=base, pack_root=pack_root)

    assert definition.layout.steps[0].source == pack_root / 'native.py'
    assert definition.definition_id != original.definition_id
    assert definition.replacements[0].entry == Path('workflows/alternate.md.html')
    assert any(resource.logical_path.endswith('workflows/alternate.md.html')
               for resource in definition.resources)


def test_pack_graph_identity_is_stable_when_pack_moves_beside_base(tmp_path):
    base = tmp_path / 'base'
    base.mkdir()
    nested = base / 'selected'
    nested.mkdir()
    make_graph(nested)
    (nested / 'cfg.toml').write_text('version=1\n')
    (nested / 'pack.toml').write_text(
        'version=1\nid="selected"\nconfig="cfg.toml"\n'
        '[workflow]\nversion=2\nroot="pack"\nentry="flow.md.html"\n')
    sibling = tmp_path / 'selected'
    shutil.copytree(nested, sibling)

    nested_definition = load_selected_definition(load_launch_context(base, nested))
    sibling_definition = load_selected_definition(load_launch_context(base, sibling))

    assert nested_definition.definition_id == sibling_definition.definition_id
    assert nested_definition.graph_id == sibling_definition.graph_id
    assert nested_definition.entry != sibling_definition.entry


def test_supplied_definition_must_keep_declared_entry_owner(tmp_path):
    base = tmp_path / 'base'
    base.mkdir()
    nested = base / 'selected'
    nested.mkdir()
    make_graph(nested)
    (nested / 'cfg.toml').write_text('version=1\n')
    (nested / 'pack.toml').write_text(
        'version=1\nid="selected"\nconfig="cfg.toml"\n'
        '[workflow]\nversion=2\nroot="pack"\nentry="flow.md.html"\n')
    context = load_launch_context(base, nested)
    from api_agent.workflows.definition import load_definition
    wrong_owner = load_definition(nested / 'flow.md.html', base_root=base,
                                  pack_root=nested, entry_root='base')

    with pytest.raises(ResourcePackError, match='does not match selected'):
        load_workflow(context, {}, definition=wrong_owner)


def test_cli_explicit_entry_root_preserves_pack_owner(tmp_path, capsys):
    base = tmp_path / 'base'
    base.mkdir()
    pack_root = base / 'selected'
    pack_root.mkdir()
    entry = make_graph(pack_root)
    (pack_root / 'cfg.toml').write_text('version=1\n')
    (pack_root / 'pack.toml').write_text(
        'version=1\nid="selected"\nconfig="cfg.toml"\n'
        '[workflow]\nversion=2\nroot="pack"\nentry="flow.md.html"\n')
    from api_agent.workflows.__main__ import main

    assert main(['validate', '--entry', str(entry), '--base-root', str(base),
                 '--pack-root', str(pack_root), '--entry-root', 'pack']) == 0

    selected = load_selected_definition(load_launch_context(base, pack_root))
    assert selected.definition_id in capsys.readouterr().out


def test_cli_selected_pack_applies_ordered_manifest_replacement(tmp_path, capsys, monkeypatch):
    base = tmp_path / 'base'
    base.mkdir()
    make_graph(base)
    pack_root = tmp_path / 'pack'
    shutil.copytree(base, pack_root)
    (pack_root / 'workflows/alternate.md.html').write_text(
        (pack_root / 'workflows/inspect.md.html').read_text().replace(
            'Hello.', 'Selected pack prompt.'))
    (pack_root / 'cfg.toml').write_text('version=1\n')
    (pack_root / 'pack.toml').write_text(
        'version=1\nid="selected"\nconfig="cfg.toml"\n'
        '[workflow]\nversion=2\nroot="base"\nentry="flow.md.html"\n'
        'replacements=[{target_id="inspect",entry="workflows/alternate.md.html",root_id="pack"}]\n')
    context = load_launch_context(base, pack_root)
    selected = load_selected_definition(context)
    from api_agent.workflows.definition import load_definition
    original = load_definition(base / 'flow.md.html', base_root=base, pack_root=pack_root)
    assert b'Selected pack prompt.\n' in [resource.content_bytes for resource in selected.resources
                                          if resource.kind == 'prompt']
    assert b'Hello.\n' in [resource.content_bytes for resource in original.resources
                           if resource.kind == 'prompt']
    from api_agent.workflows.__main__ import main
    from api_agent.runtime import resource_pack
    observed = []
    original = resource_pack.load_selected_definition

    def record(actual_context):
        observed.append(actual_context.workflow_replacements)
        return original(actual_context)

    monkeypatch.setattr(resource_pack, 'load_selected_definition', record)
    monkeypatch.chdir(tmp_path)

    assert main(['validate', '--base-root', str(base), '--resource-pack-dir', str(pack_root)]) == 0
    assert selected.definition_id in capsys.readouterr().out
    assert main(['diagram', '--base-root', str(base), '--resource-pack-dir', str(pack_root)]) == 0
    assert 'flowchart TD' in capsys.readouterr().out
    assert observed == [context.workflow_replacements, context.workflow_replacements]


def test_selected_workflow_uses_bundled_application_definition():
    root = Path(__file__).resolve().parents[2]
    context = load_launch_context(root)
    definition = load_selected_definition(context)
    from api_agent.workflows.application import build
    joined = build(root, llm=None, selection='sample')
    assert definition.entry == root / 'flow.md.html'
    assert definition.graph_id
    assert joined.definition.definition_id == definition.definition_id
    assert joined.layout.steps[0].id == 'renderer'


def test_load_workflow_accepts_catalog_bound_to_exact_selected_definition():
    root = Path(__file__).resolve().parents[2]
    context = load_launch_context(root)
    definition = load_selected_definition(context)
    source = next(step.source for step in definition.layout.steps if step.id == 'renderer')
    from api_agent.workflows.definition import load_native_catalog
    from api_agent.workflows.execution import NodeFunction
    catalog = load_native_catalog(definition, source)

    joined = load_workflow(context, {'render': NodeFunction(catalog['render'], 'llm')},
                           definition=definition, services={'llm': None, 'selection': 'sample'})

    assert joined.definition is definition


def test_load_workflow_implicitly_loads_matching_immutable_definition():
    root = Path(__file__).resolve().parents[2]
    context = load_launch_context(root)
    admitted = load_selected_definition(context)
    source = next(step.source for step in admitted.layout.steps if step.id == 'renderer')
    from api_agent.workflows.definition import load_native_catalog
    from api_agent.workflows.execution import NodeFunction
    catalog = load_native_catalog(admitted, source)

    joined = load_workflow(context, {'render': NodeFunction(catalog['render'], 'llm')},
                           services={'llm': None, 'selection': 'sample'})

    assert joined.definition is not admitted
    assert joined.definition.definition_id == admitted.definition_id
    assert joined.layout.steps[0].source == source


def test_load_workflow_rejects_definition_from_other_selected_root(tmp_path):
    root = Path(__file__).resolve().parents[2]
    context = load_launch_context(root)
    from api_agent.workflows.definition import load_definition
    foreign = load_definition(root / 'flow.md.html', base_root=root, pack_root=tmp_path)

    with pytest.raises(ResourcePackError, match='does not match selected'):
        load_workflow(context, {}, definition=foreign)
