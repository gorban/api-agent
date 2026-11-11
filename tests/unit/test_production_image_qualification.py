"""Offline checks for the production image archive inspector."""
import io
from pathlib import Path
import pytest
import runpy
import tarfile
from types import SimpleNamespace
import zipfile


ROOT = Path(__file__).resolve().parents[2]


def _tar(path, names):
    with tarfile.open(path, 'w') as archive:
        for name in names:
            info = tarfile.TarInfo(name)
            info.size = 0
            archive.addfile(info, io.BytesIO())


def _zip(path, names):
    with zipfile.ZipFile(path, 'w') as archive:
        for name in names:
            archive.writestr(name, b'')


def test_inspector_rejects_forbidden_content_in_every_layer_and_flattened_image(tmp_path):
    module = runpy.run_path(str(ROOT / 'scripts/qualify_production_image.py'))
    layer = tmp_path / 'layer.tar'
    flattened = tmp_path / 'flattened.tar'
    _tar(layer, ['opt/api-agent/api_agent/tui/app.py'])
    _tar(flattened, ['opt/api-agent/api_agent/runtime/production.py'])

    assert module['forbidden_members'](layer) == ['opt/api-agent/api_agent/tui/app.py']
    assert module['forbidden_members'](flattened) == []


def test_inspector_rejects_forbidden_content_nested_inside_a_wheel(tmp_path):
    module = runpy.run_path(str(ROOT / 'scripts/qualify_production_image.py'))
    wheel = tmp_path / 'provider.whl'
    _zip(wheel, ['provider/tests/secret.py'])
    layer = tmp_path / 'layer.tar'
    with tarfile.open(layer, 'w') as archive:
        archive.add(wheel, arcname='opt/api-agent/provider.whl')

    assert module['forbidden_members'](layer) == [
        'opt/api-agent/provider.whl!provider/tests/secret.py',
    ]


@pytest.mark.parametrize('member', (
    'api_agent/runtime/trace_viewer.py',
    'api_agent/runtime/debug_server.py',
    'api_agent/runtime/source_display.py',
))
def test_inspector_rejects_interactive_modules_in_application_paths_and_nested_archives(tmp_path, member):
    module = runpy.run_path(str(ROOT / 'scripts/qualify_production_image.py'))
    wheel = tmp_path / 'provider.whl'
    _zip(wheel, [member])
    layer = tmp_path / 'layer.tar'
    with tarfile.open(layer, 'w') as archive:
        archive.add(wheel, arcname='opt/api-agent/provider.whl')

    assert module['forbidden_members'](layer) == [
        'opt/api-agent/provider.whl!' + member,
    ]


def test_inspector_explicitly_allows_unmodified_base_stdlib_viewer_and_ensurepip_paths(tmp_path):
    module = runpy.run_path(str(ROOT / 'scripts/qualify_production_image.py'))
    bundled = tmp_path / 'pip.whl'
    _zip(bundled, ['idlelib/stackviewer.py'])
    layer = tmp_path / 'layer.tar'
    with tarfile.open(layer, 'w') as archive:
        _tarred = tarfile.TarInfo('usr/local/lib/python3.13/idlelib/stackviewer.py')
        _tarred.size = 0
        archive.addfile(_tarred, io.BytesIO())
        archive.add(bundled, arcname='usr/local/lib/python3.13/ensurepip/_bundled/pip-23.whl')

    assert module['forbidden_members'](layer) == []


def test_qualification_plan_is_a_fresh_compile_of_the_root_workflow():
    # Its bound module ships only in the production wheel, so equality with a
    # fresh compile, not a source-checkout load, keeps it runnable.
    from api_agent.workflows.artifact_plans import QUALIFICATION_PLAN, _qualification_plan
    plan = (ROOT / QUALIFICATION_PLAN).read_bytes()
    fresh = _qualification_plan(ROOT)

    assert plan == fresh, 'qualification/simple_chat.json is stale; regenerate with python -m api_agent.workflows.artifact_plans regenerate --root .'


def test_failed_command_output_is_reported_before_the_error(capsys):
    import subprocess
    module = runpy.run_path(str(ROOT / 'scripts/qualify_production_image.py'))

    with pytest.raises(subprocess.CalledProcessError):
        module['_run']('sh', '-c', 'echo progress; echo broken base image >&2; exit 3')

    assert capsys.readouterr().err == 'progress\nbroken base image\n'


def test_scenario_image_builds_on_the_tag_being_qualified(monkeypatch, tmp_path):
    module = runpy.run_path(str(ROOT / 'scripts/qualify_production_image.py'))
    commands = []
    class Stop(Exception):
        pass
    def run(*command, cwd=None):
        commands.append(command)
        raise Stop
    monkeypatch.setitem(module['_runtime_qualification'].__globals__, '_run', run)
    (tmp_path / 'state').mkdir()
    monkeypatch.setattr(module['tempfile'], 'mkdtemp', lambda prefix: str(tmp_path / 'state'))

    with pytest.raises(Stop):
        module['_runtime_qualification'](tmp_path, tmp_path, tag='custom-runtime')

    assert commands == [('docker', 'build', '--no-cache', '-f', 'Dockerfile.qualification',
                         '--build-arg', 'BASE_IMAGE=custom-runtime:local',
                         '--tag', 'custom-runtime:qualification', '.')]
    assert (ROOT / 'Dockerfile.qualification').read_text().splitlines()[:2] == [
        'ARG BASE_IMAGE=api-agent-runtime:local', 'FROM ${BASE_IMAGE}']


def test_scenario_state_mount_is_writable_by_the_image_user(monkeypatch, tmp_path):
    import subprocess
    module = runpy.run_path(str(ROOT / 'scripts/qualify_production_image.py'))
    state = tmp_path / 'state'
    state.mkdir(mode=0o700)
    class Stop(Exception):
        pass
    def run(*command, cwd=None):
        if command[:2] == ('docker', 'run'):
            raise Stop
        return subprocess.CompletedProcess(command, 0, '', '')
    monkeypatch.setitem(module['_runtime_qualification'].__globals__, '_run', run)
    monkeypatch.setattr(module['tempfile'], 'mkdtemp', lambda prefix: str(state))

    with pytest.raises(Stop):
        module['_runtime_qualification'](tmp_path, tmp_path, tag='custom-runtime')

    # The image runs as its own unprivileged user, not the qualifying host user.
    assert state.stat().st_mode & 0o777 == 0o777


def test_runtime_result_guard_accepts_complete_transcript_and_rejects_regression():
    module = runpy.run_path(str(ROOT / 'scripts/qualify_production_image.py'))
    results = {
        'startup': {'status': 200}, 'ready': {'status': 200}, 'models': {'status': 200},
        'chat': {'status': 200, 'body': 'deterministic response'},
        'stream': {'status': 200, 'body': 'event: token\\nevent: done'},
        'controlled_error': {'status': 400}, 'heartbeat': {'status': 200, 'body': '{"status":"running"}'},
        'cancel': {'status': 200, 'body': '{"status":"cancelled"}'},
        'in_flight_stream': 'event: cancelled', 'viewer': {'status': 404},
        'debug': {'status': 404}, 'unconfigured_host': {'status': 400},
        'unconfigured_origin': {'status': 403}, 'shutdown_seconds': .2, 'provider_cleanup': 'closed\n',
    }

    module['assert_runtime_results'](results, shutdown_bound=10)
    results['shutdown_seconds'] = 11
    with pytest.raises(RuntimeError, match='shutdown'):
        module['assert_runtime_results'](results, shutdown_bound=10)


@pytest.mark.parametrize(('change', 'message'), (
    (lambda results: results['viewer'].update(status=200), 'viewer response'),
    (lambda results: results['chat'].update(body='unexpected'), 'chat response'),
    (lambda results: results['stream'].update(body='event: token'), 'stream response'),
    (lambda results: results.update(in_flight_stream='event: done'), 'cancellation'),
    (lambda results: results['cancel'].update(body='{"status":"running"}'), 'cancellation'),
    (lambda results: results['unconfigured_host'].update(status=200), 'unconfigured_host response'),
    (lambda results: results['unconfigured_origin'].update(status=200), 'unconfigured_origin response'),
    (lambda results: results.update(provider_cleanup='open\n'), 'cleanup marker'),
))
def test_runtime_result_guard_rejects_each_response_contract_regression(change, message):
    module = runpy.run_path(str(ROOT / 'scripts/qualify_production_image.py'))
    results = {
        'startup': {'status': 200}, 'ready': {'status': 200}, 'models': {'status': 200},
        'chat': {'status': 200, 'body': 'deterministic response'},
        'stream': {'status': 200, 'body': 'event: token\nevent: done'},
        'controlled_error': {'status': 400}, 'heartbeat': {'status': 200, 'body': '{"status":"running"}'},
        'cancel': {'status': 200, 'body': '{"status":"cancelled"}'},
        'in_flight_stream': 'event: cancelled', 'viewer': {'status': 404},
        'debug': {'status': 404}, 'unconfigured_host': {'status': 400},
        'unconfigured_origin': {'status': 403}, 'shutdown_seconds': .2, 'provider_cleanup': 'closed\n',
    }

    change(results)

    with pytest.raises(RuntimeError, match=message):
        module['assert_runtime_results'](results, shutdown_bound=10)


def test_tracing_export_guard_requires_the_runtime_failure_warning():
    module = runpy.run_path(str(ROOT / 'scripts/qualify_production_image.py'))
    results = {'container_logs': module['TRACE_FAILURE']}

    module['assert_tracing_export_failure'](results)

    with pytest.raises(RuntimeError, match='tracing export'):
        module['assert_tracing_export_failure']({})


def test_runtime_import_closure_rejects_excluded_viewer_modules():
    module = runpy.run_path(str(ROOT / 'scripts/qualify_production_image.py'))
    clean = {'container_logs': 'import time:        10 |         20 | api_agent.runtime.production\n'}

    assert module['assert_runtime_import_closure'](clean) == ['api_agent.runtime.production']

    with pytest.raises(RuntimeError, match='excluded modules'):
        module['assert_runtime_import_closure']({
            'container_logs': (
                'import time:        10 |         20 | api_agent.runtime.production\n'
                'import time:        10 |         20 | api_agent.runtime.viewer_host\n'),
        })


def test_inspector_tolerates_nonfile_and_unreadable_nested_members(tmp_path, monkeypatch):
    module = runpy.run_path(str(ROOT / 'scripts/qualify_production_image.py'))
    layer = tmp_path / 'layer.tar'
    with tarfile.open(layer, 'w') as archive:
        directory = tarfile.TarInfo('directory')
        directory.type = tarfile.DIRTYPE
        archive.addfile(directory)

    assert module['forbidden_members'](layer) == []
    monkeypatch.setattr(module['tarfile'], 'is_tarfile', lambda _: (_ for _ in ()).throw(OSError('bad tar')))
    assert module['_forbidden_archive'](b'not-an-archive', 'layer.tar') == set()


def test_inspector_tolerates_zip_directories(tmp_path):
    module = runpy.run_path(str(ROOT / 'scripts/qualify_production_image.py'))
    wheel = tmp_path / 'provider.whl'
    _zip(wheel, ['directory/'])
    layer = tmp_path / 'layer.tar'
    with tarfile.open(layer, 'w') as archive:
        archive.add(wheel, arcname='opt/api-agent/provider.whl')

    assert module['forbidden_members'](layer) == []


def test_wait_ready_retries_process_errors_and_nonready_responses(monkeypatch):
    module = runpy.run_path(str(ROOT / 'scripts/qualify_production_image.py'))
    scope = module['_wait_ready'].__globals__
    responses = iter((
        module['subprocess'].CalledProcessError(1, 'docker'),
        {'status': 503},
        {'status': 200},
    ))

    def request(*_args, **_kwargs):
        response = next(responses)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setitem(scope, '_http', request)
    monkeypatch.setattr(module['time'], 'sleep', lambda _: None)

    assert module['_wait_ready']('container') == {'status': 200}


def test_wait_ready_fails_after_its_bounded_retry_budget(monkeypatch):
    module = runpy.run_path(str(ROOT / 'scripts/qualify_production_image.py'))
    monkeypatch.setitem(module['_wait_ready'].__globals__, '_http',
                        lambda *_args, **_kwargs: {'status': 503})
    monkeypatch.setattr(module['time'], 'sleep', lambda _: None)

    with pytest.raises(RuntimeError, match='never became ready'):
        module['_wait_ready']('container')


def test_runtime_qualification_reports_stuck_shutdown_and_removes_container(monkeypatch, tmp_path):
    module = runpy.run_path(str(ROOT / 'scripts/qualify_production_image.py'))
    scope = module['_runtime_qualification'].__globals__
    state = tmp_path / 'state'
    state.mkdir()
    calls = []

    def run(*command, cwd=None):
        calls.append(command)
        return SimpleNamespace(stdout='true\n' if command[:2] == ('docker', 'inspect') else '', stderr='')

    monkeypatch.setitem(scope, '_run', run)
    monkeypatch.setitem(scope, '_published_port', lambda _: 8080)
    monkeypatch.setitem(scope, '_wait_ready_external', lambda _: {'status': 200, 'body': ''})
    monkeypatch.setitem(scope, '_external_http', lambda *_args, **_kwargs: {'status': 200, 'body': ''})
    monkeypatch.setattr(module['tempfile'], 'mkdtemp', lambda **_kwargs: str(state))
    monkeypatch.setattr(module['subprocess'], 'Popen',
                        lambda *_args, **_kwargs: SimpleNamespace(communicate=lambda **_kwargs: ('', '')))
    monkeypatch.setattr(module['time'], 'sleep', lambda _: None)

    with pytest.raises(RuntimeError, match='did not stop'):
        module['_runtime_qualification'](tmp_path, tmp_path, tag='test')

    assert any(command[:3] == ('docker', 'rm', '--force') for command in calls)


def test_runtime_qualification_ignores_cleanup_failure(monkeypatch, tmp_path):
    module = runpy.run_path(str(ROOT / 'scripts/qualify_production_image.py'))
    scope = module['_runtime_qualification'].__globals__
    calls = []

    def run(*command, cwd=None):
        calls.append(command)
        if command[:2] == ('docker', 'build'):
            return SimpleNamespace(stdout='', stderr='')
        raise module['subprocess'].CalledProcessError(1, command)

    monkeypatch.setitem(scope, '_run', run)

    with pytest.raises(module['subprocess'].CalledProcessError):
        module['_runtime_qualification'](tmp_path, tmp_path, tag='test')

    assert any(command[:3] == ('docker', 'rm', '--force') for command in calls)


def test_qualifier_reports_forbidden_layer_content(monkeypatch, tmp_path):
    module = runpy.run_path(str(ROOT / 'scripts/qualify_production_image.py'))
    scope = module['qualify'].__globals__

    def run(*command, cwd=None):
        if command[:3] == ('docker', 'image', 'inspect'):
            return SimpleNamespace(stdout='[{"Id":"sha256:test"}]', stderr='')
        if command[:2] == ('docker', 'save'):
            output = Path(command[3])
            with tarfile.open(output, 'w') as archive:
                manifest = b'[{"Layers":["layer.tar"]}]'
                info = tarfile.TarInfo('manifest.json')
                info.size = len(manifest)
                archive.addfile(info, io.BytesIO(manifest))
                info = tarfile.TarInfo('layer.tar')
                info.size = 0
                archive.addfile(info, io.BytesIO())
            return SimpleNamespace(stdout='', stderr='')
        if command[:2] == ('docker', 'create'):
            return SimpleNamespace(stdout='container\n', stderr='')
        return SimpleNamespace(stdout='', stderr='')

    def export(command, *, check, stdout, **_kwargs):
        with tarfile.open(fileobj=stdout, mode='w'):
            pass
        return SimpleNamespace(returncode=0)

    monkeypatch.setitem(scope, '_run', run)
    monkeypatch.setitem(scope, 'forbidden_members', lambda _: ['forbidden'])
    monkeypatch.setattr(module['subprocess'], 'run', export)

    with pytest.raises(RuntimeError, match='forbidden content'):
        module['qualify'](tmp_path, tmp_path / 'evidence', tag='test')


def test_immutable_scenario_base_is_tagged_verified_and_cleaned_after_build_failure(monkeypatch, tmp_path):
    import subprocess
    module = runpy.run_path(str(ROOT / 'scripts/qualify_production_image.py'))
    image_id = 'sha256:' + 'a' * 64
    commands = []
    state = tmp_path / 'state'
    state.mkdir()
    monkeypatch.setattr(module['tempfile'], 'mkdtemp', lambda prefix: str(state))

    def run(*command, cwd=None):
        commands.append(command)
        if command[:3] == ('docker', 'image', 'inspect'):
            return SimpleNamespace(stdout=image_id + '\n')
        if command[:2] == ('docker', 'build'):
            raise subprocess.CalledProcessError(1, command)
        return SimpleNamespace(stdout='')

    monkeypatch.setitem(module['_runtime_qualification'].__globals__, '_run', run)
    with pytest.raises(subprocess.CalledProcessError):
        module['_runtime_qualification'](tmp_path, tmp_path, tag='runtime', base_image=image_id)
    tag_command, inspect_command, build_command, cleanup_command = commands
    assert tag_command[:3] == ('docker', 'tag', image_id)
    base_tag = tag_command[3]
    assert base_tag.startswith('api-agent-qualification-base:')
    assert inspect_command[:4] == ('docker', 'image', 'inspect', base_tag)
    assert f'BASE_IMAGE={base_tag}' in build_command
    assert cleanup_command == ('docker', 'image', 'rm', base_tag)


def test_scenario_base_tag_mismatch_fails_before_build_and_is_cleaned(monkeypatch, tmp_path):
    module = runpy.run_path(str(ROOT / 'scripts/qualify_production_image.py'))
    image_id = 'sha256:' + 'a' * 64
    commands = []
    state = tmp_path / 'state'
    state.mkdir()
    monkeypatch.setattr(module['tempfile'], 'mkdtemp', lambda prefix: str(state))

    def run(*command, cwd=None):
        commands.append(command)
        return SimpleNamespace(stdout='sha256:' + 'b' * 64 + '\n')

    monkeypatch.setitem(module['_runtime_qualification'].__globals__, '_run', run)
    with pytest.raises(RuntimeError, match='base image tag'):
        module['_runtime_qualification'](tmp_path, tmp_path, tag='runtime', base_image=image_id)
    assert not any(command[:2] == ('docker', 'build') for command in commands)
    assert commands[-1] == ('docker', 'image', 'rm', commands[0][3])
