"""Managed static Allure 2 reports with immutable per-run and preview routes."""
import asyncio
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from functools import partial
import hashlib
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

from api_agent.providers.control import CleanupPendingError, finish_cleanup
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
    history_error: str | None = None


class _Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, owner, **kwargs):
        self.owner = owner
        super().__init__(*args, **kwargs)

    def do_GET(self):
        if self.headers.get('Host') != f'127.0.0.1:{self.owner.port}':
            self.send_error(403)
            return
        with self.owner._publication_lock:
            summary = self.owner._summary_routes.get(self.path)
            binding = self.owner._history_routes.get(self.path)
        if binding is None and summary is None:
            super().do_GET()
            return
        work = self.owner._counts_report(*summary) if summary is not None else self.owner._history_report(*binding)
        timeout = self.owner.timeout if summary is not None else self.owner._history_timeout(binding[2], binding[3])
        future = asyncio.run_coroutine_threadsafe(work, self.owner._loop)
        try:
            url = future.result(timeout=timeout + 5)
        except FutureTimeout:
            future.cancel()
            self.send_error(504, 'Allure report generation timed out')
            return
        except Exception:
            self.send_error(503, 'Allure report unavailable; check the launcher')
            return
        if summary is not None:
            body = json.dumps({'label': url}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)
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
    # POSIX reuse permits rebinding after closed HTTP connections (TIME_WAIT),
    # without allowing adoption of an active listener. Preserve Windows non-reuse.
    allow_reuse_address = os.name == 'posix'


class ManagedAllureViewer:
    """One operation owns the listening socket, reports and headed browser.

    Existing bindings never change. Refresh adds new routes and rewrites only the
    history navigation index. Cleanup failures retain resources for aclose retry.
    """
    def __init__(self, port=5252, *, browser_factory=None, timeout=120, cli='allure', history_count=HISTORY_LAUNCHES):
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError('Allure viewer port must be between 1 and 65535')
        if type(history_count) is not int or history_count < 1:
            raise ValueError('Allure history display count must be a positive integer')
        self.port = port
        self.history_count = history_count
        self.timeout = timeout
        self.cli = cli
        self._inspection = InspectionOwner()
        self.browser_factory = browser_factory or (lambda: managed_browser(headless=False, inspection=self._inspection))
        self._temporary = None
        self._server = self._thread = self._browser = None
        self._processes = []
        self._bindings = {}
        self._history_routes = {}
        self._summary_routes = {}
        self._publication_lock = threading.RLock()
        self._loop = None
        self._http_tasks = set()
        self._operations = set()
        self._history_tasks = set()
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
            if self.cli == 'allure':
                from api_agent.runtime.mise_tools import ensure_tools
                cancellation = threading.Event()
                try:
                    await offload(partial(ensure_tools, 'java', 'allure', timeout=self.timeout,
                                          cancellation=cancellation), cancellation=cancellation)
                except (OSError, TimeoutError) as exc:
                    raise ViewerError('Unable to prepare Allure and Java') from exc
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

    async def _report(self, manifest, files, *, preview=False, result_file=None, history=None, archive=True):
        digest = validate_manifest(manifest, preview=preview)
        selected = None if result_file is None else ((result_file,) if isinstance(result_file, str) else tuple(sorted(result_file)))
        if selected is not None and (not selected or len(set(selected)) != len(selected) or any(
                name not in files or not name.endswith('-result.json') for name in selected)):
            raise ViewerError('The exact selected Allure result is unavailable')
        single = selected[0] if selected is not None and len(selected) == 1 else None
        selector = ('suite' if selected is None else
                    hashlib.sha256(single.encode()).hexdigest() if single else
                    'subset-' + hashlib.sha256(json.dumps(selected).encode()).hexdigest())
        if history is not None and self.history_count != HISTORY_LAUNCHES:
            from .allure_history_window import prepare_history
            history = await offload(prepare_history, history, self.history_count)
        if history is not None:
            context = {name: hashlib.sha256(body).hexdigest() for name, body in history.items()}
            selector += ('' if archive else '-no-archive') + '-history-' + hashlib.sha256(json.dumps(context, sort_keys=True).encode()).hexdigest()
        route = f'runs/{manifest["run_id"]}/{digest}/{selector}'
        if route in self._bindings:
            return self._bindings[route]
        await self._ready()
        root = Path(self._temporary.name)
        url = f'http://127.0.0.1:{self.port}/{route}/index.html'
        with tempfile.TemporaryDirectory(prefix='input-', dir=root) as staging:
            markers = []
            for name, body in files.items():
                if selected is not None and name.endswith('-result.json') and name not in selected:
                    continue
                original = body
                if history is not None and name.endswith('-result.json'):
                    body = _history_result(body, manifest)
                if selected is not None and name.endswith('-result.json'):
                    from .allure_selection import marked_result
                    body, marker = marked_result(name, body, original=original)
                    markers.append(marker)
                (Path(staging) / name).write_bytes(body)
            if 'environment.properties' not in files:
                (Path(staging) / 'environment.properties').write_text(
                    f'Run={manifest["run_id"]}\nPublication={"Live preview; suite accounting pending" if preview else "Finalized immutable run"}\n')
            if history is not None:
                recorded_at = manifest['started_at'] if preview else manifest['finished_at']
                if 'history' in files:
                    raise ViewerError('Allure artifact conflicts with derived history staging')
                directory = Path(staging) / 'history'
                directory.mkdir()
                for name, body in history.items():
                    (directory / name).write_bytes(body)
                if 'executor.json' not in files:
                    (Path(staging) / 'executor.json').write_text(json.dumps({
                        'name': 'Managed local viewer',
                        **({'buildName': 'All recorded runs',
                            'buildUrl': f'http://127.0.0.1:{self.port}/archive.html'} if archive else {}),
                        'reportName': manifest['mode'] + ' — ' + recorded_at,
                        'buildOrder': int(datetime.fromisoformat(
                            recorded_at.replace('Z', '+00:00')).timestamp() * 1000),
                        'reportUrl': url}))
            # Generate outside the served tree; publish a route only after success.
            with tempfile.TemporaryDirectory(prefix='report-', dir=root) as generated:
                output = Path(generated) / 'report'
                await self._command([*self._cli, 'generate', staging, '--output', str(output)])
                if not (output / 'index.html').is_file():
                    raise ViewerError('Allure generation produced no report')
                if history is not None and self.history_count > HISTORY_LAUNCHES:
                    from .allure_history_window import extend_history
                    await offload(extend_history, output, history, self.history_count)
                suffix = ''
                if selected is not None:
                    cases = list((output / 'data' / 'test-cases').glob('*.json'))
                    from .allure_selection import verify_selected_cases
                    verify_selected_cases(cases, markers)
                if single:
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

    async def show_preview(self, snapshot, result_file, *, source=None):
        async with self._operation(), self._lock:
            async with component_timeout('Allure selected-test viewer', self.timeout):
                from .allure_selection import PreviewSelection, selected_snapshot
                if isinstance(snapshot, PreviewSelection):
                    try:
                        snapshot = await offload(selected_snapshot, snapshot.selection)
                    except ValueError:
                        raise ViewerError('The exact selected Allure result is unavailable') from None
                manifest = json.loads(json.dumps(snapshot.manifest))
                validate_manifest(manifest, preview=snapshot.preview)
                files = verified_files(manifest, snapshot.files.__getitem__)
                history = None
                archive = error = None
                if source is not None:
                    try:
                        async with component_timeout('Allure prior test history', self.timeout / 2) as budget:
                            if self._history_tasks:
                                raise ViewerError('Prior Allure history is still settling')
                            await self._ready()
                            task = asyncio.create_task(self._selected_history(source, manifest, budget))
                            self._history_tasks.add(task)
                            task.add_done_callback(self._history_finished)
                            try:
                                history, available = await asyncio.shield(task)
                                if available:
                                    archive = f'http://127.0.0.1:{self.port}/archive.html'
                            finally:
                                if not task.done():
                                    task.cancel()
                    except CleanupPendingError:
                        raise
                    except Exception:
                        error = 'Prior Allure history is unavailable; showing this attempt without history.'
                url = await self._report(manifest, files, preview=snapshot.preview,
                                         result_file=result_file, history=history, archive=archive is not None)
                return replace(await self._open(url), archive_url=archive, history_error=error)

    def _history_finished(self, task):
        self._history_tasks.discard(task)
        if not task.cancelled():
            task.exception()

    async def _drain_history(self, budget):
        tasks = tuple(self._history_tasks)
        # The foreground requests cancellation before releasing the viewer lock.
        # Never cancel again while a synchronous read is draining.
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=budget.remaining)
            if pending:
                raise CleanupPendingError((self,), self.aclose)

    async def _selected_history(self, source, manifest, budget):
        source = source() if callable(source) else source
        if source.repo != manifest['repo']:
            raise ViewerError('Allure history repository does not match the selected test')
        manifests = json.loads(json.dumps(await offload(source.history, budget=budget)))
        if manifests:
            await self._archive(source, manifests, budget)
        cutoff = datetime.fromisoformat(manifest['started_at'].replace('Z', '+00:00'))
        previous = [row for row in manifests if row['mode'] == manifest['mode']
                    and row['run_id'] != manifest['run_id']
                    and datetime.fromisoformat(row['finished_at'].replace('Z', '+00:00')) <= cutoff]
        history = {}
        for row in reversed(previous[:self.history_count - 1]):
            files = await offload(source.load, row, budget=budget)
            url = await self._report(row, files, history=history)
            history = await offload(self._generated_history, url)
        return history, bool(manifests)

    async def show_history(self, source):
        async with self._operation(), self._lock:
            async with component_timeout('Allure history preparation', self.timeout) as budget:
                await self._ready()
                manifests = await offload(source.history, budget=budget)
                if not manifests:
                    raise ViewerError('No finalized integration or E2E Allure runs are available')
                manifests = json.loads(json.dumps(manifests))
                windows = await self._archive(source, manifests, budget)
                manifest = manifests[0]
                previous = windows[manifest['run_id']]
            async with component_timeout('Allure history viewer', self._history_timeout(previous, ())) as budget:
                url = await self._historical_report(source, manifest, previous, budget)
                location = await self._open(url)
                return ViewerLocation(location.url, location.inspection_endpoint, location.browser_error,
                                      f'http://127.0.0.1:{self.port}/archive.html')

    async def show_archive(self, source):
        async with self._operation(), self._lock:
            async with component_timeout('Allure run archive', self.timeout) as budget:
                await self._ready()
                manifests = json.loads(json.dumps(await offload(source.history, budget=budget)))
                if not manifests:
                    raise ViewerError('No finalized integration or E2E Allure runs are available')
                await self._archive(source, manifests, budget)
                return await self._open(f'http://127.0.0.1:{self.port}/archive.html')

    async def _archive(self, source, manifests, budget):
        from .allure_archive import checkpoint, prepare
        stop = threading.Event()
        with tempfile.TemporaryDirectory(prefix='api-agent-allure-index-') as staging:
            output = Path(staging) / 'archive.html'
            windows, routes, summaries = await offload(
                prepare, source, manifests, self.history_count, budget, stop, output,
                cancellation=stop)
            checkpoint(budget, stop)
            await offload(self._publish_archive, output, routes, summaries)
            return windows

    def _publish_archive(self, output, routes, summaries):
        from .allure_source import MAX_ARTIFACT
        body = RunStorage._read_source(output, max_bytes=MAX_ARTIFACT)
        index = Path(self._temporary.name) / 'archive.html'
        with self._publication_lock:
            history = {**self._history_routes, **routes}
            counts = {**self._summary_routes, **summaries}
            interruption = None
            try:
                output.replace(index)
            except BaseException as error:
                # Resolve an interrupted atomic replacement from its owned bytes.
                if (output.exists() or not index.exists()
                        or RunStorage._read_source(index, max_bytes=MAX_ARTIFACT) != body):
                    raise
                if not isinstance(error, OSError):
                    interruption = error
            self._history_routes, self._summary_routes = history, counts
            if interruption is not None:
                raise interruption

    async def _counts_report(self, source, manifest):
        from .allure_counts import count_label, result_counts
        task = asyncio.current_task()
        self._http_tasks.add(task)
        try:
            async with self._operation(), self._lock:
                async with component_timeout('Allure archive counts', self.timeout) as budget:
                    files = await offload(source.load, manifest, budget=budget, results_only=True)
                    rows = {name: _history_result(body, manifest) for name, body in files.items()}
                    return count_label(manifest['mode'], result_counts(manifest, rows))
        finally:
            self._http_tasks.discard(task)

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

    async def _historical_report(self, source, manifest, previous, budget, following=()):
        history = {}
        selected = None
        items = [*reversed(previous), manifest, *following]
        for item in items:
            files = await offload(source.load, item, budget=budget)
            url = await self._report(item, files, history=history)
            if item['run_id'] == manifest['run_id']:
                selected = url
            history = await offload(self._generated_history, url)
        if not following:
            return selected
        context = hashlib.sha256(json.dumps([validate_manifest(item) for item in items]).encode()).hexdigest()
        route = f'runs/{manifest["run_id"]}/{validate_manifest(manifest)}/surrounding-{context}'
        if route not in self._bindings:
            root = Path(self._temporary.name)
            source_path = root / selected.split(f':{self.port}/', 1)[1].split('/index.html')[0]
            latest_path = root / url.split(f':{self.port}/', 1)[1].split('/index.html')[0]
            destination = root / route
            def publish():
                from .allure_trends import surrounding_widgets
                with tempfile.TemporaryDirectory(prefix='surrounding-', dir=root) as staging:
                    target = Path(staging) / 'report'
                    shutil.copytree(source_path, target)
                    surrounding_widgets(source_path, latest_path, target, len(following), self.history_count)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    if not destination.exists():
                        shutil.move(str(target), destination)
            await offload(publish)
            self._bindings[route] = f'http://127.0.0.1:{self.port}/{route}/index.html'
        return self._bindings[route]

    def _history_timeout(self, previous, following):
        # Each batch of up to 20 actual reports gets one generation budget.
        # A large configured window with little available history stays bounded.
        batches = 1 + (len(previous) + len(following)) // HISTORY_LAUNCHES
        return self.timeout * batches

    async def _history_report(self, source, manifest, previous=(), following=()):
        task = asyncio.current_task()
        self._http_tasks.add(task)
        try:
            async with self._operation(), self._lock:
                async with component_timeout('Allure archived run', self._history_timeout(previous, following)) as budget:
                    return await self._historical_report(source, manifest, previous, budget, following)
        finally:
            self._http_tasks.discard(task)

    async def aclose(self):
        """Retry resources whose previous cleanup did not confirm disposal."""
        self._closed = True
        current = asyncio.current_task()
        for task in tuple((self._operations | self._http_tasks) - {current}):
            task.cancel()
        async with self._lock:
            async with report_timeout('Allure viewer cleanup', 15) as budget:
                await finish_cleanup(self._drain_history(budget))
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
