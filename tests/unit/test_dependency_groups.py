"""Standard source-only dependency groups use generic environment preparation."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from api_agent.runtime import dependencies as dep


@pytest.fixture
def context(tmp_path):
    return SimpleNamespace(base_root=tmp_path, pack_root=tmp_path, external_pack=False)


def manifest(context, text):
    (context.pack_root / 'pyproject.toml').write_text(text)


def test_source_only_group_prepares_unit_environment(context):
    manifest(context, '[dependency-groups]\ndev=["synthetic-tool>=2"]\n')
    with patch.object(dep, '_installed_requirements', return_value=()) as probe, patch.object(dep, '_relaunch', return_value=0):
        python = context.pack_root / '.venv/bin/python'
        python.parent.mkdir(parents=True)
        python.touch()
        assert dep.ensure_environment(context, [], collect=True, groups=('dev',)) == 0
    assert probe.call_args.args[1] == ('synthetic-tool>=2', 'pytest>=8,<10', 'pytest-cov>=6,<8', 'pytest-xdist>=3,<4')


def test_normalized_groups_includes_and_extras_are_additive(context):
    manifest(context, '''[project.optional-dependencies]
DEV_tools=["extra-tool>=1"]
[dependency-groups]
Dev.Tools=["first-tool", {include-group="shared_SET"}, "last-tool"]
shared-set=["included-tool>=1", "included-tool<3"]
unused=[{future-extension="ignored"}]
'''.replace('Dev.Tools=', '"Dev.Tools"='))
    assert dep.requirements(context, groups=('DEV-tools',)) == (
        'extra-tool>=1', 'first-tool', 'included-tool>=1', 'included-tool<3', 'last-tool')


@pytest.mark.parametrize(('text', 'message'), [
    ('dev=[{include-group="missing"}]', 'Unknown included'),
    ('dev=[{include-group="other"}]\nother=[{include-group="DEV"}]', 'Cyclic'),
    ('dev="invalid"', 'must be a list'),
    ('dev=[2]', 'Invalid dependency group item'),
    ('dev=[{include-group="other", extra="invalid"}]', 'Invalid dependency group item'),
    ('dev=[{include-group=2}]', 'Invalid dependency group name'),
    ('dev=["not a requirement !"]', 'PEP 508'),
    ('dev=["tool @ https://example.invalid/tool.whl"]', 'Direct URL'),
    ('dev=[]\nDEV=[]', 'Colliding dependency group'),
    ('"-invalid"=[]\ndev=[]', 'Invalid dependency group name'),
])
def test_reached_invalid_group_is_rejected(context, text, message):
    manifest(context, '[dependency-groups]\n' + text)
    with pytest.raises(ValueError, match=message):
        dep.requirements(context, groups=('dev',))


def test_groups_table_must_be_mapping(context):
    manifest(context, 'dependency-groups=[]')
    with pytest.raises(ValueError, match='must be a table'):
        dep.requirements(context, groups=('dev',))


def test_unrequested_groups_are_not_evaluated(context):
    manifest(context, '[dependency-groups]\ndev=[{future-extension="ignored"}]')
    assert dep.requirements(context) == ()


def test_optional_name_collisions_are_rejected(context):
    manifest(context, '[project.optional-dependencies]\ndev=[]\nDEV=[]')
    with pytest.raises(ValueError, match='Colliding optional'):
        dep.requirements(context, groups=('dev',))


def test_include_expansion_retains_duplicate_entries():
    assert dep._dependency_groups({'dev': ['tool', {'include-group': 'common'}, 'tool'], 'common': ['tool']}, ('dev',)) == {'dev': ['tool', 'tool', 'tool']}


def test_selected_group_resolves_local_source_extras(context):
    library = context.pack_root / 'library'
    library.mkdir()
    (library / 'pyproject.toml').write_text('''[project]
name="synthetic-library"
version="1.0"
[project.optional-dependencies]
tools=["external-tool>=2; extra == 'tools'"]
''')
    context.workflow_base_root = library
    manifest(context, '[dependency-groups]\ndev=["synthetic-library[tools]==1.0"]')
    assert dep.requirements(context, groups=('dev',)) == ('external-tool>=2',)


def test_source_only_selected_group_ignores_unrelated_future_contents(context):
    manifest(context, '''[dependency-groups]
dev=["selected-tool>=1"]
future=[{unsupported-extension="unrequested"}]
''')
    assert dep.requirements(context, groups=('dev',)) == ('selected-tool>=1',)


def test_admitted_source_only_group_uses_captured_bytes_after_verification(tmp_path, monkeypatch):
    from api_agent.runtime import pack_admission as admission
    from api_agent.runtime.resource_pack import ResourcePackError, load_launch_context
    from test_pack_composition import BASE, pack

    first = pack(tmp_path, 'first')
    second = pack(tmp_path, 'second')
    path = first / 'pyproject.toml'
    path.write_text('[dependency-groups]\ndev=["admitted-tool>=1"]\n')
    context = load_launch_context(BASE, [first, second])
    original = admission.verify_composition

    def verify_then_replace(selected, expected=None):
        original(selected, expected)
        path.write_text('[dependency-groups]\ndev=["unadmitted-tool>=2"]\n')

    monkeypatch.setattr(admission, 'verify_composition', verify_then_replace)
    wanted = dep.requirements(context, groups=('dev',))
    assert 'admitted-tool>=1' in wanted
    assert 'unadmitted-tool>=2' not in wanted
    with pytest.raises(ResourcePackError, match='changed'):
        original(context)
