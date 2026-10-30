"""Authority enrollment and recording failures remain fail-closed."""
import asyncio

import pytest

from api_agent.providers.contracts import ContractError
from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
from api_agent.testing.authority_scope import TestAuthorityScope, authority_scope, ajoin_current
from api_agent.testing.recording import RunIdentity, RunRecorder


def make_boundary():
    return EmissionBoundary(ContentPolicy(True))


def test_failed_freeze_releases_all_credential_leases():
    scope = TestAuthorityScope(['ui', 'tools'])
    first, second = make_boundary(), make_boundary()
    for index in range(65):
        first.register_secret(f'synthetic-secret-{index}')
    scope.join('ui', first)
    scope.join('tools', second)
    with pytest.raises(ContractError):
        scope.freeze()
    first.register_secret('synthetic-after-first')
    second.register_secret('synthetic-after-second')
    scope.close()


def test_owner_admission_waits_for_lease_and_invalidates_stale_commit():
    async def scenario():
        scope = TestAuthorityScope(['ui'])
        first, second = make_boundary(), make_boundary()
        scope.join('ui', first)
        generation, _ = scope.snapshot()
        lease = scope.freeze()
        with pytest.raises(ContractError):
            scope.join('ui', second)
        waiting = asyncio.create_task(scope.ajoin('ui', second))
        await asyncio.sleep(0)
        assert not waiting.done()
        lease.release()
        await waiting
        emitted = []
        assert not scope.commit_if_current(generation, lambda: emitted.append('stale'))
        current, _ = scope.snapshot()
        assert scope.commit_if_current(current, lambda: emitted.append('current'))
        assert emitted == ['current']
        scope.close()
    asyncio.run(scenario())


def test_undeclared_async_owner_permanently_invalidates_scope():
    async def scenario():
        scope = TestAuthorityScope(['ui'])
        scope.join('ui', make_boundary())
        with pytest.raises(ContractError):
            await scope.ajoin('tools', make_boundary())
        with pytest.raises(ContractError):
            scope.freeze()
        scope.close()
    asyncio.run(scenario())


def test_upgrade_rejects_unowned_invalid_overlimit_and_released_lease():
    async def scenario():
        scope = TestAuthorityScope(['ui'])
        owner, other = make_boundary(), make_boundary()
        scope.join('ui', owner)
        lease = scope.freeze()
        for source, values in [(owner, ['']), (other, ['synthetic-secret']),
                               (owner, [f'synthetic-secret-{i}' for i in range(65)])]:
            with pytest.raises(ContractError):
                await lease.aupgrade(source, values)
        assert owner.export({'text': 'synthetic-secret-0'}) == {'text': 'synthetic-secret-0'}
        lease.release()
        with pytest.raises(ContractError):
            await lease.aupgrade(owner, ['synthetic-secret'])
        scope.close()
    asyncio.run(scenario())


def test_invalid_admission_arguments_and_context_join():
    async def scenario():
        with authority_scope(['ui']) as scope:
            with pytest.raises(ContractError):
                scope.join('invalid', make_boundary())
            with pytest.raises(ContractError):
                await scope.ajoin('invalid', make_boundary())
            with pytest.raises(ValueError):
                await scope.ajoin('ui', make_boundary(), timeout=0)
            await ajoin_current('ui', make_boundary())
            assert len(scope.authorities()) == 1
    asyncio.run(scenario())


def test_recorder_refuses_to_discard_unowned_artifact(tmp_path):
    recorder = RunRecorder(tmp_path, RunIdentity.create('synthetic', 'integration'))
    recorder.record_artifact('owned.json', b'{}')
    with pytest.raises(ValueError):
        recorder.discard_artifacts(['owned.json', 'unowned.json'])
    assert (tmp_path / 'owned.json').read_bytes() == b'{}'


def test_cancelled_or_closed_admission_cannot_join_later():
    async def scenario():
        scope = TestAuthorityScope(['ui'])
        scope.join('ui', make_boundary())
        lease = scope.freeze()
        cancelled_owner = make_boundary()
        cancelled = asyncio.create_task(scope.ajoin('ui', cancelled_owner))
        await asyncio.sleep(0)
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        lease.release()
        assert cancelled_owner not in scope.authorities()
        lease = scope.freeze()
        blocked = asyncio.create_task(scope.ajoin('ui', make_boundary()))
        await asyncio.sleep(0)
        assert not blocked.done()
        scope.close()
        with pytest.raises(ContractError):
            await blocked
        lease.release()
        with pytest.raises(ContractError):
            scope.authorities()
    asyncio.run(scenario())


def test_pytest_scope_rejects_marker_keyword_arguments():
    from types import SimpleNamespace
    from api_agent.testing.provider_setup import _test_credential_authority_scope
    marker = SimpleNamespace(args=('ui',), kwargs={'extra': 'invalid'})
    request = SimpleNamespace(node=SimpleNamespace(get_closest_marker=lambda name: marker))
    fixture = _test_credential_authority_scope.__wrapped__(request)
    with pytest.raises(ContractError):
        next(fixture)


@pytest.mark.parametrize('kind,text', [
    ('inference_evidence_incomplete', 'invalid'),
    ('workflow_inference_evidence_incomplete', '{"invocation_id":"invalid"}'),
])
def test_recording_event_handler_rejects_invalid_incomplete_evidence_event(tmp_path, kind, text):
    from test_recorded_integration import fixture, Storage
    from api_agent.testing.integration import IntegrationRunResult, RunnerEvent
    from api_agent.testing.recorded import run_recorded_integration
    from api_agent.testing.contracts import TestResult
    context, identity, config = fixture(tmp_path)
    storage = Storage()
    def runner(context, tests, **hooks):
        hooks['before_run']()
        with pytest.raises(ValueError):
            hooks['on_event'](RunnerEvent(kind, identity=identity, text=text))
        result = TestResult(identity, 'passed', 0)
        hooks['on_result'](result)
        outcome = IntegrationRunResult(0, (result,), '')
        hooks['after_run'](outcome)
        return outcome
    result = run_recorded_integration(context, [identity], config, accepted=True, runner=runner, storage=storage)
    assert result.exit_code == 0
    assert len(storage.calls) == 1


@pytest.mark.parametrize('operation', ['release', 'close'])
def test_closed_waiter_loop_does_not_block_scope_cleanup(operation):
    scope = TestAuthorityScope(['ui'])
    owner = make_boundary()
    scope.join('ui', owner)
    lease = scope.freeze()
    loop = asyncio.new_event_loop()
    waiter = loop.create_future()
    loop.close()
    scope._waiters.add((loop, waiter))
    try:
        if operation == 'release':
            lease.release()
            owner.register_secret('synthetic-after-release')
            assert scope.authorities() == (owner,)
        else:
            scope.close()
            with pytest.raises(ContractError):
                scope.authorities()
    finally:
        lease.release()
        scope.close()


def test_close_drains_failed_evidence_job():
    from concurrent.futures import Future
    from threading import Event, Thread
    scope = TestAuthorityScope(['ui'])
    scope.join('ui', make_boundary())
    waiting = Event()
    class TrackedFuture(Future):
        def result(self, timeout=None):
            waiting.set()
            return super().result(timeout)
    job = TrackedFuture()
    scope.enroll_evidence(job)
    started, finished = Event(), Event()
    def close():
        started.set()
        try:
            scope.close()
        finally:
            finished.set()
    thread = Thread(target=close)
    thread.start()
    try:
        assert started.wait(2)
        assert waiting.wait(2)
        assert not finished.is_set()
        job.set_exception(RuntimeError('synthetic evidence failure'))
        assert finished.wait(2)
        with pytest.raises(ContractError):
            scope.authorities()
    finally:
        if not job.done():
            job.set_result(None)
        thread.join(2)
    assert not thread.is_alive()


def test_recorder_removes_partially_written_artifact_and_cannot_finalize(tmp_path, monkeypatch):
    from pathlib import Path
    recorder = RunRecorder(tmp_path, RunIdentity.create('synthetic', 'integration'))
    open_path = Path.open
    class FailedWriter:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            self.stream.__enter__()
            return self
        def __exit__(self, *args):
            return self.stream.__exit__(*args)
        def write(self, data):
            self.stream.write(data[:1])
            raise OSError('synthetic write failure')
    def open_with_failure(path, mode='r', *args, **kwargs):
        stream = open_path(path, mode, *args, **kwargs)
        return FailedWriter(stream) if path.name == 'partial.json' and mode == 'xb' else stream
    monkeypatch.setattr(Path, 'open', open_with_failure)
    with pytest.raises(OSError):
        recorder.record_artifact('partial.json', b'payload')
    assert not (tmp_path / 'partial.json').exists()
    with pytest.raises(RuntimeError):
        recorder.finalize(status='completed')
    assert not (tmp_path / 'manifest.json').exists()
