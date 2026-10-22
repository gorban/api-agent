"""Viewer owns browser and host disposal when a selected view closes."""
import asyncio
import os
import signal
import subprocess
import sys
import time
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

from api_agent.providers.control import CleanupPendingError
from api_agent.providers.sessions import InspectionOwner
from api_agent.runtime import view_flow as viewer
from api_agent.workflows import viewer_projection, viewer_render
from api_agent.workflows.viewer_render import ViewBundle, ViewFile, ViewRoute, ViewSource

ROOT = Path(__file__).resolve().parents[2]


def test_selected_view_uses_original_page_and_retries_browser_cleanup(tmp_path, monkeypatch):
    entry = tmp_path / 'flow.md.html'
    entry.write_text('# sample\n')
    definition = SimpleNamespace(entry=entry, entry_root='base', roots={'base': tmp_path},
                                 replacements=(object(),), definition_id='selected-id')
    raw = b'# sample\n'
    projection = SimpleNamespace(graph_id='graph', definition_id='selected-id',
                                 projection_sha256='projection', entry_path='base/flow.md.html',
                                 documents=(SimpleNamespace(logical_path='base/flow.md.html', raw_bytes=raw),),
                                 resources=())
    captured = []
    monkeypatch.setattr(viewer, 'load_selected_definition', lambda context: definition)
    monkeypatch.setattr(viewer_projection, 'project_view', lambda selected: projection)

    def bundle(selected, *, roots, verify, **kwargs):
        captured.append((selected, roots, verify))
        file = ViewFile('r/base/flow.md.html', 'text/html; charset=utf-8', b'<p>view</p>')
        return ViewBundle(file.path, (file,), 'graph', 'selected-id', 'projection',
                          (ViewSource('base/flow.md.html', 'declaration', sha256(raw).hexdigest(), len(raw), True),),
                          (ViewRoute(file.path, file.content_type, sha256(file.body).hexdigest(), len(file.body)),))
    monkeypatch.setattr(viewer_render, 'build_view_bundle', bundle)
    monkeypatch.setattr(viewer_render, 'verify_bundle_proof', lambda bundle, projection, **kwargs: None)

    class Page:
        async def goto(self, url, *, wait_until):
            reader, writer = await asyncio.open_connection('127.0.0.1', int(url.split(':')[2].split('/')[0]))
            writer.write(('GET /r/base/flow.md.html HTTP/1.1\r\nHost: '
                          + url.split('/')[2] + '\r\n\r\n').encode())
            await writer.drain()
            data = await reader.read()
            writer.close()
            await writer.wait_closed()
            assert b'<p>view</p>' in data
            assert wait_until == 'load'
            return SimpleNamespace(status=200)

    class Browser:
        closes = 0
        async def start(self):
            return SimpleNamespace(page=Page(), inspection_endpoint='http://127.0.0.1:12345')
        async def aclose(self):
            self.closes += 1
            if self.closes == 1:
                raise CleanupPendingError((self,), self.aclose)

    class Inspection:
        settled = False
        async def wait_closed(self):
            return None
        async def aclose(self):
            raise CleanupPendingError((self,), self.aclose)
        async def settle_terminated_inspection(self):
            self.settled = True
            return True

    browser = Browser()
    monkeypatch.setattr(viewer, 'managed_browser', lambda **_: browser)
    inspection = Inspection()
    monkeypatch.setattr(viewer, 'InspectionOwner', lambda: inspection)
    asyncio.run(viewer.view_flow(SimpleNamespace()))
    assert browser.closes == 2
    assert inspection.settled
    assert captured == [(projection, definition.roots, False)]


def test_pending_disposal_retains_original_loop_and_host_until_confirmed(tmp_path, monkeypatch):
    entry = tmp_path / 'flow.md.html'
    definition = SimpleNamespace(entry=entry, entry_root='base', roots={'base': tmp_path},
                                 replacements=(object(),), definition_id='selected-id')
    projection = SimpleNamespace(documents=(SimpleNamespace(logical_path='base/flow.md.html'),))
    monkeypatch.setattr(viewer, 'load_selected_definition', lambda context: definition)
    monkeypatch.setattr(viewer_projection, 'project_view', lambda selected: projection)
    monkeypatch.setattr(viewer_render, 'build_view_bundle', lambda *a, **kw: object())

    class Host:
        entry_url = 'http://127.0.0.1:1/r/base/flow.md.html'
        closed = False
        async def aclose(self):
            self.closed = True

    class Page:
        async def goto(self, *args, **kwargs):
            return SimpleNamespace(status=200)

    class Browser:
        def __init__(self):
            self.attempts = 0
            self.owner_loop = None
            self.release = False
            self.four_attempts = asyncio.Event()
        async def start(self):
            self.owner_loop = asyncio.get_running_loop()
            return SimpleNamespace(page=Page(), inspection_endpoint=None)
        async def aclose(self):
            self.attempts += 1
            if self.attempts >= 4:
                self.four_attempts.set()
            if not self.release:
                raise CleanupPendingError((self,), self.aclose)

    class Inspection:
        async def wait_closed(self):
            return None
        async def aclose(self):
            return None

    host = Host()
    browser = Browser()
    async def start_host(bundle, *, projection, **kwargs):
        return host
    monkeypatch.setattr(viewer, 'start_viewer_host', start_host)
    monkeypatch.setattr(viewer, 'managed_browser', lambda **_: browser)
    monkeypatch.setattr(viewer, 'InspectionOwner', Inspection)

    async def exercise():
        task = asyncio.create_task(viewer.view_flow(SimpleNamespace()))
        await asyncio.wait_for(browser.four_attempts.wait(), timeout=5)
        assert not task.done()
        assert browser.owner_loop is asyncio.get_running_loop()
        assert not host.closed
        browser.release = True
        await asyncio.wait_for(task, timeout=5)
        assert host.closed
        assert browser.attempts >= 5
    asyncio.run(exercise())


def test_run_view_flow_sigint_during_entered_renderer_returns_130(tmp_path):
    jar = tmp_path / 'fake.jar'
    jar.write_bytes(b'fake-renderer')
    pid_file = tmp_path / 'renderer.pid'
    java = tmp_path / 'fake-java'
    java.write_text('#!/usr/bin/env python3\nimport os,time\n'
                    f'open({str(pid_file)!r},"w").write(str(os.getpid()))\n'
                    'time.sleep(60)\n')
    java.chmod(0o755)
    script = (
        'import sys; from pathlib import Path; from types import SimpleNamespace\n'
        f'sys.path.insert(0,{str(ROOT / "src")!r})\n'
        'from api_agent.runtime import view_flow as flow\n'
        'from api_agent.workflows import viewer_projection, viewer_render\n'
        'from hashlib import sha256\n'
        'viewer_render.PLANTUML_SHA256=sha256(b"fake-renderer").hexdigest()\n'
        f'flow.load_selected_definition=lambda context: SimpleNamespace(entry=Path({str(tmp_path / "flow.md.html")!r}),entry_root="base",roots={{"base":Path({str(tmp_path)!r})}},replacements=(object(),))\n'
        'viewer_projection.project_view=lambda definition: SimpleNamespace(documents=())\n'
        f'viewer_render.build_view_bundle=lambda *a,**kw: viewer_render.PlantUMLRenderer(Path({str(jar)!r}),java={str(java)!r},timeout=20).render(b"@startuml\\nA-->B\\n@enduml\\n")\n'
        'raise SystemExit(flow.run_view_flow(None))\n')
    process = subprocess.Popen([sys.executable, '-I', '-c', script], cwd=tmp_path,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for _ in range(100):
            if pid_file.exists() or process.poll() is not None:
                break
            time.sleep(.05)
        assert pid_file.exists()
        process.send_signal(signal.SIGINT)
        out, err = process.communicate(timeout=5)
        assert process.returncode == 130, (out, err)
        assert b'RuntimeError' not in err
        state = subprocess.run(['ps', '-o', 'stat=', '-p', pid_file.read_text()],
                               capture_output=True, text=True, timeout=2).stdout.strip()
        assert not state or state.startswith('Z')
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_navigation_failure_disposes_browser_and_host(tmp_path, monkeypatch):
    definition = SimpleNamespace(entry=tmp_path / 'flow.md.html', entry_root='base', roots={'base': tmp_path},
                                 replacements=(object(),), definition_id='selected-id')
    projection = SimpleNamespace(documents=(SimpleNamespace(logical_path='base/flow.md.html'),))
    monkeypatch.setattr(viewer, 'load_selected_definition', lambda context: definition)
    monkeypatch.setattr(viewer_projection, 'project_view', lambda selected: projection)
    monkeypatch.setattr(viewer_render, 'build_view_bundle', lambda *a, **kw: object())
    closed = []

    class Host:
        entry_url = 'http://127.0.0.1:1/r/base/flow.md.html'
        async def aclose(self):
            closed.append('host')

    class Page:
        async def goto(self, *args, **kwargs):
            return SimpleNamespace(status=404)

    class Browser:
        async def start(self):
            return SimpleNamespace(page=Page(), inspection_endpoint=None)
        async def aclose(self):
            closed.append('browser')

    class Inspection:
        async def aclose(self):
            closed.append('inspection')

    async def start_host(bundle, *, projection, **kwargs):
        return Host()

    monkeypatch.setattr(viewer, 'start_viewer_host', start_host)
    monkeypatch.setattr(viewer, 'managed_browser', lambda **_: Browser())
    monkeypatch.setattr(viewer, 'InspectionOwner', Inspection)
    import pytest
    with pytest.raises(RuntimeError, match='entry did not load'):
        asyncio.run(viewer.view_flow(SimpleNamespace()))
    assert closed == ['browser', 'inspection', 'host']


def test_run_view_flow_reports_interrupt_without_success(monkeypatch):
    async def operation(prepared):
        return None
    monkeypatch.setattr(viewer, '_prepare_view', lambda context: 'prepared')
    monkeypatch.setattr(viewer, '_serve_view', operation)
    assert viewer.run_view_flow(SimpleNamespace()) == 0

    def interrupted(coroutine):
        coroutine.close()
        raise KeyboardInterrupt

    monkeypatch.setattr(viewer.asyncio, 'run', interrupted)
    assert viewer.run_view_flow(SimpleNamespace()) == 130


def test_terminated_inspection_requires_failed_exact_stack_and_terminal_proof():
    class Context:
        async def close(self):
            raise ConnectionError('driver pipe is gone')

    class Browser:
        async def close(self):
            raise AssertionError('unreached')

    class Driver:
        terminal = False
        async def stop(self):
            raise AssertionError('unreached')
        async def terminated_inspection(self, session):
            return self.terminal

    async def exercise():
        inspection = InspectionOwner()
        context, browser, driver = Context(), Browser(), Driver()
        session = SimpleNamespace(context=context, browser=browser)
        inspection.observe(session)
        inspection.retain([(driver, 'stop'), (browser, 'close'), (context, 'close')])
        assert not await inspection.settle_terminated_inspection()
        with pytest.raises(CleanupPendingError):
            await inspection.aclose()
        assert not await inspection.settle_terminated_inspection()
        assert len(inspection._resources) == 3
        failed_close = inspection._closing
        other_failure = asyncio.get_running_loop().create_future()
        other_failure.set_exception(RuntimeError('unclassified close failure'))
        inspection._closing = other_failure
        assert not await inspection.settle_terminated_inspection()
        inspection._closing = failed_close
        other_failure.exception()
        driver.terminal = True
        class HostileEqual:
            def __eq__(self, other):
                return True

        inspection._resources[1] = (HostileEqual(), 'close')
        assert not await inspection.settle_terminated_inspection()
        assert len(inspection._resources) == 3
        inspection._resources.append((object(), 'close'))
        assert not await inspection.settle_terminated_inspection()
        inspection._resources.pop()
        inspection._resources[1] = (browser, 'close')
        assert await inspection.settle_terminated_inspection()
        assert not inspection._resources and not inspection.sessions

    import pytest
    asyncio.run(exercise())
