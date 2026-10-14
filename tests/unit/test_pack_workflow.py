"""Pack workflow selection is explicit, portable and inert until requested."""
from pathlib import Path
import pytest
from api_agent.runtime.resource_pack import load_launch_context, load_workflow, ResourcePackError


def pack(tmp_path, declaration=''):
    base = tmp_path / 'base'
    selected = base / 'demo'
    selected.mkdir(parents=True)
    (selected / 'cfg.toml').write_text('version=1\n')
    (selected / 'layout.md').write_text('# Placeholder declaration\n')
    (selected / 'pack.toml').write_text('version=1\nid="demo"\nowner=".."\nconfig="cfg.toml"\n'+declaration)
    return base, selected


def test_optional_workflow_does_not_block_other_pack_operations(tmp_path):
    base, _ = pack(tmp_path)
    context = load_launch_context(base)
    assert context.layout_path is None
    with pytest.raises(ResourcePackError, match='no workflow'):
        load_workflow(context, {})


def test_declared_layout_is_pack_relative_independent_of_cwd(tmp_path, monkeypatch):
    base, selected = pack(tmp_path, '[workflow]\nversion=1\nlayout="layout.md"\n')
    monkeypatch.chdir(tmp_path)
    assert load_launch_context(base).layout_path == selected / 'layout.md'


@pytest.mark.parametrize('declaration', [
    'workflow="invalid"', 'workflow={version=2,layout="layout.md"}',
    'workflow={version=true,layout="layout.md"}', 'workflow={version=1}',
    'workflow={version=1,layout="layout.md",extra=true}',
    'workflow={version=1,layout="/tmp/layout.md"}',
    'workflow={version=1,layout="missing.md"}',
    'workflow={version=1,layout="cfg.toml"}',
])
def test_invalid_workflow_rejected_without_loading_functions(tmp_path, declaration):
    base, _ = pack(tmp_path, declaration+'\n')
    with pytest.raises(ResourcePackError, match='workflow'):
        load_launch_context(base)


def test_symlink_outside_selected_artifact_is_rejected(tmp_path):
    base, selected = pack(tmp_path, '[workflow]\nversion=1\nlayout="linked.md"\n')
    outside = tmp_path / 'outside.md'
    outside.write_text('not an artifact')
    (selected / 'linked.md').symlink_to(outside)
    with pytest.raises(ResourcePackError, match='base artifact'):
        load_launch_context(base)


def test_external_pack_can_explicitly_select_base_artifact_layout(tmp_path):
    base, selected = pack(tmp_path)
    external = tmp_path / 'external'
    external.mkdir()
    (external / 'cfg.toml').write_text('version=1\n')
    (external / 'pack.toml').write_text('version=1\nid="external"\nconfig="cfg.toml"\n[workflow]\nversion=1\nlayout="../base/demo/layout.md"\n')
    assert load_launch_context(base, external).layout_path == selected / 'layout.md'


def test_pack_workflow_joins_the_bundled_catalog():
    root = Path(__file__).resolve().parents[2]
    from api_agent.workflows.contrived import build
    workflow = build(root, llm=None, selection='gpt-example', trial='unit')
    context = load_launch_context(root)
    joined = load_workflow(context, workflow.registry, workflow.conditions,
                           config=workflow.config, services=workflow.services)
    assert joined.layout.path == workflow.layout.path
