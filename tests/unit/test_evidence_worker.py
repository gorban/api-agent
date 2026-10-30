import asyncio
import json
import threading

import pytest

from api_agent.providers.setup_transport import MAX_EVIDENCE_BYTES, SetupServer, _EvidenceReceiver
from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary


@pytest.mark.parametrize('diagnostics', [{}, {'version':1, 'response_validation':'rejected'}])
def test_final_snapshot_rechecks_rejected_body_even_without_diagnostics(diagnostics):
    from api_agent.providers.rejected_evidence import guard_rejected_evidence
    body = {'provider_response':{'content':'synthetic-', 'extra_a':'cre',
                                 'unrelated':'ordinary', 'extra_b':'dential'}}
    assert guard_rejected_evidence(body, {'worker-only-secret'}) == body
    row = dict(phase='response', content=body, reason='captured', _rejected=True)
    row.update({'diagnostics':diagnostics} if diagnostics else {})
    final = EmissionBoundary(ContentPolicy(True), secrets=('synthetic-credential', 'rejected'))

    frames = SetupServer._prepare_cancellable([row], threading.Event(), final, True)

    assert len(frames) == 1
    retained = json.loads(frames[0][2])
    assert retained['status'] == 'evidence'
    assert retained['content'] == {} and retained['reason'] == 'unavailable'
    assert '_rejected' not in retained and 'diagnostics' not in retained


@pytest.mark.parametrize('late_credential', [False, True])
def test_isolated_final_snapshot_preserves_safe_rejection_or_withholds_late_credential(late_credential):
    async def scenario():
        body = {'provider_response':{'content':'synthetic-', 'unknown':'credential'}}
        projection = EmissionBoundary(ContentPolicy(True),
            secrets=('synthetic-credential',) if late_credential else ('unrelated',))
        frames = await SetupServer._prepare_isolated(
            [dict(phase='response', content=body, reason='captured', _rejected=True)], projection)
        assert len(frames) == 1
        retained = json.loads(frames[0][2])
        assert retained['status'] == 'evidence'
        assert retained['content'] == ({} if late_credential else body)
        assert retained['reason'] == ('unavailable' if late_credential else 'captured')
        assert '_rejected' not in retained
    asyncio.run(scenario())


def test_framing_only_path_cannot_publish_unadmitted_rejected_body():
    frames = SetupServer._prepare_evidence([
        dict(phase='response', content={'text':'unadmitted-body'}, reason='captured', _rejected=True)])
    assert len(frames) == 1
    retained = json.loads(frames[0][2])
    assert retained['status'] == 'evidence'
    assert retained['content'] == {} and retained['reason'] == 'unavailable'
    assert '_rejected' not in retained


@pytest.mark.parametrize('change', [dict(_rejected=False), dict(_rejected=1), dict(phase='request')])
def test_final_projection_rejects_invalid_internal_admission_marker(change):
    frames = SetupServer._prepare_cancellable([
        dict(phase='response', content={'text':'invalid-marker-body'}, reason='captured', _rejected=True) | change],
        threading.Event(), EmissionBoundary(ContentPolicy(True)), True)
    assert b'invalid-marker-body' not in b''.join(frame for _, _, frame in frames)


@pytest.mark.parametrize('raw', [False, True])
def test_final_snapshot_guards_fragments_in_json_embedded_in_text(raw):
    row = dict(phase='response', reason='captured', _rejected=True,
               content={'provider_response':{'content':'prefix {"a":"synthetic-","b":"credential"}'}})
    frames = SetupServer._prepare_cancellable([row], threading.Event(),
        EmissionBoundary(ContentPolicy(True), secrets=('synthetic-credential',)), raw)
    retained = json.loads(frames[0][2])
    assert retained['content'] == {} and retained['reason'] == 'unavailable'
    assert '_rejected' not in retained


def test_isolated_projection_preserves_large_capture_and_overlapping_secret():
    async def scenario():
        projection = EmissionBoundary(ContentPolicy(True), secrets=('abc', 'cde'), preserve_json_text=True)
        text = 'z' * (MAX_EVIDENCE_BYTES - 1024) + 'abcde'
        frames = await asyncio.wait_for(SetupServer._prepare_isolated(
            [dict(phase='request', content={'text': text}, reason='captured')], projection), 5)
        assert frames
        decoded = [json.loads(frame) for _, _, frame in frames]
        assert decoded[0]['status'] == 'evidence_start'
        assert decoded[0]['reason'] == 'captured'
        receiver = _EvidenceReceiver()
        result = None
        for frame in decoded:
            result = receiver.accept(frame)
        assert result['content']['text'].endswith('[REDACTED]')
        assert 'abcde' not in result['content']['text']
    asyncio.run(scenario())


def test_cancelled_projection_kills_and_reaps_owned_process(monkeypatch):
    async def scenario():
        original = asyncio.create_subprocess_exec
        created = []
        async def hanging(executable, *_args, **kwargs):
            process = await original(executable, '-c', 'import time; time.sleep(30)', **kwargs)
            created.append(process)
            return process
        monkeypatch.setattr(asyncio, 'create_subprocess_exec', hanging)
        projection = EmissionBoundary(ContentPolicy(True), secrets=('synthetic-secret',))
        task = asyncio.create_task(SetupServer._prepare_isolated(
            [dict(phase='request', content={'text': 'hello'}, reason='captured')], projection))
        while not created:
            await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert created[0].returncode is not None
    asyncio.run(scenario())


def test_isolated_projection_preserves_structured_carrier_redaction():
    async def scenario():
        projection = EmissionBoundary(ContentPolicy(True), secrets=('secret-value',), preserve_json_text=True)
        rows = [dict(phase='response', content={'payload': '{"parts":["secret-", "value"]}'}, reason='captured')]
        frames = await asyncio.wait_for(SetupServer._prepare_isolated(rows, projection), 5)
        receiver = _EvidenceReceiver()
        result = None
        for _, _, frame in frames:
            value = json.loads(frame)
            result = value if value['status'] == 'evidence' else receiver.accept(value)
        assert 'secret-value' not in repr(result)
        assert 'secret-' not in repr(result)
    asyncio.run(scenario())


def test_cancellation_during_spawn_still_reaps_child(monkeypatch):
    async def scenario():
        original = asyncio.create_subprocess_exec
        started = asyncio.Event()
        allow_return = asyncio.Event()
        created = []
        async def delayed(executable, *_args, **kwargs):
            process = await original(executable, '-c', 'import time; time.sleep(30)', **kwargs)
            created.append(process)
            started.set()
            await allow_return.wait()
            return process
        monkeypatch.setattr(asyncio, 'create_subprocess_exec', delayed)
        projection = EmissionBoundary(ContentPolicy(True))
        task = asyncio.create_task(SetupServer._prepare_isolated([], projection))
        await started.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert created[0].returncode is None
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        allow_return.set()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert created[0].returncode is not None
    asyncio.run(scenario())


def test_unsupported_platform_rejects_projection_before_reading_input(monkeypatch):
    from api_agent.providers import evidence_worker
    monkeypatch.setattr(evidence_worker.sys, 'platform', 'unsupported')
    assert evidence_worker.main() == 1


def test_cancellation_during_serialization_retains_owner_until_thread_finishes(monkeypatch):
    import threading
    import api_agent.providers.setup_transport as transport
    async def scenario():
        entered = threading.Event()
        release = threading.Event()
        def slow_measure(_rows, _maximum):
            entered.set()
            release.wait(2)
            return True
        monkeypatch.setattr(transport, '_bounded_evidence', slow_measure)
        projection = EmissionBoundary(ContentPolicy(True))
        task = asyncio.create_task(SetupServer._prepare_isolated([], projection))
        assert await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert task.done()
    asyncio.run(scenario())


def test_full_content_byte_budget_is_not_consumed_by_transport_wrappers():
    async def scenario():
        projection = EmissionBoundary(ContentPolicy(True), preserve_json_text=True)
        overhead = len(json.dumps({'text': ''}, separators=(',', ':')).encode())
        content = {'text': 'z' * (MAX_EVIDENCE_BYTES - overhead)}
        assert len(json.dumps(content, separators=(',', ':')).encode()) == MAX_EVIDENCE_BYTES
        frames = await asyncio.wait_for(SetupServer._prepare_isolated(
            [dict(phase='request', content=content, reason='captured')], projection), 5)
        receiver = _EvidenceReceiver()
        result = None
        for _, _, frame in frames:
            value = json.loads(frame)
            result = value if value['status'] == 'evidence' else receiver.accept(value)
        assert result['reason'] == 'captured'
        assert len(result['content']['text']) == len(content['text'])
    asyncio.run(scenario())


def test_timeout_retains_stalled_projection_until_owned_cleanup(monkeypatch):
    from types import SimpleNamespace
    import pytest
    from api_agent.providers.control import CleanupPendingError
    from api_agent.providers.evidence_authority import EvidenceAuthority
    from api_agent.providers.setup_registry import SetupRegistry
    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()
        async def stalled(_rows, _projection):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()
                raise
        monkeypatch.setattr(SetupServer, '_prepare_isolated', staticmethod(stalled))
        server = SetupServer(SetupRegistry('op', 'selected', object),
                             'synthetic-authority-capability-0000000', timeout=1, cleanup_timeout=.05)
        source = EmissionBoundary(ContentPolicy(True))
        boundary = EvidenceAuthority((source,), [])
        peer = SimpleNamespace(writer=SimpleNamespace(write=lambda _frame: None), evidence_sent=set(), evidence_phase=None)
        publishing = asyncio.create_task(server._publish_evidence(peer, [], boundary, raw=True))
        await entered.wait()
        publishing.cancel()
        try:
            await asyncio.wait_for(publishing, .2)
        except asyncio.CancelledError:
            pass
        assert len(server._evidence_tasks) == 1
        assert server._evidence_slots.locked()
        with pytest.raises(CleanupPendingError) as pending:
            await server.aclose()
        assert server in pending.value.owners
        release.set()
        await asyncio.gather(*tuple(server._evidence_tasks), return_exceptions=True)
        await asyncio.sleep(0)
        assert not server._evidence_tasks
        assert not server._evidence_slots.locked()
        await server.aclose()
    asyncio.run(scenario())


def test_oversized_fallback_omission_does_not_shadow_provider_capture():
    import threading
    from api_agent.workflows.inference_capture import select_capture_rows
    projection = EmissionBoundary(ContentPolicy(True))
    rows = [dict(phase='request', content={'text': 'x' * MAX_EVIDENCE_BYTES}, reason='captured', _source='fallback'),
            dict(phase='request', content={'text': 'provider value'}, reason='captured')]
    frames = SetupServer._prepare_cancellable(rows, threading.Event(), projection, True)
    decoded = [json.loads(frame) for _, _, frame in frames]
    assert len(decoded) == 1
    assert decoded[0]['content'] == {'text': 'provider value'}
    assert decoded[0]['reason'] == 'captured'


def test_cancelled_spawn_kills_descendant_after_leader_exits(monkeypatch):
    import os
    import sys

    async def scenario():
        original = asyncio.create_subprocess_exec
        entered = asyncio.Event()
        release = asyncio.Event()
        children = []

        async def delayed(executable, *_args, **kwargs):
            code = ('import subprocess,sys; '
                    'subprocess.Popen([sys.executable,"-c","import time; time.sleep(30)"]); '
                    'sys.exit(0)')
            process = await original(executable, '-c', code, **kwargs)
            children.append(process)
            while process.returncode is None:
                await asyncio.sleep(.01)
            entered.set()
            await release.wait()
            return process

        monkeypatch.setattr(asyncio, 'create_subprocess_exec', delayed)
        projection = EmissionBoundary(ContentPolicy(True))
        task = asyncio.create_task(SetupServer._prepare_isolated([], projection))
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        release.set()
        try:
            await asyncio.wait_for(task, 5)
        except asyncio.CancelledError:
            pass
        assert children[0].returncode == 0
        try:
            os.killpg(children[0].pid, 0)
        except ProcessLookupError:
            pass
        else:
            # A killed descendant may briefly remain as an unreaped zombie;
            # its held stdout pipe must nevertheless be closed.
            assert await asyncio.wait_for(children[0].stdout.read(), 2) == b''
    asyncio.run(scenario())
