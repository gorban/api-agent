"""Worker exception evidence survives the real report and recorder path."""
import json
from types import SimpleNamespace

import pytest

from api_agent.config.resolve import ResolvedValue
from api_agent.testing.collection import collect_tests
from api_agent.testing.integration import run_integration_tests
from api_agent.testing.recording import RunIdentity, RunRecorder
from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary


@pytest.mark.parametrize('allow_content', [False, True])
def test_worker_nested_command_evidence_respects_content_policy(tmp_path, allow_content):
    suite = tmp_path / 'tests/integration'
    suite.mkdir(parents=True)
    (suite / 'test_failure.py').write_text('''\
from api_agent.runtime.prerequisites import PrerequisiteError
from api_agent.testing.authority_scope import current_scope

def test_failure():
    boundary = current_scope().authorities()[0]
    owner = boundary.credential_owner()
    secret = 'retired-' + 'credential-sentinel'
    owner.register(secret)
    owner.close()
    cause = PrerequisiteError("command failed")
    cause.local_details = "Command: synthetic\\nExit code: 7\\nstderr: network-diagnostic-sentinel " + secret
    raise RuntimeError("worker failed") from cause
''')
    ctx = SimpleNamespace(base_root=tmp_path, owner_root=tmp_path, suite_paths=lambda mode: (suite,))
    tests = collect_tests(ctx, 'integration')
    config = {'LOG_CONTENT': allow_content, 'TRACELOOP_TRACE_CONTENT': not allow_content}
    fields = {name: ResolvedValue(value, 'environment', True) for name, value in config.items()}

    run = run_integration_tests(ctx, tests, accepted=True, config=config, config_fields=fields)
    recorder = RunRecorder(tmp_path / 'report', RunIdentity.create(tests[0].repo, 'integration'),
                           evidence_boundary=EmissionBoundary(ContentPolicy(True)),
                           content_policy=ContentPolicy(allow_content))
    filename = recorder.record_test(run.tests[0])
    result = json.loads((recorder.directory / filename).read_text())
    recorded = json.dumps(result)

    assert run.exit_code == 1
    assert ('network-diagnostic-sentinel' in recorded) is allow_content
    assert 'retired-credential-sentinel' not in recorded
    if allow_content:
        assert 'Exit code: 7' in recorded


def test_worker_details_use_retired_credentials_and_skip_unavailable_authority():
    from api_agent.testing.authority_scope import authority_scope
    from api_agent.testing.workflow_capture import (WORKER_DETAILS_UNAVAILABLE, worker_capture,
                                                    worker_exception_details)

    values = {'LOG_CONTENT': True}
    snapshot = {'config': values, 'capture_sources': {
        'LOG_CONTENT': {'value': True, 'source': 'environment', 'explicit': True}}}
    cause = RuntimeError('inner')
    cause.local_details = 'stderr: synthetic-private-credential'
    error = RuntimeError('outer')
    error.__cause__ = cause
    assert worker_exception_details(error) == ''
    with authority_scope(('workflow',)) as scope:
        with worker_capture(snapshot, scope, ('workflow',)):
            scope.retain_diagnostic_history()
            credential = scope.authorities()[0].credential_owner()
            credential.register('synthetic-private-credential')
            credential.close()
            details = worker_exception_details(error)
            assert 'stderr:' in details
            assert 'synthetic-private-credential' not in details
    with authority_scope(('workflow', 'ui')) as scope:
        with worker_capture(snapshot, scope, ('workflow', 'ui')):
            assert worker_exception_details(error) == WORKER_DETAILS_UNAVAILABLE


def test_worker_details_are_withheld_without_content_permission():
    from api_agent.testing.authority_scope import authority_scope
    from api_agent.testing.workflow_capture import worker_capture, worker_exception_details

    snapshot = {'config': {'LOG_CONTENT': False}, 'capture_sources': {
        'LOG_CONTENT': {'value': False, 'source': 'environment', 'explicit': True}}}
    error = RuntimeError('failure')
    error.local_details = 'private detail'
    with authority_scope(('workflow',)) as scope:
        with worker_capture(snapshot, scope, ('workflow',)):
            assert worker_exception_details(error) == ''


def test_diagnostic_history_requires_a_lease_and_retention():
    from api_agent.providers.contracts import ContractError
    from api_agent.testing.authority_scope import TestAuthorityScope

    scope = TestAuthorityScope(('workflow',))
    scope.join('workflow', EmissionBoundary(ContentPolicy(True)))
    with pytest.raises(ContractError):
        scope.diagnostic_secrets()
    lease = scope.freeze(credential_only=True)
    try:
        with pytest.raises(ContractError):
            scope.diagnostic_secrets()
    finally:
        lease.release()
        scope.close()


def test_details_only_preserves_nested_and_secondary_evidence_without_trace():
    from api_agent.testing.diagnostics import format_exception

    nested = RuntimeError('nested')
    nested.local_details = 'nested evidence'
    secondary = ValueError('secondary')
    secondary.local_details = 'secondary evidence'
    error = RuntimeError('outer')
    error.__cause__ = nested
    error.diagnostic_errors = (secondary,)
    details = format_exception(error, details_only=True)
    assert 'nested evidence' in details and 'secondary evidence' in details
    assert 'RuntimeError' not in details and 'ValueError' not in details


def test_upgraded_then_retired_credential_remains_in_failure_redaction_history():
    import asyncio
    from api_agent.testing.authority_scope import authority_scope
    from api_agent.testing.workflow_capture import worker_capture, worker_exception_details

    snapshot = {'config': {'LOG_CONTENT': True}, 'capture_sources': {
        'LOG_CONTENT': {'value': True, 'source': 'environment', 'explicit': True}}}
    secret = 'upgraded-retired-credential'
    error = RuntimeError('failure')
    error.local_details = 'stderr: ' + secret
    with authority_scope(('workflow',)) as scope:
        with worker_capture(snapshot, scope, ('workflow',)):
            boundary = scope.authorities()[0]
            lease = scope.freeze(credential_only=True)
            asyncio.run(lease.aupgrade(boundary, (secret,)))
            assert secret in scope.diagnostic_secrets()
            owner = boundary.credential_owner()
            owner.register(secret)
            owner.close()
            lease.release()
            assert secret not in boundary._secrets
            assert secret not in worker_exception_details(error)


@pytest.mark.parametrize('allow_content', [False, True])
def test_teardown_explains_unavailable_evidence_without_private_details(tmp_path, allow_content):
    from api_agent.testing.workflow_capture import WORKER_DETAILS_UNAVAILABLE

    suite = tmp_path / 'tests/integration'
    suite.mkdir(parents=True)
    (suite / 'test_teardown.py').write_text('''\
import pytest

@pytest.fixture
def resource():
    yield
    error = RuntimeError('teardown failed')
    error.local_details = 'private-teardown-detail'
    raise error

def test_failure(resource):
    pass
''')
    ctx = SimpleNamespace(base_root=tmp_path, owner_root=tmp_path, suite_paths=lambda mode: (suite,))
    tests = collect_tests(ctx, 'integration')
    config = {'LOG_CONTENT': allow_content}
    fields = {'LOG_CONTENT': ResolvedValue(allow_content, 'environment', True)}
    run = run_integration_tests(ctx, tests, accepted=True, config=config, config_fields=fields)
    assert run.exit_code == 1
    assert (WORKER_DETAILS_UNAVAILABLE in run.tests[0].traceback) is allow_content
    assert 'private-teardown-detail' not in run.tests[0].traceback


def test_cancelled_upgrade_does_not_enter_redaction_history():
    import asyncio
    from api_agent.testing.authority_scope import TestAuthorityScope

    async def scenario():
        boundary = EmissionBoundary(ContentPolicy(True))
        first = TestAuthorityScope(('workflow',))
        second = TestAuthorityScope(('workflow',))
        first.retain_diagnostic_history()
        first.join('workflow', boundary)
        second.join('workflow', boundary)
        lease = first.freeze(credential_only=True)
        blocker = second.freeze(credential_only=True)
        try:
            task = asyncio.create_task(lease.aupgrade(boundary, ('unadmitted-secret',)))
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert 'unadmitted-secret' not in first.diagnostic_secrets()
        finally:
            blocker.release()
            lease.release()
            first.close()
            second.close()
    asyncio.run(scenario())


def test_default_managed_tool_worker_reports_command_details(tmp_path):
    suite = tmp_path / 'tests/integration'
    suite.mkdir(parents=True)
    (suite / 'test_tool.py').write_text('''\
def test_tool():
    error = RuntimeError('tool execution failed')
    error.local_details = 'Command: jq\\nExit code: 1\\nstderr: pure-tool-network-detail'
    raise error
''')
    ctx = SimpleNamespace(base_root=tmp_path, owner_root=tmp_path, suite_paths=lambda mode: (suite,))
    tests = collect_tests(ctx, 'integration')
    config = {'LOG_CONTENT': True}
    fields = {'LOG_CONTENT': ResolvedValue(True, 'environment', True)}
    run = run_integration_tests(ctx, tests, accepted=True, config=config, config_fields=fields)
    recorder = RunRecorder(tmp_path / 'report', RunIdentity.create(tests[0].repo, 'integration'),
                           evidence_boundary=EmissionBoundary(ContentPolicy(True)),
                           content_policy=ContentPolicy(True))
    filename = recorder.record_test(run.tests[0])
    recorded = json.loads((recorder.directory / filename).read_text())
    assert run.exit_code == 1
    assert 'pure-tool-network-detail' in recorded['statusDetails']['trace']


def _dispatch_report(item, report, error):
    from api_agent.testing.provider_setup import pytest_runtest_makereport

    call = SimpleNamespace(excinfo=SimpleNamespace(value=error) if error is not None else None)
    hook = pytest_runtest_makereport(item, call)
    next(hook)
    with pytest.raises(StopIteration) as finished:
        hook.send(report)
    assert finished.value.value is report


@pytest.mark.parametrize('allowed', [False, True])
def test_report_hook_projects_unavailable_worker_evidence_by_content_permission(monkeypatch, allowed):
    from api_agent.testing import provider_setup
    from api_agent.testing.workflow_capture import WORKER_DETAILS_UNAVAILABLE

    snapshot = {'config': {'LOG_CONTENT': allowed}, 'capture_sources': {
        'LOG_CONTENT': {'value': allowed, 'source': 'environment', 'explicit': True}}}
    monkeypatch.setattr(provider_setup, '_test_snapshot', lambda item: snapshot)
    error = RuntimeError('failure')
    error.local_details = 'untrusted-private-evidence'
    report = SimpleNamespace(when='call', passed=False, failed=True, longrepr='pytest failure')
    item = SimpleNamespace()

    _dispatch_report(item, report, error)

    assert item._api_agent_ui_outcome == 'failure'
    assert (WORKER_DETAILS_UNAVAILABLE in report.longrepr) is allowed
    assert 'untrusted-private-evidence' not in report.longrepr


def test_report_hook_appends_available_safe_evidence(monkeypatch):
    from api_agent.testing import provider_setup, workflow_capture

    error = RuntimeError('failure')
    seen = []
    monkeypatch.setattr(provider_setup, '_test_snapshot', lambda item: {'config': {}})
    monkeypatch.setattr(workflow_capture, 'worker_exception_details',
                        lambda error, **kwargs: seen.append((error, kwargs)) or 'safe evidence')
    report = SimpleNamespace(when='setup', passed=False, failed=True, longrepr='pytest failure')

    _dispatch_report(SimpleNamespace(), report, error)

    assert seen == [(error, {'allow_unavailable': False})]
    assert report.longrepr == 'pytest failure\nsafe evidence'


def test_report_hook_withholds_when_snapshot_cannot_be_read(monkeypatch):
    from api_agent.testing import provider_setup

    def invalid_snapshot(item):
        raise ValueError('untrusted snapshot error')
    monkeypatch.setattr(provider_setup, '_test_snapshot', invalid_snapshot)
    error = RuntimeError('failure')
    error.local_details = 'untrusted-private-evidence'
    report = SimpleNamespace(when='teardown', passed=False, failed=True, longrepr='pytest failure')

    _dispatch_report(SimpleNamespace(), report, error)

    assert report.longrepr == 'pytest failure'


def test_report_hook_does_not_project_a_nonfailed_report(monkeypatch):
    from api_agent.testing import provider_setup

    def unexpected_snapshot(item):
        pytest.fail('nonfailed report must not read the snapshot')
    monkeypatch.setattr(provider_setup, '_test_snapshot', unexpected_snapshot)
    report = SimpleNamespace(when='call', passed=True, failed=False, longrepr='original')
    item = SimpleNamespace()

    _dispatch_report(item, report, RuntimeError('not a failure'))

    assert report.longrepr == 'original'
    assert item._api_agent_ui_outcome == 'success'
