import asyncio
from types import SimpleNamespace as NS

import pytest

from api_agent.runtime.local_terminal import format_log, run_local_terminal


def test_log_selection_preserves_text_and_labels_each_line():
    event = NS(source='ui', text='first\nsecond\n')
    assert format_log(event, 'agent') == ''
    assert format_log(event, 'ui') == 'first\nsecond\n'
    assert format_log(event, 'all') == '[ui] first\n[ui] second\n'


@pytest.mark.parametrize('failure,expected', [(None, 7), (ValueError('failed'), 1), (asyncio.CancelledError(), 130)])
def test_terminal_owns_same_loop_and_disposes_on_every_outcome(failure, expected):
    actions, output, diagnostics = [], [], []
    class Owner:
        pending_cleanup = None
        def diagnostic(self, error): return str(error)
        def __init__(self, context, resolve, *, argv, on_event):
            self.event = on_event
        async def start(self):
            actions.append(('start', asyncio.get_running_loop()))
            self.event(NS(kind='status', snapshot=None))
            self.event(NS(kind='log', source='agent', text='ready\n'))
        async def wait(self):
            actions.append(('wait', asyncio.get_running_loop()))
            if failure is not None:
                raise failure
            return 7
        def request_stop(self):
            actions.append(('stop', asyncio.get_running_loop()))
        async def aclose(self):
            actions.append(('close', asyncio.get_running_loop()))
    status = asyncio.run(run_local_terminal(None, lambda: NS(values={'RUN_LOG_SOURCE': 'all'}),
        owner_factory=Owner, output=output.append, diagnostic=diagnostics.append))
    assert status == expected
    assert output == ['[agent] ready\n']
    assert actions[-1][0] == 'close'
    assert len({loop for _, loop in actions}) == 1
    assert ('stop' in [name for name, _ in actions]) == (expected == 130)
    assert diagnostics == (['failed'] if expected == 1 else [])


def test_terminal_retains_cleanup_failure():
    from api_agent.providers.control import CleanupPendingError
    async def retry(): pass
    pending = CleanupPendingError(('owner',), retry)
    class Owner:
        pending_cleanup = pending
        def diagnostic(self, error): return str(error)
        def __init__(self, *args, **kwargs): pass
        async def start(self): pass
        async def wait(self): return 0
        async def aclose(self): raise RuntimeError('pending')
    with pytest.raises(CleanupPendingError, match='cleanup pending') as caught:
        asyncio.run(run_local_terminal(None, lambda: NS(values={'RUN_LOG_SOURCE': 'agent'}), owner_factory=Owner))
    assert caught.value.cleanup_pending is pending


def test_default_owner_factory_and_plain_cleanup_error(monkeypatch):
    import sys
    class Owner:
        pending_cleanup = None
        def diagnostic(self, error): return str(error)
        def __init__(self, *args, **kwargs): pass
        async def start(self): pass
        async def wait(self): return 0
        async def aclose(self): raise RuntimeError('dispose')
    monkeypatch.setitem(sys.modules, 'api_agent.runtime.local_run', NS(LocalRunOwner=Owner))
    with pytest.raises(RuntimeError, match='dispose'):
        asyncio.run(run_local_terminal(None, lambda: NS(values={'RUN_LOG_SOURCE': 'agent'})))


@pytest.mark.parametrize('inspection', [None, 'http://127.0.0.1:9001'])
def test_terminal_status_prints_verified_destinations_and_optional_inspection(inspection):
    from api_agent.runtime.local_contracts import LocalDestination, LocalEvent, LocalSnapshot
    diagnostics = []
    class Owner:
        pending_cleanup = None
        def diagnostic(self, error): return str(error)
        def __init__(self, *args, on_event, **kwargs): self.event = on_event
        async def start(self):
            self.event(LocalEvent('status', LocalSnapshot('ready', destinations=(LocalDestination('Agent', 'https://localhost/'),), inspection_endpoint=inspection)))
        async def wait(self): return 0
        async def aclose(self): pass
    asyncio.run(run_local_terminal(None, lambda: NS(values={'RUN_LOG_SOURCE': 'agent'}), owner_factory=Owner, diagnostic=diagnostics.append))
    assert diagnostics[:2] == ['Run local: ready', 'Agent: https://localhost/']
    assert diagnostics[2:] == ([f'Browser CDP: {inspection}'] if inspection else [])


@pytest.mark.parametrize('handoff,expected', [(None, 7), (5, 5)])
def test_launcher_dispatches_terminal_local_operation_without_stdin(monkeypatch, handoff, expected):
    import os
    from pathlib import Path
    from api_agent.runtime.bootstrap import main
    calls = []
    monkeypatch.setattr(os, 'environ', {})
    monkeypatch.setattr('api_agent.runtime.worktrees.verify_worktrees', lambda *a, **k: None)
    monkeypatch.setattr('api_agent.config.read_dotenv', lambda path: {})
    prepared_groups = []
    def prepare(*args, **kwargs):
        prepared_groups.append(kwargs['groups'])
        return handoff
    monkeypatch.setattr('api_agent.runtime.dependencies.ensure_environment', prepare)
    monkeypatch.setattr('builtins.input', lambda *a: pytest.fail('terminal local operation read stdin'))
    async def run(context, resolve, *, argv, schema):
        calls.append(resolve().values)
        assert resolve().values['RUN_LOG_SOURCE'] == 'all'
        return 7
    monkeypatch.setattr('api_agent.runtime.local_terminal.run_local_terminal', run)
    assert main(['--tui-mode=run-local', '--no-std-in', '--unattended-user-index=0'], base_root=Path(__file__).resolve().parents[2]) == expected
    assert len(calls) == (1 if handoff is None else 0)
    assert prepared_groups == [('browser',)]


@pytest.mark.parametrize('container', [False, True])
def test_host_browser_uses_forwarder_only_inside_container(monkeypatch, container):
    import os
    import shutil
    import subprocess
    from api_agent.runtime.local_terminal import open_host_url
    calls = []
    monkeypatch.setattr(os, 'environ', {'DISTROBOX_ENTER_PATH': '/test'} if container else {'CONTAINER_ID': 'other-container'})
    monkeypatch.setattr(shutil, 'which', lambda name: '/usr/bin/distrobox-host-exec')
    monkeypatch.setattr(subprocess, 'run', lambda *args, **kwargs: calls.append((args, kwargs)))
    import webbrowser
    browser_calls = []
    monkeypatch.setattr(webbrowser, 'open', lambda *args, **kwargs: browser_calls.append((args, kwargs)) or True)
    asyncio.run(open_host_url('https://localhost/'))
    if container:
        assert calls[0][0][0] == ['/usr/bin/distrobox-host-exec', 'xdg-open', 'https://localhost/']
        assert calls[0][1]['timeout'] == 10
        assert calls[0][1]['check'] is True
        assert calls[0][1]['stdin'] == subprocess.DEVNULL
        assert browser_calls == []
    else:
        assert calls == []
        assert browser_calls == [(('https://localhost/',), {'new': 2})]


def test_missing_host_forwarder_is_actionable_without_spawning(monkeypatch):
    import os
    import shutil
    import subprocess
    from api_agent.runtime.local_terminal import open_host_url
    monkeypatch.setattr(os, 'environ', {'DISTROBOX_ENTER_PATH': '/test'})
    monkeypatch.setattr(shutil, 'which', lambda name: None)
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: pytest.fail('must not spawn'))
    with pytest.raises(RuntimeError, match='Host browser forwarding is unavailable'):
        asyncio.run(open_host_url('https://localhost/'))


def test_default_browser_failure_is_actionable(monkeypatch):
    import os
    import webbrowser
    from api_agent.runtime.local_terminal import open_host_url
    monkeypatch.setattr(os, 'environ', {})
    monkeypatch.setattr(webbrowser, 'open', lambda *a, **k: False)
    with pytest.raises(RuntimeError, match='default browser could not open'):
        asyncio.run(open_host_url('https://localhost/'))


@pytest.mark.parametrize('failure,status', [(RuntimeError('synthetic-secret'), 1), (asyncio.CancelledError(), 130)])
def test_terminal_factory_failure_has_safe_fallback_and_no_owner_cleanup(failure, status):
    diagnostics = []
    def failed_factory(*args, **kwargs): raise failure
    assert asyncio.run(run_local_terminal(None, lambda: NS(values={'RUN_LOG_SOURCE': 'agent'}),
        owner_factory=failed_factory, diagnostic=diagnostics.append)) == status
    assert diagnostics == (['Local operation could not start.'] if status == 1 else [])


@pytest.mark.parametrize('failure', [KeyboardInterrupt('interrupted'), SystemExit(4)])
@pytest.mark.parametrize('retained', [False, True])
def test_terminal_cleanup_preserves_nonordinary_interruption(failure, retained):
    from api_agent.providers.control import CleanupPendingError
    async def retry(): pass
    pending = CleanupPendingError(('owner',), retry) if retained else None
    class Owner:
        pending_cleanup = pending
        def __init__(self, *args, **kwargs): pass
        async def start(self): pass
        async def wait(self): return 0
        async def aclose(self): raise failure
    with pytest.raises(type(failure)) as caught:
        asyncio.run(run_local_terminal(None, lambda: NS(values={'RUN_LOG_SOURCE': 'agent'}), owner_factory=Owner))
    assert caught.value is failure
    if retained:
        assert caught.value.cleanup_pending is pending


def test_clean_base_requirements_need_the_explicit_browser_extra():
    from pathlib import Path
    from packaging.requirements import Requirement
    from api_agent.runtime.dependencies import requirements
    root = Path(__file__).resolve().parents[2]
    context = NS(base_root=root, pack_root=root)
    base_names = {Requirement(raw).name for raw in requirements(context)}
    local_names = {Requirement(raw).name for raw in requirements(context, groups=('browser',))}
    assert 'playwright' not in base_names
    assert 'playwright-stealth' not in base_names
    assert {'playwright', 'playwright-stealth'} <= local_names
    assert base_names <= local_names
