"""Offline regressions for retained-worker ownership through publication."""
import os
from pathlib import Path
from types import SimpleNamespace
import threading

import pytest

from api_agent.testing import inspection, integration
from api_agent.testing.collection import collect_tests


_RETAINED = '''
from types import SimpleNamespace
from api_agent.testing.provider_setup import IntegrationLLM

def test_browser():
    owner = IntegrationLLM({'DEBUG': True}, '.')
    async def create():
        owner.inspection.observe(SimpleNamespace(inspection_endpoint=None,
            browser=SimpleNamespace(is_connected=lambda: True), context=SimpleNamespace(pages=[object()])))
    owner.loop.run_until_complete(create())
    owner.finish_run()
'''


def selection(tmp_path, source=_RETAINED):
    suite = tmp_path / 'tests/integration'
    suite.mkdir(parents=True)
    (suite / 'test_sample.py').write_text(source)
    context = SimpleNamespace(base_root=tmp_path, owner_root=tmp_path,
        suite_paths=lambda mode: (suite,))
    return context, collect_tests(context, 'integration')


@pytest.mark.parametrize('callback', ['after_run', 'finished'])
def test_publication_interrupt_reaps_transferred_worker(tmp_path, callback):
    context, tests = selection(tmp_path)
    handles = []
    def after(run):
        handles.extend(run.inspections)
        if callback == 'after_run':
            raise KeyboardInterrupt
    def event(row):
        if callback == 'finished' and row.kind == 'finished':
            raise KeyboardInterrupt
    try:
        with pytest.raises(KeyboardInterrupt) as error:
            integration.run_integration_tests(context, tests, accepted=True,
                config={'DEBUG': True}, after_run=after, on_event=event)
        assert error.value.inspections == tuple(handles)
        workers = [handle for handle in handles if isinstance(handle, inspection.IntegrationInspection)]
        assert len(workers) == 1
        assert all(handle.closed for handle in handles)
        assert workers[0].process.poll() is not None
        assert not Path(workers[0].directory.temporary.name).exists()
    finally:
        for handle in handles:
            handle.abort()


@pytest.mark.parametrize('callback', ['after_run', 'finished'])
def test_cancellation_during_publication_reaps_before_return(tmp_path, callback):
    context, tests = selection(tmp_path)
    cancelled = threading.Event()
    def after(run):
        if callback == 'after_run':
            cancelled.set()
    def event(row):
        if callback == 'finished' and row.kind == 'finished':
            cancelled.set()
    result = integration.run_integration_tests(context, tests, accepted=True,
        config={'DEBUG': True}, cancel_event=cancelled, after_run=after, on_event=event)
    try:
        assert result.cancelled and result.exit_code == 130
        workers = [handle for handle in result.inspections if isinstance(handle, inspection.IntegrationInspection)]
        assert len(workers) == 1
        assert all(handle.closed for handle in result.inspections)
        assert workers[0].process.poll() is not None
    finally:
        for handle in result.inspections:
            handle.abort()


def test_natural_close_during_transfer_preserves_passed_outcome(tmp_path, monkeypatch):
    source = _RETAINED.replace('from types import', 'from pathlib import Path\nfrom types import').replace(
        'is_connected=lambda: True', "is_connected=lambda: not Path('natural-close').exists()")
    context, tests = selection(tmp_path, source)
    original = inspection.IntegrationInspection
    class DelayedTransfer(original):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            (tmp_path / 'natural-close').touch()
            self._monitor.join(timeout=5)
            assert self.closed
    monkeypatch.setattr(inspection, 'IntegrationInspection', DelayedTransfer)
    result = integration.run_integration_tests(context, tests, accepted=True, config={'DEBUG': True})
    assert result.exit_code == 0, result.output
    assert result.tests[0].status == 'passed'
    assert all(handle.closed for handle in result.inspections)


def test_interrupt_after_initialization_transfer_cannot_strand_handle(tmp_path, monkeypatch):
    context, tests = selection(tmp_path)
    captured = []
    original = inspection.IntegrationInspection
    class InterruptedTransfer(original):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            captured.append(self)
            raise KeyboardInterrupt
    monkeypatch.setattr(inspection, 'IntegrationInspection', InterruptedTransfer)
    try:
        with pytest.raises(KeyboardInterrupt):
            integration.run_integration_tests(context, tests, accepted=True, config={'DEBUG': True})
        assert len(captured) == 1
        assert captured[0].closed and captured[0].process.poll() is not None
        assert not Path(captured[0].directory.temporary.name).exists()
    finally:
        for handle in captured:
            handle.abort()


@pytest.mark.skipif(os.name != 'posix', reason='POSIX liveness descriptor ownership')
@pytest.mark.parametrize('failure', ['popen', 'reader', 'monitor'])
def test_startup_failure_reaps_worker_and_closes_liveness_pipe(tmp_path, monkeypatch, failure):
    context, tests = selection(tmp_path)
    pipes, workers, worker_fds = [], [], []
    original_pipe = os.pipe
    original_popen = integration.subprocess.Popen
    original_start = threading.Thread.start
    def pipe():
        descriptors = original_pipe()
        pipes.append(descriptors)
        return descriptors
    def popen(args, **kwargs):
        worker = kwargs.get('pass_fds')
        if worker:
            worker_fds.extend(worker)
        if worker and failure == 'popen':
            raise ValueError('synthetic startup failure')
        process = original_popen(args, **kwargs)
        if worker:
            workers.append(process)
        return process
    def start(thread):
        target = getattr(thread._target, '__name__', '')
        if target == ('read_output' if failure == 'reader' else '_watch') and failure != 'popen':
            raise RuntimeError('synthetic thread startup failure')
        return original_start(thread)
    monkeypatch.setattr(os, 'pipe', pipe)
    monkeypatch.setattr(integration.subprocess, 'Popen', popen)
    monkeypatch.setattr(threading.Thread, 'start', start)
    result = integration.run_integration_tests(context, tests, accepted=True, config={'DEBUG': True})
    assert result.exit_code == 1
    assert all(handle.closed for handle in result.inspections)
    assert pipes
    liveness_pipes = [descriptors for descriptors in pipes if descriptors[0] in worker_fds]
    assert len(liveness_pipes) == 1
    for descriptors in liveness_pipes:
        for descriptor in descriptors:
            with pytest.raises(OSError):
                os.fstat(descriptor)
    assert all(worker.poll() is not None for worker in workers)


def test_non_posix_reader_start_failure_reaps_worker_and_closes_stdout(tmp_path, monkeypatch):
    """The no-liveness-pipe platform still owns a worker when reader startup fails."""
    context, tests = selection(tmp_path)
    workers, launch_options = [], []
    original_popen = integration.subprocess.Popen
    original_start = threading.Thread.start

    class NonPosixOS:
        name = 'nt'
        def __getattr__(self, name):
            return getattr(os, name)

    def popen(args, **kwargs):
        launch_options.append(kwargs)
        worker = original_popen(args, **kwargs)
        workers.append(worker)
        return worker

    def start(thread):
        if getattr(thread._target, '__name__', '') == 'read_output':
            raise RuntimeError('synthetic reader startup failure')
        return original_start(thread)

    from api_agent.testing import processes
    platform = NonPosixOS()
    monkeypatch.setattr(integration, 'os', platform)
    monkeypatch.setattr(processes, 'os', platform)
    monkeypatch.setattr(integration, 'collect_tests', lambda *args, **kwargs: tests)
    monkeypatch.setattr(integration.subprocess, 'Popen', popen)
    monkeypatch.setattr(threading.Thread, 'start', start)
    try:
        result = integration.run_integration_tests(context, tests, accepted=True, config={'DEBUG': True})
        assert result.exit_code == 1
        assert len(workers) == 1
        assert 'pass_fds' not in launch_options[0]
        assert 'API_AGENT_LAUNCHER_LIVENESS_FD' not in launch_options[0]['env']
        assert launch_options[0]['start_new_session'] is False
        assert workers[0].poll() is not None
        assert workers[0].stdout.closed
        assert all(handle.closed for handle in result.inspections)
    finally:
        for worker in workers:
            if worker.poll() is None:
                worker.kill()
                worker.wait(timeout=5)
            if worker.stdout is not None and not worker.stdout.closed:
                worker.stdout.close()
