"""Selected platform declarations survive real unit worker boundaries."""
from dataclasses import replace
import sys
from types import SimpleNamespace

import pytest

from api_agent.testing.collection import collect_tests
from api_agent.testing.runner import run_unit_tests, format_unit_summary


def repository(tmp_path, body):
    root = tmp_path / 'example'
    (root / 'tests/unit').mkdir(parents=True)
    (root / 'src').mkdir()
    (root / 'src/example.py').write_text('VALUE = 1\n')
    (root / 'tests/unit/test_example.py').write_text(body)
    return SimpleNamespace(base_root=root, owner_root=root,
                           suite_paths=lambda mode: [root / 'tests/unit'])


@pytest.mark.parametrize('jobs', [0, 2])
def test_excluded_platform_remains_visible_without_running_body(tmp_path, monkeypatch, jobs):
    from api_agent.testing import recording, storage
    def forbidden(*args, **kwargs):
        pytest.fail('Unit applicability must not enter Allure persistence')
    monkeypatch.setattr(recording, 'RunRecorder', forbidden)
    monkeypatch.setattr(storage, 'RunStorage', forbidden)
    other = 'win32' if sys.platform != 'win32' else 'linux'
    ctx = repository(tmp_path, 'import pytest\nfrom example import VALUE\n'
        'def test_current():\n    assert VALUE == 1\n'
        f'@pytest.mark.unit_platform({other!r})\n'
        'def test_other():\n    raise AssertionError("excluded body executed")\n')
    identities = collect_tests(ctx, 'unit')
    assert identities[1].platforms == (other,)
    result = run_unit_tests(ctx, identities, parallel_jobs=jobs)
    assert result.exit_code == 0
    assert [row.status for row in result.tests] == ['passed', 'not_applicable']
    assert f'Not applicable on {sys.platform}' in result.tests[1].traceback
    assert all(area.complete for area in result.coverage)
    assert '1 not applicable' in format_unit_summary(result)


@pytest.mark.parametrize('full', [False, True])
def test_only_excluded_tests_do_not_waive_coverage(tmp_path, full):
    other = 'win32' if sys.platform != 'win32' else 'linux'
    ctx = repository(tmp_path, f'import pytest\n@pytest.mark.unit_platform({other!r})\n'
        'def test_other():\n    assert False\n')
    result = run_unit_tests(ctx, collect_tests(ctx, 'unit'), require_full_coverage=full)
    assert result.tests[0].status == 'not_applicable'
    assert result.exit_code == int(full)
    assert ('Coverage FAIL' if full else 'NOT APPLICABLE') in format_unit_summary(result)


@pytest.mark.parametrize('marked', [False, True])
def test_resource_skip_is_not_platform_inapplicability(tmp_path, marked):
    marker = f'@pytest.mark.unit_platform({sys.platform!r})\n' if marked else ''
    ctx = repository(tmp_path, 'import pytest\n' + marker +
        'def test_resource():\n    pytest.skip("resource unavailable")\n')
    result = run_unit_tests(ctx, collect_tests(ctx, 'unit'), require_full_coverage=False)
    assert result.exit_code == 1 and result.tests[0].status == 'skipped'


@pytest.mark.parametrize('change', ['remove', 'add', 'different'])
def test_discovery_execution_declaration_drift_is_rejected(tmp_path, monkeypatch, change):
    other = 'win32' if sys.platform != 'win32' else 'linux'
    ctx = repository(tmp_path, f'import pytest\n@pytest.mark.unit_platform({other!r})\n'
        'def test_other():\n    assert True\n')
    identities = collect_tests(ctx, 'unit')
    if change == 'remove':
        identities = [replace(identities[0], platforms=())]
    elif change == 'add':
        (ctx.base_root / 'tests/unit/test_example.py').write_text('def test_other():\n    assert True\n')
    else:
        identities = [replace(identities[0], platforms=(sys.platform,))]
    # Hold the discovery snapshot constant, then exercise execution report validation.
    from api_agent.testing import runner
    monkeypatch.setattr(runner, 'collect_tests', lambda *args, **kwargs: identities)
    result = run_unit_tests(ctx, identities, require_full_coverage=False)
    assert result.exit_code == 1 and result.tests[0].status == 'error'
    assert 'declaration changed' in result.tests[0].traceback


def test_excluded_setup_with_failed_teardown_stays_failed(tmp_path):
    other = 'win32' if sys.platform != 'win32' else 'linux'
    ctx = repository(tmp_path, f'import pytest\n@pytest.mark.unit_platform({other!r})\n'
        'def test_other():\n    assert False\n')
    (ctx.base_root / 'tests/unit/conftest.py').write_text(
        'def pytest_runtest_teardown(item):\n    raise RuntimeError("teardown failed")\n')
    result = run_unit_tests(ctx, collect_tests(ctx, 'unit'), require_full_coverage=False)
    assert result.exit_code == 1 and result.tests[0].status == 'failed'
    assert 'teardown failed' in result.tests[0].traceback
