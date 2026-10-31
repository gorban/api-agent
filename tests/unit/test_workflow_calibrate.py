"""Manual calibration never authenticates through a second provider pathway."""
import asyncio
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import json
import os
import stat

import pytest

from api_agent.providers.control import CleanupPendingError
from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
from api_agent.testing.diagnostics import CredentialScrubber
from api_agent.workflows import calibrate
from api_agent.workflows.sampling import DECLARED, FIRST

def structural_events(trial, route, *, retry=True):
    """Synthetic emitted lifecycles with independent scope and attempt identities."""
    events = []
    graph = f'graph-{trial}'
    def start(scope, parent, owner, kind, **values):
        row = dict(event='scope_start', scope_id=scope, parent_id=parent, owner=owner, kind=kind, **values)
        events.append(row)
        return row
    def end(row):
        events.append(dict(row, event='scope_end', outcome='passed'))
    graph_scope = start(graph, None, 'GRAPH', 'graph', attempt=1, max_passes=1)
    events.append(dict(event='edge', owner='GRAPH', source='START', target='first', implicit=True, attempt_id=None))
    for component in (['first'] if route == 'skip' else ['first', 'second']):
        scope = f'{graph}-{component}'
        component_scope = start(scope, graph, component, 'component', component=component, attempt=1, max_passes=2, attempt_id=None)
        for attempt in ((1, 2) if retry else (1,)):
            attempt_id = f'{scope}-attempt-{attempt}'
            events.append(dict(event='attempt', scope_id=scope, component=component, attempt=attempt, max_passes=2, attempt_id=attempt_id))
            for suffix in ('draft', 'format', 'guard'):
                node = f'{component}_{suffix}'
                node_scope = start(f'{attempt_id}-{node}', scope, node, 'node', component=component, node=node,
                                   attempt=attempt, max_passes=2, attempt_id=attempt_id)
                events.append(dict(event='node', scope_id=node_scope['scope_id'], node=node, component=component, attempt=attempt))
                end(node_scope)
        end(component_scope)
        target = ('END' if route == 'skip' else 'second') if component == 'first' else 'END'
        condition = ('first_draft.route == "skip"' if route == 'skip' else 'otherwise') if component == 'first' else 'always'
        events.append(dict(event='edge', owner='GRAPH', source=component, target=target, condition=condition,
                           implicit=component == 'second', visit_id=scope, attempt_id=None))
    end(graph_scope)
    return events




ROOT = Path(calibrate.__file__).resolve().parents[3]


@pytest.fixture
def setup(tmp_path, monkeypatch):
    config = {'LLM_PROVIDER': str(ROOT / 'src/api_agent/llm_providers/gpt/factory.py') + ':factory', 'NO_STD_IN': True, 'LLM_ALLOWED_MODELS': '.*', 'LLM_DEFAULT_MODEL': 'synthetic-model', 'SYNTHETIC_API_KEY': 'synthetic-secret'}
    result = SimpleNamespace(values=config, fields={})
    context = SimpleNamespace(base_root=ROOT, owner_root=tmp_path)
    calls, instances = [], []
    class Provider:
        def __init__(self, values, owner_root, **options):
            assert options['skip_unavailable'] is False
            assert options['interaction'] is None
            calls.append('setup')
            self.emissions = EmissionBoundary(ContentPolicy(False))
            self.closed = False
            instances.append(self)
        def describe(self):
            self.emissions.register_secret('runtime-secret')
            return ()
        def close(self):
            calls.append('close')
            self.closed = True
    monkeypatch.setattr('api_agent.testing.provider_setup.IntegrationLLM', Provider)
    monkeypatch.setattr('api_agent.auth.managers.registry_for_context', lambda context: object())
    monkeypatch.setattr('api_agent.providers.selection.ModelSelectionPolicy.select_default', lambda self, catalog: 'synthetic-model')
    fingerprint = {'model': 'synthetic-model', 'fingerprint_version': 2}
    monkeypatch.setattr(calibrate, 'fingerprint', lambda *a, **k: dict(fingerprint))
    def graph(provider, selection, trial, config, **kwargs):
        assert (tmp_path / 'candidate.json.reservation').exists()
        calls.append(f'graph {trial}')
        kwargs['observe']({'event': 'llm', 'node': 'first_draft', 'usage': {'input_tokens': 1}})
        route = ('skip', 'next_a', 'next_b')[(trial - 1) % 3]
        for event in structural_events(trial, route):
            kwargs['observe'](event)
        return {'first_draft': {'route': route}}, FIRST if route == 'skip' else DECLARED
    monkeypatch.setattr(calibrate, 'run_graph', graph)
    state = SimpleNamespace(context=context, result=result, calls=calls, instances=instances,
        destination=tmp_path / 'candidate.json', fingerprint=fingerprint)
    return state


def test_pilot_reserves_before_setup_observes_calls_and_finalizes_after_cleanup(setup):
    lines = []
    report = calibrate.pilot(setup.context, setup.result, setup.destination, 60, output=lines.append)
    assert setup.calls[0] == 'setup' and setup.calls[-1] == 'close'
    assert report['trials'] == 60 and report['model'] == 'synthetic-model'
    assert setup.destination.is_file() and setup.instances[0].closed
    assert not setup.destination.with_name('candidate.json.reservation').exists()
    events = [json.loads(line) for line in setup.destination.with_name('candidate.json.progress.jsonl').read_text().splitlines()]
    assert any(row.get('events', [{}])[0].get('event') == 'llm' for row in events)
    assert 'Calibration graph 60/60: LLM call 60 completed.' in lines
    assert 'manually updating' in lines[-1]
    assert stat.S_IMODE(setup.destination.with_name('candidate.json.progress.jsonl').stat().st_mode) == 0o600


def test_graph_defect_keeps_incremental_safe_evidence_without_bounds(setup, monkeypatch):
    def fail(*a, **kwargs):
        kwargs['observe']({'event': 'llm', 'node': 'first_draft', 'usage': {'input_tokens': 2}})
        raise RuntimeError('runtime-secret synthetic-secret private prompt')
    monkeypatch.setattr(calibrate, 'run_graph', fail)
    with pytest.raises(RuntimeError) as caught:
        calibrate.pilot(setup.context, setup.result, setup.destination, 60, output=lambda line: None)
    assert setup.instances[0].closed and not setup.destination.exists()
    text = setup.destination.with_name('candidate.json.progress.jsonl').read_text()
    assert 'private prompt' not in text and 'runtime-secret' not in text and 'synthetic-secret' not in text
    rows = [json.loads(line) for line in text.splitlines()]
    assert rows[-1]['status'] == 'failed' and rows[-1]['events'][0]['kind'] == 'unknown'
    assert caught.value.calibration_scrubber('runtime-secret') == '[REDACTED]'


@pytest.mark.parametrize('count', [0, 61, True])
def test_invalid_count_never_sets_up_provider(setup, count):
    with pytest.raises(ValueError, match='1 and 60'):
        calibrate.pilot(setup.context, setup.result, setup.destination, count)
    assert setup.instances == [] and not setup.destination.exists()


@pytest.mark.parametrize('suffix', ['', '.receipt-key', '.reservation', '.progress.jsonl'])
def test_existing_or_linked_candidate_sidecar_stops_before_setup(setup, suffix):
    occupied = setup.destination.with_name(setup.destination.name + suffix)
    occupied.symlink_to(occupied.parent / 'absent-target')
    with pytest.raises(ValueError, match='symlinks'):
        calibrate.pilot(setup.context, setup.result, setup.destination, 60)
    assert occupied.is_symlink() and setup.instances == []


def test_parent_symlink_is_rejected_without_retargeting(tmp_path):
    parent = tmp_path / 'alias'
    parent.symlink_to(tmp_path / 'elsewhere', target_is_directory=True)
    with pytest.raises(ValueError, match='symlinks'):
        calibrate.candidate_path(parent / 'candidate.json')


def test_relative_candidate_path_is_canonical_after_link_checks(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert calibrate.candidate_path('candidate.json') == tmp_path / 'candidate.json'


def test_source_change_during_pilot_prevents_finalization(setup, monkeypatch):
    values = iter([setup.fingerprint, dict(setup.fingerprint, model='changed')])
    monkeypatch.setattr(calibrate, 'fingerprint', lambda *a, **k: next(values))
    with pytest.raises(ValueError, match='changed during'):
        calibrate.pilot(setup.context, setup.result, setup.destination, 60, output=lambda text: None)
    assert not setup.destination.exists() and setup.instances[0].closed


@pytest.mark.parametrize('mode', ['setup', 'cleanup'])
def test_provider_setup_or_cleanup_failure_never_finalizes_candidate(setup, monkeypatch, mode):
    provider_type = __import__('api_agent.testing.provider_setup', fromlist=['IntegrationLLM']).IntegrationLLM
    if mode == 'setup':
        monkeypatch.setattr(provider_type, 'describe', Mock(side_effect=RuntimeError('setup unavailable')))
        expected = RuntimeError
    else:
        monkeypatch.setattr(provider_type, 'close', Mock(side_effect=RuntimeError('cleanup unavailable')))
        expected = CleanupPendingError
    with pytest.raises(expected) as caught:
        calibrate.pilot(setup.context, setup.result, setup.destination, 60, output=lambda text: None)
    assert not setup.destination.exists()
    if mode == 'cleanup':
        provider = setup.instances[0]
        monkeypatch.setattr(provider_type, 'close', lambda self: setattr(self, 'closed', True))
        asyncio.run(caught.value.retry())
        assert provider.closed


def test_combined_failure_preserves_primary_and_both_cleanup_duties(setup, monkeypatch):
    owners = [SimpleNamespace(closed=False), SimpleNamespace(closed=False)]
    async def first_retry(): owners[0].closed = True
    async def second_retry(): owners[1].closed = True
    first = CleanupPendingError((owners[0],), first_retry)
    second = CleanupPendingError((owners[1],), second_retry)
    primary = RuntimeError('graph failure')
    primary.cleanup_pending = first
    monkeypatch.setattr(calibrate, 'run_graph', Mock(side_effect=primary))
    provider_type = __import__('api_agent.testing.provider_setup', fromlist=['IntegrationLLM']).IntegrationLLM
    monkeypatch.setattr(provider_type, 'close', Mock(side_effect=[second, None]))
    with pytest.raises(RuntimeError) as caught:
        calibrate.pilot(setup.context, setup.result, setup.destination, 60, output=lambda text: None)
    assert caught.value is primary and primary.cleanup_pending is first
    outer = first.cleanup_pending
    assert outer.cleanup_pending is second
    from api_agent.testing.suite_lock import suite_lock, SuiteBusyError
    with pytest.raises(SuiteBusyError):
        with suite_lock(): pass
    asyncio.run(first.retry())
    with pytest.raises(SuiteBusyError):
        with suite_lock(): pass
    asyncio.run(second.retry())
    with pytest.raises(SuiteBusyError):
        with suite_lock(): pass
    asyncio.run(outer.retry())
    with suite_lock(): pass
    assert not setup.destination.exists()


@pytest.mark.parametrize('cycle', [False, True])
def test_cleanup_attachment_deduplicates_and_preserves_existing_duties(cycle):
    primary, first, second = RuntimeError(), RuntimeError(), RuntimeError()
    primary.cleanup_pending = first
    if cycle:
        first.cleanup_pending = primary
    calibrate._attach_cleanup(primary, second)
    assert primary.cleanup_pending is first and first.cleanup_pending is second
    calibrate._attach_cleanup(primary, second)
    assert first.cleanup_pending is second


@pytest.mark.parametrize('failure', [KeyboardInterrupt, CleanupPendingError])
def test_cleanup_preserves_interrupt_or_existing_receipt(failure):
    closed = []
    async def retry(): closed.append(True)
    error = failure((object(),), retry) if failure is CleanupPendingError else failure()
    provider = SimpleNamespace(close=Mock(side_effect=error))
    with pytest.raises(failure) as caught:
        calibrate._cleanup(provider, None)
    assert caught.value is error if failure is KeyboardInterrupt else caught.value.cleanup_pending is error
    if failure is KeyboardInterrupt:
        assert isinstance(error.cleanup_pending, CleanupPendingError)


def test_typed_configuration_uses_only_calibration_provider_requirements(monkeypatch):
    monkeypatch.setattr('api_agent.config.read_dotenv', lambda path: {'NO_STD_IN': 'True'})
    monkeypatch.setattr('api_agent.workflows.calibrate.os.environ', {})
    gate = Mock()
    monkeypatch.setattr('api_agent.runtime.worktrees.verify_worktrees', gate)
    context, result = calibrate.configuration(ROOT, ['--gpt-auth-mode=api_key', '--workflow-llm-timeout-seconds=120'])
    assert result.values['NO_STD_IN'] is True and result.values['TUI_MODE'] == 'workflow-calibration'
    assert result.values['WORKFLOW_LLM_TIMEOUT_SECONDS'] == 120
    assert 'ALLURE_S3_BUCKET' not in result.values and result.errors == ()
    assert gate.call_args.kwargs['stdin'] is None


@pytest.mark.parametrize('options', [['--workflow-llm-timeout-seconds=invalid'], ['selector']])
def test_bad_configuration_fails_before_provider_setup(monkeypatch, options):
    monkeypatch.setattr('api_agent.config.read_dotenv', lambda path: {})
    monkeypatch.setattr('api_agent.workflows.calibrate.os.environ', {})
    monkeypatch.setattr('api_agent.runtime.worktrees.verify_worktrees', lambda *a, **k: None)
    with pytest.raises(ValueError) as caught:
        calibrate.configuration(ROOT, options)
    assert isinstance(caught.value.calibration_config, dict)


def test_source_checkout_rejects_unrelated_and_installed_paths(tmp_path, monkeypatch):
    assert calibrate.source_checkout() == ROOT
    assert calibrate.source_checkout(ROOT) == ROOT
    with pytest.raises(ValueError, match='running'):
        calibrate.source_checkout(tmp_path)
    monkeypatch.setattr(calibrate, '__file__', str(tmp_path / 'site-packages/api_agent/workflows/calibrate.py'))
    with pytest.raises(ValueError, match='running'):
        calibrate.source_checkout()


def test_main_uses_explicit_candidate_and_default_sixty_graphs(setup, monkeypatch):
    monkeypatch.setattr(calibrate, 'configuration', lambda *a: (setup.context, setup.result))
    run = Mock()
    monkeypatch.setattr(calibrate, 'pilot', run)
    assert calibrate.main(['--candidate', str(setup.destination), '--no-std-in']) == 0
    assert run.call_args.args[3] == 60
    run.call_args.kwargs['output']('synthetic-secret')


@pytest.mark.parametrize('failure,expected', [(RuntimeError('synthetic-secret'), 1), (KeyboardInterrupt(), 130)])
def test_main_preserves_configured_secret_scrubbing_on_early_pilot_errors(setup, monkeypatch, capsys, failure, expected):
    monkeypatch.setattr(calibrate, 'configuration', lambda *a: (setup.context, setup.result))
    monkeypatch.setattr(calibrate, 'pilot', Mock(side_effect=failure))
    assert calibrate.main(['--candidate', str(setup.destination)]) == expected
    assert 'synthetic-secret' not in capsys.readouterr().err


def test_main_retains_cleanup_with_scrubbed_boundary_wrapper(setup, monkeypatch):
    async def retry(): pass
    error = CleanupPendingError((object(),), retry)
    error.calibration_scrubber = CredentialScrubber({'API_KEY': 'synthetic-secret'})
    monkeypatch.setattr(calibrate, 'configuration', lambda *a: (setup.context, setup.result))
    monkeypatch.setattr(calibrate, 'pilot', Mock(side_effect=error))
    with pytest.raises(RuntimeError) as caught:
        calibrate.main(['--candidate', str(setup.destination)])
    assert caught.value.cleanup_pending is error


def test_main_invalid_count_does_not_resolve_configuration(monkeypatch, tmp_path):
    configure = Mock(side_effect=AssertionError('configuration must not run'))
    monkeypatch.setattr(calibrate, 'configuration', configure)
    assert calibrate.main(['--candidate', str(tmp_path / 'candidate.json'), '--graphs', '61']) == 1
    configure.assert_not_called()


@pytest.mark.parametrize('fault', ['mode', 'owner'])
def test_progress_descriptor_validation_closes_rejected_file_before_setup(setup, monkeypatch, fault):
    opened = []
    original_open, original_stat = os.open, os.fstat
    def opened_progress(path, *args, **kwargs):
        descriptor = original_open(path, *args, **kwargs)
        if str(path).endswith('.progress.jsonl'):
            opened.append(descriptor)
        return descriptor
    def metadata(descriptor):
        actual = original_stat(descriptor)
        if descriptor in opened:
            return SimpleNamespace(st_mode=stat.S_IFIFO if fault == 'mode' else actual.st_mode,
                st_uid=actual.st_uid if fault == 'mode' else actual.st_uid + 1)
        return actual
    monkeypatch.setattr(calibrate, 'os', SimpleNamespace(**(vars(calibrate.os) | {'open': opened_progress, 'fstat': metadata})))
    with pytest.raises(ValueError, match='regular file owned'):
        calibrate.pilot(setup.context, setup.result, setup.destination, 60)
    assert setup.instances == []
    with pytest.raises(OSError): original_stat(opened[0])


def test_constructor_failure_retains_structural_failure_evidence(setup, monkeypatch):
    monkeypatch.setattr('api_agent.testing.provider_setup.IntegrationLLM', Mock(side_effect=RuntimeError('constructor failed')))
    with pytest.raises(RuntimeError):
        calibrate.pilot(setup.context, setup.result, setup.destination, 60)
    rows = [json.loads(line) for line in setup.destination.with_name('candidate.json.progress.jsonl').read_text().splitlines()]
    assert rows[-1]['status'] == 'failed' and not setup.destination.exists()


def test_node_observation_preserves_structural_events_without_claiming_llm_call(setup, monkeypatch):
    def graph(provider, selection, trial, config, **kwargs):
        route = ('skip', 'next_a', 'next_b')[(trial - 1) % 3]
        for event in structural_events(trial, route):
            kwargs['observe'](event)
        return {'first_draft': {'route': route}}, FIRST if route == 'skip' else DECLARED
    monkeypatch.setattr(calibrate, 'run_graph', graph)
    lines = []
    calibrate.pilot(setup.context, setup.result, setup.destination, 60, output=lines.append)
    assert not any('LLM call' in line for line in lines)
    assert 'first_draft' in setup.destination.with_name('candidate.json.progress.jsonl').read_text()


def test_progress_write_failure_keeps_primary_and_cleanup_runs(setup, monkeypatch):
    original_fdopen = os.fdopen
    class BrokenProgress:
        def __init__(self, stream): self.stream = stream
        def __enter__(self): return self
        def __exit__(self, *args): self.stream.close()
        def write(self, text): raise OSError('synthetic progress disk failure')
        def flush(self): self.stream.flush()
    def fdopen(descriptor, mode, **kwargs):
        stream = original_fdopen(descriptor, mode, **kwargs)
        return BrokenProgress(stream) if mode == 'w' else stream
    monkeypatch.setattr(calibrate.os, 'fdopen', fdopen)
    with pytest.raises(OSError) as caught:
        calibrate.pilot(setup.context, setup.result, setup.destination, 60, output=lambda line: None)
    assert setup.instances[0].closed and not setup.destination.exists()
    assert any('Calibration progress artifact failure category: unavailable' in note for note in caught.value.__notes__)


def test_main_scrubs_typed_configuration_failure_values(setup, monkeypatch, capsys):
    error = ValueError('synthetic-secret invalid option')
    error.calibration_config = setup.result.values
    monkeypatch.setattr(calibrate, 'configuration', Mock(side_effect=error))
    assert calibrate.main(['--candidate', str(setup.destination)]) == 1
    assert 'synthetic-secret' not in capsys.readouterr().err


def test_module_entry_runs_count_guard_before_configuration(tmp_path, monkeypatch):
    import runpy
    monkeypatch.setattr('sys.argv', ['calibrate', '--candidate', str(tmp_path / 'candidate.json'), '--graphs', '61'])
    with pytest.warns(RuntimeWarning, match='found in sys.modules'):
        with pytest.raises(SystemExit) as caught:
            runpy.run_module('api_agent.workflows.calibrate', run_name='__main__')
    assert caught.value.code == 1 and not (tmp_path / 'candidate.json').exists()


@pytest.mark.parametrize('interrupt', [False, True])
def test_cleanup_exception_keeps_existing_and_new_duties_until_each_acknowledges(tmp_path, interrupt):
    from api_agent.testing.suite_lock import suite_locked, suite_lock, SuiteBusyError
    first_owner, provider = SimpleNamespace(closed=False), SimpleNamespace(closed=False)
    counts = {'first': 0, 'provider': 0}
    async def first_retry():
        counts['first'] += 1
        first_owner.closed = True
    first = CleanupPendingError((first_owner,), first_retry)
    error = KeyboardInterrupt('cleanup interrupted') if interrupt else OSError('cleanup unavailable')
    error.cleanup_pending = first
    def close():
        counts['provider'] += 1
        if counts['provider'] == 1:
            raise error
        provider.closed = True
    provider.close = close
    @suite_locked
    def run():
        calibrate._cleanup(provider, None)
    expected = KeyboardInterrupt if interrupt else CleanupPendingError
    with pytest.raises(expected) as caught:
        run()
    from api_agent.testing.suite_lock import _cleanup_receipts
    pending = tuple(_cleanup_receipts(caught.value))
    assert len(pending) == 2 and first in pending
    second = next(item for item in pending if item is not first)
    assert second.owners == (provider,)
    with pytest.raises(SuiteBusyError):
        with suite_lock(): pass
    asyncio.run(first.retry())
    with pytest.raises(SuiteBusyError):
        with suite_lock(): pass
    asyncio.run(second.retry())
    with suite_lock(): pass
    asyncio.run(first.retry())
    asyncio.run(second.retry())
    assert counts == {'first': 1, 'provider': 2}


def test_noncanonical_provider_is_rejected_before_setup(setup, tmp_path):
    factory = tmp_path / 'other_provider.py'
    factory.write_text('def factory(): pass\n')
    setup.result.values['LLM_PROVIDER'] = str(factory) + ':factory'
    with pytest.raises(ValueError):
        calibrate.pilot(setup.context, setup.result, setup.destination, 60)
    assert setup.instances == [] and not setup.destination.exists()


def test_calibration_configuration_preserves_standard_unattended_auth_without_allure(monkeypatch):
    authentication = {'password_manager': {'manager': 'keeper', 'cli_path': '/bin/keeper'},
        'roles': {'LLM User': [{'vault': 'synthetic', 'user': 'synthetic'}]}}
    monkeypatch.setattr('api_agent.config.read_dotenv', lambda path: {'NO_STD_IN': 'True', 'UNATTENDED_AUTH': json.dumps(authentication)})
    monkeypatch.setattr('api_agent.workflows.calibrate.os.environ', {})
    monkeypatch.setattr('api_agent.runtime.worktrees.verify_worktrees', lambda *a, **k: None)
    _, result = calibrate.configuration(ROOT, ['--unattended-user-index=0'])
    assert result.values['UNATTENDED_AUTH'] == authentication
    assert result.values['UNATTENDED_USER_INDEX'] == 0 and 'ALLURE_S3_BUCKET' not in result.values


def test_maintenance_command_rejects_collection_only_intent_before_setup(monkeypatch):
    monkeypatch.setattr('api_agent.config.read_dotenv', lambda path: {})
    monkeypatch.setattr('api_agent.workflows.calibrate.os.environ', {})
    with pytest.raises(ValueError, match='COLLECT_ONLY'):
        calibrate.configuration(ROOT, ['--collect-only'])


def test_child_receipt_and_outer_completion_keep_lease_until_both_acknowledge():
    from api_agent.testing.suite_lock import suite_locked, suite_lock, SuiteBusyError, _cleanup_receipts
    owner, calls = SimpleNamespace(closed=False), []
    async def retry():
        owner.closed = True
        calls.append('child')
    child = CleanupPendingError((owner,), retry)
    provider = SimpleNamespace(closed=False)
    def close():
        if not owner.closed:
            raise child
        provider.closed = True
        calls.append('outer')
    provider.close = close
    @suite_locked
    def run(): calibrate._cleanup(provider, None)
    with pytest.raises(CleanupPendingError) as caught: run()
    outer = caught.value
    assert tuple(_cleanup_receipts(outer)) == (outer, child)
    asyncio.run(child.retry())
    assert owner.closed and not provider.closed
    with pytest.raises(SuiteBusyError):
        with suite_lock(): pass
    asyncio.run(outer.retry())
    asyncio.run(outer.retry())
    with suite_lock(): pass
    assert provider.closed and calls == ['child', 'outer']


def test_relative_builtin_factory_is_forwarded_with_operation_owner_root(setup, monkeypatch):
    setup.context.owner_root = ROOT
    setup.result.values['LLM_PROVIDER'] = 'src/api_agent/llm_providers/gpt/factory.py:factory'
    from api_agent.workflows.fixture_driver import provider_identity
    observed = []
    def graph(provider, selection, trial, config, **kwargs):
        assert kwargs['owner_root'] == ROOT
        observed.append(provider_identity(config, owner_root=kwargs['owner_root']))
        route = ('skip', 'next_a', 'next_b')[(trial - 1) % 3]
        for event in structural_events(trial, route):
            kwargs['observe'](event)
        return {'first_draft': {'route': route}}, FIRST if route == 'skip' else DECLARED
    monkeypatch.setattr(calibrate, 'run_graph', graph)
    report = calibrate.pilot(setup.context, setup.result, setup.destination, 60, output=lambda line: None)
    assert len(observed) == 60 and all(row['kind'] == 'bundled_gpt' for row in observed)
    assert report['trials'] == 60


def test_failed_outer_retry_retains_successor_completion_until_acknowledgement():
    from api_agent.testing.suite_lock import suite_locked, suite_lock, SuiteBusyError
    provider = SimpleNamespace(closed=False)
    attempts = []
    def close():
        attempts.append(True)
        if len(attempts) < 3:
            raise OSError('synthetic disposal unavailable')
        provider.closed = True
    provider.close = close
    @suite_locked
    def run(): calibrate._cleanup(provider, None)
    with pytest.raises(CleanupPendingError) as initial: run()
    with pytest.raises(CleanupPendingError) as following:
        asyncio.run(initial.value.retry())
    assert following.value.owners == (provider,) and not provider.closed
    with pytest.raises(SuiteBusyError):
        with suite_lock(): pass
    asyncio.run(following.value.retry())
    with suite_lock(): pass
    asyncio.run(initial.value.retry())
    assert provider.closed and len(attempts) == 3


def test_standard_loop_owned_child_retry_completes_outer_and_releases_kernel_lease():
    from api_agent.providers.sessions import _Owner
    from api_agent.testing.suite_lock import suite_locked, suite_lock, SuiteBusyError
    class Child(_Owner):
        def __init__(self):
            super().__init__(1)
            self.attempts = 0
            self.disposed = False
        async def _dispose(self):
            self.attempts += 1
            if self.attempts == 1:
                raise OSError('synthetic first disposal failure')
            self.disposed = True
    loop, child = asyncio.new_event_loop(), Child()
    provider = SimpleNamespace(loop=loop, closed=False)
    def close():
        loop.run_until_complete(child.aclose())
        provider.closed = True
        loop.close()
    provider.close = close
    @suite_locked
    def run(): calibrate._cleanup(provider, None)
    try:
        with pytest.raises(CleanupPendingError) as caught: run()
        with pytest.raises(SuiteBusyError):
            with suite_lock(): pass
        asyncio.run(caught.value.retry())
        assert child.disposed and provider.closed and loop.is_closed() and child.attempts == 2
        with suite_lock(): pass
    finally:
        if not loop.is_closed(): close()


@pytest.mark.parametrize('state', ['closed', 'running'])
def test_unavailable_owner_loop_retains_retry_duty_with_actionable_cause(state):
    loop = SimpleNamespace(is_closed=lambda: state == 'closed', is_running=lambda: state == 'running')
    provider = SimpleNamespace(loop=loop, close=Mock(side_effect=OSError('synthetic')))
    with pytest.raises(CleanupPendingError) as initial: calibrate._cleanup(provider, None)
    with pytest.raises(CleanupPendingError) as following: asyncio.run(initial.value.retry())
    assert following.value.owners == (provider,)
    assert 'open, idle owning loop' in str(following.value.__cause__)


def test_busy_owner_loop_successor_keeps_child_acknowledgement_until_recovery():
    from api_agent.providers.sessions import _Owner
    from api_agent.testing.suite_lock import suite_locked, suite_lock, SuiteBusyError
    class Child(_Owner):
        def __init__(self):
            super().__init__(1)
            self.attempts = 0
        async def _dispose(self):
            self.attempts += 1
            if self.attempts == 1:
                raise OSError('synthetic disposal failure')
    loop, child = asyncio.new_event_loop(), Child()
    busy = [False]
    facade = SimpleNamespace(is_closed=loop.is_closed, is_running=lambda: busy[0], run_until_complete=loop.run_until_complete)
    provider = SimpleNamespace(loop=facade, closed=False)
    def close():
        loop.run_until_complete(child.aclose())
        provider.closed = True
        loop.close()
    provider.close = close
    @suite_locked
    def run(): calibrate._cleanup(provider, None)
    try:
        with pytest.raises(CleanupPendingError) as initial: run()
        original_child = initial.value.cleanup_pending
        busy[0] = True
        with pytest.raises(CleanupPendingError) as following: asyncio.run(initial.value.retry())
        assert following.value.cleanup_pending is original_child
        with pytest.raises(SuiteBusyError):
            with suite_lock(): pass
        busy[0] = False
        asyncio.run(following.value.retry())
        assert child._closed and provider.closed and loop.is_closed()
        with suite_lock(): pass
        asyncio.run(initial.value.retry())
        assert child.attempts == 2
    finally:
        if not loop.is_closed(): close()


@pytest.mark.parametrize('override, expected', [('none', 'pack-model'), ('env', 'env-model'), ('cli', 'cli-model')])
def test_calibration_uses_shared_launcher_pack_precedence(tmp_path, monkeypatch, override, expected):
    from dataclasses import replace
    from api_agent.runtime.resource_pack import load_launch_context
    from api_agent.providers import loading
    original = load_launch_context(ROOT)
    shared = tmp_path / 'shared.env'
    launcher, pack = tmp_path / 'launcher', tmp_path / 'pack'
    context = replace(original, shared_dotenv_paths=(shared, shared), launcher_root=launcher, pack_root=pack)
    monkeypatch.setattr('api_agent.runtime.resource_pack.load_launch_context', lambda root: context)
    monkeypatch.setattr('api_agent.runtime.worktrees.verify_worktrees', lambda *a, **k: None)
    monkeypatch.setattr(calibrate.os, 'environ', {'LLM_DEFAULT_MODEL': 'env-model'} if override == 'env' else {})
    reads = []
    provider = str(ROOT / 'src/api_agent/llm_providers/gpt/factory.py') + ':factory'
    layers = {ROOT / '.env': {'LLM_DEFAULT_MODEL': 'base-model'},
              shared: {'LLM_DEFAULT_MODEL': 'shared-model', 'LLM_PROVIDER': provider},
              launcher / '.env': {'LLM_DEFAULT_MODEL': 'launcher-model'},
              pack / '.env': {'LLM_DEFAULT_MODEL': 'pack-model'}}
    def read(path):
        reads.append(path)
        return layers[path]
    monkeypatch.setattr('api_agent.config.read_dotenv', read)
    bootstrap = Mock(wraps=loading.bootstrap_schema)
    monkeypatch.setattr(loading, 'bootstrap_schema', bootstrap)
    options = ['--gpt-auth-mode=api_key', '--api-agent-dir=' + str(ROOT)]
    if override == 'cli': options.append('--llm-default-model=cli-model')
    _, result = calibrate.configuration(ROOT, options)
    assert reads == [ROOT / '.env', shared, launcher / '.env', pack / '.env']
    assert bootstrap.call_args.args[3]['LLM_PROVIDER'] == provider
    assert result.values['LLM_PROVIDER'] == provider
    assert result.values['LLM_DEFAULT_MODEL'] == expected
    assert 'GPT_AUTH_MODE' in result.values and not result.errors


def test_pilot_uses_profile_model_instead_of_application_default(setup, monkeypatch):
    setup.result.values.update(LLM_WORKFLOW_TEST_MODEL='profile-model', LLM_DEFAULT_MODEL='application-model')
    setup.fingerprint['model'] = 'profile-model'
    provider_type = __import__('api_agent.testing.provider_setup', fromlist=['IntegrationLLM']).IntegrationLLM
    monkeypatch.setattr(provider_type, 'describe', lambda self: [SimpleNamespace(selected_llm='profile-model')])
    monkeypatch.setattr('api_agent.providers.selection.ModelSelectionPolicy.select_default',
                        lambda *args: pytest.fail('Explicit workflow profile was replaced by application default'))
    original = calibrate.run_graph
    selections = []
    def observe_selection(provider, selection, *args, **kwargs):
        selections.append(selection)
        return original(provider, selection, *args, **kwargs)
    monkeypatch.setattr(calibrate, 'run_graph', observe_selection)
    report = calibrate.pilot(setup.context, setup.result, setup.destination, 60, output=lambda text: None)
    assert report['model'] == 'profile-model' and selections == ['profile-model'] * 60
    assert setup.result.values['LLM_DEFAULT_MODEL'] == 'application-model'


def test_pilot_requires_the_exact_profile_model_to_be_advertised(setup):
    setup.result.values['LLM_WORKFLOW_TEST_MODEL'] = 'unadvertised-model'
    with pytest.raises(ValueError, match='uniquely advertised'):
        calibrate.pilot(setup.context, setup.result, setup.destination, 60, output=lambda text: None)
    assert setup.calls == ['setup', 'close']
    assert not setup.destination.exists() and setup.instances[0].closed


def test_unset_explicit_profile_stops_pilot_before_authentication(setup):
    setup.result.values['LLM_WORKFLOW_TEST_MODEL'] = ''
    with pytest.raises(ValueError, match='qualified exact model'):
        calibrate.pilot(setup.context, setup.result, setup.destination, 60, output=lambda text: None)
    assert setup.instances == [] and not setup.destination.exists()


@pytest.mark.parametrize('value', [0, 121, True, 60.0, None])
def test_invalid_coverage_ceiling_never_allocates_pilot_or_provider(setup, value):
    setup.result.values['LLM_WORKFLOW_TEST_MAX_GRAPHS'] = value
    with pytest.raises(ValueError, match='integer from 1 to 120'):
        calibrate.pilot(setup.context, setup.result, setup.destination, 60)
    assert setup.instances == [] and not setup.destination.exists()
    assert not setup.destination.with_name('candidate.json.progress.jsonl').exists()


def test_sixty_graph_pilot_and_storage_use_resolved_larger_coverage_ceiling(setup):
    setup.result.values['LLM_WORKFLOW_TEST_MAX_GRAPHS'] = 120
    setup.fingerprint['operating_ceiling'] = 120
    report = calibrate.pilot(setup.context, setup.result, setup.destination, 60, output=lambda text: None)
    assert report['trials'] == 60 and report['operating_ceiling'] == 120
    from api_agent.workflows.evidence import read_calibration
    from api_agent.workflows.sampling import validate_calibration
    saved = read_calibration(setup.destination)
    assert validate_calibration(saved, DECLARED, setup.fingerprint, ceiling=120) == report['max_runs']
    assert setup.calls.count('graph 60') == 1 and 'graph 61' not in setup.calls


@pytest.mark.parametrize('provider, expected', [('gpt', 60), ('bedrock', 120)])
def test_calibration_ceiling_uses_provider_profile_and_typed_explicit_override(monkeypatch, provider, expected):
    monkeypatch.setattr('api_agent.config.read_dotenv', lambda path: {})
    monkeypatch.setattr(calibrate.os, 'environ', {})
    monkeypatch.setattr('api_agent.runtime.worktrees.verify_worktrees', lambda *args, **kwargs: None)
    factory = str(ROOT / f'src/api_agent/llm_providers/{provider}/factory.py') + ':factory'
    _, result = calibrate.configuration(ROOT, ['--llm-provider=' + factory])
    assert result.values['LLM_WORKFLOW_TEST_MAX_GRAPHS'] == expected
    _, explicit = calibrate.configuration(ROOT, ['--llm-provider=' + factory, '--llm-workflow-test-max-graphs=60'])
    assert explicit.values['LLM_WORKFLOW_TEST_MAX_GRAPHS'] == 60
    assert explicit.fields['LLM_WORKFLOW_TEST_MAX_GRAPHS'].explicit


@pytest.mark.parametrize('provider', ['gpt', 'bedrock'])
@pytest.mark.parametrize('value', ['121', 'True', '60.0'])
def test_maintenance_configuration_rejects_explicit_unsupported_ceiling_before_setup(monkeypatch, provider, value):
    monkeypatch.setattr('api_agent.config.read_dotenv', lambda path: {})
    monkeypatch.setattr(calibrate.os, 'environ', {})
    monkeypatch.setattr('api_agent.runtime.worktrees.verify_worktrees', lambda *args, **kwargs: None)
    factory = str(ROOT / f'src/api_agent/llm_providers/{provider}/factory.py') + ':factory'
    with pytest.raises(ValueError, match='LLM_WORKFLOW_TEST_MAX_GRAPHS'):
        calibrate.configuration(ROOT, ['--llm-provider=' + factory, '--llm-workflow-test-max-graphs=' + value])


@pytest.mark.parametrize('legacy_profile,expected', [(None, None), ({'max_output_tokens': 1024}, 1024)])
def test_explicit_maintenance_retains_legacy_cap_not_normal_test_override(setup, monkeypatch, legacy_profile, expected):
    setup.result.values['LLM_WORKFLOW_TEST_MAX_OUTPUT_TOKENS'] = 2048
    monkeypatch.setattr(calibrate, 'calibration_profile', lambda *args, **kwargs: legacy_profile)
    seen = []
    def graph(provider, selection, trial, config, **kwargs):
        seen.append(config.get('LLM_WORKFLOW_TEST_MAX_OUTPUT_TOKENS'))
        raise RuntimeError('stop after verifying maintenance request cap')
    monkeypatch.setattr(calibrate, 'run_graph', graph)
    with pytest.raises(RuntimeError, match='maintenance request cap'):
        calibrate.pilot(setup.context, setup.result, setup.destination, 60, output=lambda text: None)
    assert seen == [expected]
    assert setup.result.values['LLM_WORKFLOW_TEST_MAX_OUTPUT_TOKENS'] == 2048
    assert not setup.destination.exists()
