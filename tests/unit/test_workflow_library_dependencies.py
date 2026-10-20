"""Synthetic selected-library dependency admission; no real installation."""
import json
from types import SimpleNamespace
from unittest.mock import patch
import pytest
from api_agent.runtime import dependencies as dep


def project(root, requirements, dev=None):
    root.mkdir(parents=True, exist_ok=True)
    text = '[project]\ndependencies=' + json.dumps(requirements) + '\n'
    if dev is not None:
        text += '[project.optional-dependencies]\ndev=' + json.dumps(dev) + '\n'
    (root / 'pyproject.toml').write_text(text)
    return root


@pytest.fixture
def context(tmp_path):
    return SimpleNamespace(base_root=project(tmp_path / 'runtime', ['runtime-tool>=1']),
        pack_root=project(tmp_path / 'pack', ['pack-tool>=2']), external_pack=True,
        workflow_base_root=project(tmp_path / 'library', ['native-tool>=3', 'api_agent==1'], ['library-dev>=4']))


def test_selected_source_is_fresh_and_cwd_independent(context, tmp_path, monkeypatch):
    monkeypatch.chdir(project(tmp_path / 'ambient', ['ambient-tool>=1']))
    project(tmp_path / 'unselected', ['unselected-tool>=1'])
    assert dep.requirements(context, collect=True, groups=('dev',)) == (
        'runtime-tool>=1', 'pack-tool>=2', 'native-tool>=3', 'library-dev>=4',
        'pytest>=8,<10', 'pytest-cov>=6,<8', 'pytest-xdist>=3,<4')
    project(context.workflow_base_root, ['changed-tool>=5'])
    assert dep.requirements(context) == ('runtime-tool>=1', 'pack-tool>=2', 'changed-tool>=5')
    with pytest.raises(ValueError, match='Unknown optional dependency group'):
        dep.requirements(context, groups=('dev',))


def test_alias_reads_project_once(context, tmp_path):
    alias = tmp_path / 'alias'
    alias.symlink_to(context.base_root, target_is_directory=True)
    context.workflow_base_root = alias
    original = type(alias).open
    reads = []
    def observe(path, *args, **kwargs):
        reads.append(path)
        return original(path, *args, **kwargs)
    with patch.object(type(alias), 'open', observe):
        assert dep.requirements(context) == ('runtime-tool>=1', 'pack-tool>=2')
    assert reads == [context.base_root / 'pyproject.toml', context.pack_root / 'pyproject.toml']


@pytest.mark.parametrize('selection', ['absent', 'none', 'installed', 'empty'])
def test_missing_project_does_not_search_parent(context, tmp_path, selection):
    parent = project(tmp_path / 'distribution', ['parent-tool>=1'])
    installed = parent / 'package'
    installed.mkdir()
    if selection == 'absent':
        del context.workflow_base_root
    elif selection == 'none':
        context.workflow_base_root = None
    else:
        context.workflow_base_root = installed
        if selection == 'empty':
            (installed / 'pyproject.toml').write_text('[tool.example]\nvalue=1\n')
    assert dep.requirements(context) == ('runtime-tool>=1', 'pack-tool>=2')


@pytest.mark.parametrize('invalid', ['bad requirement !!!', 'native-tool @ https://example.invalid/tool.whl', 42])
@pytest.mark.parametrize('optional', [False, True])
def test_invalid_library_requirement_precedes_install(context, invalid, optional):
    project(context.workflow_base_root, [] if optional else [invalid], [invalid] if optional else [])
    with patch.object(dep.subprocess, 'run') as install:
        with pytest.raises(ValueError):
            dep.ensure_environment(context, [], groups=('dev',))
    install.assert_not_called()
    assert not (context.pack_root / '.venv').exists()


def test_environment_installs_exact_selected_dependencies_and_rereads(context):
    environment = context.pack_root / '.venv'
    (environment / 'bin').mkdir(parents=True)
    (environment / 'bin/python').touch()
    first = ('runtime-tool>=1', 'pack-tool>=2', 'native-tool>=3')
    second = ('runtime-tool>=1', 'pack-tool>=2', 'changed-tool>=7')
    with patch.object(dep.shutil, 'which', return_value='uv'), \
         patch.object(dep, '_installed_requirements', side_effect=[first, (), second, ()]) as probe, \
         patch.object(dep.subprocess, 'run') as install, \
         patch.object(dep, 'active_environment', return_value=True):
        assert dep.ensure_environment(context, []) is None
        project(context.workflow_base_root, ['changed-tool>=7'])
        assert dep.ensure_environment(context, []) is None
    assert [call.args[0] for call in install.call_args_list] == [
        ['uv', 'pip', 'install', '--python', str(environment / 'bin/python'), *first],
        ['uv', 'pip', 'install', '--python', str(environment / 'bin/python'), *second]]
    assert [call.args[1] for call in probe.call_args_list] == [first, first, second, second]


def named_library(context, *, version='0.1.0'):
    manifest = context.workflow_base_root / 'pyproject.toml'
    manifest.write_text('[project]\nname="example-library"\nversion=' + json.dumps(version)
        + '\ndependencies=["native-tool>=3"]\n[project.optional-dependencies]\n'
        + 'tools=["extra-tool>=2; extra == \'tools\'"]\n')


def test_source_library_satisfies_pack_requirement_and_expands_extra(context):
    named_library(context)
    project(context.pack_root, ['example_library[tools]==0.1.0'])
    assert dep.requirements(context) == ('runtime-tool>=1', 'extra-tool>=2', 'native-tool>=3')


@pytest.mark.parametrize('requirement', ['example-library==2', 'example-library[missing]==0.1.0'])
def test_source_library_rejects_unsatisfied_contract(context, requirement):
    named_library(context)
    project(context.pack_root, [requirement])
    with pytest.raises(ValueError, match='Selected source workflow library'):
        dep.requirements(context)


def test_inactive_source_requirement_marker_does_not_require_version(context):
    named_library(context)
    project(context.pack_root, ['example-library==2; python_version < "1"'])
    assert dep.requirements(context) == ('runtime-tool>=1', 'native-tool>=3')


def test_installed_library_requirement_is_retained(context):
    named_library(context)
    context.workflow_library_files = frozenset({'workflow.json'})
    project(context.pack_root, ['example-library==0.1.0'])
    assert dep.requirements(context) == ('runtime-tool>=1', 'example-library==0.1.0', 'native-tool>=3')


def test_source_extra_cycle_and_inactive_children(context):
    named_library(context)
    manifest = context.workflow_base_root / 'pyproject.toml'
    manifest.write_text(manifest.read_text().replace('tools=[', 'tools=["example-library[tools]==0.1.0", "plain-tool>=1", "ignored-tool; python_version < \'1\'", '))
    project(context.pack_root, ['example-library[tools]==0.1.0'])
    assert dep.requirements(context) == ('runtime-tool>=1', 'plain-tool>=1', 'extra-tool>=2', 'native-tool>=3')


@pytest.mark.parametrize('metadata', ['name="example-library"', 'name="example-library"\ndynamic=["version"]'])
def test_source_identity_without_static_version_rejects(context, metadata):
    (context.workflow_base_root / 'pyproject.toml').write_text('[project]\n' + metadata)
    project(context.pack_root, ['example-library'])
    with pytest.raises(ValueError, match='missing version'):
        dep.requirements(context)


def test_requested_extra_uses_normalized_name_for_children_and_cycles(context):
    named_library(context)
    manifest = context.workflow_base_root / 'pyproject.toml'
    manifest.write_text(manifest.read_text().replace('tools=["extra-tool>=2; extra == \'tools\'"]',
        'speed-mode=["example-library[Speed_Mode]==0.1.0", "extra-tool>=2; extra == \'speed-mode\'"]'))
    project(context.pack_root, ['example-library[Speed_Mode]==0.1.0'])
    assert dep.requirements(context) == ('runtime-tool>=1', 'extra-tool>=2', 'native-tool>=3')


@pytest.mark.parametrize('second', ['[]', '["other-tool>=1"]'])
def test_normalized_extra_declarations_cannot_collide(context, second):
    named_library(context)
    manifest = context.workflow_base_root / 'pyproject.toml'
    manifest.write_text(manifest.read_text() + 'speed-mode=[]\nSpeed_Mode=' + second + '\n')
    project(context.pack_root, ['example-library[speed-mode]==0.1.0'])
    with pytest.raises(ValueError, match='colliding extras'):
        dep.requirements(context)
