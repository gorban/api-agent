"""Local native pipe failures retain fixed causes, never submitted values."""
import asyncio
import json
import os
import sys

import pytest

from api_agent.llm_providers.bedrock import login_worker as worker
from test_bedrock_login_worker import native_cli


@pytest.mark.parametrize('raw,reason', [
    (b'', 'input_closed'),
    (b'x' * (worker.MAX_FRAME + 1), 'input_limit'),
    (b'{\n', 'input_malformed'),
    (b'\xff\n', 'input_malformed'),
    (b'null\n', 'input_malformed'),
    (b'{"input":"\\ud800"}\n', 'input_malformed'),
    (b'{"input":"\\udfff"}\n', 'input_malformed'),
    (b'[]\n', 'input_malformed'),
    (b'{}\n', 'input_malformed'),
    (b'{"input":2}\n', 'input_malformed'),
    (b'{"input":"synthetic\\nvalue"}\n', 'input_multiline'),
    (b'{"input":"synthetic\\rvalue"}\n', 'input_multiline'),
])
def test_reply_failure_reports_exact_local_cause_and_reaps(native_cli, raw, reason):
    process, frames = native_cli([('reply', raw)], interactive=True)

    with pytest.raises(worker.AcquisitionFailure) as failure:
        worker.diagnosed('sign_in', worker.cli, ['synthetic'], {}, interactive=True)

    assert (failure.value.stage, failure.value.kind, failure.value.reason) == ('sign_in', 'invalid_request', reason)
    assert str(failure.value) == 'sign_in'
    assert frames == []
    assert process.poll() is not None
    assert process.stdin.closed and process.stdout.closed



@pytest.mark.parametrize('operation', ['write', 'flush'])
def test_native_pipe_failure_is_not_classified_as_invalid_input(native_cli, monkeypatch, operation):
    process, _ = native_cli([('reply', b'{"input":"synthetic-code"}\n')], interactive=True)
    def fail(*args):
        raise OSError('synthetic pipe failure')
    monkeypatch.setattr(process.stdin, operation, fail)

    with pytest.raises(worker.AcquisitionFailure) as failure:
        worker.diagnosed('sign_in', worker.cli, ['synthetic'], {}, interactive=True)

    assert (failure.value.kind, failure.value.reason) == ('unknown', 'unknown')
    assert process.poll() is not None
    assert process.stdin.closed and process.stdout.closed


def test_output_limit_reports_exact_cause_and_reaps(native_cli):
    process, frames = native_cli([('output', b'x' * (worker.MAX_OUTPUT + 1))])

    with pytest.raises(worker.AcquisitionFailure) as failure:
        worker.diagnosed('sign_in', worker.cli, ['synthetic'], {})

    assert failure.value.reason == 'output_limit'
    assert frames == []
    assert process.poll() is not None and process.stdout.closed


@pytest.mark.parametrize('reason,expected', [('input_closed', 'input_closed'), ('synthetic-secret', 'input_malformed')])
def test_protocol_exception_contains_only_fixed_evidence(reason, expected):
    failure = worker.CLIProtocolFailure(reason)

    assert vars(failure) == {'reason': expected}
    assert failure.args == ('Native command protocol failed',)


_SCRIPT = '''import os,sys
from api_agent.llm_providers.bedrock import login_worker as w
def run(config):
    child=[sys.executable,'-c',"import sys; print('prompt',flush=True); value=input(); print('accepted:'+str(len(value)),flush=True)"]
    output=w.diagnosed('sign_in',w.cli,child,dict(os.environ),interactive=True)
    w.emit({'result':output.decode()})
w.run=run
w.main()
'''


@pytest.mark.asyncio
@pytest.mark.parametrize('chunks', [
    [b'{}\n{"input":"synthetic-code"}\n'],
    [b'{}\n{"input":"synthetic', b'-code"}\n'],
])
async def test_real_child_accepts_coalesced_and_fragmented_frames(chunks):
    process = await asyncio.create_subprocess_exec(sys.executable, '-c', _SCRIPT,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, env=dict(os.environ))
    try:
        async with asyncio.timeout(5):
            for chunk in chunks:
                process.stdin.write(chunk)
                await process.stdin.drain()
                await asyncio.sleep(0.01)
            frames = []
            while True:
                raw = await process.stdout.readline()
                if not raw:
                    break
                frames.append(json.loads(raw))
            assert await process.wait() == 0
        assert frames[-1] == {'result': 'prompt\naccepted:14\n'}
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()
