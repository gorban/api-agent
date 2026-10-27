"""Native CLI rejection survives visible input and ordinary setup serialization."""
import asyncio
import os
import sys
import threading
from types import SimpleNamespace

import pytest
from textual.app import App
from textual.widgets import Input

from api_agent.llm_providers.bedrock.login_interaction import LoginInteraction
from api_agent.llm_providers.bedrock.login_transport import LoginTransport
from api_agent.providers.contracts import ProviderAccessError
from api_agent.providers.setup_transport import _error, _raise_error
from api_agent.tui.manager_recovery import ManagerRecoveryScreen, TUIInteraction


_CODE = 'c3ludGhldGljLWNvZGUtY2FuYXJ5LTk0Mjc='
_STATE = 'synthetic-state-canary-3186'
_EXPECTED_STATE = 'synthetic-expected-canary-5274'
_URL = 'https://synthetic.invalid/signin?secret=synthetic-url-canary-6832'
_FAILURES = [
    ('Failed to decode the verification code.', 'invalid_request', 'invalid_code'),
    ('Failed to retrieve an auth_code or state from the verification code', 'invalid_request', 'invalid_code'),
    ('Error loading or redeeming a login authorization code: State parameter '
     + _STATE + ' does not match expected value ' + _EXPECTED_STATE + '.',
     'authentication', 'state_mismatch'),
    ("Error loading or redeeming a login authorization code: Unable to complete the login process due to an expired authorization code. Please reauthenticate using 'aws login'.",
     'authentication', 'expired_code'),
    ('Unrecognized synthetic failure ' + _STATE + ' ' + _URL, 'unknown', 'unknown'),
]


class RejectionApp(App):
    def __init__(self):
        super().__init__()
        self.interaction = TUIInteraction(self, threading.Event())


def synthetic_worker(output, status):
    # The native writer prefixes errors; split prefix/body across flushed writes.
    chunks = ('\naws: [ERR', 'OR]: ', output.strip(), '\n')
    command = [sys.executable, '-c',
               "import sys, time; print('Enter code:', flush=True); "
               "sys.stdin.buffer.readline(); "
               f"[(sys.stdout.write(chunk), sys.stdout.flush(), time.sleep(0.01)) for chunk in {chunks!r}]; sys.exit({status})"]
    return f'''import os
from api_agent.llm_providers.bedrock import login_worker as worker
def run(config):
    worker.diagnosed('sign_in', worker.cli, {command!r}, dict(os.environ), interactive=True)
    worker.emit({{'auth_done': True}})
    worker.emit({{'result': {{'accepted': True}}}})
worker.run = run
worker.main()
'''


@pytest.mark.asyncio
@pytest.mark.parametrize('output,kind,reason', _FAILURES)
async def test_native_rejection_reaches_setup_with_safe_fixed_reason(monkeypatch, output, kind, reason):
    with pytest.raises(ProviderAccessError) as native:
        await native_handoff(monkeypatch, output, 1)

    frame = _error(native.value)
    with pytest.raises(ProviderAccessError) as setup:
        _raise_error(frame)

    assert setup.value.kind == kind
    assert setup.value.code == reason
    assert setup.value.execution == 'not_started'
    assert 'Stage: sign_in;' in str(setup.value)
    guidance = {
        'invalid_code': 'Copy the complete authorization code',
        'state_mismatch': 'belongs to a different sign-in attempt',
        'expired_code': 'Restart sign-in when ready',
    }
    if reason in guidance:
        assert guidance[reason] in str(setup.value)
    for canary in (_CODE, _STATE, _EXPECTED_STATE, _URL):
        assert canary not in repr(frame)
        assert canary not in str(native.value)
        assert canary not in str(setup.value)


@pytest.mark.asyncio
@pytest.mark.parametrize('output,kind,reason', _FAILURES[:4])
async def test_successful_cli_marker_output_does_not_establish_rejection(monkeypatch, output, kind, reason):
    assert await native_handoff(monkeypatch, output, 0) == {'accepted': True}


async def native_handoff(monkeypatch, output, status):
    launch = asyncio.create_subprocess_exec
    children = []

    async def launch_synthetic_worker(*command, **kwargs):
        assert command[0] == sys.executable
        process = await launch(sys.executable, '-c', synthetic_worker(output, status), **kwargs)
        children.append(process)
        return process

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', launch_synthetic_worker)
    app = RejectionApp()
    interaction = LoginInteraction(SimpleNamespace(interactive=True, interaction=app.interaction))
    transport = LoginTransport(timeout=5)
    async with app.run_test(size=(85, 30)) as pilot:
        try:
            await interaction.open()
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, ManagerRecoveryScreen)
            screen.query_one('#manager-input', Input).value = _CODE + '\r\n'
            await pilot.press('enter')
            return await transport.acquire(
                {'method': 'direct', 'login': True},
                {'PATH': os.defpath, 'PYTHONPATH': os.environ.get('PYTHONPATH', '')}, interaction)
        finally:
            await transport.aclose()
            await interaction.aclose()
            assert children and all(child.returncode is not None for child in children)
            assert not transport._processes
            assert not interaction._tasks
