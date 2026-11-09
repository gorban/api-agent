"""Build and inspect a local-only production image without registry publication."""
import argparse
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from api_agent.config.process_settings import process_setting  # noqa: E402


FORBIDDEN = (
    '.md.html', '.svg', '/viewer/', '/ui/', '/tui/', '/tests/', '/test_',
    'playwright', 'node_modules', '/node/', 'launch.sh', 'docker.sock',
)
APPLICATION_MODULE_MARKERS = ('viewer', 'debug', 'display')
TRACE_FAILURE = 'Trace delivery failed; tracing resources were closed. Some traces may be missing.'
FORBIDDEN_PRODUCTION_IMPORTS = (
    'api_agent.runtime.trace_viewer',
    'api_agent.runtime.tracing_viewer_config',
    'api_agent.runtime.viewer_host',
    'api_agent.runtime.viewer_modal',
    'api_agent.workflows.viewer_projection',
    'api_agent.workflows.viewer_repository',
)
BASE_STDLIB_PATH_ALLOWLIST = (
    # These are unmodified paths from the digest-pinned Python base image.
    # The production Dockerfile never writes beneath /usr/local/lib/python*.
    'usr/local/lib/python*/idlelib/stackviewer.py',
    'usr/local/lib/python*/ensurepip/*',
)


def forbidden_members(archive):
    """Return forbidden paths, including recursively archived wheel/zip/tar contents."""
    return sorted(_forbidden_archive(Path(archive).read_bytes(), Path(archive).name))


def _allow_base_stdlib_path(path):
    from fnmatch import fnmatch
    return any(fnmatch(path, pattern) for pattern in BASE_STDLIB_PATH_ALLOWLIST)


def _forbidden_archive(body, label):
    hits = set()

    def forbidden_name(name):
        lowered = name.lower()
        if any(value in '/' + lowered for value in FORBIDDEN):
            return True
        path = Path(lowered)
        application_path = lowered.startswith('api_agent/') or '/api_agent/' in lowered
        return (application_path and path.suffix in {'.py', '.pyi'}
                and any(marker in path.name for marker in APPLICATION_MODULE_MARKERS))

    def inspect(payload, name, ancestry=(), *, report=True):
        qualified = '!'.join((*ancestry, name)) if report else ''
        if forbidden_name(name):
            hits.add(qualified)
        try:
            if tarfile.is_tarfile(io.BytesIO(payload)):
                with tarfile.open(fileobj=io.BytesIO(payload)) as archive:
                    for member in archive.getmembers():
                        if member.isfile():
                            stream = archive.extractfile(member)
                            inspect(stream.read(), member.name,
                                    (*ancestry, name) if report else ancestry)
            elif zipfile.is_zipfile(io.BytesIO(payload)):
                with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                    for member in archive.infolist():
                        if not member.is_dir():
                            inspect(archive.read(member), member.filename,
                                    (*ancestry, name) if report else ancestry)
        except (OSError, tarfile.TarError, zipfile.BadZipFile):
            return
    inspect(body, label, report=False)
    return {hit for hit in hits if not _allow_base_stdlib_path(hit.split('!')[0])}


def _run(*command, cwd=None):
    try:
        return subprocess.run(command, cwd=cwd, check=True, text=True, capture_output=True)
    except subprocess.CalledProcessError as error:
        sys.stderr.write((error.stdout or '') + (error.stderr or ''))
        raise


def assert_runtime_results(results, *, shutdown_bound):
    """Fail closed on the HTTP and lifecycle results required for qualification."""
    expected = {'startup': 200, 'ready': 200, 'models': 200, 'chat': 200,
                'stream': 200, 'controlled_error': 400, 'heartbeat': 200,
                'cancel': 200, 'viewer': 404, 'debug': 404,
                'unconfigured_host': 400, 'unconfigured_origin': 403}
    for name, status in expected.items():
        if results.get(name, {}).get('status') != status:
            raise RuntimeError(f'qualification {name} response was unexpected: {results.get(name)}')
    if 'deterministic response' not in results['chat']['body']:
        raise RuntimeError('qualification chat response was unexpected')
    if not all(event in results['stream']['body'] for event in ('event: token', 'event: done')):
        raise RuntimeError('qualification stream response was unexpected')
    if 'event: cancelled' not in results.get('in_flight_stream', ''):
        raise RuntimeError('qualification in-flight stream did not emit cancellation')
    if _lifecycle_status(results.get('heartbeat')) != 'running':
        raise RuntimeError('qualification heartbeat did not confirm the in-flight stream')
    if _lifecycle_status(results.get('cancel')) != 'cancelled':
        raise RuntimeError('qualification cancellation was not confirmed')
    if results.get('shutdown_seconds', shutdown_bound + 1) > shutdown_bound:
        raise RuntimeError('qualification shutdown exceeded its configured bound')
    if results.get('provider_cleanup') != 'closed\n':
        raise RuntimeError('qualification provider cleanup marker was absent')


def assert_tracing_export_failure(results):
    """Require the real tracing lifecycle warning from the refused OTLP export."""
    if TRACE_FAILURE not in results.get('container_logs', ''):
        raise RuntimeError('qualification tracing export failure was not logged')


def _lifecycle_status(response):
    try:
        return json.loads(response.get('body', '')).get('status')
    except (AttributeError, json.JSONDecodeError):
        return None


def assert_runtime_import_closure(results):
    """Require import-time tracing to show no viewer/debug production imports."""
    imports = {
        line.rsplit('|', 1)[-1].strip()
        for line in results.get('container_logs', '').splitlines()
        if line.startswith('import time:') and '|' in line
    }
    forbidden = sorted(
        imported for imported in imports
        if imported.startswith(FORBIDDEN_PRODUCTION_IMPORTS))
    if forbidden:
        raise RuntimeError(f'qualification imported excluded modules: {forbidden}')
    return sorted(imported for imported in imports if imported.startswith('api_agent.'))


def _http(container, path, *, method='POST', body=None):
    program = (
        'import http.client,json,sys; path,method,raw=sys.argv[1:]; '
        'headers={"Origin":"http://localhost:3000","X-API-Agent-Session":"n"*32}; '
        'body=None if raw=="-" else raw; '
        'headers.update({"Content-Type":"application/json"} if body is not None else {}); '
        'c=http.client.HTTPConnection("127.0.0.1",8080,timeout=15); '
        'c.request(method,path,body,headers); r=c.getresponse(); '
        'print(json.dumps({"status":r.status,"body":r.read().decode()}))')
    return json.loads(_run('docker', 'exec', container, 'python', '-c', program, path, method,
                           '-' if body is None else json.dumps(body)).stdout)


def timeouts():
    """Scenario bounds in seconds, from their declared settings."""
    return {key: process_setting(f'QUALIFICATION_{key.upper()}_TIMEOUT_SECONDS')
            for key in ('http', 'ready', 'stream', 'shutdown', 'stop')}


def _external_http(port, path, *, method='POST', body=None, host='qualification.example.test',
                   origin='https://console.example.test'):
    """Send a host-originating request through the published container port."""
    headers = {'Host': host, 'Origin': origin, 'X-API-Agent-Session': 'n' * 32}
    if body is not None:
        headers['Content-Type'] = 'application/json'
        body = json.dumps(body)
    import http.client
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=timeouts()['http'])
    connection.request(method, path, body, headers)
    response = connection.getresponse()
    return {'status': response.status, 'body': response.read().decode()}


def _published_port(container):
    published = _run('docker', 'port', container, '8080/tcp').stdout.strip()
    try:
        _, value = published.rsplit(':', 1)
        return int(value)
    except ValueError:
        raise RuntimeError('qualification container did not publish its HTTP port') from None


def _wait_ready(container):
    for _ in range(40):
        try:
            response = _http(container, '/app/agent/internals/health/ready', method='GET')
        except subprocess.CalledProcessError:
            time.sleep(.25)
            continue
        if response['status'] == 200:
            return response
        time.sleep(.25)
    raise RuntimeError('qualification container never became ready')


def _wait_ready_external(port):
    deadline = time.monotonic() + timeouts()['ready']
    while True:
        try:
            response = _external_http(port, '/app/agent/internals/health/ready', method='GET')
        except OSError:
            response = None
        if response is not None and response['status'] == 200:
            return response
        if time.monotonic() >= deadline:
            raise RuntimeError('qualification container never became ready')
        time.sleep(.25)


def _runtime_qualification(root, evidence, *, tag, shutdown_bound=None, trace_endpoint=None):
    """Exercise the published API from a host client through Docker port forwarding."""
    bounds = timeouts()
    if shutdown_bound is None:
        shutdown_bound = bounds['shutdown']
    image, state = tag + ':qualification', Path(tempfile.mkdtemp(prefix='v2-image-state-'))
    name = 'api-agent-qualification-' + uuid.uuid4().hex
    # The image runs as its own unprivileged user; a private 0700 mount is not its.
    state.chmod(0o777)
    _run('docker', 'build', '--no-cache', '-f', 'Dockerfile.qualification',
         '--build-arg', f'BASE_IMAGE={tag}:local', '--tag', image, '.',
         cwd=root)
    environment = (
        'API_AGENT_PRODUCTION_PLAN=/opt/api-agent/plans/simple_chat.json',
        'API_AGENT_LLM_PROVIDER=/opt/api-agent/providers/deterministic_llm.py:create',
        'API_AGENT_LLM_DEFAULT_MODEL=deterministic',
        'API_AGENT_LISTEN_HOST=0.0.0.0',
        'API_AGENT_ALLOWED_HOSTS=qualification.example.test',
        'API_AGENT_CHAT_ORIGIN=https://console.example.test',
        'API_AGENT_PERMITTED_BROWSER_ORIGINS=https://console.example.test',
        'API_AGENT_CLIENT_ADDRESS_POLICY=any',
        'API_AGENT_SESSION_NONCE=' + 'n' * 32,
        'API_AGENT_STATE_DIR=/var/lib/api-agent', 'DEBUG=1', 'RUN_UI=1',
        'PYTHONPROFILEIMPORTTIME=1',
        f'API_AGENT_SHUTDOWN_TIMEOUT_SECONDS={shutdown_bound:g}')
    if trace_endpoint is not None:
        environment += (
            'TRACE_EXPORT_ENDPOINT=' + trace_endpoint,
            'TRACE_HTTP_DIAGNOSTICS_LEVEL=info',
        )
    results = {}
    try:
        options = [item for value in environment for item in ('-e', value)]
        _run('docker', 'run', '--detach', '--name', name, '--read-only',
             '--publish', '127.0.0.1::8080',
             '--tmpfs', '/tmp:rw,noexec,nosuid,size=16m',
             '--mount', f'type=bind,src={state},dst=/var/lib/api-agent', *options, image)
        port = _published_port(name)
        results['published_port'] = port
        results['ready'] = _wait_ready_external(port)
        results['startup'] = _external_http(port, '/app/agent/internals/health/startup', method='GET')
        results['models'] = _external_http(port, '/app/agent/models', method='GET')
        request = {'session_id': str(uuid.uuid4()),
                   'messages': [{'role': 'user', 'content': 'hello'}], 'model': 'deterministic'}
        results['chat'] = _external_http(port, '/app/agent/chat', body=request)
        results['stream'] = _external_http(port, '/app/agent/chat/stream',
                                           body={**request, 'session_id': str(uuid.uuid4())})
        error = {**request, 'session_id': str(uuid.uuid4()),
                 'messages': [{'role': 'user', 'content': 'controlled-error'}]}
        results['controlled_error'] = _external_http(port, '/app/agent/chat', body=error)
        results['viewer'] = _external_http(port, '/app/agent/flow', method='GET')
        results['debug'] = _external_http(port, '/app/agent/debug', method='GET')
        results['unconfigured_host'] = _external_http(
            port, '/app/agent/internals/health/ready', method='GET', host='unconfigured.example.test')
        results['unconfigured_origin'] = _external_http(
            port, '/app/agent/models', method='GET', origin='https://unconfigured.example.test')
        session = str(uuid.uuid4())
        blocked = {**request, 'session_id': session,
                   'messages': [{'role': 'user', 'content': 'block-until-cancelled'}]}
        source = (
            'import http.client,json,sys; '
            'c=http.client.HTTPConnection("127.0.0.1",int(sys.argv[1]),timeout=float(sys.argv[3])); '
            'c.request("POST","/app/agent/chat/stream",sys.argv[2],'
            '{"Host":"qualification.example.test","Origin":"https://console.example.test",'
            '"X-API-Agent-Session":"n"*32,"Content-Type":"application/json"}); '
            'r=c.getresponse(); print(r.status); print(r.read().decode())')
        stream = subprocess.Popen(('python', '-c', source, str(port), json.dumps(blocked), str(bounds['stream'])),
                                  text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        time.sleep(1)
        results['heartbeat'] = _external_http(port, '/app/agent/chat/heartbeat', body={'session_id': session})
        results['cancel'] = _external_http(port, '/app/agent/chat/cancel', body={'session_id': session})
        results['in_flight_stream'] = stream.communicate(timeout=bounds['stream'])[0]
        started = time.monotonic()
        _run('docker', 'kill', '--signal', 'TERM', name)
        stop_by = time.monotonic() + bounds['stop']
        while _run('docker', 'inspect', name, '--format', '{{.State.Running}}').stdout.strip() != 'false':
            if time.monotonic() >= stop_by:
                raise RuntimeError('qualification container did not stop after SIGTERM')
            time.sleep(.125)
        _run('docker', 'wait', name)
        results['shutdown_seconds'] = time.monotonic() - started
        results['provider_cleanup'] = (state / 'deterministic-provider-closed').read_text()
        logs = _run('docker', 'logs', name)
        results['container_logs'] = logs.stdout + logs.stderr
        results['production_import_closure'] = assert_runtime_import_closure(results)
        label = 'traced' if trace_endpoint is not None else 'runtime'
        (evidence / f'{label}-scenario-transcript.json').write_text(
            json.dumps(results, sort_keys=True, indent=2) + '\n')
        assert_runtime_results(results, shutdown_bound=shutdown_bound)
        if trace_endpoint is not None:
            assert_tracing_export_failure(results)
        (evidence / 'runtime-scenarios.json').write_text(json.dumps(results, sort_keys=True, indent=2) + '\n')
        return results
    finally:
        try:
            _run('docker', 'rm', '--force', name)
        except subprocess.CalledProcessError:
            pass


def qualify(root, evidence, *, tag='api-agent-runtime'):
    """Build, archive and inspect exact local image bytes; never push."""
    root, evidence = Path(root).resolve(), Path(evidence).resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    base_tag = tag + ':local'
    _run('docker', 'build', '--no-cache', '--tag', base_tag, '.', cwd=root)
    inspect = json.loads(_run('docker', 'image', 'inspect', base_tag).stdout)[0]
    digest = inspect['RepoDigests'][0] if inspect.get('RepoDigests') else inspect['Id']
    commit = _run('git', 'rev-parse', 'HEAD', cwd=root).stdout.strip()
    (evidence / 'image-digest.txt').write_text(f'image={digest}\ncommit={commit}\n')
    archive = evidence / 'image.tar'
    _run('docker', 'save', '--output', str(archive), base_tag)
    layer_hits = {}
    with tempfile.TemporaryDirectory(prefix='v2-image-inspect-') as temporary:
        temporary = Path(temporary)
        with tarfile.open(archive) as saved:
            saved.extractall(temporary, filter='data')
        manifest = json.loads((temporary / 'manifest.json').read_text())
        for layer in manifest[0]['Layers']:
            hits = forbidden_members(temporary / layer)
            if hits:
                layer_hits[layer] = hits
        container = _run('docker', 'create', base_tag).stdout.strip()
        try:
            flattened = evidence / 'flattened.tar'
            with flattened.open('wb') as output:
                subprocess.run(('docker', 'export', container), check=True, stdout=output)
        finally:
            _run('docker', 'rm', '--force', container)
    flattened_hits = forbidden_members(evidence / 'flattened.tar')
    report = {'image': digest, 'commit': commit, 'layers': layer_hits, 'flattened': flattened_hits,
              'base_stdlib_path_allowlist': BASE_STDLIB_PATH_ALLOWLIST}
    (evidence / 'forbidden-content.json').write_text(json.dumps(report, sort_keys=True, indent=2) + '\n')
    (evidence / 'inspection-policy.json').write_text(json.dumps({
        'nested_archives': 'tar, wheel, and zip contents are recursively inspected',
        'base_stdlib_path_allowlist': BASE_STDLIB_PATH_ALLOWLIST,
        'base_stdlib_path_allowlist_reason': (
            'The digest-pinned Python base image supplies these paths; the production '
            'Dockerfile does not modify /usr/local/lib/python*.'),
    }, sort_keys=True, indent=2) + '\n')
    if layer_hits or flattened_hits:
        raise RuntimeError('production image contains forbidden content')
    python_inventory = _run('docker', 'run', '--rm', '--entrypoint', 'python', base_tag,
                     '-c', 'import csv,importlib.metadata,json; rows=[]\n'
                     'for d in importlib.metadata.distributions():\n'
                     ' m=d.metadata; licenses=[m.get("License-Expression"),m.get("License")]'
                     ' +[v for k,v in m.items() if k=="Classifier" and v.startswith("License :: ")]\n'
                     ' rows.append({"name":m["Name"],"version":d.version,'
                     '"licenses":[v for v in licenses if v] or ["unknown"],'
                     '"record_hashes":[r[1] for r in csv.reader((d.read_text("RECORD") or "").splitlines()) if len(r)>1 and r[1]]})\n'
                     'print(json.dumps(sorted(rows,key=lambda row:row["name"].lower())))').stdout
    os_inventory = _run('docker', 'run', '--rm', '--entrypoint', 'python', base_tag,
                        '-c', 'import json,pathlib,re,subprocess\n'
                        'rows=[]\n'
                        'packages=subprocess.run(["dpkg-query","-W","-f=${binary:Package}\\t${Version}\\n"],check=True,text=True,capture_output=True).stdout.splitlines()\n'
                        'for row in packages:\n'
                        ' name,version=row.split("\\t",1); copyright=pathlib.Path("/usr/share/doc")/name.split(":",1)[0]/"copyright"\n'
                        ' text=copyright.read_text(errors="replace") if copyright.is_file() else ""\n'
                        ' licenses=re.findall(r"(?mi)^License:\\s*(.+)$",text)\n'
                        ' rows.append({"name":name,"version":version,"licenses":licenses or ["unknown"]})\n'
                        'print(json.dumps(sorted(rows,key=lambda row:row["name"].lower())))').stdout
    (evidence / 'sbom-licenses.json').write_text(json.dumps({
        'python_distributions': json.loads(python_inventory),
        'os_packages': json.loads(os_inventory),
    }, sort_keys=True, indent=2) + '\n')
    (evidence / 'sbom-method.txt').write_text(
        'Exact-image Python dist-info METADATA License-Expression/License/classifiers and RECORD hashes; '
        'Debian dpkg status package/version inventory with copyright License fields where available. '
        'Unknown licenses are explicit.\\n')
    runtime = _runtime_qualification(root, evidence, tag=tag)
    telemetry = _runtime_qualification(root, evidence, tag=tag, trace_endpoint='http://127.0.0.1:1/v1/traces')
    (evidence / 'runtime-scenarios.json').write_text(
        json.dumps({'runtime': runtime, 'failing_telemetry': telemetry}, sort_keys=True, indent=2) + '\n')
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('.'))
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--tag', default='api-agent-runtime')
    args = parser.parse_args(argv)
    qualify(args.root, args.evidence, tag=args.tag)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
