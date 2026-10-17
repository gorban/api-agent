"""Managed static Allure 2 reports with immutable per-run and preview routes."""
import asyncio
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from functools import partial
import hashlib
import html
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import shlex
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
from .storage import RunStorage

HISTORY_LAUNCHES = 20
HISTORY_FILES = ('history.json', 'history-trend.json', 'duration-trend.json',
                 'retry-trend.json', 'categories-trend.json')


def _history_result(body, manifest):
    """Upgrade our legacy retry parameter in derived staging, never in storage."""
    ambiguous = False
    def members(pairs):
        nonlocal ambiguous
        value = dict(pairs)
        if len(value) != len(pairs):
            ambiguous = True
        return value
    row = json.loads(body, object_pairs_hook=members)
    if ambiguous:
        return body
    full_name = row.get('fullName')
    if not isinstance(full_name, str) or not full_name:
        return body
    file, separator, node = full_name.partition('::')
    if not file or not separator or not node:
        return body
    head, bracket, parameters_text = node.partition('[')
    expected_name = head.rsplit('::', 1)[-1] + bracket + parameters_text
    expected_labels = [{'name': 'parentSuite', 'value': manifest['repo']},
                       {'name': 'suite', 'value': manifest['mode']},
                       {'name': 'subSuite', 'value': Path(file).name}]
    if row.get('name') != expected_name or row.get('labels') != expected_labels:
        return body
    expected = hashlib.sha256(
        f'{manifest["repo"]}:{manifest["mode"]}:{full_name}'.encode()).hexdigest()
    parameters = row.get('parameters')
    if (row.get('historyId') != expected or not isinstance(parameters, list)
            or len(parameters) != 1):
        return body
    parameter = parameters[0]
    if (not isinstance(parameter, dict) or set(parameter) != {'name', 'value'}
            or parameter['name'] != 'attempt' or not isinstance(parameter['value'], str)
            or re.fullmatch('[1-9][0-9]*', parameter['value']) is None):
        return body
    parameter['excluded'] = True
    return json.dumps(row).encode()


@dataclass(frozen=True)
class ViewerLocation:
    url: str
    inspection_endpoint: str | None = None
    browser_error: str | None = None
    archive_url: str | None = None


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
    def __init__(self, port=5252, *, browser_factory=None, timeout=120, cli='allure'):
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError('Allure viewer port must be between 1 and 65535')
        self.port = port
        self.timeout = timeout
        self.cli = cli
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
                        raise ViewerError(f'Allure command failed (exit {process.returncode}); check ALLURE_CLI, its host PATH and Java installation')
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
            try:
                if not isinstance(self.cli, str):
                    raise ValueError()
                arguments = shlex.split(self.cli)
                if not arguments or any('\0' in argument for argument in arguments):
                    raise ValueError()
            except (TypeError, ValueError):
                raise ViewerError('Invalid ALLURE_CLI; provide a command and quote arguments containing spaces') from None
            executable = shutil.which(arguments[0])
            if executable is None:
                raise ViewerError('Allure 2 CLI command is missing from PATH; configure ALLURE_CLI or install with brew install allure; see https://allurereport.org/docs/v2/install/')
            arguments[0] = executable
            version = await self._command([*arguments, '--version'], version=True)
            if re.fullmatch(r'2\.\d+\.\d+(?:[-+][A-Za-z0-9.\-]+)?', version) is None:
                raise ViewerError('Unsupported Allure CLI; an externally installed Allure 2 version is required')
            self._cli = tuple(arguments)
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

    async def _report(self, manifest, files, *, preview=False, result_file=None, history=None):
        digest = validate_manifest(manifest, preview=preview)
        if result_file is not None and (result_file not in files or not result_file.endswith('-result.json')):
            raise ViewerError('The exact selected Allure result is unavailable')
        selector = hashlib.sha256(result_file.encode()).hexdigest() if result_file else 'suite'
        if history is not None:
            context = {name: hashlib.sha256(body).hexdigest() for name, body in history.items()}
            selector += '-history-' + hashlib.sha256(json.dumps(context, sort_keys=True).encode()).hexdigest()
        route = f'runs/{manifest["run_id"]}/{digest}/{selector}'
        if route in self._bindings:
            return self._bindings[route]
        await self._ready()
        root = Path(self._temporary.name)
        url = f'http://127.0.0.1:{self.port}/{route}/index.html'
        with tempfile.TemporaryDirectory(prefix='input-', dir=root) as staging:
            for name, body in files.items():
                if result_file and name.endswith('-result.json') and name != result_file:
                    continue
                if history is not None and name.endswith('-result.json'):
                    body = _history_result(body, manifest)
                (Path(staging) / name).write_bytes(body)
            if 'environment.properties' not in files:
                (Path(staging) / 'environment.properties').write_text(
                    f'Run={manifest["run_id"]}\nPublication={"Live preview; suite accounting pending" if preview else "Finalized immutable run"}\n')
            if history is not None:
                if 'history' in files:
                    raise ViewerError('Allure artifact conflicts with derived history staging')
                directory = Path(staging) / 'history'
                directory.mkdir()
                for name, body in history.items():
                    (directory / name).write_bytes(body)
                if 'executor.json' not in files:
                    (Path(staging) / 'executor.json').write_text(json.dumps({
                        'name': 'Managed local viewer', 'buildName': 'Browse previous runs',
                        'buildUrl': f'http://127.0.0.1:{self.port}/archive.html',
                        'reportName': manifest['mode'] + ' — ' + manifest['finished_at'],
                        'buildOrder': int(datetime.fromisoformat(
                            manifest['finished_at'].replace('Z', '+00:00')).timestamp() * 1000),
                        'reportUrl': url}))
            # Generate outside the served tree; publish a route only after success.
            with tempfile.TemporaryDirectory(prefix='report-', dir=root) as generated:
                output = Path(generated) / 'report'
                await self._command([*self._cli, 'generate', staging, '--output', str(output)])
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
        url += suffix
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
                manifests = json.loads(json.dumps(manifests))
                digests = {row['run_id']: validate_manifest(row, repo=source.repo) for row in manifests}
                windows = {}
                recent = {}
                for manifest in reversed(manifests):
                    previous = recent.get(manifest['mode'], ())
                    windows[manifest['run_id']] = previous
                    recent[manifest['mode']] = (manifest, *previous)[:HISTORY_LAUNCHES - 1]
                links = []
                for manifest in manifests:
                    digest = digests[manifest['run_id']]
                    previous = windows[manifest['run_id']]
                    context = hashlib.sha256(json.dumps([
                        digests[row['run_id']] for row in previous]).encode()).hexdigest()
                    route = f'/open/{manifest["run_id"]}/{digest}/{context}'
                    self._history_routes.setdefault(route, (source, manifest, previous))
                    url = route
                    label = f'{manifest["finished_at"]} — {manifest["mode"]} — {manifest["status"]} — {manifest["run_id"]}'
                    links.append(f'<li><a href="{html.escape(url, quote=True)}">{html.escape(label)}</a></li>')
                index = Path(self._temporary.name) / 'archive.html'
                temporary = index.with_suffix('.tmp')
                temporary.write_text('<!doctype html><meta charset="utf-8"><title>Allure run archive</title>'
                                     '<h1>Allure run archive</h1><p>Newest first. Native history shows up to 20 launches of the same mode; older reports remain available here.</p><ul>'
                                     + ''.join(links) + '</ul>', encoding='utf-8')
                temporary.replace(index)
                manifest = manifests[0]
                url = await self._historical_report(source, manifest, windows[manifest['run_id']], budget)
                location = await self._open(url)
                return ViewerLocation(location.url, location.inspection_endpoint, location.browser_error,
                                      f'http://127.0.0.1:{self.port}/archive.html')

    def _generated_history(self, url):
        from .allure_source import MAX_ARTIFACT, MAX_RUN
        directory = Path(self._temporary.name) / url.split(f':{self.port}/', 1)[1].split('/index.html')[0] / 'history'
        if directory.is_symlink():
            raise ViewerError('Invalid generated Allure history')
        files = {}
        size = 0
        for name in HISTORY_FILES:
            path = directory / name
            if path.exists() or path.is_symlink():
                try:
                    body = RunStorage._read_source(path, max_bytes=MAX_ARTIFACT)
                except Exception:
                    raise ViewerError('Invalid generated Allure history') from None
                size += len(body)
                if len(body) > MAX_ARTIFACT or size > MAX_RUN:
                    raise ViewerError('Generated Allure history exceeds transfer limit')
                files[name] = body
        return files

    async def _historical_report(self, source, manifest, previous, budget):
        history = {}
        for item in [*reversed(previous), manifest]:
            files = await offload(source.load, item, budget=budget)
            url = await self._report(item, files, history=history)
            history = await offload(self._generated_history, url)
        return url

    async def _history_report(self, source, manifest, previous=()):
        task = asyncio.current_task()
        self._http_tasks.add(task)
        try:
            async with self._operation(), self._lock:
                async with component_timeout('Allure history report', self.timeout) as budget:
                    return await self._historical_report(source, manifest, previous, budget)
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
