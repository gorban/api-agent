from pathlib import Path
from types import SimpleNamespace

import pytest

from api_agent.testing.collection import CollectionError, collect_tests, provider_integration_suites


def make_provider(root, declaration='version=1\n[tests]\nintegration=["tests/integration"]\n'):
    root.mkdir(parents=True)
    factory = root / 'factory.py'
    factory.write_text('raise RuntimeError("factory must not be imported")\n')
    (root / 'cfg').mkdir()
    (root / 'cfg/tests.toml').write_text(declaration)
    suite = root / 'tests/integration'
    suite.mkdir(parents=True)
    (suite / 'test_provider.py').write_text('def test_provider():\n    raise RuntimeError("must not execute")\n')
    return str(factory) + ':create'


def context(root):
    return SimpleNamespace(base_root=root, owner_root=root, defaults={}, suite_paths=lambda mode: ())


def test_fresh_selection_generic_nested_and_same_path_dedup(tmp_path):
    base = tmp_path / 'base'
    generic = base / 'tests/integration'
    generic.mkdir(parents=True)
    (generic / 'test_shared.py').write_text('def test_shared():\n    pass\n')
    first = make_provider(tmp_path / 'one/provider')
    second = make_provider(tmp_path / 'two/provider')
    ctx = context(base)
    tests = collect_tests(ctx, 'integration', config={'LLM_PROVIDER': first, 'UPSTREAM_API_PROVIDER': first})
    assert len(tests) == 2
    assert {test.root for test in tests} == {None, Path(first.rpartition(':')[0]).parent}
    changed = collect_tests(ctx, 'integration', config={'LLM_PROVIDER': second})
    assert tests[1].id != changed[1].id
    with pytest.raises(CollectionError, match='No e2e suites'):
        collect_tests(ctx, 'e2e', config={'LLM_PROVIDER': first})


@pytest.mark.parametrize('declaration', ['oops=[', 'version=2\n[tests]\nintegration=[]', 'version=true\n[tests]\nintegration=[]', 'version=1\n[tests]\nintegration="x"', 'version=1\n[tests]\ne2e=[]', 'version=1\nextra=1\n[tests]\nintegration=[]', 'version=1\n[tests]\nintegration=[1]', 'version=1\n[tests]\nintegration=[""]', 'version=1\n[tests]\nintegration=["/tmp"]', 'version=1\n[tests]\nintegration=["../outside"]', 'version=1\n[tests]\nintegration=["missing"]'])
def test_invalid_manifest(tmp_path, declaration):
    selected = make_provider(tmp_path / 'provider', declaration)
    with pytest.raises(CollectionError):
        provider_integration_suites(context(tmp_path), {'API_PROVIDER': selected})


def test_empty_absent_and_invalid_selection(tmp_path):
    selected = make_provider(tmp_path / 'provider', 'version=1\n[tests]\nintegration=[]')
    ctx = context(tmp_path)
    assert provider_integration_suites(ctx, {'LLM_PROVIDER': selected}) == {}
    (tmp_path / 'provider/cfg/tests.toml').unlink()
    assert provider_integration_suites(ctx, {'LLM_PROVIDER': selected}) == {}
    assert provider_integration_suites(ctx, {}) == {}
    with pytest.raises(CollectionError, match='Invalid selected provider'):
        provider_integration_suites(ctx, {'LLM_PROVIDER': 'missing.py:create'})


@pytest.mark.parametrize('ancestor', ['', 'tests'])
def test_ancestor_package_initializer_escape_is_rejected_before_pytest(tmp_path, monkeypatch, ancestor):
    selected = make_provider(tmp_path / 'provider')
    marker = tmp_path / 'executed'
    outside = tmp_path / 'outside.py'
    outside.write_text(f'from pathlib import Path\nPath({str(marker)!r}).touch()\n')
    (tmp_path / 'provider' / ancestor / '__init__.py').symlink_to(outside)
    from unittest.mock import Mock
    child = Mock(side_effect=AssertionError('pytest must not start'))
    monkeypatch.setattr('api_agent.testing.collection.subprocess.Popen', child)
    with pytest.raises(CollectionError, match='content escapes its root'):
        collect_tests(context(tmp_path), 'integration', config={'LLM_PROVIDER': selected})
    child.assert_not_called()
    assert not marker.exists()


@pytest.mark.parametrize('target', ['manifest', 'suite'])
def test_symlink_escape(tmp_path, target):
    selected = make_provider(tmp_path / 'provider')
    outside = tmp_path / 'outside'
    outside.mkdir()
    if target == 'manifest':
        source = tmp_path / 'provider/cfg/tests.toml'
        (outside / 'tests.toml').write_text(source.read_text())
        source.unlink()
        source.symlink_to(outside / 'tests.toml')
    else:
        source = tmp_path / 'provider/tests/integration'
        (source / 'test_provider.py').unlink()
        source.rmdir()
        source.symlink_to(outside, target_is_directory=True)
    with pytest.raises(CollectionError, match='root'):
        provider_integration_suites(context(tmp_path), {'UI_PROVIDER': selected})


def test_default_selection_and_resolved_fresh_override(tmp_path, monkeypatch):
    first = make_provider(tmp_path / 'one')
    second = make_provider(tmp_path / 'two')
    ctx = context(tmp_path)
    ctx.defaults = {'ACCESS_PROVIDER': first}
    (tmp_path / '.env').write_text('ACCESS_PROVIDER=unusable.py:create\n')
    monkeypatch.setenv('ACCESS_PROVIDER', 'unusable.py:create')
    assert collect_tests(ctx, 'integration')[0].root == tmp_path / 'one'
    assert collect_tests(ctx, 'integration', config={'ACCESS_PROVIDER': SimpleNamespace(value=second)})[0].root == tmp_path / 'two'


def test_unreadable_manifest(tmp_path, monkeypatch):
    selected = make_provider(tmp_path / 'provider')
    def unreadable(path, **kwargs):
        raise OSError('unreadable')
    monkeypatch.setattr(Path, 'read_text', unreadable)
    with pytest.raises(CollectionError, match='Cannot read'):
        provider_integration_suites(context(tmp_path), {'LLM_PROVIDER': selected})


def test_provider_runner_uses_canonical_root_and_rejects_stale_selection(tmp_path):
    from dataclasses import replace
    from api_agent.testing.integration import run_integration_tests
    selected = make_provider(tmp_path / 'one')
    other = make_provider(tmp_path / 'two')
    (tmp_path / 'one/tests/integration/test_provider.py').write_text('from pathlib import Path\ndef test_provider():\n    assert Path.cwd().name == "one"\n')
    ctx = context(tmp_path)
    ctx.defaults = {'LLM_PROVIDER': selected}
    tests = collect_tests(ctx, 'integration')
    result = run_integration_tests(ctx, tests, accepted=True)
    assert result.exit_code == 0
    with pytest.raises(CollectionError, match='freshly collected'):
        run_integration_tests(ctx, [replace(tests[0], root=tmp_path / 'two')], accepted=True)
    ctx.defaults = {'LLM_PROVIDER': other}
    with pytest.raises(CollectionError, match='freshly collected'):
        run_integration_tests(ctx, tests, accepted=True)


@pytest.mark.parametrize('nested', [False, True])
def test_provider_root_shared_with_base_or_pack_is_collected_once(tmp_path, nested):
    base = tmp_path / 'base'
    owner = base / 'packs/nested' if nested else base
    selected = make_provider(owner)
    if nested:
        generic = base / 'tests/integration'
        generic.mkdir(parents=True)
        (generic / 'test_generic.py').write_text('def test_generic():\n    pass\n')
    ctx = SimpleNamespace(base_root=base, owner_root=owner, defaults={},
                          suite_paths=lambda mode: (owner / 'tests/integration',))
    tests = collect_tests(ctx, 'integration', config={'LLM_PROVIDER': selected, 'API_PROVIDER': selected})
    assert len(tests) == (2 if nested else 1)
    assert all(test.root is None for test in tests)
    assert len({test.id for test in tests}) == len(tests)


@pytest.mark.parametrize('target', ['directory', 'test_file', 'suite_conftest', 'root_conftest', 'parent_conftest', 'internal_alias_escape'])
def test_descendant_symlink_escape_rejected_before_pytest_import(tmp_path, monkeypatch, target):
    selected = make_provider(tmp_path / 'provider')
    root = tmp_path / 'provider'
    suite = root / 'tests/integration'
    outside = tmp_path / 'outside'
    outside.mkdir()
    code = outside / 'test_external.py'
    code.write_text('raise RuntimeError("external code must not be imported")\n')
    if target == 'directory':
        (suite / 'escape').symlink_to(outside, target_is_directory=True)
    elif target == 'internal_alias_escape':
        internal = root / 'internal'
        internal.mkdir()
        (internal / 'escape.py').symlink_to(code)
        (suite / 'alias').symlink_to(internal, target_is_directory=True)
    else:
        source = {'test_file': suite / 'test_provider.py',
                  'suite_conftest': suite / 'conftest.py',
                  'root_conftest': root / 'conftest.py',
                  'parent_conftest': root / 'tests/conftest.py'}[target]
        source.unlink(missing_ok=True)
        source.symlink_to(code)
    def cannot_collect(*args, **kwargs):
        pytest.fail('pytest must not start before rejecting an escaping link')
    monkeypatch.setattr('api_agent.testing.collection._run_collection', cannot_collect)
    with pytest.raises(CollectionError, match='content escapes its root'):
        collect_tests(context(tmp_path), 'integration', config={'LLM_PROVIDER': selected})


def test_internal_suite_aliases_and_cycles_are_bounded_and_deduplicated(tmp_path):
    selected = make_provider(tmp_path / 'provider')
    root = tmp_path / 'provider'
    suite = root / 'tests/integration'
    (root / 'alias').symlink_to(suite, target_is_directory=True)
    (root / 'cfg/tests.toml').write_text('version=1\n[tests]\nintegration=["tests/integration", "alias"]\n')
    assert len(collect_tests(context(tmp_path), 'integration', config={'LLM_PROVIDER': selected})) == 1
    (suite / 'cycle').symlink_to(suite, target_is_directory=True)
    # Validation itself must terminate even when pytest would recursively follow the link.
    assert provider_integration_suites(context(tmp_path), {'LLM_PROVIDER': selected})[root] == [suite, suite]


def test_unreadable_suite_inspection_is_actionable(tmp_path, monkeypatch):
    selected = make_provider(tmp_path / 'provider')
    def unreadable(path):
        raise OSError('private filesystem detail')
    monkeypatch.setattr(Path, 'iterdir', unreadable)
    with pytest.raises(CollectionError, match='Cannot inspect provider integration suite'):
        provider_integration_suites(context(tmp_path), {'LLM_PROVIDER': selected})


def test_nested_provider_overlap_and_file_alias_execute_each_physical_test_once(tmp_path):
    from api_agent.testing.integration import run_integration_tests
    base = tmp_path / 'base'
    root = base / 'tests/integration/provider'
    selected = make_provider(root)
    suite = root / 'tests/integration'
    marker = tmp_path / 'executed.txt'
    code = ('import pytest\nfrom pathlib import Path\n'
            '@pytest.mark.parametrize("value", [1, 2])\n'
            'def test_provider(value):\n'
            f'    with Path({str(marker)!r}).open("a") as stream:\n'
            '        stream.write(str(value) + "\\n")\n')
    (suite / 'test_provider.py').write_text(code)
    (suite / 'test_alias.py').symlink_to(suite / 'test_provider.py')
    ctx = context(base)
    tests = collect_tests(ctx, 'integration', config={'LLM_PROVIDER': selected})
    assert len(tests) == 2
    assert {test.repo for test in tests} == {'base'}
    assert {test.node_id.rpartition('::')[2] for test in tests} == {'test_provider[1]', 'test_provider[2]'}
    result = run_integration_tests(ctx, tests, accepted=True, config={'LLM_PROVIDER': selected})
    assert result.exit_code == 0
    assert sorted(marker.read_text().splitlines()) == ['1', '2']
