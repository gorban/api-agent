"""Child coverage is opt-in, owner-scoped and derived from admitted bytes."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from api_agent.runtime.resource_pack import ResourcePackError, load_launch_context
from api_agent.testing.coverage_config import owner_coverage_patches
from test_pack_composition import BASE, pack


@pytest.mark.parametrize('setting, expected', [
    ('', ()), ('[tool.coverage.run]\nbranch=true', ()), ('[tool.coverage.run]\npatch=[]', ()),
    ('[tool.coverage.run]\npatch=["subprocess", "subprocess"]', ('subprocess',)),
])
def test_unadmitted_owner_reads_standard_opt_in(tmp_path, setting, expected):
    (tmp_path / 'pyproject.toml').write_text(setting)
    assert owner_coverage_patches(SimpleNamespace(), tmp_path) == expected


def test_missing_legacy_project_defaults_off(tmp_path):
    assert owner_coverage_patches(SimpleNamespace(), tmp_path) == ()


@pytest.mark.parametrize('setting', [
    'tool=1', '[tool]\ncoverage=[]', '[tool.coverage]\nrun="bad"',
    '[tool.coverage.run]\npatch="subprocess"',
    '[tool.coverage.run]\npatch=["execv"]',
    '[tool.coverage.run]\npatch=[true]',
    '[tool.coverage.run]\npatch={subprocess=true}',
])
def test_malformed_patch_declarations_fail_loudly(tmp_path, setting):
    (tmp_path / 'pyproject.toml').write_text(setting)
    with pytest.raises(ValueError, match='Unit coverage'):
        owner_coverage_patches(SimpleNamespace(), tmp_path)


def test_admitted_owner_isolation_and_captured_bytes(tmp_path, monkeypatch):
    first, second = pack(tmp_path, 'first'), pack(tmp_path, 'second')
    (first / 'pyproject.toml').write_text('[tool.coverage.run]\npatch=["subprocess"]')
    ctx = load_launch_context(BASE, [first, second])
    def no_reread(path):
        raise AssertionError('Coverage settings must use captured declarations')
    monkeypatch.setattr(Path, 'read_bytes', no_reread)
    assert owner_coverage_patches(ctx, first) == ('subprocess',)
    assert owner_coverage_patches(ctx, second) == ()
    with pytest.raises(ResourcePackError, match='an admitted'):
        owner_coverage_patches(ctx, tmp_path / 'not-admitted')


def test_admitted_freshness_precedes_coverage_settings(tmp_path):
    selected = pack(tmp_path, 'selected')
    project = selected / 'pyproject.toml'
    project.write_text('[tool.coverage.run]\npatch=["subprocess"]')
    ctx = load_launch_context(BASE, [selected, pack(tmp_path, "other")])
    project.write_text('[tool.coverage.run]\npatch=[]')
    with pytest.raises(ResourcePackError, match='changed'):
        owner_coverage_patches(ctx, selected)


def test_different_pack_root_uses_captured_owner_contribution(tmp_path):
    selected = pack(tmp_path, 'selected', manifest='owner="../owner"\n')
    owner = tmp_path / 'owner'
    owner.mkdir()
    (selected / 'pyproject.toml').write_text('[tool.coverage.run]\npatch=["subprocess"]')
    ctx = load_launch_context(BASE, [selected, pack(tmp_path, "other")])
    assert owner_coverage_patches(ctx, owner) == ('subprocess',)


def test_exact_owner_project_precedes_mapped_contributions(tmp_path):
    selected = pack(tmp_path, 'selected', manifest='owner="../owner"\n')
    owner = pack(tmp_path, 'owner')
    (selected / 'pyproject.toml').write_text('[tool.coverage.run]\npatch=["subprocess"]')
    (owner / 'pyproject.toml').write_text('[tool.coverage.run]\npatch=[]')
    ctx = load_launch_context(BASE, [selected, owner])
    assert owner_coverage_patches(ctx, owner) == ()


@pytest.mark.parametrize('first_policy, second_policy, expected', [
    (None, None, ()), (None, [], ()), (None, ['subprocess'], ('subprocess',)),
    (['subprocess'], ['subprocess'], ('subprocess',)), ([], [], ()),
])
def test_shared_owner_coalesces_only_explicit_policies(tmp_path, first_policy, second_policy, expected):
    import json
    owner = tmp_path / 'owner'
    owner.mkdir()
    roots = [pack(tmp_path, name, manifest='owner="../owner"\n') for name in ('first', 'second')]
    for root, policy in zip(roots, (first_policy, second_policy)):
        if policy is not None:
            (root / 'pyproject.toml').write_text('[tool.coverage.run]\npatch=' + json.dumps(policy))
    ctx = load_launch_context(BASE, roots)
    assert owner_coverage_patches(ctx, owner) == expected


def test_shared_owner_rejects_conflicting_explicit_policies(tmp_path):
    owner = tmp_path / 'owner'
    owner.mkdir()
    first = pack(tmp_path, 'first', manifest='owner="../owner"\n')
    second = pack(tmp_path, 'second', manifest='owner="../owner"\n')
    (first / 'pyproject.toml').write_text('[tool.coverage.run]\npatch=[]')
    (second / 'pyproject.toml').write_text('[tool.coverage.run]\npatch=["subprocess"]')
    ctx = load_launch_context(BASE, [first, second])
    with pytest.raises(ResourcePackError, match='conflicting'):
        owner_coverage_patches(ctx, owner)


def test_actual_shared_owner_composition_collects_and_passes_normal_coverage(tmp_path, monkeypatch):
    from api_agent.testing.collection import collect_tests
    from api_agent.testing.runner import run_unit_tests
    base = tmp_path / 'base'
    (base / 'tests/unit').mkdir(parents=True)
    (base / 'tests/unit/test_base.py').write_text('def test_base():\n    assert True\n')
    owner = tmp_path / 'owner'
    (owner / 'tests/unit').mkdir(parents=True)
    (owner / 'src').mkdir()
    (owner / 'src/shared_value.py').write_text('VALUE=7\n')
    (owner / 'tests/unit/test_owner.py').write_text('from shared_value import VALUE\ndef test_owner():\n    assert VALUE == 7\n')
    roots = [pack(tmp_path, name, manifest='owner="../owner"\n[tests]\nunit=["tests/unit"]\n')
             for name in ('first', 'second')]
    (roots[0] / 'pyproject.toml').write_text('[tool.coverage.run]\nbranch=true')
    monkeypatch.setenv('PYTHONPATH', str(BASE / 'src'))
    ctx = load_launch_context(base, roots)
    identities = collect_tests(ctx, 'unit')
    assert len(identities) == 2
    result = run_unit_tests(ctx, identities)
    assert result.exit_code == 0, result.output
    assert all(test.status == 'passed' for test in result.tests)
    assert result.coverage and all(area.complete for area in result.coverage)


@pytest.mark.parametrize('owner_project, expected', [
    (None, ('subprocess',)), ('[tool.coverage.run]\nbranch=true', ()),
])
def test_exact_owner_project_priority_requires_a_captured_project(tmp_path, owner_project, expected):
    owner = pack(tmp_path, 'owner')
    selected = pack(tmp_path, 'selected', manifest='owner="../owner"\n')
    (selected / 'pyproject.toml').write_text('[tool.coverage.run]\npatch=["subprocess"]')
    if owner_project is not None:
        (owner / 'pyproject.toml').write_text(owner_project)
    ctx = load_launch_context(BASE, [owner, selected])
    assert owner_coverage_patches(ctx, owner) == expected


def test_new_exact_owner_project_is_not_read_after_admission(tmp_path):
    owner = pack(tmp_path, 'owner')
    selected = pack(tmp_path, 'selected', manifest='owner="../owner"\n')
    (selected / 'pyproject.toml').write_text('[tool.coverage.run]\npatch=["subprocess"]')
    ctx = load_launch_context(BASE, [owner, selected])
    (owner / 'pyproject.toml').write_text('[tool.coverage.run]\npatch=[]')
    with pytest.raises(ResourcePackError, match='appeared'):
        owner_coverage_patches(ctx, owner)
