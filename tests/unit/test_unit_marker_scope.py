"""Explicit unit-only marker exclusions keep integration cases out of unit gates."""
import pytest
from api_agent.runtime.resource_pack import load_launch_context, ResourcePackError
from api_agent.testing.collection import collect_tests
from test_resource_pack import make_pack
from test_collection import context, suite


def test_unit_marker_exclusions_are_declared_without_becoming_suite_paths(tmp_path):
    base, pack = tmp_path / 'base', tmp_path / 'pack'
    make_pack(base / 'demo')
    make_pack(pack)
    with (pack / 'pack.toml').open('a') as output:
        output.write('unit_exclude_markers = ["network"]\n')

    loaded = load_launch_context(base, pack)

    assert loaded.unit_exclude_markers == ('network',)
    assert set(loaded.tests) == {'unit', 'integration', 'e2e'}


@pytest.mark.parametrize('value', ['"network"', '[true]', '["bad-name"]', '["duplicate", "duplicate"]'])
def test_unit_marker_exclusions_are_validated(tmp_path, value):
    base, pack = tmp_path / 'base', tmp_path / 'pack'
    make_pack(base / 'demo')
    make_pack(pack)
    with (pack / 'pack.toml').open('a') as output:
        output.write('unit_exclude_markers = ' + value + '\n')

    with pytest.raises(ResourcePackError, match='unit_exclude_markers'):
        load_launch_context(base, pack)


def test_owner_marker_filter_does_not_remove_base_or_change_integration(tmp_path):
    base, pack = tmp_path / 'base', tmp_path / 'pack'
    suite(base)
    units = suite(pack)
    selected = suite(pack, 'integration')
    (units / 'test_unit.py').write_text('def test_unit():\n    pass\n')
    ctx = context(base, pack, {'unit': (units,), 'integration': (selected,)})
    ctx.unit_exclude_markers = ('mutation',)

    tests = collect_tests(ctx, 'unit')

    assert len(tests) == 3
    assert len([case for case in tests if case.repo == 'base']) == 2
    assert [case.node_id for case in tests if case.repo == 'pack'] == ['tests/unit/test_unit.py::test_unit']
    assert len(collect_tests(ctx, 'integration')) == 2
