"""Managed static Allure 2 reports with immutable per-run and preview routes."""
import asyncio
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import partial
import hashlib
import html
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
from urllib.parse import quote

from api_agent.providers.control import finish_cleanup
from api_agent.providers.sessions import InspectionOwner
from api_agent.providers.progress import component_timeout, report_timeout
from api_agent.runtime.managed_browser import managed_browser
from api_agent.runtime.tasks import offload
from .allure_source import (PreviewSnapshot, RunSource, ViewerError, validate_manifest,
                            verified_files)
from .processes import finalize


@dataclass(frozen=True)
class ViewerLocation:
    url: str
    inspection_endpoint: str | None = None
    browser_error: str | None = None


class _Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, owner, **kwargs):
        self.owner = owner
        super().__init__(*args, **kwargs)

    def do_GET(self):
        if self.headers.get('Host') != f'127.0.0.1:{self.owner.port}':
            self.send_error(403)
            return
        binding = self.owner._history_routes.get(self.path)
        if binding is None:
            super().do_GET()
            return
        future = asyncio.run_coroutine_threadsafe(self.owner._history_report(*binding), self.owner._loop)
        try:
            url = future.result(timeout=self.owner.timeout + 5)
        except FutureTimeout:
            future.cancel()
            self.send_error(504, 'Allure report generation timed out')
            return
        except Exception:
            self.send_error(503, 'Allure report unavailable; check the launcher')
            return
        self.send_response(302)
        self.send_header('Location', url)
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()

    def do_HEAD(self):
        if self.headers.get('Host') != f'127.0.0.1:{self.owner.port}':
            self.send_error(403)
            return
        super().do_HEAD()

    def log_message(self, *args):
        pass

    def list_directory(self, path):
        self.send_error(404)
        return None

    def setup(self):
        super().setup()
        self.connection.settimeout(2)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False


class ManagedAllureViewer:
    """One operation owns the listening socket, reports and headed browser.

    Existing bindings never change. Refresh adds new routes and rewrites only the
    history navigation index. Cleanup failures retain resources for aclose retry.
    """
    def __init__(self, port=5252, *, browser_factory=None, timeout=120):
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError('Allure viewer port must be between 1 and 65535')
        self.port = port
        self.timeout = timeout
        self._inspection = InspectionOwner()
        self.browser_factory = browser_factory or (lambda: managed_browser(headless=False, inspection=self._inspection))
        self._temporary = None
        self._server = self._thread = self._browser = None
        self._processes = []
        self._bindings = {}
        self._history_routes = {}
        self._loop = None
        self._http_tasks = set()
        self._operations = set()
        self._lock = asyncio.Lock()
        self._closed = False
        self._cli = None

    @asynccontextmanager
    async def _operation(self):
        if self._closed:
            raise ViewerError('Allure viewer is closed')
        task = asyncio.current_task()
        self._operations.add(task)
        try:
            yield
        finally:
            self._operations.discard(task)

    async def _command(self, arguments, *, version=False):
        async with report_timeout('Allure CLI version' if version else 'Allure report generation',
                                  15 if version else self.timeout) as budget:
            with tempfile.TemporaryFile() as output:
                try:
                    process = subprocess.Popen(arguments, stdout=output, stderr=output,
                                               start_new_session=os.name == 'posix')
                except OSError:
                    raise ViewerError('Allure could not start; install Allure 2 and check Java on PATH') from None
                self._processes.append(process)
                try:
                    await asyncio.to_thread(process.wait, timeout=budget.remaining)
                    if process.returncode != 0:
                        raise ViewerError('Allure failed; check the Allure 2 CLI and Java installation')
                    output.seek(0)
                    return output.read(4096).decode('utf-8', errors='replace').strip()
                except subprocess.TimeoutExpired:
                    raise ViewerError('Allure timed out; check Java and the viewer generation timeout') from None
                finally:
                    await finish_cleanup(asyncio.to_thread(finalize, process))
                    self._processes.remove(process)

    async def _ready(self):
        if self._closed:
            raise ViewerError('Allure viewer is closed')
        if self._cli is None:
            cli = shutil.which('allure')
            if cli is None:
                raise ViewerError('Allure 2 CLI is missing from PATH; install with brew install allure or see https://allurereport.org/docs/v2/install/')
            version = await self._command([cli, '--version'], version=True)
            if re.fullmatch(r'2\.\d+\.\d+(?:[-+][A-Za-z0-9.\-]+)?', version) is None:
                raise ViewerError('Unsupported Allure CLI; an externally installed Allure 2 version is required')
            self._cli = cli
        if self._temporary is None:
            self._temporary = tempfile.TemporaryDirectory(prefix='api-agent-allure-viewer-')
        if self._server is None:
            try:
                self._server = _Server(('127.0.0.1', self.port), partial(
                    _Handler, directory=self._temporary.name, owner=self))
            except OSError:
                raise ViewerError(f'Allure viewer port {self.port} is occupied; stop its owner or configure ALLURE_VIEWER_PORT') from None
            self._thread = threading.Thread(target=partial(self._server.serve_forever, poll_interval=.05), daemon=True)
            self._loop = asyncio.get_running_loop()
            self._thread.start()
        # Check our exact socket using a nonce file, never adopt another listener.
        marker = Path(self._temporary.name) / '.ready'
        marker.write_text(Path(self._temporary.name).name)
        import httpx
        async with component_timeout('Allure viewer readiness', 5):
            async with httpx.AsyncClient(trust_env=False) as client:
                response = await client.get(f'http://127.0.0.1:{self.port}/.ready')
                if response.status_code != 200 or response.text != marker.read_text():
                    raise ViewerError('Owned Allure report server did not become ready')

    async def _report(self, manifest, files, *, preview=False, result_file=None):
        digest = validate_manifest(manifest, preview=preview)
        if result_file is not None and (result_file not in files or not result_file.endswith('-result.json')):
            raise ViewerError('The exact selected Allure result is unavailable')
        selector = hashlib.sha256(result_file.encode()).hexdigest() if result_file else 'suite'
        route = f'runs/{manifest["run_id"]}/{digest}/{selector}'
        if route in self._bindings:
            return self._bindings[route]
        await self._ready()
        root = Path(self._temporary.name)
        with tempfile.TemporaryDirectory(prefix='input-', dir=root) as staging:
            for name, body in files.items():
                if result_file and name.endswith('-result.json') and name != result_file:
                    continue
                (Path(staging) / name).write_bytes(body)
            if 'environment.properties' not in files:
                (Path(staging) / 'environment.properties').write_text(
                    f'Run={manifest["run_id"]}\nPublication={"Live preview; suite accounting pending" if preview else "Finalized immutable run"}\n')
            # Generate outside the served tree; publish a route only after success.
            with tempfile.TemporaryDirectory(prefix='report-', dir=root) as generated:
                output = Path(generated) / 'report'
                await self._command([self._cli, 'generate', staging, '--output', str(output)])
                if not (output / 'index.html').is_file():
                    raise ViewerError('Allure generation produced no report')
                suffix = ''
                if result_file:
                    cases = list((output / 'data' / 'test-cases').glob('*.json'))
                    if len(cases) != 1:
                        raise ViewerError('Allure did not preserve the exact selected attempt')
                    try:
                        case = json.loads(cases[0].read_bytes())
                        uid = case['uid']
                        if not isinstance(uid, str) or re.fullmatch('[a-zA-Z0-9_-]+', uid) is None or cases[0].stem != uid:
                            raise ValueError()
                    except (ValueError, KeyError, TypeError):
                        raise ViewerError('Allure generated an invalid selected-result link') from None
                    suffix = '#testresult/' + quote(uid, safe='')
                destination = root / route
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(output), destination)
        url = f'http://127.0.0.1:{self.port}/{route}/index.html{suffix}'
        self._bindings[route] = url
        return url

    async def _open(self, url):
        try:
            async with component_timeout('Allure viewer browser', 60):
                if self._browser is None:
                    self._browser = self.browser_factory()
                session = await self._browser.start()
                await session.page.goto(url)
                return ViewerLocation(url, session.inspection_endpoint)
        except Exception:
            return ViewerLocation(url, browser_error='Could not open the managed browser; the report remains available at its URL')

    async def show_run(self, source, manifest, result_file=None):
        async with self._operation(), self._lock:
            async with component_timeout('Allure run viewer', self.timeout) as budget:
                manifest = json.loads(json.dumps(manifest))
                await self._ready()
                files = await offload(source.load, manifest, budget=budget)
                url = await self._report(manifest, files, result_file=result_file)
                return await self._open(url)

    async def show_preview(self, snapshot, result_file):
        async with self._operation(), self._lock:
            async with component_timeout('Allure selected-test viewer', self.timeout):
                manifest = json.loads(json.dumps(snapshot.manifest))
                validate_manifest(manifest, preview=snapshot.preview)
                files = verified_files(manifest, snapshot.files.__getitem__)
                url = await self._report(manifest, files, preview=snapshot.preview, result_file=result_file)
                return await self._open(url)

    async def show_history(self, source):
        async with self._operation(), self._lock:
            async with component_timeout('Allure history viewer', self.timeout) as budget:
                await self._ready()
                manifests = await offload(source.history, budget=budget)
                if not manifests:
                    raise ViewerError('No finalized integration or E2E Allure runs are available')
                links = []
                for manifest in manifests:
                    digest = validate_manifest(manifest, repo=source.repo)
                    route = f'/open/{manifest["run_id"]}/{digest}'
                    self._history_routes[route] = (source, json.loads(json.dumps(manifest)))
                    url = route
                    label = f'{manifest["finished_at"]} — {manifest["mode"]} — {manifest["status"]} — {manifest["run_id"]}'
                    links.append(f'<li><a href="{html.escape(url, quote=True)}">{html.escape(label)}</a></li>')
                index = Path(self._temporary.name) / 'index.html'
                temporary = index.with_suffix('.tmp')
                temporary.write_text('<!doctype html><meta charset="utf-8"><title>Allure run history</title>'
                                     '<h1>Allure run history</h1><p>Newest first. Each report preserves its run identity.</p><ul>'
                                     + ''.join(links) + '</ul>', encoding='utf-8')
                temporary.replace(index)
                return await self._open(f'http://127.0.0.1:{self.port}/index.html')

    async def _history_report(self, source, manifest):
        task = asyncio.current_task()
        self._http_tasks.add(task)
        try:
            async with self._operation(), self._lock:
                async with component_timeout('Allure history report', self.timeout) as budget:
                    files = await offload(source.load, manifest, budget=budget)
                    return await self._report(manifest, files)
        finally:
            self._http_tasks.discard(task)

    async def aclose(self):
        """Retry resources whose previous cleanup did not confirm disposal."""
        self._closed = True
        current = asyncio.current_task()
        for task in tuple((self._operations | self._http_tasks) - {current}):
            task.cancel()
        async with self._lock:
            async with report_timeout('Allure viewer cleanup', 15):
                tasks = tuple(task for task in self._http_tasks if task is not current)
                for task in tasks:
                    task.cancel()
                if tasks:
                    async def drain():
                        await asyncio.gather(*tasks, return_exceptions=True)
                    await finish_cleanup(drain())
                if self._browser is not None:
                    await finish_cleanup(self._browser.aclose())
                    self._browser = None
                await finish_cleanup(self._inspection.aclose())
                for process in tuple(self._processes):
                    await finish_cleanup(asyncio.to_thread(finalize, process))
                    self._processes.remove(process)
                if self._server is not None:
                    await finish_cleanup(asyncio.to_thread(self._server.shutdown))
                    self._server.server_close()
                    self._thread.join(timeout=2)
                    if self._thread.is_alive():
                        raise ViewerError('Allure server cleanup remains pending')
                    self._server = self._thread = None
                if self._temporary is not None:
                    self._temporary.cleanup()
                    self._temporary = None
