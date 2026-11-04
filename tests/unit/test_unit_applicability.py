"""Platform applicability is explicit, strict and bound to every report phase."""
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from api_agent.testing import applicability as scope


def item(*markers):
    return SimpleNamespace(iter_markers=lambda name: iter(markers))


def marker(*args, **kwargs):
    return SimpleNamespace(args=args, kwargs=kwargs)


@pytest.mark.parametrize('values', [None, 'linux', {}, ['unknown'], ['linux', 'linux'], [1], [True]])
def test_invalid_platform_declarations_reject(values):
    with pytest.raises(ValueError, match='Invalid unit_platform'):
        scope.validate_platforms(values)


def test_platform_declarations_are_canonical_and_optional():
    assert scope.validate_platforms(['win32', 'linux']) == ('linux', 'win32')
    assert scope.collected_platforms(item()) == ()
    assert scope.collected_platforms(item(marker('linux'))) == ('linux',)
    assert scope.declaration((), 'darwin') is None
    assert scope.explanation(('win32', 'linux'), 'darwin') == 'Not applicable on darwin; requires linux, win32.'


@pytest.mark.parametrize('markers', [(marker(),), (marker('linux', reason='custom'),), (marker('linux'), marker('darwin'))])
def test_ambiguous_or_reason_bearing_markers_reject(markers):
    with pytest.raises(ValueError, match='one nonempty'):
        scope.collected_platforms(item(*markers))


@pytest.mark.parametrize('host', ['', None, 1])
def test_invalid_host_rejects(host):
    with pytest.raises(ValueError, match='host'):
        scope.declaration(('linux',), host)


@pytest.mark.parametrize('value', [
    1, [], {}, {'invalid': True}, {'platforms': [], 'host': 'linux'},
    {'platforms': ['invalid'], 'host': 'linux'}, {'platforms': ['linux'], 'host': ''},
    {'platforms': ('linux',), 'host': 'linux'},
    {'platforms': ['win32', 'linux'], 'host': 'linux'},
    {'platforms': ['linux'], 'host': 'linux', 'reason': 'custom'},
])
def test_malformed_report_metadata_is_explicitly_invalid(value):
    report = SimpleNamespace(user_properties=[(scope._PROPERTY, value)])
    assert scope.report_declaration(report) == {'invalid': True}
    assert not scope.reports_match((), [{'applicability': value}], 'linux')


def test_duplicate_properties_cannot_masquerade_as_unscoped_report():
    declaration = scope.declaration(('linux',), 'darwin')
    report = SimpleNamespace(user_properties=[(scope._PROPERTY, declaration), (scope._PROPERTY, declaration)])
    invalid = scope.report_declaration(report)
    assert json.loads(json.dumps(invalid)) == {'invalid': True}
    assert not scope.reports_match((), [{'applicability': invalid}], 'darwin')
    assert not scope.reports_match(('linux',), [{'applicability': invalid}], 'darwin')


def test_report_declaration_preserves_valid_metadata_and_ignores_unrelated_properties():
    declaration = scope.declaration(('linux',), 'darwin')
    assert scope.report_declaration(SimpleNamespace(user_properties=[('unrelated', 7)])) is None
    assert scope.report_declaration(SimpleNamespace(user_properties=[(scope._PROPERTY, declaration)])) == declaration
    assert scope.report_declaration(SimpleNamespace(user_properties=[(scope._PROPERTY, None)])) == {'invalid': True}


@pytest.mark.parametrize('phase', ['setup', 'call', 'teardown'])
def test_each_phase_must_match_selected_declaration(phase):
    declaration = scope.declaration(('linux',), 'darwin')
    reports = [{'phase': name, 'applicability': dict(declaration)} for name in ('setup', 'call', 'teardown')]
    assert scope.reports_match(('linux',), reports, 'darwin')
    next(report for report in reports if report['phase'] == phase)['applicability'] = None
    assert not scope.reports_match(('linux',), reports, 'darwin')
    assert scope.reports_match((), [{'phase': phase}], 'darwin')


def test_plugin_registers_validates_and_enforces_platform_only(monkeypatch):
    plugin = scope.unit_platform_plugin()
    registrations = []
    plugin.pytest_configure(SimpleNamespace(addinivalue_line=lambda *args: registrations.append(args)))
    assert registrations[0][0] == 'markers'
    unscoped, supported, unsupported = item(), item(marker('linux')), item(marker('darwin'))
    plugin.pytest_collection_modifyitems([unscoped, supported, unsupported])
    monkeypatch.setattr(scope, 'sys', SimpleNamespace(platform='linux'))
    plugin.pytest_runtest_setup(unscoped)
    plugin.pytest_runtest_setup(supported)
    with pytest.raises(pytest.skip.Exception, match='Not applicable on linux; requires darwin'):
        plugin.pytest_runtest_setup(unsupported)
    with pytest.raises(pytest.UsageError, match='nonempty'):
        plugin.pytest_collection_modifyitems([item(marker())])


@pytest.mark.parametrize('platforms', [(), ('linux',)])
def test_report_hook_adds_only_declared_platform_metadata(monkeypatch, platforms):
    plugin = scope.unit_platform_plugin()
    monkeypatch.setattr(scope, 'sys', SimpleNamespace(platform='darwin'))
    report = SimpleNamespace(user_properties=[])
    hook = plugin.pytest_runtest_makereport(SimpleNamespace(_api_agent_unit_platforms=platforms))
    next(hook)
    with pytest.raises(StopIteration):
        hook.send(SimpleNamespace(get_result=lambda: report))
    assert scope.report_declaration(report) == scope.declaration(platforms, 'darwin')


def test_discovery_import_does_not_require_pytest():
    script = '''
import builtins
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name == 'pytest': raise RuntimeError('pytest imported outside worker')
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
from api_agent.testing.applicability import validate_platforms
assert validate_platforms(('linux',)) == ('linux',)
'''
    result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
